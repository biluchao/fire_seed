/**
 * 火种系统 (FireSeed) 共享内存管理器 (实现)
 * ==============================================
 */

#include "shm_manager.h"

#include <cstring>
#include <cerrno>
#include <chrono>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <stdexcept>
#include <string>
#include <fstream>
#include <cstdio>

namespace fire_seed {
namespace memory {

// ---------- 内部辅助函数 ----------
static void throw_system_error(const std::string& prefix) {
    throw std::runtime_error(prefix + ": " + std::strerror(errno));
}

// ---------- 公共实现 ----------

void ShmManager::create(const std::string& name, size_t size, bool use_hugepages) {
    if (name.empty()) throw std::runtime_error("共享内存名称不能为空");
    if (size == 0) throw std::runtime_error("共享内存大小必须大于0");

    // 如果已打开，先清理
    close();

    // 确定打开标志
    int flags = O_RDWR | O_CREAT;
    if (use_hugepages) {
        // 使用大页，需要通过 hugetlbfs 挂载点，但 shm_open 通常位于 /dev/shm，
        // 不支持大页。实际应改用 open + mmap 方式，此处暂不支持 shm_open 大页。
        // 仅作标记，并回退普通分配
        use_hugepages = false;
    }

    fd_ = shm_open(name.c_str(), flags, 0666);
    if (fd_ < 0) {
        throw_system_error("shm_open 失败");
    }

    if (ftruncate(fd_, static_cast<off_t>(size)) != 0) {
        int saved_errno = errno;
        close();
        errno = saved_errno;
        throw_system_error("ftruncate 设置共享内存大小失败");
    }

    void* addr = mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd_, 0);
    if (addr == MAP_FAILED) {
        int saved_errno = errno;
        close();
        errno = saved_errno;
        throw_system_error("mmap 映射共享内存失败");
    }

    data_    = addr;
    size_    = size;
    name_    = name;
    created_ = true;

    // 清零内存（默认行为）
    std::memset(data_, 0, size_);
}

void ShmManager::attach(const std::string& name) {
    if (name.empty()) throw std::runtime_error("共享内存名称不能为空");

    close();

    fd_ = shm_open(name.c_str(), O_RDONLY, 0666);
    if (fd_ < 0) {
        throw_system_error("shm_open 打开已存在的共享内存失败");
    }

    struct stat sb;
    if (fstat(fd_, &sb) != 0) {
        int saved_errno = errno;
        close();
        errno = saved_errno;
        throw_system_error("无法获取共享内存大小");
    }
    size_t sz = sb.st_size;

    void* addr = mmap(nullptr, sz, PROT_READ, MAP_SHARED, fd_, 0);
    if (addr == MAP_FAILED) {
        int saved_errno = errno;
        close();
        errno = saved_errno;
        throw_system_error("mmap 只读映射共享内存失败");
    }

    data_ = addr;
    size_ = sz;
    name_ = name;
}

void ShmManager::close() {
    if (data_) {
        munmap(data_, size_);
        data_ = nullptr;
    }
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
    size_ = 0;
    name_.clear();
    created_ = false;
}

void ShmManager::destroy() {
    if (!name_.empty()) {
        shm_unlink(name_.c_str());
    }
    close();
}

void ShmManager::renew_lease() {
    if (!data_ || size_ < 8) return;
    uint64_t now = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
    // 安全写入（原子性简单保证）
    uint64_t* lease_ptr = static_cast<uint64_t*>(data_);
    *lease_ptr = now;
}

bool ShmManager::is_lease_valid(uint64_t timeout_ms) const {
    if (!data_ || size_ < 8) return false;
    const uint64_t* lease_ptr = static_cast<const uint64_t*>(data_);
    uint64_t lease_time = *lease_ptr;
    if (lease_time == 0) return false;  // 未初始化
    uint64_t now = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
    return (now - lease_time) <= timeout_ms;
}

bool ShmManager::hugepages_available() {
    // 尝试读取 /sys/kernel/mm/hugepages 下的目录判断
    std::ifstream f("/proc/meminfo");
    if (!f.is_open()) return false;
    std::string line;
    while (std::getline(f, line)) {
        if (line.find("HugePages_Total") == 0) {
            auto pos = line.find(':');
            if (pos != std::string::npos) {
                std::string val = line.substr(pos+1);
                long total = std::stol(val);
                return total > 0;
            }
        }
    }
    return false;
}

} // namespace memory
} // namespace fire_seed

// ------------------------------------------------------------
// C 接口实现
// ------------------------------------------------------------
extern "C" {

using ShmMgr = fire_seed::memory::ShmManager;

ShmMgr* shm_manager_create() {
    return new ShmMgr();
}

void shm_manager_destroy(ShmMgr* mgr) {
    delete mgr;
}

int shm_manager_create_or_open(ShmMgr* mgr, const char* name, size_t size, int use_hugepages) {
    if (!mgr || !name) return -1;
    try {
        mgr->create(name, size, use_hugepages != 0);
        return 0;
    } catch (...) {
        return -1;
    }
}

int shm_manager_attach(ShmMgr* mgr, const char* name) {
    if (!mgr || !name) return -1;
    try {
        mgr->attach(name);
        return 0;
    } catch (...) {
        return -1;
    }
}

void* shm_manager_data(ShmMgr* mgr) {
    return mgr ? mgr->data() : nullptr;
}

size_t shm_manager_size(ShmMgr* mgr) {
    return mgr ? mgr->size() : 0;
}

void shm_manager_close(ShmMgr* mgr) {
    if (mgr) mgr->close();
}

void shm_manager_destroy_segment(ShmMgr* mgr) {
    if (mgr) mgr->destroy();
}

void shm_manager_renew_lease(ShmMgr* mgr) {
    if (mgr) mgr->renew_lease();
}

int shm_manager_is_lease_valid(ShmMgr* mgr, uint64_t timeout_ms) {
    if (!mgr) return 0;
    return mgr->is_lease_valid(timeout_ms) ? 1 : 0;
}

int shm_manager_hugepages_available() {
    return fire_seed::memory::ShmManager::hugepages_available() ? 1 : 0;
}

} // extern "C"
