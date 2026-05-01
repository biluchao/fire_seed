/**
 * 火种系统 (FireSeed) 冰山订单随机切片器
 * ==============================================
 * 将大额挂单拆分为一系列可见的小额挂单（冰山），
 * 显单大小在配置范围内随机波动，避免被高频做市商模式识别。
 *
 * 核心特性：
 * - 显单大小在 [min_show, max_show] 内均匀随机
 * - 随机种子可注入（便于幽灵环境重现）
 * - 支持双随机策略（均匀/对数正态切换）
 * - 剩余总量不足显单时直接全部显示
 * - 记录所有切片历史供审计
 *
 * 编译：
 *   g++ -c iceberg_slicer.cpp -o iceberg_slicer.o -std=c++17 -O2
 */

#include <vector>
#include <random>
#include <stdexcept>
#include <cstdint>
#include <cstring>
#include <cmath>

namespace fire_seed {
namespace execution {

// ------------------------------------------------------------
// 冰山订单切片器
// ------------------------------------------------------------
class IcebergSlicer {
public:
    /**
     * 构造函数
     * @param total_qty     总委托数量
     * @param min_show      单次最小显单量
     * @param max_show      单次最大显单量
     * @param seed          随机种子 (0 = 使用 std::random_device)
     * @param use_lognormal 是否使用对数正态分布 (默认均匀)
     */
    IcebergSlicer(double total_qty,
                  double min_show,
                  double max_show,
                  uint64_t seed = 0,
                  bool use_lognormal = false)
        : total_qty_(total_qty),
          remaining_qty_(total_qty),
          min_show_(min_show),
          max_show_(max_show),
          finished_(false),
          use_lognormal_(use_lognormal)
    {
        if (total_qty_ <= 0.0)
            throw std::invalid_argument("total_qty must be positive");
        if (min_show_ <= 0.0 || max_show_ <= 0.0)
            throw std::invalid_argument("show sizes must be positive");
        if (min_show_ > max_show_)
            throw std::invalid_argument("min_show cannot exceed max_show");

        // 初始化随机数生成器
        if (seed != 0) {
            rng_.seed(seed);
        } else {
            std::random_device rd;
            rng_.seed(rd());
        }
    }

    /**
     * 获取下一个冰山显单量。
     * @return 本次显示的数量 (单位与 total_qty 一致)
     */
    double next_slice() {
        if (finished_) return 0.0;

        double qty;
        if (remaining_qty_ <= max_show_) {
            // 剩余量小于等于最大显单，全部显示
            qty = remaining_qty_;
            finished_ = true;
        } else {
            // 随机生成显单量
            qty = random_show_qty();
            // 确保不超过剩余量
            if (qty > remaining_qty_ - min_show_) {
                // 如果取随机值后剩余不足，回退到最小显单
                qty = std::min(max_show_, remaining_qty_);
            }
        }

        remaining_qty_ -= qty;
        if (remaining_qty_ <= 1e-12) {
            remaining_qty_ = 0.0;
            finished_ = true;
        }

        history_.push_back(qty);
        return qty;
    }

    /**
     * 重置切片器（可重新开始分割同一订单）
     */
    void reset() {
        remaining_qty_ = total_qty_;
        finished_ = false;
        history_.clear();
    }

    /** 是否已经全部输出完毕 */
    bool is_finished() const { return finished_; }

    /** 剩余未显示的挂单量 */
    double remaining_quantity() const { return remaining_qty_; }

    /** 获取切片历史记录 */
    std::vector<double> get_history() const { return history_; }

    /** 获取总切片数 */
    size_t slice_count() const { return history_.size(); }

    /** 动态调整显单区间 */
    void set_show_range(double min_show, double max_show) {
        if (min_show <= 0.0 || max_show <= 0.0 || min_show > max_show)
            throw std::invalid_argument("Invalid show range");
        min_show_ = min_show;
        max_show_ = max_show;
    }

    /** 切换分布类型 */
    void set_lognormal(bool use_lognormal) {
        use_lognormal_ = use_lognormal;
    }

private:
    double total_qty_;
    double remaining_qty_;
    double min_show_;
    double max_show_;
    bool   finished_;
    bool   use_lognormal_;
    std::mt19937_64 rng_;
    std::vector<double> history_;

    /**
     * 生成随机显单量。
     * 均匀模式：U(min_show, max_show)
     * 对数正态模式：中位数为 (min+max)/2，标准差自适应
     */
    double random_show_qty() {
        if (use_lognormal_) {
            // 参数：mu = ln(median), sigma = (ln(max) - ln(min)) / 6 (约覆盖99.7%区间)
            double ln_min = std::log(min_show_);
            double ln_max = std::log(max_show_);
            double mu = (ln_min + ln_max) * 0.5;
            double sigma = (ln_max - ln_min) / 6.0;
            std::lognormal_distribution<double> dist(mu, sigma);
            return std::max(min_show_, std::min(max_show_, dist(rng_)));
        } else {
            // 均匀分布，但为避免模式识别，加入 2% 的上下浮动噪声
            double range = max_show_ - min_show_;
            std::uniform_real_distribution<double> dist(0.0, range);
            double base = min_show_ + dist(rng_);
            // 添加微小抖动
            double noise = (std::uniform_real_distribution<double>(-0.02 * range, 0.02 * range))(rng_);
            return std::max(min_show_, std::min(max_show_, base + noise));
        }
    }
};

} // namespace execution
} // namespace fire_seed

// ------------------------------------------------------------
// C 接口 (供 pybind11 绑定)
// ------------------------------------------------------------
extern "C" {

using IcebergExecutor = fire_seed::execution::IcebergSlicer;

IcebergExecutor* iceberg_create(double total_qty,
                                double min_show, double max_show,
                                uint64_t seed, int use_lognormal) {
    try {
        return new IcebergExecutor(total_qty, min_show, max_show, seed, use_lognormal != 0);
    } catch (...) {
        return nullptr;
    }
}

void iceberg_destroy(IcebergExecutor* s) { delete s; }

double iceberg_next_slice(IcebergExecutor* s) {
    if (!s) return 0.0;
    return s->next_slice();
}

void iceberg_reset(IcebergExecutor* s) { if (s) s->reset(); }
int iceberg_is_finished(IcebergExecutor* s) { return (s && s->is_finished()) ? 1 : 0; }
double iceberg_remaining(IcebergExecutor* s) { return s ? s->remaining_quantity() : 0.0; }
int iceberg_slice_count(IcebergExecutor* s) { return s ? static_cast<int>(s->slice_count()) : 0; }
void iceberg_set_range(IcebergExecutor* s, double min_s, double max_s) {
    if (s) s->set_show_range(min_s, max_s);
}
void iceberg_set_lognormal(IcebergExecutor* s, int use_ln) {
    if (s) s->set_lognormal(use_ln != 0);
}

} // extern "C"
