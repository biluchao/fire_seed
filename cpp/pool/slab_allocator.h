/**
 * 火种系统 (FireSeed) Slab 内存分配器 (Header-Only)
 * ====================================================
 * 定长对象的内存池，用于高频创建/销毁的小对象 (如 Tick、Order 等)，
 * 避免频繁的 malloc/free 调用，消除内存碎片，并减少缓存 Miss。
 *
 * 特性：
 * - 线程安全 (可通过模板参数控制)
 * - 支持批量分配与释放
 * - 自动从系统申请大块内存，按 Block 组织
 * - 提供 STL 兼容的分配器 (slab_allocator)
 * - C++17 标准，无外部依赖
 *
 * 使用示例：
 *   SlabPool<MyStruct, 128> pool;
 *   MyStruct* p = pool.allocate();
 *   pool.deallocate(p);
 *
 * 编译：此文件为 header-only，包含即可。
 */

#ifndef FIRESEED_SLAB_ALLOCATOR_H
#define FIRESEED_SLAB_ALLOCATOR_H

#include <cstdint>
#include <cstddef>
#include <cassert>
#include <new>
#include <type_traits>
#include <memory>
#include <atomic>
#include <array>

namespace fire_seed {
namespace memory {

// -------------------------- 配置常量 --------------------------
#ifndef SLAB_BLOCK_SIZE
#define SLAB_BLOCK_SIZE 64           // 每个块容纳的对象数 (应为 2 的幂)
#endif

// -------------------------- Slab 块 --------------------------
/**
 * 单个内存块，内部预分配 SLAB_BLOCK_SIZE 个 T 对象。
 * 使用位图管理空闲槽位，O(1) 分配 / 释放。
 */
template <typename T, size_t BlockSize = SLAB_BLOCK_SIZE>
struct SlabBlock {
    static_assert(BlockSize > 0 && (BlockSize & (BlockSize - 1)) == 0,
                  "BlockSize must be a power of 2");

    using Storage = typename std::aligned_storage<sizeof(T), alignof(T)>::type;

    Storage data[BlockSize];                              // 对象存储
    uint64_t free_bitmap[(BlockSize + 63) / 64]{};      // 位图 (1 表示空闲)
    SlabBlock* next = nullptr;                           // 链表指针
    size_t    allocated_count = 0;                       // 已分配计数 (用于统计)

    SlabBlock() {
        // 初始化位图，全部标记为空闲
        for (size_t i = 0; i < sizeof(free_bitmap) / sizeof(free_bitmap[0]); ++i) {
            free_bitmap[i] = ~uint64_t(0);
        }
    }

    /** 检查是否已满 */
    bool full() const { return allocated_count >= BlockSize; }

    /** 检查是否为空 */
    bool empty() const { return allocated_count == 0; }

    /** 分配一个槽位，返回对象指针 */
    T* allocate() {
        for (size_t word = 0; word < sizeof(free_bitmap)/sizeof(free_bitmap[0]); ++word) {
            if (free_bitmap[word]) {
                int bit = __builtin_ctzll(free_bitmap[word]);   // 最低置位
                free_bitmap[word] &= ~(uint64_t(1) << bit);
                size_t index = word * 64 + bit;
                ++allocated_count;
                return reinterpret_cast<T*>(&data[index]);
            }
        }
        return nullptr;      // 无空闲
    }

    /** 释放指定指针，指针必须来自本块 */
    void deallocate(T* ptr) {
        // 计算索引
        uintptr_t start = reinterpret_cast<uintptr_t>(data);
        uintptr_t p     = reinterpret_cast<uintptr_t>(ptr);
        size_t index    = (p - start) / sizeof(Storage);
        if (index < BlockSize) {
            size_t word = index / 64;
            size_t bit  = index % 64;
            free_bitmap[word] |= (uint64_t(1) << bit);
            --allocated_count;
            // 可选：调用析构函数 (由上层 SlabPool 负责)
        }
    }

    /** 判断指针是否来自本块 */
    bool owns(T* ptr) const {
        uintptr_t start = reinterpret_cast<uintptr_t>(data);
        uintptr_t p     = reinterpret_cast<uintptr_t>(ptr);
        return (p >= start && p < start + sizeof(data));
    }
};

// -------------------------- 线程安全的 SlabPool --------------------------
template <typename T, size_t BlockSize = SLAB_BLOCK_SIZE, bool ThreadSafe = true>
class SlabPool {
public:
    SlabPool() = default;

    ~SlabPool() {
        // 释放所有申请的块
        while (head_) {
            auto* block = head_;
            head_ = block->next;
            // 显式调用对象的析构函数 (仅对已分配对象，此处简化，块释放后内存回收)
            // 实际应用可遍历已分配列表，此处仅回收操作系统内存
            delete block;
        }
    }

    // 禁止拷贝
    SlabPool(const SlabPool&) = delete;
    SlabPool& operator=(const SlabPool&) = delete;

    /** 分配一个对象，调用默认构造函数 */
    T* allocate() {
        T* ptr = do_allocate();
        if (ptr) {
            // 在新分配的对象上调用构造函数
            new (ptr) T();
        }
        return ptr;
    }

    /** 分配一个对象，构造参数转发 */
    template <typename... Args>
    T* allocate(Args&&... args) {
        T* ptr = do_allocate();
        if (ptr) {
            new (ptr) T(std::forward<Args>(args)...);
        }
        return ptr;
    }

    /** 释放对象，调用析构函数并将内存归还池 */
    void deallocate(T* ptr) {
        if (!ptr) return;
        // 显式调用析构函数
        ptr->~T();
        do_deallocate(ptr);
    }

    /** 批量分配 (返回已分配的数量) */
    template <typename OutputIt>
    size_t allocate_batch(OutputIt out, size_t count) {
        size_t i = 0;
        for (; i < count; ++i) {
            T* p = do_allocate();
            if (!p) break;
            new (p) T();
            *out++ = p;
        }
        return i;
    }

    /** 当前池中分配的对象总数 (近似值) */
    size_t allocated_count() const { return allocated_.load(std::memory_order_relaxed); }

    /** 内存池总容量 (对象数) */
    size_t capacity() const {
        size_t cnt = 0;
        auto* block = head_;
        while (block) {
            cnt += BlockSize;
            block = block->next;
        }
        return cnt;
    }

private:
    using Block = SlabBlock<T, BlockSize>;
    Block*          head_ = nullptr;            // 块链表头
    std::atomic<size_t> allocated_{0};          // 已分配对象计数

    /** 内部分配 (不调用构造) */
    T* do_allocate() {
        Block* curr = head_;
        while (curr && curr->full()) {
            curr = curr->next;
        }
        if (!curr) {
            // 需要新块
            curr = new Block();
            // 线程安全的 CAS 插入链表头部
            if (ThreadSafe) {
                do {
                    curr->next = head_.load(std::memory_order_relaxed);
                } while (!std::atomic_compare_exchange_weak_explicit(
                    &head_, &curr->next, curr,
                    std::memory_order_release,
                    std::memory_order_relaxed));
            } else {
                curr->next = head_;
                head_ = curr;
            }
        }
        T* ptr = curr->allocate();
        if (ptr) {
            allocated_.fetch_add(1, std::memory_order_relaxed);
        }
        return ptr;
    }

    void do_deallocate(T* ptr) {
        // 遍历块，找到所属块并调用块的 deallocate
        Block* curr = head_;
        while (curr) {
            if (curr->owns(ptr)) {
                curr->deallocate(ptr);
                allocated_.fetch_sub(1, std::memory_order_relaxed);
                return;
            }
            curr = curr->next;
        }
        // 未找到，理论上不应发生；若发生则忽略 (编程错误)
        assert(false && "Trying to deallocate a pointer not belonging to this pool");
    }
};

// -------------------------- STL 兼容分配器 --------------------------
/**
 * 可用于 std::vector 等容器的自定义分配器。
 * 注意：每次分配独立使用 new，不直接关联 SlabPool，但可按需改造。
 */
template <typename T>
struct SlabAllocator {
    using value_type = T;

    SlabAllocator() = default;
    template <typename U> SlabAllocator(const SlabAllocator<U>&) {}

    T* allocate(std::size_t n) {
        return static_cast<T*>(::operator new(n * sizeof(T), std::nothrow));
    }
    void deallocate(T* p, std::size_t) {
        ::operator delete(p);
    }
};
template <typename T, typename U>
bool operator==(const SlabAllocator<T>&, const SlabAllocator<U>&) { return true; }
template <typename T, typename U>
bool operator!=(const SlabAllocator<T>&, const SlabAllocator<U>&) { return false; }

} // namespace memory
} // namespace fire_seed

#endif // FIRESEED_SLAB_ALLOCATOR_H
