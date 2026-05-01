/**
 * 火种系统 (FireSeed) 共享内存管理器 (头文件)
 * ==============================================
 * 负责 POSIX 共享内存的创建、映射、大页分配以及生命周期控制。
 * 提供 C++ 接口和 C 兼容接口，便于 Python / C++ 模块间零拷贝通信。
 *
 * 依赖：
 * - Linux 内核 >= 2.6.32 (POSIX shm)
 * - libc (shm_open, mmap, ftruncate 等)
 *
 * 使用示例：
 *   ShmManager mgr;
 *   mgr.create("fire_seed_queue", 1 << 20);   // 1MB 共享内存
 *   void* buf = mgr.data();
 *   ...
 *   mgr.close();  // 或析构自动关闭
 */

#ifndef FIRESEED_SHM_MANAGER_H
#define FIRESEED_SHM_MANAGER_H

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>

namespace fire_seed {
namespace memory {

class ShmManager {
public:
    ShmManager() = default;
    ~ShmManager() { close(); }

    // 禁止拷贝，允许移动
    ShmManager(const ShmManager&) = delete;
    ShmManager& operator=(const ShmManager&) = delete;

    /**
     * 创建或打开一个共享内存段。
     * @param name       共享内存名称（如 "/fire_seed_shm"）
     * @param size       所需大小（字节），若为0则打开已存在的段
     * @param use_hugepages 是否尝试使用大页（2MB/1GB），需系统支持
     * @throws std::runtime_error 操作失败时抛出
     */
    void create(const std::string& name, size_t size, bool use_hugepages = false);

    /**
     * 附加到已存在的共享内存段（只读映射）。
     * @param name 共享内存名称
     */
    void attach(const std::string& name);

    /**
     * 获取映射的起始地址。
     * @return 指向共享内存的指针，未映射时返回 nullptr
     */
    void* data() const { return data_; }

    /**
     * 获取已映射内存的总大小（字节）。
     */
    size_t size() const { return size_; }

    /**
     * 获取关联的 POSIX 文件描述符（shm_fd）。
     */
    int fd() const { return fd_; }

    /**
     * 解除映射并关闭描述符，但不删除共享内存对象（供其他进程使用）。
     */
    void close();

    /**
     * 彻底销毁共享内存段（解除映射并标记删除）。
     */
    void destroy();

    /**
     * 向共享内存写入递增租约时间戳，用于检测对端是否存活。
     * 需预留前 8 字节作为心跳字段。
     */
    void renew_lease();

    /**
     * 检查租约是否仍在有效期内。
     * @param timeout_ms 允许的最大静默时间（毫秒）
     * @return true 表示对端仍在心跳有效期内
     */
    bool is_lease_valid(uint64_t timeout_ms) const;

    /**
     * 检测系统是否支持大页（/sys/kernel/mm/hugepages 挂载）。
     */
    static bool hugepages_available();

private:
    int     fd_   = -1;
    void*   data_ = nullptr;
    size_t  size_ = 0;
    std::string name_;
    bool    created_ = false;

    void unmap();
    void unlink();
};

} // namespace memory
} // namespace fire_seed

// ------------------------------------------------------------
// C 接口 (用于 pybind11 或 ctypes 绑定)
// ------------------------------------------------------------
extern "C" {
    using ShmMgr = fire_seed::memory::ShmManager;

    ShmMgr* shm_manager_create();
    void    shm_manager_destroy(ShmMgr* mgr);
    int     shm_manager_create_or_open(ShmMgr* mgr, const char* name, size_t size, int use_hugepages);
    int     shm_manager_attach(ShmMgr* mgr, const char* name);
    void*   shm_manager_data(ShmMgr* mgr);
    size_t  shm_manager_size(ShmMgr* mgr);
    void    shm_manager_close(ShmMgr* mgr);
    void    shm_manager_destroy_segment(ShmMgr* mgr);
    void    shm_manager_renew_lease(ShmMgr* mgr);
    int     shm_manager_is_lease_valid(ShmMgr* mgr, uint64_t timeout_ms);
    int     shm_manager_hugepages_available();
}

#endif // FIRESEED_SHM_MANAGER_H
