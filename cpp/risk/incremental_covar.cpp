/**
 * 火种系统 (FireSeed) 增量协方差估计与收缩
 * ============================================
 * 提供：
 * - 指数加权移动协方差 (EWMA, RiskMetrics 方法)
 * - Ledoit-Wolf 型收缩到对角线矩阵
 * - 均值、协方差矩阵、标准差的高效查询
 *
 * 编译：
 *   g++ -c incremental_covar.cpp -o incremental_covar.o -std=c++17 -O2
 */

#include <vector>
#include <cmath>
#include <stdexcept>
#include <cstring>
#include <algorithm>

namespace fire_seed {
namespace risk {

// ------------------------------------------------------------
// 增量协方差估计器 (EWMA)
// ------------------------------------------------------------
class IncrementalCovariance {
public:
    /**
     * 构造函数
     * @param dim       资产数量（矩阵维度）
     * @param lambda    衰减因子，典型值 0.94 (RiskMetrics)，范围 (0,1)
     */
    IncrementalCovariance(int dim, double lambda = 0.94)
        : dim_(dim), lambda_(lambda), initialized_(false)
    {
        if (dim_ <= 0) throw std::invalid_argument("Dimension must be positive");
        if (lambda_ <= 0.0 || lambda_ >= 1.0)
            throw std::invalid_argument("Lambda must be in (0,1)");

        mean_.resize(dim_, 0.0);
        cov_.resize(dim_ * dim_, 0.0);
        tmp_diff_.resize(dim_, 0.0);
    }

    /** 重置所有状态 */
    void reset() {
        initialized_ = false;
        std::fill(mean_.begin(), mean_.end(), 0.0);
        std::fill(cov_.begin(), cov_.end(), 0.0);
    }

    /**
     * 用新的观测向量更新协方差矩阵
     * @param x 收益率向量，长度必须等于 dim_
     */
    void update(const double* x) {
        if (!x) throw std::invalid_argument("Null input pointer");

        if (!initialized_) {
            // 第一个观测直接作为均值，协方差保持为零
            std::memcpy(mean_.data(), x, dim_ * sizeof(double));
            initialized_ = true;
            return;
        }

        const double alpha = 1.0 - lambda_;
        // 1. 计算旧均值与新均值的差
        for (int i = 0; i < dim_; ++i) {
            double old_mean = mean_[i];
            mean_[i] = lambda_ * old_mean + alpha * x[i];
            tmp_diff_[i] = x[i] - old_mean;      // 用于协方差更新
        }

        // 2. 更新协方差矩阵 (RiskMetrics 型：C := λ*C + (1-λ)*(x-μ_old)(x-μ_old)^T)
        //    注意：此处使用旧均值以保持无偏性，实际应用中常接受轻微偏差以换取简单性。
        for (int i = 0; i < dim_; ++i) {
            double* cov_row = &cov_[i * dim_];
            double di = tmp_diff_[i] * alpha;
            for (int j = 0; j < dim_; ++j) {
                cov_row[j] = lambda_ * cov_row[j] + di * tmp_diff_[j];
            }
        }
    }

    /** 重载：接受 std::vector<double> */
    void update(const std::vector<double>& x) {
        if (static_cast<int>(x.size()) != dim_)
            throw std::invalid_argument("Input vector size mismatch");
        update(x.data());
    }

    /** 获取当前均值向量 */
    std::vector<double> get_mean() const {
        if (!initialized_) throw std::runtime_error("No data yet");
        return mean_;
    }

    /** 获取当前协方差矩阵 (扁平化，行优先) */
    std::vector<double> get_cov() const {
        if (!initialized_) throw std::runtime_error("No data yet");
        return cov_;
    }

    /** 获取对角线方差向量 */
    std::vector<double> get_var() const {
        if (!initialized_) throw std::runtime_error("No data yet");
        std::vector<double> var(dim_);
        for (int i = 0; i < dim_; ++i) var[i] = cov_[i * dim_ + i];
        return var;
    }

    /** 获取收缩后的协方差矩阵 (Ledoit-Wolf 型收缩到对角线迹) */
    std::vector<double> get_shrunk_cov(double delta = -1.0) const {
        if (!initialized_) throw std::runtime_error("No data yet");
        // 若未显式指定收缩率，自动计算 Ledoit-Wolf 近似最优值
        if (delta < 0.0) delta = compute_ledoit_wolf_delta();

        // 计算目标矩阵： (trace/n) * I
        double trace = 0.0;
        for (int i = 0; i < dim_; ++i) trace += cov_[i * dim_ + i];
        double target_diag = trace / dim_;

        std::vector<double> result(dim_ * dim_, 0.0);
        for (int i = 0; i < dim_; ++i) {
            for (int j = 0; j < dim_; ++j) {
                double cov_ij = cov_[i * dim_ + j];
                if (i == j)
                    result[i * dim_ + j] = (1.0 - delta) * cov_ij + delta * target_diag;
                else
                    result[i * dim_ + j] = (1.0 - delta) * cov_ij;   // 仅收缩非对角线
            }
        }
        return result;
    }

    /** 手动设置收缩强度 */
    void set_shrinkage_delta(double delta) {
        if (delta < 0.0 || delta > 1.0) throw std::invalid_argument("Delta must be in [0,1]");
        user_delta_ = delta;
        delta_provided_ = true;
    }

    /** 获取当前衰减因子 */
    double get_lambda() const { return lambda_; }

    /** 获取资产维度 */
    int get_dim() const { return dim_; }

private:
    int dim_;
    double lambda_;
    bool initialized_;

    std::vector<double> mean_;
    std::vector<double> cov_;      // 行优先扁平存储
    std::vector<double> tmp_diff_; // 预分配的工作区

    double user_delta_ = 0.0;
    bool   delta_provided_ = false;

    /** 基于当前协方差矩阵估计 Ledoit-Wolf 最优收缩率 (简化版) */
    double compute_ledoit_wolf_delta() const {
        // 若无历史数据，返回一个安全值
        if (dim_ <= 1) return 0.0;

        // 计算协方差矩阵非对角线元素的方差 (简化：使用平方和)
        double sum_sq = 0.0, sum_abs = 0.0;
        for (int i = 0; i < dim_; ++i) {
            for (int j = 0; j < dim_; ++j) {
                if (i == j) continue;
                double c = cov_[i * dim_ + j];
                sum_sq += c * c;
                sum_abs += std::abs(c);
            }
        }
        // 若非对角线项几乎为0，无需收缩
        if (sum_sq < 1e-12) return 0.0;

        // 近似：δ = 1 - (Σ c²) / (Σ c² + ... )  但无解析解；
        // 这里使用启发式：收缩率与 dim 和样本量有关，但缺乏样本量 n。
        // 我们用固定小值 0.01 ~ 0.2 基于 dim 做保守估计。
        // 生产环境中可通过外部传入 delta。
        return std::min(0.5, dim_ * 0.02);
    }
};

} // namespace risk
} // namespace fire_seed

// ------------------------------------------------------------
// C 接口 (方便 pybind11 / ctypes 调用)
// ------------------------------------------------------------
extern "C" {

using CovEstimator = fire_seed::risk::IncrementalCovariance;

CovEstimator* cov_create(int dim, double lambda) {
    try {
        return new CovEstimator(dim, lambda);
    } catch (...) {
        return nullptr;
    }
}

void cov_destroy(CovEstimator* est) {
    delete est;
}

void cov_update(CovEstimator* est, const double* x, int len) {
    if (est && x && len == est->get_dim()) {
        est->update(x);
    }
}

int cov_get_dim(CovEstimator* est) {
    return est ? est->get_dim() : 0;
}

void cov_get_mean(CovEstimator* est, double* out) {
    if (est && out) {
        auto m = est->get_mean();
        std::memcpy(out, m.data(), m.size() * sizeof(double));
    }
}

void cov_get_cov(CovEstimator* est, double* out) {
    if (est && out) {
        auto c = est->get_cov();
        std::memcpy(out, c.data(), c.size() * sizeof(double));
    }
}

void cov_get_shrunk_cov(CovEstimator* est, double delta, double* out) {
    if (est && out) {
        auto c = est->get_shrunk_cov(delta);
        std::memcpy(out, c.data(), c.size() * sizeof(double));
    }
}

void cov_set_shrinkage(CovEstimator* est, double delta) {
    if (est) est->set_shrinkage_delta(delta);
}

} // extern "C"
