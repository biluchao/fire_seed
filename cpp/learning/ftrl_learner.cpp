/**
 * 火种系统 (FireSeed) 在线 FTRL 逻辑回归学习器
 * =================================================
 * 实现 FTRL-Proximal 算法 (Follow The Regularized Leader)，
 * 用于在线学习因子权重或信号融合模型的参数。
 *
 * 参考文献：
 *   McMahan, H. B., et al. (2013). Ad Click Prediction: a View from the Trenches.
 *
 * 特性：
 * - 支持 L1/L2 混合正则化，自动产生稀疏权重
 * - 增量学习，逐个样本更新
 * - 纯 C++ 实现，无外部依赖（仅标准库）
 *
 * 编译：
 *   g++ -c ftrl_learner.cpp -o ftrl_learner.o -std=c++17 -O2
 */

#include <vector>
#include <cmath>
#include <cstring>
#include <stdexcept>
#include <algorithm>
#include <numeric>

namespace fire_seed {
namespace learning {

class FtrlLearner {
public:
    /**
     * 构造函数
     * @param dim         特征维度
     * @param alpha       FTRL 学习率参数，典型值 0.1
     * @param beta        FTRL 平滑参数，典型值 1.0
     * @param lambda1     L1 正则化系数，典型值 1.0
     * @param lambda2     L2 正则化系数，典型值 1.0
     */
    FtrlLearner(int dim, double alpha = 0.1, double beta = 1.0,
                double lambda1 = 1.0, double lambda2 = 1.0)
        : dim_(dim), alpha_(alpha), beta_(beta),
          lambda1_(lambda1), lambda2_(lambda2)
    {
        if (dim_ <= 0) throw std::invalid_argument("dim must be positive");

        // 权重向量
        w_.resize(dim_, 0.0);
        // n 累积梯度平方和（每个特征）
        n_.resize(dim_, 0.0);
        // z 累积梯度（带正则化）
        z_.resize(dim_, 0.0);
    }

    /**
     * 预测：逻辑回归输出 sigmoid(w·x)
     * @param x 特征向量，长度必须等于 dim
     * @return 0~1 之间的概率值
     */
    double predict(const std::vector<double>& x) const {
        if (static_cast<int>(x.size()) != dim_)
            throw std::invalid_argument("Feature dimension mismatch");
        double dot = std::inner_product(w_.begin(), w_.end(), x.begin(), 0.0);
        return sigmoid(dot);
    }

    /**
     * 在线更新：使用一个样本 (x, y) 更新模型
     * @param x 特征向量
     * @param y 标签 (0 或 1)
     */
    void update(const std::vector<double>& x, double y) {
        if (static_cast<int>(x.size()) != dim_)
            throw std::invalid_argument("Feature dimension mismatch");

        double p = predict(x);
        double g = p - y;   // 逻辑回归梯度

        // 对每个特征更新 FTRL 参数
        for (int i = 0; i < dim_; ++i) {
            double xi = x[i];
            double gi = g * xi;
            double ni = n_[i];
            double zi = z_[i];

            // 更新 n
            n_[i] = ni + gi * gi;

            // 更新 z
            z_[i] = zi + gi - (std::sqrt(n_[i]) - std::sqrt(ni)) / alpha_ * w_[i];

            // 更新 w
            double new_w = 0.0;
            if (std::abs(z_[i]) <= lambda1_) {
                new_w = 0.0;
            } else {
                double sign = (z_[i] >= 0) ? 1.0 : -1.0;
                new_w = - (sign * lambda1_ - z_[i]) /
                         ((beta_ + std::sqrt(n_[i])) / alpha_ + lambda2_);
            }
            w_[i] = new_w;
        }
    }

    /** 获取当前权重向量 */
    std::vector<double> get_weights() const {
        return w_;
    }

    /** 获取特征维度 */
    int get_dim() const { return dim_; }

    /** 重置所有学习状态 */
    void reset() {
        std::fill(w_.begin(), w_.end(), 0.0);
        std::fill(n_.begin(), n_.end(), 0.0);
        std::fill(z_.begin(), z_.end(), 0.0);
    }

    /** 手动设置某个特征的权重（用于热启动） */
    void set_weight(int index, double value) {
        if (index < 0 || index >= dim_) throw std::out_of_range("index out of range");
        w_[index] = value;
    }

    /** 获取内部累积梯度平方和（用于诊断） */
    std::vector<double> get_n() const { return n_; }
    std::vector<double> get_z() const { return z_; }

    /** 动态修改学习率参数 */
    void set_alpha(double alpha) { alpha_ = alpha; }
    void set_beta(double beta) { beta_ = beta; }
    void set_lambda1(double l1) { lambda1_ = l1; }
    void set_lambda2(double l2) { lambda2_ = l2; }

private:
    int    dim_;
    double alpha_;
    double beta_;
    double lambda1_;
    double lambda2_;

    std::vector<double> w_;   // 权重
    std::vector<double> n_;   // 累积梯度平方
    std::vector<double> z_;   // FTRL z 变量

    static double sigmoid(double x) {
        return 1.0 / (1.0 + std::exp(-x));
    }
};

} // namespace learning
} // namespace fire_seed

// ------------------------------------------------------------
// C 接口 (供 pybind11 绑定)
// ------------------------------------------------------------
extern "C" {

using Ftrl = fire_seed::learning::FtrlLearner;

Ftrl* ftrl_create(int dim, double alpha, double beta, double l1, double l2) {
    try {
        return new Ftrl(dim, alpha, beta, l1, l2);
    } catch (...) {
        return nullptr;
    }
}

void ftrl_destroy(Ftrl* f) {
    delete f;
}

double ftrl_predict(Ftrl* f, const double* x, int len) {
    if (!f || !x || len != f->get_dim()) return 0.0;
    std::vector<double> vec(x, x + len);
    return f->predict(vec);
}

void ftrl_update(Ftrl* f, const double* x, int len, double y) {
    if (!f || !x || len != f->get_dim()) return;
    std::vector<double> vec(x, x + len);
    f->update(vec, y);
}

int ftrl_get_dim(Ftrl* f) {
    return f ? f->get_dim() : 0;
}

void ftrl_get_weights(Ftrl* f, double* out) {
    if (!f || !out) return;
    auto w = f->get_weights();
    std::memcpy(out, w.data(), w.size() * sizeof(double));
}

void ftrl_set_weight(Ftrl* f, int idx, double val) {
    if (f) f->set_weight(idx, val);
}

void ftrl_reset(Ftrl* f) {
    if (f) f->reset();
}

} // extern "C
