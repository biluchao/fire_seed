/**
 * 火种系统 (FireSeed) SIMD 向量化数学模块
 * ============================================
 * 使用 AVX2/FMA 指令集加速批量数值运算，
 * 主要用于技术指标计算 (SMA, EMA, ATR, 布林带等)。
 *
 * 特性：
 * - 自动检测 CPU 是否支持 AVX2/FMA，否则回退标准循环
 * - 提供批量运算接口：加减乘除、指数、对数、平方根等
 * - 与 Python 侧无缝对接（C 接口）
 *
 * 编译：
 *   g++ -c vector_math.cpp -o vector_math.o -std=c++17 -O3 -mavx2 -mfma
 */

#include <cstdint>
#include <cstddef>
#include <cstring>
#include <cmath>
#include <vector>
#include <algorithm>
#include <stdexcept>

// 根据系统检测 AVX2 支持 (仅用于运行时回退，编译需开启)
#if defined(__AVX2__) && defined(__FMA__)
    #define FIRESEED_USE_AVX2 1
    #include <immintrin.h>
#else
    #define FIRESEED_USE_AVX2 0
#endif

namespace fire_seed {
namespace math {

// ------------------------------------------------------------
// 运行时检测 AVX2 支持 (简单方法：查看编译时宏)
// ------------------------------------------------------------
bool has_avx2() {
#if FIRESEED_USE_AVX2
    return true;
#else
    return false;
#endif
}

// ------------------------------------------------------------
// 向量加法: dst[i] = a[i] + b[i]    (长度为 n)
// ------------------------------------------------------------
void vector_add(const double* a, const double* b, double* dst, size_t n) {
#if FIRESEED_USE_AVX2
    size_t i = 0;
    for (; i + 4 <= n; i += 4) {
        __m256d va = _mm256_loadu_pd(a + i);
        __m256d vb = _mm256_loadu_pd(b + i);
        __m256d vd = _mm256_add_pd(va, vb);
        _mm256_storeu_pd(dst + i, vd);
    }
    // 剩余尾部
    for (; i < n; ++i) dst[i] = a[i] + b[i];
#else
    for (size_t i = 0; i < n; ++i) dst[i] = a[i] + b[i];
#endif
}

// ------------------------------------------------------------
// 向量减法: dst[i] = a[i] - b[i]
// ------------------------------------------------------------
void vector_sub(const double* a, const double* b, double* dst, size_t n) {
#if FIRESEED_USE_AVX2
    size_t i = 0;
    for (; i + 4 <= n; i += 4) {
        __m256d va = _mm256_loadu_pd(a + i);
        __m256d vb = _mm256_loadu_pd(b + i);
        __m256d vd = _mm256_sub_pd(va, vb);
        _mm256_storeu_pd(dst + i, vd);
    }
    for (; i < n; ++i) dst[i] = a[i] - b[i];
#else
    for (size_t i = 0; i < n; ++i) dst[i] = a[i] - b[i];
#endif
}

// ------------------------------------------------------------
// 向量乘法: dst[i] = a[i] * b[i]
// ------------------------------------------------------------
void vector_mul(const double* a, const double* b, double* dst, size_t n) {
#if FIRESEED_USE_AVX2
    size_t i = 0;
    for (; i + 4 <= n; i += 4) {
        __m256d va = _mm256_loadu_pd(a + i);
        __m256d vb = _mm256_loadu_pd(b + i);
        __m256d vd = _mm256_mul_pd(va, vb);
        _mm256_storeu_pd(dst + i, vd);
    }
    for (; i < n; ++i) dst[i] = a[i] * b[i];
#else
    for (size_t i = 0; i < n; ++i) dst[i] = a[i] * b[i];
#endif
}

// ------------------------------------------------------------
// 向量除法: dst[i] = a[i] / b[i]   (b[i] 不能为 0)
// ------------------------------------------------------------
void vector_div(const double* a, const double* b, double* dst, size_t n) {
#if FIRESEED_USE_AVX2
    size_t i = 0;
    for (; i + 4 <= n; i += 4) {
        __m256d va = _mm256_loadu_pd(a + i);
        __m256d vb = _mm256_loadu_pd(b + i);
        __m256d vd = _mm256_div_pd(va, vb);
        _mm256_storeu_pd(dst + i, vd);
    }
    for (; i < n; ++i) dst[i] = a[i] / b[i];
#else
    for (size_t i = 0; i < n; ++i) dst[i] = a[i] / b[i];
#endif
}

// ------------------------------------------------------------
// 标量乘向量: dst[i] = factor * src[i]
// ------------------------------------------------------------
void vector_scale(const double* src, double factor, double* dst, size_t n) {
#if FIRESEED_USE_AVX2
    __m256d vfactor = _mm256_set1_pd(factor);
    size_t i = 0;
    for (; i + 4 <= n; i += 4) {
        __m256d vs = _mm256_loadu_pd(src + i);
        __m256d vd = _mm256_mul_pd(vs, vfactor);
        _mm256_storeu_pd(dst + i, vd);
    }
    for (; i < n; ++i) dst[i] = src[i] * factor;
#else
    for (size_t i = 0; i < n; ++i) dst[i] = src[i] * factor;
#endif
}

// ------------------------------------------------------------
// 计算指数移动平均 (EMA)
// dst[0] = src[0]
// dst[i] = alpha * src[i] + (1-alpha) * dst[i-1]
// ------------------------------------------------------------
void ema(const double* src, size_t n, double alpha, double* dst) {
    if (n == 0) return;
    dst[0] = src[0];
    double one_minus_alpha = 1.0 - alpha;
    for (size_t i = 1; i < n; ++i) {
        dst[i] = alpha * src[i] + one_minus_alpha * dst[i - 1];
    }
}

// ------------------------------------------------------------
// 计算简单移动平均 (SMA)
// dst[i] = mean( src[i-window+1 .. i] )   (i >= window-1)
// 输出长度 = n - window + 1 (若 n < window 则输出为空)
// ------------------------------------------------------------
void sma(const double* src, size_t n, size_t window, double* dst) {
    if (n < window) return;
    double sum = 0.0;
    for (size_t i = 0; i < window; ++i) sum += src[i];
    size_t out_idx = 0;
    dst[out_idx++] = sum / window;
    for (size_t i = window; i < n; ++i) {
        sum += src[i] - src[i - window];
        dst[out_idx++] = sum / window;
    }
}

// ------------------------------------------------------------
// 计算滚动标准差 (基于总体方差)
// dst[i] = stddev( src[i-window+1 .. i] )   (i >= window-1)
// ------------------------------------------------------------
void rolling_stddev(const double* src, size_t n, size_t window, double* dst) {
    if (n < window) return;
    double sum = 0.0, sum_sq = 0.0;
    for (size_t i = 0; i < window; ++i) {
        sum += src[i];
        sum_sq += src[i] * src[i];
    }
    size_t out_idx = 0;
    double mean = sum / window;
    double var = (sum_sq / window) - (mean * mean);
    dst[out_idx++] = (var > 0.0) ? std::sqrt(var) : 0.0;
    for (size_t i = window; i < n; ++i) {
        sum += src[i] - src[i - window];
        sum_sq += src[i] * src[i] - src[i - window] * src[i - window];
        mean = sum / window;
        var = (sum_sq / window) - (mean * mean);
        dst[out_idx++] = (var > 0.0) ? std::sqrt(var) : 0.0;
    }
}

// ------------------------------------------------------------
// 向量绝对值: dst[i] = |src[i]|
// ------------------------------------------------------------
void vector_abs(const double* src, double* dst, size_t n) {
#if FIRESEED_USE_AVX2
    // AVX2 没有直接的 abs 指令，通过掩码清除符号位
    const __m256d sign_mask = _mm256_set1_pd(-0.0); // -0.0 的位表示: 符号位为1，其余为0
    size_t i = 0;
    for (; i + 4 <= n; i += 4) {
        __m256d vs = _mm256_loadu_pd(src + i);
        __m256d vd = _mm256_andnot_pd(sign_mask, vs);
        _mm256_storeu_pd(dst + i, vd);
    }
    for (; i < n; ++i) dst[i] = std::abs(src[i]);
#else
    for (size_t i = 0; i < n; ++i) dst[i] = std::abs(src[i]);
#endif
}

// ------------------------------------------------------------
// 向量指数: dst[i] = exp(src[i])
// ------------------------------------------------------------
void vector_exp(const double* src, double* dst, size_t n) {
    // AVX2 没有内建 exp，调用标准库
    for (size_t i = 0; i < n; ++i) dst[i] = std::exp(src[i]);
}

// ------------------------------------------------------------
// 向量对数: dst[i] = log(src[i])   (src[i] > 0)
// ------------------------------------------------------------
void vector_log(const double* src, double* dst, size_t n) {
    for (size_t i = 0; i < n; ++i) dst[i] = std::log(src[i]);
}

// ------------------------------------------------------------
// 向量平方根: dst[i] = sqrt(src[i])   (src[i] >= 0)
// ------------------------------------------------------------
void vector_sqrt(const double* src, double* dst, size_t n) {
    for (size_t i = 0; i < n; ++i) dst[i] = std::sqrt(src[i]);
}

// ------------------------------------------------------------
// 点积: sum(a[i] * b[i])
// ------------------------------------------------------------
double dot_product(const double* a, const double* b, size_t n) {
    double result = 0.0;
#if FIRESEED_USE_AVX2
    __m256d vacc = _mm256_setzero_pd();
    size_t i = 0;
    for (; i + 4 <= n; i += 4) {
        __m256d va = _mm256_loadu_pd(a + i);
        __m256d vb = _mm256_loadu_pd(b + i);
        vacc = _mm256_fmadd_pd(va, vb, vacc);  // FMA: vacc = va * vb + vacc
    }
    double tmp[4];
    _mm256_storeu_pd(tmp, vacc);
    result = tmp[0] + tmp[1] + tmp[2] + tmp[3];
    // 剩余
    for (; i < n; ++i) result += a[i] * b[i];
#else
    for (size_t i = 0; i < n; ++i) result += a[i] * b[i];
#endif
    return result;
}

// ------------------------------------------------------------
// 计算真实波幅 (True Range) 数组
// high, low, close 为输入序列 (长度 n)
// tr[i] = max( high[i]-low[i], |high[i]-close[i-1]|, |low[i]-close[i-1]| )
// i=0 时 tr[0] = high[0]-low[0]
// 输出 tr 数组长度 = n
// ------------------------------------------------------------
void true_range(const double* high, const double* low, const double* close,
                size_t n, double* tr) {
    if (n == 0) return;
    tr[0] = high[0] - low[0];
    for (size_t i = 1; i < n; ++i) {
        double a = high[i] - low[i];
        double b = std::abs(high[i] - close[i-1]);
        double c = std::abs(low[i] - close[i-1]);
        tr[i] = std::max({a, b, c});
    }
}

// ------------------------------------------------------------
// 计算平均真实波幅 (ATR)
// 对 tr 序列应用 EMA 或 SMA (默认 EMA)
// ------------------------------------------------------------
void average_true_range(const double* high, const double* low, const double* close,
                        size_t n, size_t period, double* atr, int use_ema = 1) {
    std::vector<double> tr(n);
    true_range(high, low, close, n, tr.data());
    if (use_ema) {
        ema(tr.data(), n, 1.0 / period, atr);
    } else {
        // SMA 模式
        if (n >= period) {
            double sum = 0.0;
            for (size_t i = 0; i < period; ++i) sum += tr[i];
            atr[0] = sum / period;
            for (size_t i = period; i < n; ++i) {
                sum += tr[i] - tr[i - period];
                atr[i - period + 1] = sum / period;
            }
        }
    }
}

} // namespace math
} // namespace fire_seed

// ------------------------------------------------------------
// C 接口 (供 pybind11 绑定)
// ------------------------------------------------------------
extern "C" {

using namespace fire_seed::math;

int has_avx2_support() {
    return has_avx2() ? 1 : 0;
}

void c_vector_add(const double* a, const double* b, double* dst, size_t n) { vector_add(a,b,dst,n); }
void c_vector_sub(const double* a, const double* b, double* dst, size_t n) { vector_sub(a,b,dst,n); }
void c_vector_mul(const double* a, const double* b, double* dst, size_t n) { vector_mul(a,b,dst,n); }
void c_vector_div(const double* a, const double* b, double* dst, size_t n) { vector_div(a,b,dst,n); }
void c_vector_scale(const double* src, double factor, double* dst, size_t n) { vector_scale(src,factor,dst,n); }
void c_ema(const double* src, size_t n, double alpha, double* dst) { ema(src,n,alpha,dst); }
double c_dot_product(const double* a, const double* b, size_t n) { return dot_product(a,b,n); }
void c_true_range(const double* h, const double* l, const double* c, size_t n, double* tr) { true_range(h,l,c,n,tr); }
void c_atr(const double* h, const double* l, const double* c, size_t n, size_t period, double* atr, int use_ema) {
    average_true_range(h,l,c,n,period,atr,use_ema);
}

} // extern "C"
