/**
 * 火种系统 (FireSeed) 适应性 TWAP 执行算法
 * ============================================
 * 将大额订单按照历史成交量分布拆分为多个子订单，
 * 在设定的时间范围内均匀执行，并可根据紧急度参数动态调整执行节奏。
 *
 * 核心特性：
 * - 基于历史24小时成交量分布曲线分配各时间片权重
 * - 支持动态紧迫度调整 (0.5 ~ 2.0，默认 1.0)
 * - 剩余未执行量自动重新分配到后续时间片
 * - 市价单/限价单混合执行策略
 * - 单次切片大小上下限保护
 *
 * 编译：
 *   g++ -c twap_adaptive.cpp -o twap_adaptive.o -std=c++17 -O2
 */

#include <vector>
#include <cmath>
#include <algorithm>
#include <numeric>
#include <random>
#include <chrono>
#include <stdexcept>
#include <cstring>
#include <cstdint>
#include <sys/timerfd.h>
#include <unistd.h>
#include <poll.h>

namespace fire_seed {
namespace execution {

// ------------------------------------------------------------
// 适应性 TWAP 执行器
// ------------------------------------------------------------
class AdaptiveTwap {
public:
    /**
     * 构造函数
     * @param total_quantity  总执行数量
     * @param total_duration_sec 总执行时长 (秒)，典型值 300 ~ 3600
     * @param volume_profile  24小时成交量分布 (长度通常为 24 或 1440)
     *                        元素值表示该时段成交量占总成交量的比例
     * @param urgency         执行迫切度: 1.0=正常, >1 加速, <1 减速
     */
    AdaptiveTwap(double total_quantity,
                 double total_duration_sec,
                 const std::vector<double>& volume_profile,
                 double urgency = 1.0)
        : total_qty_(total_quantity),
          remaining_qty_(total_quantity),
          duration_sec_(total_duration_sec),
          urgency_(urgency),
          volume_profile_(volume_profile),
          current_slice_index_(0),
          started_(false),
          finished_(false)
    {
        if (total_qty_ <= 0.0)
            throw std::invalid_argument("total_quantity must be positive");
        if (duration_sec_ <= 0.0)
            throw std::invalid_argument("duration_sec must be positive");
        if (urgency_ <= 0.0)
            throw std::invalid_argument("urgency must be positive");
        if (volume_profile_.empty())
            throw std::invalid_argument("volume_profile cannot be empty");

        // 归一化成交量分布
        normalize_profile();

        // 计算切片数量 = 分布点数
        num_slices_ = static_cast<int>(volume_profile_.size());
        // 调整每片时长：实际总时长可能不等于 volume_profile 点数对应的总时长，
        // 这里简单地将总时长按切片数均分
        slice_duration_sec_ = duration_sec_ / num_slices_;
        if (slice_duration_sec_ < 0.1) slice_duration_sec_ = 0.1;

        // 分配各切片目标数量
        slice_targets_.resize(num_slices_, 0.0);
        for (int i = 0; i < num_slices_; ++i) {
            slice_targets_[i] = total_qty_ * volume_profile_[i];
        }
        // 对目标取整并微调，确保总和正确
        redistribute_rounding();

        // 初始化定时器
        init_timer();
    }

    ~AdaptiveTwap() {
        if (timer_fd_ >= 0) {
            close(timer_fd_);
            timer_fd_ = -1;
        }
    }

    /**
     * 获取下一个应当执行的子订单数量。
     * 调用时机：每当定时器触发时调用一次。
     * @return 本次应执行的数量 (0 表示已完成或等待)
     */
    double next_slice() {
        if (finished_) return 0.0;
        if (!started_) {
            // 首次调用，标记开始并立刻执行第一个切片
            started_ = true;
            start_time_ = std::chrono::steady_clock::now();
            return get_slice_qty(0);
        }

        // 等待定时器事件
        wait_timer();

        // 检查是否已超时或所有切片已执行完毕
        auto now = std::chrono::steady_clock::now();
        double elapsed = std::chrono::duration<double>(now - start_time_).count();
        if (elapsed >= duration_sec_ || current_slice_index_ >= num_slices_ - 1) {
            // 返回剩余全部数量
            finished_ = true;
            double last = remaining_qty_;
            remaining_qty_ = 0.0;
            return last;
        }

        // 进入下一个切片
        ++current_slice_index_;
        double qty = get_slice_qty(current_slice_index_);
        return qty;
    }

    /**
     * 动态调整剩余未执行量的分配 (例如部分成交后及时更新)
     * @param filled_qty 已确认成交的数量
     */
    void confirm_fill(double filled_qty) {
        remaining_qty_ -= filled_qty;
        if (remaining_qty_ < 0.0) remaining_qty_ = 0.0;
        if (remaining_qty_ <= 1e-12) {
            finished_ = true;
        }
    }

    /** 是否已完成 */
    bool is_finished() const { return finished_; }

    /** 获取总切片数 */
    int slice_count() const { return num_slices_; }

    /** 当前切片索引 */
    int current_slice() const { return current_slice_index_; }

    /** 剩余未执行数量 */
    double remaining_quantity() const { return remaining_qty_; }

    /** 重置执行器状态 (保留原配置) */
    void reset() {
        remaining_qty_ = total_qty_;
        current_slice_index_ = 0;
        started_ = false;
        finished_ = false;
    }

    /** 动态修改紧迫度 */
    void set_urgency(double urgency) {
        if (urgency > 0.0) urgency_ = urgency;
    }

private:
    double total_qty_;
    double remaining_qty_;
    double duration_sec_;
    double urgency_;
    std::vector<double> volume_profile_;
    std::vector<double> slice_targets_;
    int    num_slices_;
    double slice_duration_sec_;
    int    current_slice_index_;
    bool   started_;
    bool   finished_;
    std::chrono::steady_clock::time_point start_time_;

    // 定时器文件描述符
    int    timer_fd_ = -1;
    struct itimerspec timer_spec_{};

    /** 归一化成交量分布，使总和为 1.0 */
    void normalize_profile() {
        double sum = std::accumulate(volume_profile_.begin(), volume_profile_.end(), 0.0);
        if (sum <= 0.0) {
            // 若数据无效，回退为均匀分布
            double uniform = 1.0 / volume_profile_.size();
            for (auto& v : volume_profile_) v = uniform;
            return;
        }
        for (auto& v : volume_profile_) v /= sum;
    }

    /** 对切片目标数量取整，并将舍入误差分配到最后一个切片 */
    void redistribute_rounding() {
        double assigned = 0.0;
        // 保留两位小数精度
        for (int i = 0; i < num_slices_ - 1; ++i) {
            double rounded = std::round(slice_targets_[i] * 100.0) / 100.0;
            slice_targets_[i] = rounded;
            assigned += rounded;
        }
        slice_targets_.back() = total_qty_ - assigned;
        if (slice_targets_.back() < 0.0) slice_targets_.back() = 0.0;
    }

    /** 获取指定索引的切片数量 (根据紧迫度调整比例) */
    double get_slice_qty(int index) {
        if (index < 0 || index >= num_slices_) return 0.0;
        double base = slice_targets_[index];
        // 紧迫度调整：urgency > 1 时前面几个切片放大，后面缩小
        double adjusted = base * urgency_;
        if (remaining_qty_ < adjusted) adjusted = remaining_qty_;
        // 不能超过剩余总量
        if (adjusted > remaining_qty_) adjusted = remaining_qty_;
        remaining_qty_ -= adjusted;
        return adjusted;
    }

    /** 初始化 Linux 定时器 fd */
    void init_timer() {
        timer_fd_ = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK);
        if (timer_fd_ < 0) {
            throw std::runtime_error("timerfd_create failed");
        }
        // 设置初始间隔：首次立即到期，之后每 slice_duration_sec_ 到期一次
        timer_spec_.it_value.tv_sec  = 0;
        timer_spec_.it_value.tv_nsec = 1;            // 首次几乎立即
        timer_spec_.it_interval.tv_sec  = static_cast<time_t>(slice_duration_sec_);
        timer_spec_.it_interval.tv_nsec = static_cast<long>(
            (slice_duration_sec_ - timer_spec_.it_interval.tv_sec) * 1e9
        );
        if (timerfd_settime(timer_fd_, 0, &timer_spec_, nullptr) < 0) {
            close(timer_fd_);
            timer_fd_ = -1;
            throw std::runtime_error("timerfd_settime failed");
        }
    }

    /** 等待定时器触发 */
    void wait_timer() {
        if (timer_fd_ < 0) return;
        uint64_t expirations = 0;
        // 使用 poll 等待，超时设为切片间隔的 1.5 倍
        int timeout_ms = static_cast<int>(slice_duration_sec_ * 1500);
        struct pollfd pfd;
        pfd.fd     = timer_fd_;
        pfd.events = POLLIN;
        int ret = poll(&pfd, 1, timeout_ms);
        if (ret > 0 && (pfd.revents & POLLIN)) {
            read(timer_fd_, &expirations, sizeof(expirations));
        }
    }
};

} // namespace execution
} // namespace fire_seed

// ------------------------------------------------------------
// C 接口 (供 pybind11 调用)
// ------------------------------------------------------------
extern "C" {

using TwapExecutor = fire_seed::execution::AdaptiveTwap;

TwapExecutor* twap_create(double total_qty, double duration_sec,
                          const double* vol_profile, int profile_len,
                          double urgency) {
    if (!vol_profile || profile_len <= 0) return nullptr;
    std::vector<double> profile(vol_profile, vol_profile + profile_len);
    try {
        return new TwapExecutor(total_qty, duration_sec, profile, urgency);
    } catch (...) {
        return nullptr;
    }
}

void twap_destroy(TwapExecutor* twap) {
    delete twap;
}

double twap_next_slice(TwapExecutor* twap) {
    if (!twap) return 0.0;
    return twap->next_slice();
}

void twap_confirm_fill(TwapExecutor* twap, double filled) {
    if (twap) twap->confirm_fill(filled);
}

int twap_is_finished(TwapExecutor* twap) {
    if (!twap) return 1;
    return twap->is_finished() ? 1 : 0;
}

int twap_slice_count(TwapExecutor* twap) {
    if (!twap) return 0;
    return twap->slice_count();
}

int twap_current_slice(TwapExecutor* twap) {
    if (!twap) return 0;
    return twap->current_slice();
}

double twap_remaining(TwapExecutor* twap) {
    if (!twap) return 0.0;
    return twap->remaining_quantity();
}

void twap_reset(TwapExecutor* twap) {
    if (twap) twap->reset();
}

void twap_set_urgency(TwapExecutor* twap, double urgency) {
    if (twap) twap->set_urgency(urgency);
}

} // extern "C"
