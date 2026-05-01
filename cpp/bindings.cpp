/**
 * 火种系统 (FireSeed) Python 绑定模块 (pybind11)
 * ==================================================
 * 将所有 C++ 高性能模块导出为 Python 可调用扩展。
 * 编译后生成 fire_seed_cpp.so (或 .pyd)。
 *
 * 绑定策略：
 * - 对于提供 C++ 类的模块，直接绑定类
 * - 对于纯 C 接口模块，绑定 C 函数
 * - 所有函数均释放 GIL，允许 Python 多线程调用
 *
 * 编译：
 *   g++ -O3 -shared -std=c++17 -fPIC $(python3 -m pybind11 --includes) \
 *       bindings.cpp xxx.o ... -o fire_seed_cpp.so
 */

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/functional.h>
#include <cstdint>
#include <vector>
#include <string>

// ===================== 包含各模块头文件 =====================
#include "ring_queue.h"
#include "flat_msg.h"

// 风险模块
#include "incremental_covar.cpp"  // 直接包含实现 (也可编译为.o后链接)
#include "evt_var.cpp"
#include "jump_detector.cpp"

// 执行模块
#include "twap_adaptive.cpp"
#include "iceberg_slicer.cpp"

// 学习模块
#include "ftrl_learner.cpp"
#include "vib_inference.cpp"

// 数学与向量化
#include "vector_math.cpp"

// 网络模块
// #include "tcp_userstack.cpp"    // 若需绑定
// #include "af_xdp_recv.cpp"     // 若需绑定

// 系统监控 (占位)
// #include "hard_watcher.cpp"

namespace py = pybind11;
using namespace py::literals;

// ------------------------------------------------------------
// 各模块的绑定函数
// ------------------------------------------------------------

// ---- ring_queue.h ----
void bind_ring_queue(py::module& m) {
    py::class_<RingQueue>(m, "RingQueue")
        .def_static("create", [](uint32_t capacity, uint32_t elem_size) {
            RingQueue* q = ring_queue_create(capacity, elem_size);
            if (!q) throw std::runtime_error("Failed to create ring queue");
            return q;
        }, "capacity"_a, "elem_size"_a)
        .def("push", [](RingQueue& q, py::bytes data) {
            std::string s = data;
            if (s.size() != q.elem_size)
                throw std::runtime_error("Data size mismatch");
            return ring_queue_push(&q, s.data()) == 1;
        }, "data"_a)
        .def("push_batch", [](RingQueue& q, py::bytes data) {
            std::string s = data;
            size_t count = s.size() / q.elem_size;
            return ring_queue_push_batch(&q, s.data(), static_cast<uint32_t>(count));
        }, "data"_a)
        .def("pop", [](RingQueue& q) {
            std::string out(q.elem_size, '\0');
            int ok = ring_queue_pop(&q, &out[0]);
            if (!ok) return py::bytes("");
            return py::bytes(out);
        })
        .def("count", &ring_queue_count)
        .def("capacity", &ring_queue_capacity)
        .def("reset", &ring_queue_reset);
    // ring_queue_destroy 由 Python 析构管理？我们注册删除函数
    // 此处使用 unique_ptr 包装，通过自定义删除器
    m.def("destroy_queue", [](RingQueue* q) { ring_queue_destroy(q); });
}

// ---- flat_msg.h ----
void bind_flat_msg(py::module& m) {
    py::class_<FlatMsg>(m, "FlatMsg")
        .def(py::init([]() {
            FlatMsg msg;
            flat_msg_init(&msg);
            return msg;
        }))
        .def_property_readonly("type", [](const FlatMsg& m) { return m.header.type; })
        .def_property_readonly("seq", [](const FlatMsg& m) { return m.header.seq; })
        .def_property_readonly("timestamp_ms", [](const FlatMsg& m) { return m.header.timestamp_ms; })
        .def("set_header", [](FlatMsg& m, uint8_t type, uint16_t len, uint32_t seq, uint32_t ts) {
            flat_msg_set_header(&m, type, len, seq, ts);
        }, "type"_a, "payload_len"_a, "seq"_a, "timestamp_ms"_a)
        .def("set_crc32", [](FlatMsg& m) { flat_msg_set_crc32(&m); })
        .def("validate_crc32", [](FlatMsg& m) { return flat_msg_validate_crc32(&m) == 1; })
        .def("build_tick", [](FlatMsg& m, const std::string& sym,
                              double last, double bid, double ask,
                              double vol, uint64_t exch_ts,
                              uint32_t seq, uint32_t ts) {
            flat_msg_build_tick(&m, sym.c_str(), last, bid, ask, vol, exch_ts, seq, ts);
        })
        .def("build_order_req", [](FlatMsg& m, const std::string& sym,
                                   uint8_t side, uint8_t otype,
                                   double price, double qty,
                                   uint64_t client_id, uint32_t seq, uint32_t ts) {
            flat_msg_build_order_req(&m, sym.c_str(), side, otype, price, qty, client_id, seq, ts);
        })
        .def("payload", [](FlatMsg& m) {
            return py::bytes(reinterpret_cast<const char*>(m.payload), m.header.payload_len);
        })
        .def("set_payload", [](FlatMsg& m, py::bytes data) {
            std::string s = data;
            size_t len = std::min(s.size(), (size_t)FLAT_MSG_PAYLOAD_SIZE);
            memcpy(m.payload, s.data(), len);
            m.header.payload_len = static_cast<uint16_t>(len);
        });
}

// ---- incremental_covar.cpp ----
void bind_incremental_covar(py::module& m) {
    using namespace fire_seed::risk;
    py::class_<IncrementalCovariance>(m, "IncrementalCovariance")
        .def(py::init<int, double>(), "dim"_a, "lambda"_a = 0.94)
        .def("update", [](IncrementalCovariance& cov, const std::vector<double>& x) {
            cov.update(x);
        }, "x"_a)
        .def("get_mean", &IncrementalCovariance::get_mean)
        .def("get_cov", &IncrementalCovariance::get_cov)
        .def("get_var", &IncrementalCovariance::get_var)
        .def("get_shrunk_cov", &IncrementalCovariance::get_shrunk_cov, "delta"_a = -1.0)
        .def("set_shrinkage_delta", &IncrementalCovariance::set_shrinkage_delta)
        .def("reset", &IncrementalCovariance::reset)
        .def_property_readonly("dim", &IncrementalCovariance::get_dim)
        .def_property_readonly("lambda", &IncrementalCovariance::get_lambda);
}

// ---- evt_var.cpp ----
void bind_evt_var(py::module& m) {
    using namespace fire_seed::risk;
    py::class_<EvtRiskEstimator>(m, "EvtRiskEstimator")
        .def(py::init<>())
        .def("fit", [](EvtRiskEstimator& est, const std::vector<double>& returns, double threshold) {
            return est.fit(returns, threshold);
        }, "returns"_a, "threshold"_a = 0.0)
        .def("calculate_var", &EvtRiskEstimator::calculate_var, "confidence"_a = 0.99)
        .def("calculate_cvar", &EvtRiskEstimator::calculate_cvar, "confidence"_a = 0.99)
        .def("reset", &EvtRiskEstimator::reset);
}

// ---- jump_detector.cpp ----
void bind_jump_detector(py::module& m) {
    using namespace fire_seed::risk;
    py::class_<JumpDetector>(m, "JumpDetector")
        .def(py::init<int, double, int>(), "window"_a = 16, "k"_a = 4.0, "min_sample"_a = 12)
        .def("update", &JumpDetector::update)
        .def("detect", &JumpDetector::detect)
        .def("get_last_statistic", &JumpDetector::get_last_statistic)
        .def("is_jump", &JumpDetector::is_jump)
        .def("reset", &JumpDetector::reset)
        .def("set_threshold", &JumpDetector::set_threshold)
        .def("get_threshold", &JumpDetector::get_threshold);
}

// ---- twap_adaptive.cpp ----
void bind_twap(py::module& m) {
    using namespace fire_seed::execution;
    py::class_<AdaptiveTwap>(m, "AdaptiveTwap")
        .def(py::init<double, double, std::vector<double>, double>(),
             "total_qty"_a, "total_duration_sec"_a, "volume_profile"_a, "urgency"_a = 1.0)
        .def("next_slice", &AdaptiveTwap::next_slice)
        .def("confirm_fill", &AdaptiveTwap::confirm_fill)
        .def("is_finished", &AdaptiveTwap::is_finished)
        .def("slice_count", &AdaptiveTwap::slice_count)
        .def("current_slice", &AdaptiveTwap::current_slice)
        .def("remaining_quantity", &AdaptiveTwap::remaining_quantity)
        .def("reset", &AdaptiveTwap::reset)
        .def("set_urgency", &AdaptiveTwap::set_urgency);
}

// ---- iceberg_slicer.cpp ----
void bind_iceberg(py::module& m) {
    using namespace fire_seed::execution;
    py::class_<IcebergSlicer>(m, "IcebergSlicer")
        .def(py::init<double, double, double, uint64_t, bool>(),
             "total_qty"_a, "min_show"_a, "max_show"_a,
             "seed"_a = 0, "use_lognormal"_a = false)
        .def("next_slice", &IcebergSlicer::next_slice)
        .def("is_finished", &IcebergSlicer::is_finished)
        .def("remaining_quantity", &IcebergSlicer::remaining_quantity)
        .def("get_history", &IcebergSlicer::get_history)
        .def("slice_count", &IcebergSlicer::slice_count)
        .def("reset", &IcebergSlicer::reset)
        .def("set_show_range", &IcebergSlicer::set_show_range)
        .def("set_lognormal", &IcebergSlicer::set_lognormal);
}

// ---- ftrl_learner.cpp ----
void bind_ftrl(py::module& m) {
    using namespace fire_seed::learning;
    py::class_<FtrlLearner>(m, "FtrlLearner")
        .def(py::init<int, double, double, double, double>(),
             "dim"_a, "alpha"_a = 0.1, "beta"_a = 1.0,
             "lambda1"_a = 1.0, "lambda2"_a = 1.0)
        .def("predict", &FtrlLearner::predict)
        .def("update", &FtrlLearner::update)
        .def("get_weights", &FtrlLearner::get_weights)
        .def("reset", &FtrlLearner::reset)
        .def("set_weight", &FtrlLearner::set_weight)
        .def_property_readonly("dim", &FtrlLearner::get_dim)
        .def("set_alpha", &FtrlLearner::set_alpha)
        .def("set_beta", &FtrlLearner::set_beta)
        .def("set_lambda1", &FtrlLearner::set_lambda1)
        .def("set_lambda2", &FtrlLearner::set_lambda2);
}

// ---- vib_inference.cpp ----
void bind_vib(py::module& m) {
    using namespace fire_seed::perception;
    py::class_<VIBInference>(m, "VIBInference")
        .def(py::init<const std::string&, size_t, int>(),
             "model_path"_a, "input_dim"_a, "intra_threads"_a = 2)
        .def("run", &VIBInference::run)
        .def_property_readonly("input_dim", &VIBInference::input_dim);
}

// ---- vector_math.cpp ----
void bind_vector_math(py::module& m) {
    using namespace fire_seed::math;
    m.def("has_avx2", &has_avx2);
    m.def("vector_add", [](py::array_t<double> a, py::array_t<double> b) {
        auto buf_a = a.request(), buf_b = b.request();
        if (buf_a.size != buf_b.size) throw std::runtime_error("Size mismatch");
        size_t n = buf_a.size;
        auto result = py::array_t<double>(n);
        auto buf_r = result.request();
        vector_add(static_cast<double*>(buf_a.ptr), static_cast<double*>(buf_b.ptr),
                   static_cast<double*>(buf_r.ptr), n);
        return result;
    });
    m.def("vector_sub", [](py::array_t<double> a, py::array_t<double> b) {
        auto buf_a = a.request(), buf_b = b.request();
        size_t n = buf_a.size;
        auto result = py::array_t<double>(n);
        auto buf_r = result.request();
        vector_sub(static_cast<double*>(buf_a.ptr), static_cast<double*>(buf_b.ptr),
                   static_cast<double*>(buf_r.ptr), n);
        return result;
    });
    m.def("dot_product", [](py::array_t<double> a, py::array_t<double> b) {
        auto buf_a = a.request(), buf_b = b.request();
        return dot_product(static_cast<double*>(buf_a.ptr), static_cast<double*>(buf_b.ptr),
                           buf_a.size);
    });
    m.def("ema", [](py::array_t<double> src, double alpha) {
        auto buf = src.request();
        size_t n = buf.size;
        auto result = py::array_t<double>(n);
        auto buf_r = result.request();
        ema(static_cast<double*>(buf.ptr), n, alpha, static_cast<double*>(buf_r.ptr));
        return result;
    });
    m.def("atr", [](py::array_t<double> high, py::array_t<double> low,
                    py::array_t<double> close, size_t period, bool use_ema) {
        auto buf_h = high.request(), buf_l = low.request(), buf_c = close.request();
        size_t n = buf_h.size;
        auto result = py::array_t<double>(n);
        auto buf_r = result.request();
        average_true_range(static_cast<double*>(buf_h.ptr), static_cast<double*>(buf_l.ptr),
                           static_cast<double*>(buf_c.ptr), n, period,
                           static_cast<double*>(buf_r.ptr), use_ema ? 1 : 0);
        return result;
    });
    m.def("true_range", [](py::array_t<double> high, py::array_t<double> low,
                           py::array_t<double> close) {
        auto buf_h = high.request(), buf_l = low.request(), buf_c = close.request();
        size_t n = buf_h.size;
        auto result = py::array_t<double>(n);
        auto buf_r = result.request();
        true_range(static_cast<double*>(buf_h.ptr), static_cast<double*>(buf_l.ptr),
                   static_cast<double*>(buf_c.ptr), n, static_cast<double*>(buf_r.ptr));
        return result;
    });
    m.def("vector_scale", [](py::array_t<double> src, double factor) {
        auto buf = src.request();
        size_t n = buf.size;
        auto result = py::array_t<double>(n);
        auto buf_r = result.request();
        vector_scale(static_cast<double*>(buf.ptr), factor, static_cast<double*>(buf_r.ptr), n);
        return result;
    });
}

// ---- 网络模块 (占位) ----
// void bind_tcp_stack(py::module& m) { ... }
// void bind_af_xdp(py::module& m) { ... }

// ======================== 模块定义 ========================
PYBIND11_MODULE(fire_seed_cpp, m) {
    m.doc() = "火种系统 C++ 高性能计算模块";

    // 内存与消息
    bind_ring_queue(m);
    bind_flat_msg(m);

    // 风险
    bind_incremental_covar(m);
    bind_evt_var(m);
    bind_jump_detector(m);

    // 执行
    bind_twap(m);
    bind_iceberg(m);

    // 学习与推理
    bind_ftrl(m);
    bind_vib(m);

    // 数学
    bind_vector_math(m);

    // 网络 (若实现)
    // bind_tcp_stack(m);
    // bind_af_xdp(m);

    // 版本信息
    m.attr("__version__") = "3.0.0-spartan";
}
