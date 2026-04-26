cmake_minimum_required(VERSION 3.18)
project(FireSeedCore LANGUAGES CXX C)

# --- C++ 标准与编译选项 -------------------------------------------------------
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# 生产级优化标志
if(CMAKE_BUILD_TYPE STREQUAL "Release")
    add_compile_options(-O3 -DNDEBUG -march=native -mtune=native -funroll-loops)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU" OR CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
        add_compile_options(-ffast-math -fomit-frame-pointer)
    endif()
endif()

# SIMD 支持 (AVX2)
include(CheckCXXCompilerFlag)
check_cxx_compiler_flag("-mavx2" COMPILER_SUPPORTS_AVX2)
if(COMPILER_SUPPORTS_AVX2)
    add_compile_options(-mavx2)
endif()

# --- 查找依赖 -----------------------------------------------------------------
# pybind11
find_package(pybind11 2.10 QUIET)
if(NOT pybind11_FOUND)
    include(FetchContent)
    FetchContent_Declare(
        pybind11
        GIT_REPOSITORY https://github.com/pybind/pybind11.git
        GIT_TAG v2.11.1
    )
    FetchContent_MakeAvailable(pybind11)
endif()

# OpenMP (可选，用于加速某些计算)
find_package(OpenMP)
if(OpenMP_CXX_FOUND)
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} ${OpenMP_CXX_FLAGS}")
    set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} ${OpenMP_CXX_FLAGS}")
endif()

# --- 公用头文件目录 -------------------------------------------------------------
set(PUBLIC_INCLUDES
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/shared_memory
    ${CMAKE_CURRENT_SOURCE_DIR}/risk
    ${CMAKE_CURRENT_SOURCE_DIR}/execution
    ${CMAKE_CURRENT_SOURCE_DIR}/learning
    ${CMAKE_CURRENT_SOURCE_DIR}/monitoring
)

# --- 各子模块的源码组织 ---------------------------------------------------------

# 共享内存与内存池
add_library(shm_utils STATIC
    shared_memory/shm_queue.cpp
    shared_memory/shm_manager.cpp
    memory_pool/slab_allocator.cpp
    utils/cache_aligned.cpp
)
target_include_directories(shm_utils PUBLIC ${PUBLIC_INCLUDES})
target_link_libraries(shm_utils PUBLIC pthread rt)

# 风险计算核心
add_library(risk_engine STATIC
    risk/incremental_covar.cpp
    risk/evt_var.cpp
    risk/realtime_guard.cpp
    risk/jump_detector.cpp
    risk/monte_carlo.cpp
)
target_include_directories(risk_engine PUBLIC ${PUBLIC_INCLUDES})
target_link_libraries(risk_engine PUBLIC shm_utils)
if(OpenMP_CXX_FOUND)
    target_link_libraries(risk_engine PUBLIC OpenMP::OpenMP_CXX)
endif()

# 执行算法
add_library(execution_engine STATIC
    execution/twap_adaptive.cpp
    execution/sor_dynamic.cpp
    execution/iceberg_slicer.cpp
)
target_include_directories(execution_engine PUBLIC ${PUBLIC_INCLUDES})
target_link_libraries(execution_engine PUBLIC shm_utils)

# 在线学习
add_library(learning_engine STATIC
    learning/ftrl_learner.cpp
    learning/meta_learner.cpp
    learning/stacking_ensemble.cpp
)
target_include_directories(learning_engine PUBLIC ${PUBLIC_INCLUDES})
if(OpenMP_CXX_FOUND)
    target_link_libraries(learning_engine PUBLIC OpenMP::OpenMP_CXX)
endif()

# 硬件监视器
add_library(hard_watcher STATIC
    monitoring/hard_watcher.cpp
)
target_include_directories(hard_watcher PUBLIC ${PUBLIC_INCLUDES})
target_link_libraries(hard_watcher PUBLIC shm_utils)

# 序列化与工具
add_library(core_utils STATIC
    utils/msgpack_serializer.cpp
    utils/async_logger.cpp
)
target_include_directories(core_utils PUBLIC ${PUBLIC_INCLUDES})
target_link_libraries(core_utils PUBLIC pthread)

# --- Python 绑定模块 (pybind11) ------------------------------------------------
pybind11_add_module(fire_seed_core
    bindings.cpp
)
target_include_directories(fire_seed_core PRIVATE ${PUBLIC_INCLUDES})
target_link_libraries(fire_seed_core PRIVATE
    shm_utils
    risk_engine
    execution_engine
    learning_engine
    hard_watcher
    core_utils
)

# 安装目标 (可选)
install(TARGETS fire_seed_core DESTINATION lib/python/fire_seed)

# --- 测试 (可选) ---------------------------------------------------------------
option(BUILD_TESTS "构建单元测试" OFF)
if(BUILD_TESTS)
    enable_testing()
    add_subdirectory(tests/cpp)
endif()

# 打印配置信息
message(STATUS "FireSeed Core Build Configuration:")
message(STATUS "  C++ Standard: C++20")
message(STATUS "  Build Type: ${CMAKE_BUILD_TYPE}")
message(STATUS "  AVX2 Support: ${COMPILER_SUPPORTS_AVX2}")
message(STATUS "  OpenMP: ${OpenMP_CXX_FOUND}")
message(STATUS "  pybind11: ${pybind11_FOUND}")
