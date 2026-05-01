/**
 * 火种系统 (FireSeed) 用户态TCP/IP栈 (头文件)
 * ==============================================
 * 提供异步非阻塞 TCP 连接管理及事件驱动 I/O 接口。
 * 当前实现基于 Linux epoll + 非阻塞 socket，
 * 后续可替换为 DPDK / mTCP 等真实用户态协议栈（保持接口兼容）。
 *
 * 依赖：Linux 内核 >= 2.6，pthread
 */

#ifndef FIRESEED_TCP_USERSTACK_H
#define FIRESEED_TCP_USERSTACK_H

#include <cstdint>
#include <cstddef>
#include <memory>
#include <functional>
#include <string>
#include <atomic>
#include <mutex>
#include <unordered_map>
#include <vector>

#include <sys/epoll.h>

// ====================== 基础配置结构 ======================
struct TcpConfig {
    std::string dest_ip;
    uint16_t    dest_port         = 0;
    int         connect_timeout_ms = 5000;
    int         recv_timeout_ms    = 1000;
    int         send_timeout_ms    = 1000;
    bool        tcp_nodelay        = true;
    int         recv_buf_size      = 65536;
    int         send_buf_size      = 65536;
};

// ====================== 事件类型 ======================
enum class IOEvent : uint32_t {
    READABLE = 0x01,
    WRITABLE = 0x02,
    ERROR    = 0x04,
    CLOSED   = 0x08
};

// ====================== 回调函数类型 ======================
using DataCallback  = std::function<void(int fd, const uint8_t* data, size_t len)>;
using EventCallback = std::function<void(int fd, IOEvent event)>;

// ====================== 用户态TCP连接封装 ======================
class UserSpaceTcpConnection {
public:
    UserSpaceTcpConnection();
    ~UserSpaceTcpConnection();

    // 禁止拷贝，允许移动 (若需要可自行实现)
    UserSpaceTcpConnection(const UserSpaceTcpConnection&) = delete;
    UserSpaceTcpConnection& operator=(const UserSpaceTcpConnection&) = delete;

    /** 发起非阻塞连接，连接成功/失败将通过事件循环通知 */
    bool connect_async(const TcpConfig& cfg);

    /** 发送数据 (非阻塞)，返回已发送字节数，-1 表示错误 */
    ssize_t send(const uint8_t* data, size_t len);

    /** 接收数据 (非阻塞)，返回已读字节数，0 表示对端关闭 */
    ssize_t recv(uint8_t* buf, size_t max_len);

    /** 关闭连接 */
    void close();

    /** 文件描述符 */
    int fd() const { return fd_; }

    /** 是否已完成连接 */
    bool is_connected() const { return connected_.load(std::memory_order_acquire); }

    /** 设置数据到达回调 */
    void set_data_callback(DataCallback cb) { data_cb_ = std::move(cb); }

    /** 内部使用：标记连接成功（由事件循环调用） */
    void mark_connected() { connected_.store(true, std::memory_order_release); }

private:
    int                 fd_ = -1;
    std::atomic<bool>   connected_{false};
    DataCallback        data_cb_;
};

// ====================== 用户态TCP管理器 (事件循环) ======================
class UserSpaceTcpStack {
public:
    UserSpaceTcpStack();
    ~UserSpaceTcpStack();

    // 禁止拷贝
    UserSpaceTcpStack(const UserSpaceTcpStack&) = delete;
    UserSpaceTcpStack& operator=(const UserSpaceTcpStack&) = delete;

    /** 注册一个TCP连接，由事件循环接管其 I/O */
    void register_connection(std::shared_ptr<UserSpaceTcpConnection> conn);

    /** 移除一个TCP连接 */
    void unregister_connection(int fd);

    /** 启动事件循环（阻塞，直到调用 stop） */
    void run();

    /** 停止事件循环 */
    void stop();

    /** 将任意文件描述符加入 epoll 监听（不纳入连接生命周期管理） */
    void add_raw_fd(int fd, uint32_t events);

private:
    int                             epoll_fd_ = -1;
    std::atomic<bool>               running_{false};
    mutable std::mutex              mutex_;
    std::unordered_map<int, std::shared_ptr<UserSpaceTcpConnection>> connections_;
    std::unordered_map<int, uint32_t> raw_fds_;
};

#endif // FIRESEED_TCP_USERSTACK_H
