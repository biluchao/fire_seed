/**
 * 火种系统 (FireSeed) 第二重不可变监视者 (Hard Watcher)
 * ==========================================================
 * 该模块独立于 Python 管理层，以纯 C++ 实现，不接受任何外部参数修改。
 * 其唯一职责是：
 *   1. 持续监控系统关键健康指标（回滚次数、夏普比率、哥德尔休眠时长等）
 *   2. 当指标超出硬编码的安全阈值时，强制冻结进化模块，并发出紧急信号
 *   3. 所有判断逻辑均硬编码，不受配置或数据库影响，确保在极端场景下不会失效
 *
 * 编译：
 *   g++ -c hard_watcher.cpp -o hard_watcher.o -std=c++17 -O2
 */

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <mutex>
#include <thread>

namespace fire_seed {
namespace monitoring {

// -------------------------- 硬编码安全阈值 --------------------------
namespace threshold {
    constexpr int    MAX_ROLLBACKS_PER_DAY     = 3;      // 单日最大回滚次数
    constexpr int    MAX_STRATEGY_CHURN_PER_WEEK = 10;   // 每周策略更替上限
    constexpr double MIN_AVG_SHARPE            = -0.5;  // 平均夏普最低容忍值
    constexpr int    MAX_SELF_DOUBT_HOURS      = 12;     // 连续怀疑状态最长小时数
    constexpr int    CHECK_INTERVAL_SEC        = 5;      // 检查间隔（秒）
}

// -------------------------- 监控指标（由外部更新） --------------------------
struct alignas(64) WatcherMetrics {
    std::atomic<int>    rollback_count{0};
    std::atomic<int>    strategy_churn{0};
    std::atomic<double> avg_sharpe{0.0};
    std::atomic<int>    self_doubt_hours{0};
    std::atomic<bool>   system_active{true};    // 外部通知系统是否存活
};

// -------------------------- 硬监视器类 --------------------------
class HardWatcher {
public:
    HardWatcher() : metrics_(), running_(false) {}

    ~HardWatcher() { stop(); }

    // 禁止拷贝
    HardWatcher(const HardWatcher&) = delete;
    HardWatcher& operator=(const HardWatcher&) = delete;

    /**
     * 启动监视线程，以固定间隔评估系统健康度。
     * @param evolution_pid 进化模块的进程/线程 ID（用于发送冻结信号）
     */
    void start(int evolution_pid = -1) {
        if (running_.exchange(true)) return;
        evolution_pid_ = evolution_pid;
        worker_ = std::thread(&HardWatcher::monitor_loop, this);
    }

    /** 停止监视线程 */
    void stop() {
        running_.store(false);
        if (worker_.joinable()) {
            worker_.join();
        }
    }

    // ---- 指标更新接口（由外部实时调用） ----
    void set_rollback_count(int count) { metrics_.rollback_count.store(count, std::memory_order_relaxed); }
    void set_strategy_churn(int churn)  { metrics_.strategy_churn.store(churn, std::memory_order_relaxed); }
    void set_avg_sharpe(double sharpe)  { metrics_.avg_sharpe.store(sharpe, std::memory_order_relaxed); }
    void set_self_doubt_hours(int hrs)  { metrics_.self_doubt_hours.store(hrs, std::memory_order_relaxed); }
    void set_system_active(bool active) { metrics_.system_active.store(active, std::memory_order_relaxed); }

    /** 立即执行一次同步健康评估（供外部主动调用） */
    bool evaluate_now() {
        return is_system_healthy(metrics_);
    }

private:
    WatcherMetrics metrics_;
    std::atomic<bool> running_{false};
    std::thread worker_;
    int evolution_pid_ = -1;

    /** 静态硬编码健康判断（不依赖任何外部参数） */
    static bool is_system_healthy(const WatcherMetrics& m) {
        // 1. 回滚次数超标
        if (m.rollback_count.load(std::memory_order_acquire) > threshold::MAX_ROLLBACKS_PER_DAY) {
            std::cerr << "[HardWatcher] CRITICAL: rollback count "
                      << m.rollback_count.load() << " > " << threshold::MAX_ROLLBACKS_PER_DAY << std::endl;
            return false;
        }
        // 2. 策略更替频率超限
        if (m.strategy_churn.load(std::memory_order_acquire) > threshold::MAX_STRATEGY_CHURN_PER_WEEK) {
            std::cerr << "[HardWatcher] CRITICAL: strategy churn "
                      << m.strategy_churn.load() << " > " << threshold::MAX_STRATEGY_CHURN_PER_WEEK << std::endl;
            return false;
        }
        // 3. 平均夏普跌破底线
        if (m.avg_sharpe.load(std::memory_order_acquire) < threshold::MIN_AVG_SHARPE) {
            std::cerr << "[HardWatcher] CRITICAL: avg sharpe "
                      << m.avg_sharpe.load() << " < " << threshold::MIN_AVG_SHARPE << std::endl;
            return false;
        }
        // 4. 哥德尔监视器过度休眠
        if (m.self_doubt_hours.load(std::memory_order_acquire) > threshold::MAX_SELF_DOUBT_HOURS) {
            std::cerr << "[HardWatcher] CRITICAL: self-doubt hours "
                      << m.self_doubt_hours.load() << " > " << threshold::MAX_SELF_DOUBT_HOURS << std::endl;
            return false;
        }
        return true;
    }

    /** 执行紧急冻结动作 */
    void enforce_freeze() {
        std::cerr << "[HardWatcher] Enforcing evolution freeze..." << std::endl;
        if (evolution_pid_ > 0) {
            // 发送 SIGSTOP 冻结进化进程（需确保此代码有足够权限）
            kill(evolution_pid_, SIGSTOP);
        }
        // 写入系统日志
        FILE* fp = fopen("/var/log/fire_seed/hard_watcher.log", "a");
        if (fp) {
            auto now = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
            fprintf(fp, "[%s] HARD WATCHER: Evolution frozen due to system unhealthy.\n",
                    ctime(&now));
            fclose(fp);
        }
    }

    /** 主监视循环 */
    void monitor_loop() {
        while (running_.load(std::memory_order_relaxed)) {
            std::this_thread::sleep_for(std::chrono::seconds(threshold::CHECK_INTERVAL_SEC));

            // 系统必须处于活跃状态，否则不进行干预（可能正在进行维护）
            if (!metrics_.system_active.load(std::memory_order_acquire)) {
                continue;
            }

            if (!is_system_healthy(metrics_)) {
                enforce_freeze();
                // 冻结后继续运行，持续检查直到系统恢复或人工介入
            }
        }
    }
};

} // namespace monitoring
} // namespace fire_seed

// ------------------------------------------------------------
// C 接口 (供 Python 侧通过 ctypes 或 pybind11 调用)
// ------------------------------------------------------------
extern "C" {

using HardWatch = fire_seed::monitoring::HardWatcher;

HardWatch* hard_watcher_create() {
    return new HardWatch();
}

void hard_watcher_destroy(HardWatch* hw) {
    delete hw;
}

void hard_watcher_start(HardWatch* hw, int evolution_pid) {
    if (hw) hw->start(evolution_pid);
}

void hard_watcher_stop(HardWatch* hw) {
    if (hw) hw->stop();
}

void hard_watcher_set_rollback_count(HardWatch* hw, int count) {
    if (hw) hw->set_rollback_count(count);
}

void hard_watcher_set_strategy_churn(HardWatch* hw, int churn) {
    if (hw) hw->set_strategy_churn(churn);
}

void hard_watcher_set_avg_sharpe(HardWatch* hw, double sharpe) {
    if (hw) hw->set_avg_sharpe(sharpe);
}

void hard_watcher_set_self_doubt_hours(HardWatch* hw, int hrs) {
    if (hw) hw->set_self_doubt_hours(hrs);
}

void hard_watcher_set_system_active(HardWatch* hw, int active) {
    if (hw) hw->set_system_active(active != 0);
}

int hard_watcher_evaluate_now(HardWatch* hw) {
    if (!hw) return 0;
    return hw->evaluate_now() ? 1 : 0;
}

} // extern "C"
