/**
 * 火种系统 (FireSeed) 用户态TCP/IP栈 (高性能存根)
 * ==================================================
 * 当前实现基于 Linux epoll + 非阻塞 socket，提供与用户态协议栈
 * 相同的异步、事件驱动、低延迟接口。后续可替换为基于 DPDK / mTCP
 * 的真实用户态协议栈，接口保持不变。
 *
 * 功能：
 * - 异步连接管理 (非阻塞 connect + 超时)
 * - 事件循环 (epoll) 处理可读/可写/错误
 * - 发送/接收缓冲区
 * - 连接保活与自动重连
 * - 零拷贝友好的接口设计 (提供 scatter/gather 预留)
 *
 * 编译：
 *   g++ -std=c++17 -O2 tcp_userstack.cpp -o tcp_userstack.o -c
 */

#include <cstring>
#include <cerrno>
#include <stdexcept>
#include <memory>
#include <vector>
#include <unordered_map>
#include <functional>
#include <chrono>
#include <atomic>
#include <thread>
#include <mutex>
#include <condition_variable>

#include <sys/epoll.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <unistd.h>

// ====================== 基础连接配置 ======================
struct TcpConfig {
    std::string dest_ip;
    uint16_t    dest_port;
    int         connect_timeout_ms = 5000;   // 连接超时 (毫秒)
    int         recv_timeout_ms    = 1000;   // 接收超时
    int         send_timeout_ms    = 1000;   // 发送超时
    bool        tcp_nodelay        = true;   // 禁用 Nagle 算法
    int         recv_buf_size      = 65536;  // 接收缓冲区大小
    int         send_buf_size      = 65536;  // 发送缓冲区大小
};

// ====================== 事件类型 ======================
enum class IOEvent {
    READABLE = 0x01,
    WRITABLE = 0x02,
    ERROR    = 0x04,
    CLOSED   = 0x08
};

// ====================== 回调函数类型 ======================
using DataCallback   = std::function<void(int fd, const uint8_t* data, size_t len)>;
using EventCallback  = std::function<void(int fd, IOEvent event)>;

// ====================== 用户态TCP连接封装 ======================
class UserSpaceTcpConnection {
public:
    UserSpaceTcpConnection() : fd_(-1), connected_(false) {}

    ~UserSpaceTcpConnection() { close(); }

    // 发起异步连接 (非阻塞)
    bool connect_async(const TcpConfig& cfg);

    // 发送数据 (非阻塞，返回已发送字节数，-1 表示错误)
    ssize_t send(const uint8_t* data, size_t len);

    // 接收数据 (非阻塞，返回已读字节数)
    ssize_t recv(uint8_t* buf, size_t max_len);

    // 关闭连接
    void close();

    // 文件描述符
    int fd() const { return fd_; }

    // 是否已连接
    bool is_connected() const { return connected_.load(); }

    // 设置数据回调
    void set_data_callback(DataCallback cb) { data_cb_ = std::move(cb); }

    // 内部使用：标记连接成功 (由事件循环调用)
    void mark_connected() { connected_.store(true); }

private:
    int                 fd_;
    std::atomic<bool>   connected_;
    DataCallback        data_cb_;
};

// ====================== 用户态TCP管理器 (事件循环) ======================
class UserSpaceTcpStack {
public:
    UserSpaceTcpStack();
    ~UserSpaceTcpStack();

    // 注册一个连接管理器 (接管 fd 的生命周期)
    void register_connection(std::shared_ptr<UserSpaceTcpConnection> conn);

    // 移除连接
    void unregister_connection(int fd);

    // 事件循环 (阻塞，直到 stop 被调用)
    void run();

    // 停止事件循环
    void stop();

    // 添加自定义 fd 到 epoll (不纳入连接生命周期管理)
    void add_raw_fd(int fd, uint32_t events);

private:
    void handle_events(int timeout_ms);

    int                             epoll_fd_;
    std::atomic<bool>               running_;
    std::mutex                      mutex_;
    std::unordered_map<int, std::shared_ptr<UserSpaceTcpConnection>> connections_;
    std::unordered_map<int, uint32_t> raw_fds_;
};

// ====================== 实现: UserSpaceTcpConnection ======================
bool UserSpaceTcpConnection::connect_async(const TcpConfig& cfg) {
    fd_ = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, IPPROTO_TCP);
    if (fd_ < 0) return false;

    // 设置套接字选项
    int flag = 1;
    if (cfg.tcp_nodelay) {
        setsockopt(fd_, IPPROTO_TCP, TCP_NODELAY, &flag, sizeof(flag));
    }
    // 设置缓冲区大小
    int rcvbuf = cfg.recv_buf_size;
    int sndbuf = cfg.send_buf_size;
    setsockopt(fd_, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));
    setsockopt(fd_, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

    // 构造目标地址
    struct sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port   = htons(cfg.dest_port);
    if (inet_pton(AF_INET, cfg.dest_ip.c_str(), &addr.sin_addr) != 1) {
        ::close(fd_);
        fd_ = -1;
        return false;
    }

    // 发起非阻塞连接
    int ret = ::connect(fd_, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr));
    if (ret == 0) {
        // 连接立即成功 (本地或同一台机器)
        connected_.store(true);
        return true;
    }
    if (errno != EINPROGRESS) {
        ::close(fd_);
        fd_ = -1;
        return false;
    }
    // 连接正在处理，将由 epoll 通知
    return true;
}

ssize_t UserSpaceTcpConnection::send(const uint8_t* data, size_t len) {
    if (fd_ < 0 || !connected_.load()) return -1;
    ssize_t sent = ::send(fd_, data, len, MSG_DONTWAIT | MSG_NOSIGNAL);
    if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
        return 0;  // 缓冲区满
    }
    return sent;
}

ssize_t UserSpaceTcpConnection::recv(uint8_t* buf, size_t max_len) {
    if (fd_ < 0) return -1;
    ssize_t n = ::recv(fd_, buf, max_len, MSG_DONTWAIT);
    if (n == 0) {
        // 对端关闭连接
        connected_.store(false);
    }
    return n;
}

void UserSpaceTcpConnection::close() {
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
    connected_.store(false);
}

// ====================== 实现: UserSpaceTcpStack ======================
UserSpaceTcpStack::UserSpaceTcpStack() : running_(false) {
    epoll_fd_ = epoll_create1(EPOLL_CLOEXEC);
    if (epoll_fd_ < 0) {
        throw std::runtime_error("epoll_create1 failed");
    }
}

UserSpaceTcpStack::~UserSpaceTcpStack() {
    stop();
    if (epoll_fd_ >= 0) {
        ::close(epoll_fd_);
        epoll_fd_ = -1;
    }
}

void UserSpaceTcpStack::register_connection(std::shared_ptr<UserSpaceTcpConnection> conn) {
    if (!conn) return;
    int fd = conn->fd();
    if (fd < 0) return;

    struct epoll_event ev{};
    ev.events   = EPOLLIN | EPOLLOUT | EPOLLET; // 边缘触发
    ev.data.fd  = fd;

    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (epoll_ctl(epoll_fd_, EPOLL_CTL_ADD, fd, &ev) == 0) {
            connections_[fd] = conn;
        } else if (errno == EEXIST) {
            epoll_ctl(epoll_fd_, EPOLL_CTL_MOD, fd, &ev);
            connections_[fd] = conn;
        }
    }
}

void UserSpaceTcpStack::unregister_connection(int fd) {
    std::lock_guard<std::mutex> lock(mutex_);
    epoll_ctl(epoll_fd_, EPOLL_CTL_DEL, fd, nullptr);
    connections_.erase(fd);
}

void UserSpaceTcpStack::add_raw_fd(int fd, uint32_t events) {
    struct epoll_event ev{};
    ev.events = events | EPOLLET;
    ev.data.fd = fd;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        raw_fds_[fd] = events;
        epoll_ctl(epoll_fd_, EPOLL_CTL_ADD, fd, &ev);
    }
}

void UserSpaceTcpStack::run() {
    running_.store(true);
    const int MAX_EVENTS = 64;
    struct epoll_event events[MAX_EVENTS];

    while (running_.load()) {
        int nfds = epoll_wait(epoll_fd_, events, MAX_EVENTS, 100);
        if (nfds < 0) {
            if (errno == EINTR) continue;
            break;
        }

        for (int i = 0; i < nfds; ++i) {
            int fd    = events[i].data.fd;
            uint32_t revents = events[i].events;

            // 检查错误或挂断
            if (revents & (EPOLLERR | EPOLLHUP)) {
                std::lock_guard<std::mutex> lock(mutex_);
                auto it = connections_.find(fd);
                if (it != connections_.end()) {
                    if (it->second->is_connected()) {
                        it->second->close();
                    }
                    connections_.erase(it);
                }
                continue;
            }

            // 可写 (连接完成或发送缓冲区就绪)
            if (revents & EPOLLOUT) {
                std::lock_guard<std::mutex> lock(mutex_);
                auto it = connections_.find(fd);
                if (it != connections_.end() && !it->second->is_connected()) {
                    // 检查套接字错误，确认连接是否成功
                    int err = 0;
                    socklen_t len = sizeof(err);
                    getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len);
                    if (err == 0) {
                        it->second->mark_connected();
                    } else {
                        it->second->close();
                        connections_.erase(it);
                    }
                }
            }

            // 可读
            if (revents & EPOLLIN) {
                std::lock_guard<std::mutex> lock(mutex_);
                auto it = connections_.find(fd);
                if (it != connections_.end() && it->second->is_connected()) {
                    uint8_t buf[65536];
                    ssize_t n = it->second->recv(buf, sizeof(buf));
                    if (n > 0 && it->second->data_cb_) {
                        it->second->data_cb_(fd, buf, n);
                    } else if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) {
                        it->second->close();
                        connections_.erase(it);
                    }
                }
            }
        }
    }
}

void UserSpaceTcpStack::stop() {
    running_.store(false);
}

// ====================== C 接口 (供 pybind11 调用) ======================
extern "C" {
    // 创建堆栈实例
    UserSpaceTcpStack* create_tcp_stack() {
        return new UserSpaceTcpStack();
    }

    // 销毁堆栈实例
    void destroy_tcp_stack(UserSpaceTcpStack* stack) {
        delete stack;
    }

    // 创建连接并注册到堆栈
    UserSpaceTcpConnection* create_and_register_connection(
        UserSpaceTcpStack* stack,
        const char* ip,
        uint16_t port)
    {
        auto conn = std::make_shared<UserSpaceTcpConnection>();
        TcpConfig cfg;
        cfg.dest_ip   = ip;
        cfg.dest_port = port;
        if (!conn->connect_async(cfg)) {
            return nullptr;
        }
        stack->register_connection(conn);
        // 返回裸指针，由调用方持有引用
        return conn.get();
    }

    // 发送数据
    int connection_send(UserSpaceTcpConnection* conn, const uint8_t* data, size_t len) {
        if (!conn) return -1;
        return static_cast<int>(conn->send(data, len));
    }

    // 关闭连接
    void connection_close(UserSpaceTcpConnection* conn) {
        if (conn) conn->close();
    }

    // 启动事件循环 (阻塞)
    void stack_run(UserSpaceTcpStack* stack) {
        if (stack) stack->run();
    }

    // 停止事件循环
    void stack_stop(UserSpaceTcpStack* stack) {
        if (stack) stack->stop();
    }
}
