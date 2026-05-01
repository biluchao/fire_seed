/**
 * 火种系统 (FireSeed) 跳跃检测器 (Lee-Mykland 方法)
 * ==================================================
 * 基于双幂次变差 (Bipower Variation) 的非参数跳跃检验，
 * 实时监测价格序列中的不连续跳跃，用于：
 * - 触发感知层冻结（粒子滤波、锁相环）
 * - 风控熔断辅助决策
 * - 极端事件标记
 *
 * 参考:
 *   Lee, S., & Mykland, P. A. (2008). Jumps in financial markets.
 *   Review of Financial Studies, 21(6), 2535-2563.
 *
 * 编译：
 *   g++ -c jump_detector.cpp -o jump_detector.o -std=c++17 -O2
 */

#include <vector>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <deque>
#include <algorithm>
#include <numeric>
#include <stdexcept>

namespace fire_seed {
namespace risk {

// ------------------------------------------------------------
// Lee-Mykland 跳跃检测器
// ------------------------------------------------------------
class JumpDetector {
public:
    /**
     * 构造函数
     * @param window     滚动计算窗口大小 (通常 16~32)
     * @param k          跳跃阈值倍数 (常用 4.0，对应 99.99% 置信)
     * @param min_sample 最小样本数，低于此值不检测
     */
    JumpDetector(int window = 16, double k = 4.0, int min_sample = 12)
        : window_(window), k_(k), min_sample_(min_sample) {}

    /** 重置所有内部状态 */
    void reset() {
        returns_.clear();
        last_stat_ = 0.0;
        jump_detected_ = false;
    }

    /**
     * 添加一个新的收益率样本并实时检测跳跃
     * @param ret 对数收益率 (单次观测)
     * @return 返回 true 如果当前观测被识别为跳跃
     */
    bool update(double ret) {
        returns_.push_back(ret);
        if (static_cast<int>(returns_.size()) > window_ + 2) {
            returns_.pop_front();
        }
        // 数据不足时不检测
        if (static_cast<int>(returns_.size()) < min_sample_) {
            jump_detected_ = false;
            return false;
        }
        // 计算跳跃统计量
        last_stat_ = compute_lee_mykland_statistic();
        jump_detected_ = (last_stat_ > k_);
        return jump_detected_;
    }

    /**
     * 批量检测：对给定收益率序列的最后一个点检测是否为跳跃
     * @param returns 近期收益率序列
     * @return 返回 true 如果最新点被认为是跳跃
     */
    bool detect(const std::vector<double>& returns) {
        if (returns.empty()) return false;
        // 将序列填入内部缓冲区
        returns_.clear();
        for (double r : returns) returns_.push_back(r);
        while (static_cast<int>(returns_.size()) > window_ + 2)
            returns_.pop_front();

        if (static_cast<int>(returns_.size()) < min_sample_) {
            jump_detected_ = false;
            return false;
        }
        last_stat_ = compute_lee_mykland_statistic();
        jump_detected_ = (last_stat_ > k_);
        return jump_detected_;
    }

    /** 返回最近一次计算的统计量 */
    double get_last_statistic() const { return last_stat_; }

    /** 是否检测到跳跃 */
    bool is_jump() const { return jump_detected_; }

    /** 获取当前窗口内的收益率序列 */
    std::vector<double> get_returns() const {
        return std::vector<double>(returns_.begin(), returns_.end());
    }

    /** 动态调整阈值 */
    void set_threshold(double k) { k_ = k; }
    double get_threshold() const { return k_; }

private:
    int                 window_;
    double              k_;
    int                 min_sample_;
    std::deque<double>   returns_;
    double              last_stat_ = 0.0;
    bool                jump_detected_ = false;

    /**
     * Lee-Mykland 统计量计算
     * 公式: L(i) = r_i / sqrt( (1/(K-2)) * sum_{j=i-K+2}^{i-1} |r_j| * |r_{j-1}| )
     * 其中 K = window_
     */
    double compute_lee_mykland_statistic() const {
        int n = static_cast<int>(returns_.size());
        if (n < 3) return 0.0;
        // 使用最后 K 个点计算，K = min(window_, n)
        int K = std::min(window_, n);
        // 取最后 K 个元素
        auto it = returns_.end();
        std::vector<double> win(it - K, it);

        // 计算双幂次变差的分母
        double sum_bp = 0.0;
        for (int j = 2; j < K; ++j) {
            sum_bp += std::abs(win[j]) * std::abs(win[j - 1]);
        }
        double bp_avg = (K > 2) ? (sum_bp / (K - 2)) : 0.0;
        if (bp_avg <= 1e-12) return 0.0;

        // 最新收益率 r_i 为最后一个
        double r_i = win.back();
        double stat = r_i / std::sqrt(bp_avg);
        // 返回绝对值或正负均可，通常使用绝对值判断跳跃
        return std::abs(stat);
    }
};

} // namespace risk
} // namespace fire_seed

// ------------------------------------------------------------
// C 接口 (供 pybind11 / ctypes 调用)
// ------------------------------------------------------------
extern "C" {

using JumpDet = fire_seed::risk::JumpDetector;

JumpDet* jump_detector_create(int window, double k, int min_sample) {
    try {
        return new JumpDet(window, k, min_sample);
    } catch (...) {
        return nullptr;
    }
}

void jump_detector_destroy(JumpDet* jd) {
    delete jd;
}

int jump_detector_update(JumpDet* jd, double ret) {
    if (!jd) return 0;
    return jd->update(ret) ? 1 : 0;
}

int jump_detector_detect(JumpDet* jd, const double* returns, int n) {
    if (!jd || !returns || n <= 0) return 0;
    std::vector<double> rets(returns, returns + n);
    return jd->detect(rets) ? 1 : 0;
}

void jump_detector_reset(JumpDet* jd) {
    if (jd) jd->reset();
}

double jump_detector_get_stat(JumpDet* jd) {
    if (!jd) return 0.0;
    return jd->get_last_statistic();
}

void jump_detector_set_threshold(JumpDet* jd, double k) {
    if (jd) jd->set_threshold(k);
}

} // extern "C"
