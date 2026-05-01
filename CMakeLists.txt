cmake_minimum_required(VERSION 3.14)
project(FireSeedCpp VERSION 3.0.0 LANGUAGES CXX)

# ###############################
# C++ 标准与编译选项
# ###############################
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# 默认构建类型为 Release
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# 基础优化选项
if(CMAKE_BUILD_TYPE STREQUAL "Release")
    add_compile_options(-O3 -DNDEBUG -march=native -mtune=native)
    # 启用链接时优化 (IPO)
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
find_package(Threads REQUIRED)
find_library(MATH_LIB m)
if(NOT MATH_LIB)
    set(MATH_LIB "")
endif()

# ###############################
# 可选依赖与功能开关
# ###############################
option(USE_AF_XDP "Enable AF_XDP acceleration (requires libxdp)" OFF)
option(USE_ONNX_RUNTIME "Enable ONNX Runtime inline inference" ON)
option(USE_LARGE_PAGES "Link with libhugetlbfs for huge page support" OFF)

# ONNX Runtime (默认开启，若未找到则警告并禁用)
set(ONNX_RUNTIME_LIBS "")
if(USE_ONNX_RUNTIME)
    find_path(ONNX_RUNTIME_INCLUDE_DIR onnxruntime_cxx_api.h)
    find_library(ONNX_RUNTIME_LIBRARY onnxruntime)
    if(ONNX_RUNTIME_INCLUDE_DIR AND ONNX_RUNTIME_LIBRARY)
        message(STATUS "Found ONNX Runtime: ${ONNX_RUNTIME_LIBRARY}")
        set(ONNX_RUNTIME_LIBS ${ONNX_RUNTIME_LIBRARY})
        include_directories(${ONNX_RUNTIME_INCLUDE_DIR})
        add_definitions(-DFIRESEED_USE_ONNX)
    else()
        message(WARNING "ONNX Runtime not found, VIB inference will be disabled")
        set(USE_ONNX_RUNTIME OFF)
    endif()
endif()

# AF_XDP (可选，默认关闭)
set(XDP_LIBS "")
if(USE_AF_XDP)
    find_library(XDP_LIB xdp)
    find_library(BPF_LIB bpf)
    if(XDP_LIB AND BPF_LIB)
        set(XDP_LIBS ${XDP_LIB} ${BPF_LIB})
        add_definitions(-DFIRESEED_USE_AF_XDP)
        message(STATUS "AF_XDP enabled")
    else()
        message(WARNING "AF_XDP requested but required libraries not found, disabled")
        set(USE_AF_XDP OFF)
    endif()
endif()

# 大页内存支持 (可选)
if(USE_LARGE_PAGES)
    find_library(HUGETLBFS_LIB hugetlbfs)
    if(HUGETLBFS_LIB)
        set(HUGETLBFS_LIBS ${HUGETLBFS_LIB})
        add_definitions(-DUSE_HUGETLBFS)
        message(STATUS "Huge page support enabled")
    else()
        message(WARNING "libhugetlbfs not found")
    endif()
endif()

# ###############################
# 头文件包含目录
# ###############################
include_directories(${CMAKE_CURRENT_SOURCE_DIR})

# ###############################
# 源文件列表
# ###############################
# 主要绑定文件：将所有 C++ 模块直接包含进此翻译单元，避免重复编译
# 模块的 .cpp 文件由 bindings.cpp 内部 #include，因此只需编译 bindings.cpp
set(CORE_SOURCES
    cpp/bindings.cpp
)

# 若启用 AF_XDP，需要额外编译独立的网络接收器（不被 bindings 包含）
if(USE_AF_XDP)
    list(APPEND CORE_SOURCES
        cpp/net/af_xdp_recv.cpp
    )
endif()

# TCP 用户态栈存根根据需要添加（目前仅头文件，若需实现可加入）
# list(APPEND CORE_SOURCES cpp/net/tcp_userstack.cpp)

# ###############################
# 构建目标：Python 扩展模块
# ###############################
pybind11_add_module(fire_seed_cpp MODULE ${CORE_SOURCES})

# 链接必需的库
target_link_libraries(fire_seed_cpp PRIVATE
    Threads::Threads
    ${MATH_LIB}
    ${ONNX_RUNTIME_LIBS}
    ${XDP_LIBS}
    ${HUGETLBFS_LIBS}
)

# 为 SIMD 模块（包含在 bindings.cpp 中）添加编译器标志
target_compile_options(fire_seed_cpp PRIVATE -mfma -mavx2)

# 设置 Python 扩展模块后缀
set_target_properties(fire_seed_cpp PROPERTIES
    SUFFIX ".so"
    POSITION_INDEPENDENT_CODE ON
)

# ###############################
# 安装规则
# ###############################
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
