cmake_minimum_required(VERSION 3.14)
project(FireSeedCpp VERSION 3.0.0 LANGUAGES CXX)

# ###############################
# C++ 标准与编译选项
# ###############################
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# 全局编译优化
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

if(CMAKE_BUILD_TYPE STREQUAL "Release")
    add_compile_options(-O3 -DNDEBUG -march=native -mtune=native)
    # 启用链接时优化
    if(CMAKE_VERSION VERSION_GREATER_EQUAL "3.9")
        include(CheckIPOSupported)
        check_ipo_supported(RESULT ipo_supported OUTPUT ipo_output)
        if(ipo_supported)
            set(CMAKE_INTERPROCEDURAL_OPTIMIZATION TRUE)
        endif()
    endif()
endif()

# 严格警告（生产代码推荐）
add_compile_options(-Wall -Wextra -Wpedantic -Wno-unused-parameter)

# ###############################
# 查找必需依赖
# ###############################
find_package(pybind11 REQUIRED)

# 线程库（某些C++运行时需要显式链接）
find_package(Threads REQUIRED)

# 数学库
find_library(MATH_LIB m)
if(NOT MATH_LIB)
    set(MATH_LIB "")
endif()

# ###############################
# 可选依赖与功能开关
# ###############################
option(USE_AF_XDP "Enable AF_XDP acceleration (requires libxdp)" OFF)
option(USE_ONNX_RUNTIME "Enable ONNX Runtime inline inference" OFF)

# ###############################
# 头文件包含路径
# ###############################
set(CPP_INCLUDE_DIR ${CMAKE_CURRENT_SOURCE_DIR})
include_directories(${CPP_INCLUDE_DIR})

# ###############################
# 源文件收集
# ###############################
# 核心公共源文件
set(CORE_SOURCES
    ${CPP_INCLUDE_DIR}/cpp/bindings.cpp
)

# 通用工具与数据结构
set(UTIL_HEADERS
    ${CPP_INCLUDE_DIR}/cpp/shared_memory/shm_queue.h
    ${CPP_INCLUDE_DIR}/cpp/shared_memory/flat_msg.h
    ${CPP_INCLUDE_DIR}/cpp/pool/slab_allocator.h
)

# 风险模块
set(RISK_SOURCES
    ${CPP_INCLUDE_DIR}/cpp/risk/incremental_covar.cpp
    ${CPP_INCLUDE_DIR}/cpp/risk/evt_var.cpp
    ${CPP_INCLUDE_DIR}/cpp/risk/jump_detector.cpp
)

# 执行模块
set(EXEC_SOURCES
    ${CPP_INCLUDE_DIR}/cpp/execution/twap_adaptive.cpp
    ${CPP_INCLUDE_DIR}/cpp/execution/iceberg_slicer.cpp
)

# 学习模块
set(LEARNING_SOURCES
    ${CPP_INCLUDE_DIR}/cpp/learning/ftrl_learner.cpp
)

# SIMD 优化模块
set(SIMD_SOURCES
    ${CPP_INCLUDE_DIR}/cpp/simd/vector_math.cpp
)

# 监控模块（硬监视器）
set(MONITOR_SOURCES
    ${CPP_INCLUDE_DIR}/cpp/monitoring/hard_watcher.cpp
)

# 网络模块（根据选项启用）
set(NET_SOURCES "")
if(USE_AF_XDP)
    find_library(XDP_LIB xdp)
    find_library(BPF_LIB bpf)
    if(XDP_LIB AND BPF_LIB)
        list(APPEND NET_SOURCES
            ${CPP_INCLUDE_DIR}/cpp/net/af_xdp_recv.cpp
        )
        add_definitions(-DFIRESEED_USE_AF_XDP)
        message(STATUS "AF_XDP enabled")
    else()
        message(WARNING "AF_XDP requested but required libraries not found, disabled")
    endif()
endif()
# 用户态TCP栈始终编译（无额外依赖）
list(APPEND NET_SOURCES ${CPP_INCLUDE_DIR}/cpp/net/tcp_userstack.cpp)

# ONNX Runtime 推理模块（可选）
set(ONNX_SOURCES "")
if(USE_ONNX_RUNTIME)
    find_package(onnxruntime QUIET)
    if(onnxruntime_FOUND)
        list(APPEND ONNX_SOURCES ${CPP_INCLUDE_DIR}/cpp/learning/vib_inference.cpp)
        add_definitions(-DFIRESEED_USE_ONNX)
        message(STATUS "ONNX Runtime enabled for C++ inference")
    else()
        message(WARNING "ONNX Runtime not found, C++ inference disabled")
    endif()
endif()

# 汇总所有源文件
set(ALL_SOURCES
    ${CORE_SOURCES}
    ${RISK_SOURCES}
    ${EXEC_SOURCES}
    ${LEARNING_SOURCES}
    ${SIMD_SOURCES}
    ${MONITOR_SOURCES}
    ${NET_SOURCES}
    ${ONNX_SOURCES}
)

# ###############################
# 构建目标：Python 扩展模块
# ###############################
pybind11_add_module(fire_seed_cpp MODULE ${ALL_SOURCES})

# 链接必要的系统库
target_link_libraries(fire_seed_cpp PRIVATE
    Threads::Threads
    ${MATH_LIB}
)

# 如果使用了 AF_XDP，链接相关库
if(USE_AF_XDP AND XDP_LIB AND BPF_LIB)
    target_link_libraries(fire_seed_cpp PRIVATE ${XDP_LIB} ${BPF_LIB})
endif()

# 如果使用了 ONNX Runtime，链接其库
if(USE_ONNX_RUNTIME AND onnxruntime_FOUND)
    target_link_libraries(fire_seed_cpp PRIVATE onnxruntime::onnxruntime)
endif()

# 为 SIMD 模块添加编译器标志
target_compile_options(fire_seed_cpp PRIVATE -mfma -mavx2)

# 设置 Python 扩展模块后缀（确保可被 Python 导入）
set_target_properties(fire_seed_cpp PROPERTIES
    SUFFIX ".so"
    POSITION_INDEPENDENT_CODE ON
)

# ###############################
# 安装规则
# ###############################
# 将编译产物安装到 Python 项目根目录（与 main.py 同层）
install(TARGETS fire_seed_cpp
    LIBRARY DESTINATION ${CMAKE_SOURCE_DIR}
    ARCHIVE DESTINATION ${CMAKE_SOURCE_DIR}
)

# ###############################
# 测试（可选）
# ###############################
option(BUILD_TESTS "Build C++ unit tests" OFF)
if(BUILD_TESTS)
    enable_testing()
    find_package(GTest REQUIRED)
    add_subdirectory(tests)
endif()

message(STATUS "FireSeed C++ module configured successfully")
