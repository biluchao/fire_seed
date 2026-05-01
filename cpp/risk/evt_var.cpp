/**
 * 火种系统 (FireSeed) 极值理论 VaR 计算模块
 * ============================================
 * 基于峰值超阈值 (Peaks Over Threshold, POT) 和广义帕累托分布 (GPD)
 * 估计极端风险值 (VaR) 与条件风险值 (CVaR)。
 *
 * 若数据不足以可靠拟合 GPD，自动回退至历史模拟法。
 *
 * 编译：
 *   g++ -c evt_var.cpp -o evt_var.o -std=c++17 -O2
 */

#include <vector>
#include <cmath>
#include <algorithm>
#include <numeric>
#include <stdexcept>
#include <cstring>

namespace fire_seed {
namespace risk {

// ------------------------------------------------------------
// 简单的 GPD 参数估计 (Hill 估计量 / MLE 简化)
// ------------------------------------------------------------
class EvtRiskEstimator {
public:
    EvtRiskEstimator() : fitted_(false) {}

    /**
     * 使用历史收益率序列拟合 EVT 模型
     * @param returns  历史收益率序列 (通常取绝对值或负值尾部)
     * @param threshold 阈值 (例如取收益率的 0.95 分位数)
     * @return true 表示拟合成功
     */
    bool fit(const std::vector<double>& returns, double threshold = 0.0) {
        returns_ = returns;
        std::sort(returns_.begin(), returns_.end());
        n_ = returns_.size();
        if (n_ < 50) return false;   // 数据太少

        // 默认阈值：95% 分位数
        if (threshold == 0.0) {
            int idx = static_cast<int>(n_ * 0.95);
            if (idx >= n_) idx = n_ - 1;
            threshold_ = returns_[idx];
        } else {
            threshold_ = threshold;
        }

        // 找出超过阈值的超额值
        excesses_.clear();
        for (double r : returns_) {
            if (r > threshold_)
                excesses_.push_back(r - threshold_);
        }
        nu_ = static_cast<int>(excesses_.size());
        if (nu_ < 30) {
            // 超额数据不足，回退历史模拟
            fitted_ = false;
            return false;
        }

        // 使用 Hill 估计量估计尾部指数 ξ (仅用于 ξ > 0 即厚尾情形)
        // 这里简化：采用矩估计法 (Method of Moments)
        double sum_y = 0.0, sum_y2 = 0.0;
        for (double y : excesses_) {
            sum_y += y;
            sum_y2 += y * y;
        }
        double mean_excess = sum_y / nu_;
        double var_excess  = (sum_y2 / nu_) - (mean_excess * mean_excess);
        if (var_excess <= 1e-12) var_excess = 1e-12;

        // GPD 参数: ξ = 0.5 * (1 - (mean_excess*mean_excess)/var_excess)
        //            σ = 0.5 * mean_excess * (mean_excess*mean_excess/var_excess + 1)
        xi_ = 0.5 * (1.0 - (mean_excess * mean_excess) / var_excess);
        // 确保 ξ > -0.5 以保证良好的统计性质
        if (xi_ <= -0.5) xi_ = -0.49;
        if (xi_ > 1.0) xi_ = 1.0;     // 限制极大值

        sigma_ = 0.5 * mean_excess * ((mean_excess * mean_excess) / var_excess + 1.0);
        if (sigma_ <= 0.0) sigma_ = 0.001;

        fitted_ = true;
        return true;
    }

    /**
     * 计算给定置信水平的 VaR
     * @param confidence 置信水平，如 0.99
     * @return VaR 值 (在原始收益率单位下，正值表示损失)
     */
    double calculate_var(double confidence = 0.99) const {
        if (!fitted_ || excesses_.empty()) {
            // 回退：历史模拟法
            return historical_var(confidence);
        }
        // 根据 GPD 计算 VaR
        // VaR = u + (σ/ξ) * (((n/n_u)*(1-confidence))^(-ξ) - 1)   (ξ ≠ 0)
        // 对于 ξ = 0 退化为指数分布，这里忽略
        double p = 1.0 - confidence;
        double N = static_cast<double>(n_);
        double Nu = static_cast<double>(nu_);
        double tail_prob = (N / Nu) * p;
        if (tail_prob <= 0.0) tail_prob = 1e-10;

        double var;
        if (std::abs(xi_) < 1e-6) {
            // ξ ≈ 0
            var = threshold_ - sigma_ * std::log(tail_prob);
        } else {
            var = threshold_ + (sigma_ / xi_) * (std::pow(tail_prob, -xi_) - 1.0);
        }
        // 确保非负
        if (var < 0.0) var = 0.0;
        return var;
    }

    /**
     * 计算给定置信水平的 CVaR (Expected Shortfall)
     * CVaR = VaR + (σ + ξ*(VaR - u)) / (1 - ξ)   (ξ < 1)
     */
    double calculate_cvar(double confidence = 0.99) const {
        double var = calculate_var(confidence);
        if (!fitted_) {
            // 历史模拟法下的 CVaR: 超过VaR的平均损失
            return historical_cvar(confidence);
        }
        if (xi_ >= 1.0) {
            // 当 ξ >= 1 时，CVaR 无穷大，返回 VaR 的三倍作为保守估计
            return var * 3.0;
        }
        double cvar = var + (sigma_ + xi_ * (var - threshold_)) / (1.0 - xi_);
        if (cvar < var) cvar = var;
        return cvar;
    }

    /** 重置所有状态 */
    void reset() {
        fitted_ = false;
        returns_.clear();
        excesses_.clear();
        n_ = 0;
        nu_ = 0;
        threshold_ = 0.0;
        xi_ = 0.0;
        sigma_ = 0.0;
    }

private:
    bool fitted_;
    std::vector<double> returns_;
    std::vector<double> excesses_;
    int n_;               // 总样本数
    int nu_;              // 超过阈值的样本数
    double threshold_;
    double xi_;
    double sigma_;

    double historical_var(double confidence) const {
        if (returns_.empty()) return 0.0;
        int idx = static_cast<int>((1.0 - confidence) * returns_.size());
        if (idx < 0) idx = 0;
        if (idx >= static_cast<int>(returns_.size())) idx = returns_.size() - 1;
        return std::max(0.0, -returns_[idx]);   // 收益率越小(负)损失越大
    }

    double historical_cvar(double confidence) const {
        if (returns_.empty()) return 0.0;
        double var = historical_var(confidence);
        double sum = 0.0;
        int count = 0;
        for (double r : returns_) {
            if (-r > var) {
                sum += (-r);
                ++count;
            }
        }
        if (count == 0) return var;
        return sum / count;
    }
};

} // namespace risk
} // namespace fire_seed

// ------------------------------------------------------------
// C 接口 (供 pybind11 调用)
// ------------------------------------------------------------
extern "C" {

using EvtEstimator = fire_seed::risk::EvtRiskEstimator;

EvtEstimator* evt_create() {
    return new EvtEstimator();
}

void evt_destroy(EvtEstimator* est) {
    delete est;
}

int evt_fit(EvtEstimator* est, const double* returns, int n, double threshold) {
    if (!est || !returns || n <= 0) return 0;
    std::vector<double> rets(returns, returns + n);
    return est->fit(rets, threshold) ? 1 : 0;
}

double evt_calculate_var(EvtEstimator* est, double confidence) {
    if (!est) return 0.0;
    return est->calculate_var(confidence);
}

double evt_calculate_cvar(EvtEstimator* est, double confidence) {
    if (!est) return 0.0;
    return est->calculate_cvar(confidence);
}

void evt_reset(EvtEstimator* est) {
    if (est) est->reset();
}

} // extern "C"
