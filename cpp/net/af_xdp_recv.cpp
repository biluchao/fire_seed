/**
 * 火种系统 (FireSeed) 高性能行情接收模块 (AF_XDP)
 * =================================================
 * 基于 Linux AF_XDP 技术直接从网卡驱动层捕获交易所行情数据包，
 * 实现微秒级延迟的用户态零拷贝网络接收。
 *
 * 依赖：
 * - Linux 内核 >= 5.4
 * - libbpf (提供 xsk 相关 API)
 * - 至少一个支持 AF_XDP 的网卡队列
 *
 * 编译时需链接 libbpf, libxdp (若使用 libxdp 辅助加载)
 *
 * 本模块以单独线程运行，负责：
 * 1. 初始化 AF_XDP 套接字与 UMEM
 * 2. 持续轮询接收数据包
 * 3. 解析以太网/IP/UDP 头部，提取原始行情负载
 * 4. 将解析后的行情快照写入共享内存环形队列
 * 5. 在资源销毁时安全释放
 */

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <thread>
#include <atomic>
#include <unistd.h>
#include <net/if.h>
#include <linux/if_link.h>
#include <linux/if_xdp.h>
#include <sys/socket.h>
#include <sys/mman.h>
#include <poll.h>
#include <arpa/inet.h>
#include <bpf/libbpf.h>
#include <bpf/xsk.h>

// 共享内存队列接口 (假设存在)
#include "shm_queue.h"

// ---------- 配置常量 ----------
#define NUM_FRAMES              4096        // UMEM 帧总数
#define FRAME_SIZE              2048        // 单帧大小 (足够容纳 MTU 数据包)
#define BATCH_SIZE              64          // 批量处理包数
#define RING_CONS_SIZE          512         // 完成队列大小

// ---------- 以太网 / IP / UDP 头定义 ----------
#pragma pack(push, 1)
struct eth_hdr {
    uint8_t  dst_mac[6];
    uint8_t  src_mac[6];
    uint16_t ethertype;
};

struct ip_hdr {
    uint8_t  version_ihl;
    uint8_t  tos;
    uint16_t total_len;
    uint16_t id;
    uint16_t frag_off;
    uint8_t  ttl;
    uint8_t  protocol;
    uint16_t checksum;
    uint32_t src_addr;
    uint32_t dst_addr;
};

struct udp_hdr {
    uint16_t src_port;
    uint16_t dst_port;
    uint16_t length;
    uint16_t checksum;
};
#pragma pack(pop)

// ---------- UMEM 管理 ----------
struct UmemConfig {
    void*   buffer;             // 映射的 UMEM 内存起始地址
    size_t  buf_size;           // 总大小 = NUM_FRAMES * FRAME_SIZE
    struct xsk_umem* umem;      // umem 对象指针
};

// ---------- AF_XDP 接收器类 ----------
class AfXdpReceiver {
public:
    AfXdpReceiver(const char* iface, uint32_t queue_id);
    ~AfXdpReceiver();

    // 启动接收线程，将解析后的 Tick 写入队列
    void start(ShmQueue* output_queue);

    // 停止接收
    void stop();

private:
    // 内部初始化步骤
    bool create_socket();
    bool setup_umem();
    bool bind_socket(const char* iface, uint32_t queue_id);

    // 填充 UMEM 的 fill ring
    void fill_umem();

    // 处理接收到的数据包批次
    void process_packets(uint32_t idx, uint32_t count, const struct xsk_ring_cons* comp_ring,
                         ShmQueue* queue);

    // 解析网络包，提取行情数据，返回需传入共享内存的消息长度
    size_t parse_market_data(const uint8_t* frame, size_t len, MarketTick* tick);

    std::string             iface_;
    uint32_t                queue_id_;
    int                     xsk_socket_fd_;
    struct xsk_socket*      xsk_;
    UmemConfig              umem_;
    std::atomic<bool>       running_;
    std::unique_ptr<std::thread> worker_;
};

// ======================== 实现 ========================

AfXdpReceiver::AfXdpReceiver(const char* iface, uint32_t queue_id)
    : iface_(iface), queue_id_(queue_id), xsk_socket_fd_(-1), xsk_(nullptr), running_(false) {
    memset(&umem_, 0, sizeof(umem_));
}

AfXdpReceiver::~AfXdpReceiver() {
    stop();
    if (xsk_) {
        xsk_socket__delete(xsk_);
        xsk_ = nullptr;
    }
    if (xsk_socket_fd_ >= 0) {
        close(xsk_socket_fd_);
        xsk_socket_fd_ = -1;
    }
    if (umem_.buffer) {
        munmap(umem_.buffer, umem_.buf_size);
        umem_.buffer = nullptr;
    }
}

void AfXdpReceiver::start(ShmQueue* output_queue) {
    if (running_.load()) return;

    if (!create_socket()) {
        throw std::runtime_error("创建 AF_XDP 套接字失败");
    }
    if (!setup_umem()) {
        throw std::runtime_error("UMEM 初始化失败");
    }
    if (!bind_socket(iface_.c_str(), queue_id_)) {
        throw std::runtime_error("绑定 AF_XDP 套接字失败");
    }

    running_.store(true);
    worker_ = std::make_unique<std::thread>([this, output_queue]() {
        // 设置线程名称便于调试
        pthread_setname_np(pthread_self(), "af_xdp_recv");

        // 填充 fill ring
        fill_umem();

        // 准备 poll
        struct pollfd fds[1];
        fds[0].fd = xsk_socket_fd_;
        fds[0].events = POLLIN;

        // 接收缓冲区数组 (批量处理)
        uint32_t idx_array[BATCH_SIZE];

        while (running_.load()) {
            int ret = poll(fds, 1, 10);   // 10ms 超时
            if (ret < 0) {
                if (errno == EINTR) continue;
                std::cerr << "[AF_XDP] poll error: " << strerror(errno) << std::endl;
                break;
            }

            if (!(fds[0].revents & POLLIN)) continue;

            // 批量接收数据包
            uint32_t rcvd = xsk_ring_cons__peek(&xsk_->rx, BATCH_SIZE, &idx_array);
            if (rcvd > 0) {
                // 处理接收到的包
                process_packets(idx_array[0], rcvd, &xsk_->umem->cq, output_queue);

                // 释放已消费的帧，重新填充到 fill ring
                xsk_ring_cons__release(&xsk_->rx, rcvd);

                // 补充 fill ring
                uint32_t fill_idx;
                uint32_t fill_batch = BATCH_SIZE;
                if (xsk_ring_prod__reserve(&xsk_->umem->fq, fill_batch, &fill_idx) == fill_batch) {
                    for (uint32_t i = 0; i < fill_batch; i++) {
                        *xsk_ring_prod__fill_addr(&xsk_->umem->fq, fill_idx + i) =
                            (fill_idx + i) * FRAME_SIZE;
                    }
                    xsk_ring_prod__submit(&xsk_->umem->fq, fill_batch);
                }
            }
        }
    });
}

void AfXdpReceiver::stop() {
    running_.store(false);
    if (worker_ && worker_->joinable()) {
        worker_->join();
        worker_.reset();
    }
}

// ----------------------------------------------------------
bool AfXdpReceiver::create_socket() {
    struct xsk_socket_config cfg;
    cfg.rx_size = RING_CONS_SIZE;
    cfg.tx_size = 0;               // 只接收
    cfg.libbpf_flags = XSK_LIBBPF_FLAGS__INHIBIT_PROG_LOAD;
    cfg.bind_flags = XDP_USE_NEED_WAKEUP;

    // 将创建 UMEM 和绑定一同完成 (xsk_socket__create 需要 UMEM 已初始化)
    // 实际操作顺序是先初始化 UMEM，再绑定。此处分离步骤，该函数仅预留。
    return true;
}

bool AfXdpReceiver::setup_umem() {
    // 分配 UMEM 内存 (mmap 方式) 或通过 libbpf helper
    umem_.buf_size = NUM_FRAMES * FRAME_SIZE;
    // 使用 xsk_umem__create 分配 umem
    struct xsk_umem_config umem_cfg;
    umem_cfg.fill_size = RING_CONS_SIZE * 2;
    umem_cfg.comp_size = RING_CONS_SIZE;
    umem_cfg.frame_size = FRAME_SIZE;
    umem_cfg.frame_headroom = XSK_UMEM__DEFAULT_FRAME_HEADROOM;
    umem_cfg.flags = 0;

    int ret = xsk_umem__create(&umem_.umem, nullptr, NUM_FRAMES * FRAME_SIZE,
                               &umem_.fq, &umem_.cq, &umem_cfg);
    if (ret != 0) {
        std::cerr << "[AF_XDP] xsk_umem__create failed" << std::endl;
        return false;
    }

    // 获取映射的用户态缓冲区
    umem_.buffer = xsk_umem__get_data(umem_.umem);
    return true;
}

bool AfXdpReceiver::bind_socket(const char* iface, uint32_t queue_id) {
    struct xsk_socket_config cfg;
    cfg.rx_size = RING_CONS_SIZE;
    cfg.tx_size = 0;
    cfg.libbpf_flags = XSK_LIBBPF_FLAGS__INHIBIT_PROG_LOAD;
    cfg.bind_flags = XDP_USE_NEED_WAKEUP;

    int ret = xsk_socket__create(&xsk_, iface, queue_id, umem_.umem,
                                 &xsk_->rx, &xsk_->tx, &cfg);
    if (ret != 0) {
        std::cerr << "[AF_XDP] xsk_socket__create failed" << std::endl;
        return false;
    }
    xsk_socket_fd_ = xsk_socket__fd(xsk_);
    return true;
}

void AfXdpReceiver::fill_umem() {
    uint32_t idx;
    // 一次性填充所有帧到 fill ring
    uint32_t fill_batch = NUM_FRAMES;
    if (xsk_ring_prod__reserve(&umem_.umem->fq, fill_batch, &idx) == fill_batch) {
        for (uint32_t i = 0; i < fill_batch; i++) {
            *xsk_ring_prod__fill_addr(&umem_.umem->fq, idx + i) = i * FRAME_SIZE;
        }
        xsk_ring_prod__submit(&umem_.umem->fq, fill_batch);
    }
}

void AfXdpReceiver::process_packets(uint32_t idx, uint32_t count,
                                    const struct xsk_ring_cons* comp_ring,
                                    ShmQueue* queue) {
    for (uint32_t i = 0; i < count; i++) {
        uint64_t addr = xsk_ring_cons__rx_desc(&xsk_->rx, idx + i)->addr;
        uint32_t len = xsk_ring_cons__rx_desc(&xsk_->rx, idx + i)->len;
        const uint8_t* frame = reinterpret_cast<const uint8_t*>(umem_.buffer) + addr;

        MarketTick tick;
        size_t parsed = parse_market_data(frame, len, &tick);
        if (parsed > 0) {
            // 写入共享内存队列 (无锁写入，若满则丢弃)
            if (queue) {
                queue->try_push(tick);   // 假设 ShmQueue 有 try_push 方法
            }
        }
    }
}

size_t AfXdpReceiver::parse_market_data(const uint8_t* frame, size_t len, MarketTick* tick) {
    if (len < sizeof(eth_hdr) + sizeof(ip_hdr) + sizeof(udp_hdr)) {
        return 0;   // 包太小
    }

    auto* eth = reinterpret_cast<const eth_hdr*>(frame);
    if (ntohs(eth->ethertype) != 0x0800) return 0;   // 仅处理 IPv4

    auto* ip = reinterpret_cast<const ip_hdr*>(frame + sizeof(eth_hdr));
    if (ip->protocol != 17) return 0;                // 仅处理 UDP

    auto* udp = reinterpret_cast<const udp_hdr*>(frame + sizeof(eth_hdr) + sizeof(ip_hdr));
    size_t header_len = sizeof(eth_hdr) + sizeof(ip_hdr) + sizeof(udp_hdr);
    size_t payload_len = ntohs(udp->length) - sizeof(udp_hdr);
    if (payload_len == 0 || header_len + payload_len > len) return 0;

    const uint8_t* payload = frame + header_len;
    // 假设 payload 是 JSON 文本，这里进行极简处理：复制到 tick 的缓冲区
    // 实际应根据交易所数据格式解析，此处仅占位
    if (payload_len > sizeof(tick->raw_data) - 1) {
        payload_len = sizeof(tick->raw_data) - 1;
    }
    memcpy(tick->raw_data, payload, payload_len);
    tick->raw_data[payload_len] = '\0';
    tick->timestamp = std::chrono::system_clock::now().time_since_epoch().count();

    return header_len + payload_len;
}

// ---------- 可选的 C 接口 (方便其他语言绑定) ----------
extern "C" {
    AfXdpReceiver* create_receiver(const char* iface, uint32_t queue) {
        try {
            return new AfXdpReceiver(iface, queue);
        } catch (...) {
            return nullptr;
        }
    }

    void start_receiver(AfXdpReceiver* recv, ShmQueue* queue) {
        if (recv) recv->start(queue);
    }

    void stop_receiver(AfXdpReceiver* recv) {
        if (recv) recv->stop();
    }

    void destroy_receiver(AfXdpReceiver* recv) {
        delete recv;
    }
}
