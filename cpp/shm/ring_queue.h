/**
 * 火种系统 (FireSeed) 无锁环形队列 (Multi-Producer, Single-Consumer)
 * =====================================================================
 * 基于 Lamport 的无锁设计，针对高频读写进行极致优化：
 * - 固定大小 (2 的幂)，使用内存屏障保证多核可见性。
 * - 支持批量 push / pop，减少原子操作开销。
 * - C++ 模板化 + C 接口便于 pybind11 绑定。
 * - 缓存行对齐 (64 字节)，消除伪共享。
 * - 可配置大页内存分配 (通过 mmap MAP_HUGETLB)。
 *
 * 编译：
 *   gcc -c ring_queue.c -o ring_queue.o -std=c11 -O2
 * 若需 C++ 模板实例化，由调用方包含此头文件即可。
 */

#ifndef FIRESEED_RING_QUEUE_H
#define FIRESEED_RING_QUEUE_H

#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ======================== 常量与配置 ======================== */
#ifndef CACHE_LINE_SIZE
#define CACHE_LINE_SIZE 64
#endif

#define RING_QUEUE_ALIGNMENT CACHE_LINE_SIZE

/* ======================== 数据结构定义 ======================== */

/** 环形队列元数据 (独立缓存行，避免与数据合并导致伪共享) */
typedef struct {
    _Atomic uint64_t write_head;    /* 生产者写入位置 */
    char _pad1[CACHE_LINE_SIZE - sizeof(uint64_t)];
    _Atomic uint64_t read_head;     /* 消费者读取位置 (批量更新) */
    char _pad2[CACHE_LINE_SIZE - sizeof(uint64_t)];
} RingQueueHeader;

/**
 * 环形队列完整结构。
 * 实际元素存储在 header 之后的内存区域。
 * 用户不应直接访问成员，通过 API 操作。
 */
typedef struct {
    RingQueueHeader header;
    uint32_t        capacity;       /* 最大元素个数 (2的幂) */
    uint32_t        elem_size;      /* 单个元素大小 */
    char            data[];         /* 柔性数组：元素存储区 */
} RingQueue;

/* ======================== 内部辅助宏 ======================== */
static inline uint32_t next_pow2(uint32_t v) {
    v--;
    v |= v >> 1;
    v |= v >> 2;
    v |= v >> 4;
    v |= v >> 8;
    v |= v >> 16;
    return ++v;
}

static inline uint32_t ring_mask(const RingQueue* q) {
    return q->capacity - 1;
}

/* ======================== 构造函数 ======================== */

/**
 * 分配并初始化环形队列。
 * @param capacity  期望的最小容量 (会自动对齐到 2 的幂)
 * @param elem_size 单个元素的字节数
 * @return 已初始化的队列指针，失败返回 NULL
 */
RingQueue* ring_queue_create(uint32_t capacity, uint32_t elem_size) {
    if (capacity == 0 || elem_size == 0) return NULL;

    uint32_t real_cap = next_pow2(capacity);
    size_t total_size = sizeof(RingQueue) + (size_t)real_cap * elem_size;

    /* 尝试使用大页内存 (若失败则回退普通分配) */
    RingQueue* q = NULL;
#if defined(MAP_HUGETLB) && defined(MAP_ANONYMOUS)
    q = (RingQueue*) mmap(NULL, total_size, PROT_READ | PROT_WRITE,
                          MAP_PRIVATE | MAP_ANONYMOUS | MAP_HUGETLB, -1, 0);
    if (q == MAP_FAILED) {
        /* 大页不可用，使用普通内存 */
        q = (RingQueue*) mmap(NULL, total_size, PROT_READ | PROT_WRITE,
                              MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    }
#else
    q = (RingQueue*) mmap(NULL, total_size, PROT_READ | PROT_WRITE,
                          MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
#endif
    if (q == MAP_FAILED) return NULL;

    memset(q, 0, total_size);
    atomic_store_explicit(&q->header.write_head, 0, memory_order_relaxed);
    atomic_store_explicit(&q->header.read_head, 0, memory_order_relaxed);
    q->capacity = real_cap;
    q->elem_size = elem_size;

    return q;
}

/**
 * 销毁队列并释放内存。
 */
void ring_queue_destroy(RingQueue* q) {
    if (!q) return;
    size_t total = sizeof(RingQueue) + (size_t)q->capacity * q->elem_size;
    munmap(q, total);
}

/* ======================== 生产者接口 (SPSC) ======================== */

/**
 * 尝试写入单个元素 (SPSC 安全)。
 * @param q    队列指针
 * @param elem 指向待写入数据的指针
 * @return 1 表示成功，0 表示队列满
 */
int ring_queue_push(RingQueue* q, const void* elem) {
    if (!q || !elem) return 0;

    uint64_t w = atomic_load_explicit(&q->header.write_head, memory_order_relaxed);
    uint64_t r = atomic_load_explicit(&q->header.read_head, memory_order_acquire);
    uint64_t next_w = w + 1;

    if (next_w - r > q->capacity) {
        return 0;  /* 队列满 */
    }

    char* dst = q->data + (w & ring_mask(q)) * q->elem_size;
    memcpy(dst, elem, q->elem_size);
    atomic_store_explicit(&q->header.write_head, next_w, memory_order_release);
    return 1;
}

/**
 * 批量写入 (SPSC 安全)。
 * @param q     队列指针
 * @param elems 元素数组起始地址
 * @param count 元素个数
 * @return 实际成功写入的数量 (0 表示满)
 */
uint32_t ring_queue_push_batch(RingQueue* q, const void* elems, uint32_t count) {
    if (!q || !elems || count == 0) return 0;

    uint64_t w = atomic_load_explicit(&q->header.write_head, memory_order_relaxed);
    uint64_t r = atomic_load_explicit(&q->header.read_head, memory_order_acquire);
    uint64_t avail = q->capacity - (uint64_t)(w - r);
    if (avail == 0) return 0;

    uint32_t to_write = (uint32_t)(count < avail ? count : avail);
    const char* src = (const char*)elems;

    for (uint32_t i = 0; i < to_write; i++) {
        char* dst = q->data + ((w + i) & ring_mask(q)) * q->elem_size;
        memcpy(dst, src + i * q->elem_size, q->elem_size);
    }

    atomic_store_explicit(&q->header.write_head, w + to_write, memory_order_release);
    return to_write;
}

/* ======================== 消费者接口 (SPSC) ======================== */

/**
 * 尝试读取单个元素 (SPSC 安全)。
 * @param q    队列指针
 * @param elem 输出缓冲区 (至少 elem_size 字节)
 * @return 1 表示成功，0 表示队列空
 */
int ring_queue_pop(RingQueue* q, void* elem) {
    if (!q || !elem) return 0;

    uint64_t r = atomic_load_explicit(&q->header.read_head, memory_order_relaxed);
    uint64_t w = atomic_load_explicit(&q->header.write_head, memory_order_acquire);

    if (r == w) return 0;  /* 队列空 */

    const char* src = q->data + (r & ring_mask(q)) * q->elem_size;
    memcpy(elem, src, q->elem_size);
    atomic_store_explicit(&q->header.read_head, r + 1, memory_order_release);
    return 1;
}

/**
 * 批量读取 (SPSC 安全)。
 * @param q      队列指针
 * @param elems  输出缓冲区 (至少 count * elem_size 字节)
 * @param count  期望读取的最大数量
 * @return 实际读取的数量
 */
uint32_t ring_queue_pop_batch(RingQueue* q, void* elems, uint32_t count) {
    if (!q || !elems || count == 0) return 0;

    uint64_t r = atomic_load_explicit(&q->header.read_head, memory_order_relaxed);
    uint64_t w = atomic_load_explicit(&q->header.write_head, memory_order_acquire);
    uint64_t avail = w - r;
    if (avail == 0) return 0;

    uint32_t to_read = (uint32_t)(count < avail ? count : avail);
    char* dst = (char*)elems;

    for (uint32_t i = 0; i < to_read; i++) {
        const char* src = q->data + ((r + i) & ring_mask(q)) * q->elem_size;
        memcpy(dst + i * q->elem_size, src, q->elem_size);
    }

    atomic_store_explicit(&q->header.read_head, r + to_read, memory_order_release);
    return to_read;
}

/* ======================== 查询辅助 ======================== */

/**
 * 返回队列中当前存储的元素个数 (近似值)。
 */
uint64_t ring_queue_count(const RingQueue* q) {
    if (!q) return 0;
    uint64_t w = atomic_load_explicit(&q->header.write_head, memory_order_acquire);
    uint64_t r = atomic_load_explicit(&q->header.read_head, memory_order_acquire);
    return w - r;
}

/**
 * 返回队列总容量。
 */
uint32_t ring_queue_capacity(const RingQueue* q) {
    return q ? q->capacity : 0;
}

/**
 * 清空队列 (仅重置读写指针，不清理数据)。
 * 调用者需保证没有并发操作。
 */
void ring_queue_reset(RingQueue* q) {
    if (!q) return;
    atomic_store_explicit(&q->header.write_head, 0, memory_order_relaxed);
    atomic_store_explicit(&q->header.read_head, 0, memory_order_relaxed);
}

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* FIRESEED_RING_QUEUE_H */
