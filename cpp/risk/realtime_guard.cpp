/**
 * 火种系统 (FireSeed) 硬实时风控核心 (RealtimeGuard)
 * =====================================================
 * 本模块独立于 Python 引擎，运行在单独线程/进程中，
 * 通过共享内存直接读取最新行情与策略指令，实现微秒级的
 * 止损、熔断、强制平仓等硬实时操作，彻底消除 GIL 抖动影响。
 *
 * 核心功能：
 * - 订阅共享内存中的价格 Tick 和 Python 侧下发的止损价位
 * - 监控当前持仓，一旦价格触及止损立即通过交易所 API 发送平仓单
 * - 心跳检测：若 Python 主进程超过阈值未更新心跳，则自动冻结开仓
 * - 提供 C++ 类及 C 接口，可嵌入主进程或独立子进程
 *
 * 编译：
 *   g++ -c realtime_guard.cpp -o realtime_guard.o -std=c++17 -O2
 */

#include <atomic>
#include <chrono>
#include <cstring>
#include <iostream>
#include <mutex>
#include <thread>
#include <vector>
#include <unordered_map>
#include <sys/mman.h>
#include <unistd.h>
#include <fcntl.h>

// ---------- 假设的共享内存结构 (与 Python 侧约定) ----------
// 实际实现需与 flat_msg.h 协调，此处以简单结构示范

#pragma pack(push, 1)
struct PositionInfo {
    char     symbol[16];       // 交易对
    double   entry_price;      // 开仓均价
    double   stop_loss;        // 当前止损价 (Python 侧动态更新)
    double   take_profit;      // 止盈价 (可选)
    double   size;             // 持仓数量 (正=多，负=空)
    int      position_id;      // 持仓标识
    uint32_t update_seq;       // Python 侧写入的序列号，用于判断新鲜度
};

struct MarketTick {
    char     symbol[16];
    double   last_price;
    double   bid;
    double   ask;
    uint64_t timestamp_us;
};

struct Heartbeat {
    std::atomic<uint64_t> python_counter;   // Python 侧递增的心跳计数
    char    _pad[48];                       // 避免伪共享
};
#pragma pack(pop)

// ---------- 硬实时风控类 ----------
class RealtimeGuard {
public:
    RealtimeGuard()
        : running_(false), python_alive_(true),
          guard_thread_(), heartbeat_timeout_us_(50000)  // 50ms
    {}

    ~RealtimeGuard() { stop(); }

    /** 启动守护线程，开始监控共享内存 */
    void start(Heartbeat* heartbeat, PositionInfo* positions, size_t max_positions,
               MarketTick* tick) {
        if (running_.exchange(true)) return;
        heartbeat_   = heartbeat;
        positions_   = positions;
        max_pos_     = max_positions;
        market_tick_ = tick;

        guard_thread_ = std::thread(&RealtimeGuard::guard_loop, this);
    }

    /** 停止守护线程 */
    void stop() {
        running_.store(false);
        if (guard_thread_.joinable()) {
            guard_thread_.join();
        }
    }

    /** 外部调用：更新某持仓的止损价 (供 Python 侧使用) */
    void update_stop_loss(int position_id, double new_stop) {
        // 遍历持仓数组更新对应 ID 的止损
        for (size_t i = 0; i < max_pos_; ++i) {
            if (positions_[i].position_id == position_id) {
                positions_[i].stop_loss = new_stop;
                positions_[i].update_seq++;
                return;
            }
        }
    }

    /** 心跳计数器 (Python 侧每 Tick 调用) */
    void heartbeat() {
        if (heartbeat_) {
            heartbeat_->python_counter.fetch_add(1, std::memory_order_release);
        }
    }

private:
    std::atomic<bool> running_;
    std::atomic<bool> python_alive_;
    std::thread guard_thread_;

    // 共享内存指针
    Heartbeat*    heartbeat_   = nullptr;
    PositionInfo* positions_   = nullptr;
    size_t        max_pos_     = 0;
    MarketTick*   market_tick_ = nullptr;

    uint64_t heartbeat_timeout_us_;
    uint64_t last_python_counter_ = 0;

    /** 主守护循环，运行频率 ~1kHz */
    void guard_loop() {
        uint64_t last_hb_check = 0;
        while (running_.load(std::memory_order_relaxed)) {
            // 模拟高频检查 (实际可用高精度定时器)
            std::this_thread::sleep_for(std::chrono::microseconds(100));

            auto now = std::chrono::steady_clock::now();
            auto now_us = std::chrono::duration_cast<std::chrono::microseconds>(
                now.time_since_epoch()).count();

            // ---- 心跳检查 ----
            if (heartbeat_) {
                uint64_t hb = heartbeat_->python_counter.load(std::memory_order_acquire);
                if (hb == last_python_counter_) {
                    // 未递增，可能 Python 主循环卡顿
                    if (now_us - last_hb_check > heartbeat_timeout_us_) {
                        python_alive_.store(false);
                        std::cerr << "[RealtimeGuard] Python heartbeat lost!" << std::endl;
                        // 进入紧急防御：收紧所有止损至当前价
                        panic_tighten_stops();
                    }
                } else {
                    last_python_counter_ = hb;
                    last_hb_check = now_us;
                    python_alive_.store(true);
                }
            }

            // ---- 止损监控 ----
            if (!market_tick_ || !positions_) continue;

            const MarketTick& tick = *market_tick_;
            for (size_t i = 0; i < max_pos_; ++i) {
                const PositionInfo& pos = positions_[i];
                if (pos.size == 0.0 || pos.stop_loss <= 0.0) continue; // 空持仓

                // 检查是否与当前 Tick 品种匹配
                if (std::strcmp(tick.symbol, pos.symbol) != 0) continue;

                bool close_position = false;
                if (pos.size > 0) {
                    // 多仓
                    if (tick.last_price <= pos.stop_loss) close_position = true;
                } else {
                    // 空仓 (size < 0)
                    if (tick.last_price >= pos.stop_loss) close_position = true;
                }

                if (close_position) {
                    // 执行市价平仓 (通过交易所API或预先注册的回调)
                    execute_close(pos.position_id, pos.size);
                    // 清空该持仓槽位 (实际应由 Python 侧同步，这里简单清空)
                    positions_[i].size = 0.0;
                    positions_[i].stop_loss = 0.0;
                }
            }
        }
    }

    /** 紧急收紧所有持仓止损至当前价格 */
    void panic_tighten_stops() {
        if (!market_tick_ || !positions_) return;
        const MarketTick& tick = *market_tick_;
        for (size_t i = 0; i < max_pos_; ++i) {
            if (positions_[i].size == 0.0) continue;
            if (std::strcmp(tick.symbol, positions_[i].symbol) == 0) {
                // 将止损设为当前价格 (立刻触发平仓)
                positions_[i].stop_loss = tick.last_price;
            }
        }
    }

    /** 执行平仓操作 (占位：实际应调用交易所 API) */
    void execute_close(int position_id, double size) {
        // 通过预先注册的回调或网络接口发送平仓指令
        // 此处仅记录日志
        FILE* fp = fopen("/var/log/fire_seed/realtime_guard.log", "a");
        if (fp) {
            auto now = std::chrono::system_clock::to_time_t(
                std::chrono::system_clock::now());
            fprintf(fp, "[%s] FORCE CLOSE position %d, size %.4f\n",
                    ctime(&now), position_id, size);
            fclose(fp);
        }
        // 实际：http_post("http://localhost:8000/api/exec/close", ...)
    }
};

// ------------------------------------------------------------
// C 接口 (供 pybind11 绑定)
// ------------------------------------------------------------
extern "C" {

RealtimeGuard* rt_guard_create() {
    return new RealtimeGuard();
}

void rt_guard_destroy(RealtimeGuard* g) {
    delete g;
}

void rt_guard_start(RealtimeGuard* g, Heartbeat* hb,
                    PositionInfo* pos, size_t max_pos, MarketTick* tick) {
    if (g) g->start(hb, pos, max_pos, tick);
}

void rt_guard_stop(RealtimeGuard* g) {
    if (g) g->stop();
}

void rt_guard_update_stop(RealtimeGuard* g, int pos_id, double new_stop) {
    if (g) g->update_stop_loss(pos_id, new_stop);
}

void rt_guard_heartbeat(RealtimeGuard* g) {
    if (g) g->heartbeat();
}

} // extern "C"
