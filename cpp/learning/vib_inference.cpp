/**
 * 火种系统 (FireSeed) VIB 模型推理模块 (ONNX Runtime)
 * ========================================================
 * 加载经过 INT8 量化后的 VIB 模型，提供低延迟推理。
 * 输入: 订单簿特征向量 (1 x N)，N 由模型定义。
 * 输出: 5 个市场状态分数 (买压, 卖压, 趋势持续性, 反转风险, 波动率区间)。
 *
 * 依赖:
 *   - onnxruntime (libonnxruntime.so, 头文件 onnxruntime_cxx_api.h)
 *
 * 编译:
 *   g++ -c vib_inference.cpp -o vib_inference.o -std=c++17 -O2 -I/path/to/onnxruntime/include
 */

#include <vector>
#include <string>
#include <cstring>
#include <stdexcept>
#include <memory>
#include <iostream>

#include <onnxruntime_cxx_api.h>

namespace fire_seed {
namespace perception {

// ------------------------------------------------------------
// VIB 推理引擎
// ------------------------------------------------------------
class VIBInference {
public:
    /**
     * 构造函数
     * @param model_path   ONNX 模型文件路径
     * @param input_dim    输入特征维度
     * @param intra_threads ONNX Runtime 内部并行线程数 (建议 ≤ CPU 核心数)
     */
    VIBInference(const std::string& model_path, size_t input_dim,
                 int intra_threads = 2)
        : input_dim_(input_dim)
    {
        if (input_dim_ == 0)
            throw std::invalid_argument("input_dim must be positive");

        // 创建ONNX运行环境
        env_ = Ort::Env(ORT_LOGGING_LEVEL_WARNING, "FireSeedVIB");

        // 会话选项
        Ort::SessionOptions session_options;
        session_options.SetIntraOpNumThreads(intra_threads);
        session_options.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
        // 启用内存模式优化
        session_options.EnableMemPattern();

        try {
            session_ = std::make_unique<Ort::Session>(env_, model_path.c_str(), session_options);
        } catch (const Ort::Exception& e) {
            throw std::runtime_error(std::string("Failed to load VIB model: ") + e.what());
        }

        // 获取输入输出节点名称
        Ort::AllocatorWithDefaultOptions allocator;
        size_t num_input = session_->GetInputCount();
        for (size_t i = 0; i < num_input; ++i) {
            auto name = session_->GetInputName(i, allocator);
            input_names_.push_back(name);
        }
        size_t num_output = session_->GetOutputCount();
        for (size_t i = 0; i < num_output; ++i) {
            auto name = session_->GetOutputName(i, allocator);
            output_names_.push_back(name);
        }

        // 验证输入形状 (假设固定 batch=1)
        Ort::TypeInfo typeInfo = session_->GetInputTypeInfo(0);
        auto tensorInfo = typeInfo.GetTensorTypeAndShapeInfo();
        auto shape = tensorInfo.GetShape();
        if (!shape.empty() && shape[0] == -1) {
            // 动态 batch，允许 1
        } else if (shape.size() >= 2 && shape[1] != static_cast<int64_t>(input_dim_)) {
            // 警告但不致命
            std::cerr << "[VIB] Expected input dim " << input_dim_
                      << " but model expects " << shape[1] << std::endl;
        }

        // 预分配输入输出缓冲区
        input_tensor_values_.resize(input_dim_);
        output_tensor_values_.resize(5); // 固定5维输出

        // 内存信息 (CPU)
        memory_info_ = Ort::MemoryInfo::CreateCpu(OrtArenaAllocator, OrtMemTypeDefault);
    }

    ~VIBInference() = default;

    /**
     * 执行推理。
     * @param features 输入特征向量 (长度 == input_dim_)
     * @return 5 维输出向量: [buy_pressure, sell_pressure, trend_persistence, reversal_risk, volatility_regime]
     */
    std::vector<float> run(const std::vector<float>& features) {
        if (features.size() != input_dim_)
            throw std::invalid_argument("Feature dimension mismatch");

        // 复制输入数据
        std::copy(features.begin(), features.end(), input_tensor_values_.begin());

        // 创建输入张量
        std::vector<int64_t> input_shape = {1, static_cast<int64_t>(input_dim_)};
        Ort::Value input_tensor = Ort::Value::CreateTensor<float>(
            memory_info_, input_tensor_values_.data(), input_tensor_values_.size(),
            input_shape.data(), input_shape.size());

        // 执行推理
        try {
            auto output_tensors = session_->Run(Ort::RunOptions{nullptr},
                                                input_names_.data(), &input_tensor, 1,
                                                output_names_.data(), output_names_.size());
            // 提取输出 (假设第一个输出就是我们需要的 5 维向量)
            float* output_data = output_tensors[0].GetTensorMutableData<float>();
            auto output_shape = output_tensors[0].GetTensorTypeAndShapeInfo().GetShape();
            size_t output_count = output_tensors[0].GetTensorTypeAndShapeInfo().GetElementCount();
            if (output_count >= 5) {
                std::copy(output_data, output_data + 5, output_tensor_values_.begin());
            } else {
                throw std::runtime_error("Unexpected output element count");
            }
        } catch (const Ort::Exception& e) {
            throw std::runtime_error(std::string("VIB inference failed: ") + e.what());
        }

        return output_tensor_values_;
    }

    /**
     * 便捷接口：直接返回结构化状态字典 (通过 C 接口传递)
     */
    void run_to_raw(float* input, float* output) {
        if (!input || !output) return;
        std::vector<float> in(input, input + input_dim_);
        auto out = run(in);
        std::copy(out.begin(), out.end(), output);
    }

    /** 获取输入维度 */
    size_t input_dim() const { return input_dim_; }

private:
    Ort::Env env_{nullptr};
    std::unique_ptr<Ort::Session> session_;
    Ort::MemoryInfo memory_info_{nullptr};

    size_t input_dim_;
    std::vector<const char*> input_names_;
    std::vector<const char*> output_names_;

    std::vector<float> input_tensor_values_;
    std::vector<float> output_tensor_values_;
};

} // namespace perception
} // namespace fire_seed

// ------------------------------------------------------------
// C 接口 (供 pybind11 或 ctypes 调用)
// ------------------------------------------------------------
extern "C" {

using VIB = fire_seed::perception::VIBInference;

VIB* vib_create(const char* model_path, size_t input_dim, int intra_threads) {
    if (!model_path) return nullptr;
    try {
        return new VIB(model_path, input_dim, intra_threads);
    } catch (const std::exception& e) {
        std::cerr << "[VIB] Create failed: " << e.what() << std::endl;
        return nullptr;
    }
}

void vib_destroy(VIB* vib) {
    delete vib;
}

/**
 * 执行推理
 * @param input  输入数组 (长度 = vib->input_dim())
 * @param output 输出数组 (长度至少为 5)
 * @return 0 表示成功，-1 表示失败
 */
int vib_run(VIB* vib, const float* input, float* output) {
    if (!vib || !input || !output) return -1;
    try {
        vib->run_to_raw(const_cast<float*>(input), output);
        return 0;
    } catch (...) {
        return -1;
    }
}

size_t vib_input_dim(VIB* vib) {
    return vib ? vib->input_dim() : 0;
}

} // extern "C"
