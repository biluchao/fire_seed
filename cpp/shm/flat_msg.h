/**
 * 火种系统 (FireSeed) 零拷贝扁平消息协议 (FlatMsg)
 * ====================================================
 * 用于 Python 引擎与 C++ 模块之间的高性能共享内存通信。
 * 所有结构体均为标准 C 布局，内存对齐，无序列化/反序列化开销。
 * 可直接通过 ctypes / pybind11 在 Python 侧映射。
 *
 * 消息总长度固定为 FLAT_MSG_MAX_SIZE (256 字节)，
 * 小于 CPU 缓存行，且便于数组化.
 *
 * 编译：
 *   gcc -c flat_msg.c -o flat_msg.o -std=c11 -O2
 */

#ifndef FIRESEED_FLAT_MSG_H
#define FIRESEED_FLAT_MSG_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ======================== 常量定义 ======================== */
#define FLAT_MSG_MAX_SIZE       256          /* 消息总最大字节数 */
#define FLAT_MSG_HEADER_SIZE    16           /* 消息头大小 (bytes) */
#define FLAT_MSG_PAYLOAD_SIZE   (FLAT_MSG_MAX_SIZE - FLAT_MSG_HEADER_SIZE) /* 240 */

/* 消息类型枚举 (与 Python 侧保持同步) */
enum FlatMsgType {
    MSG_TYPE_TICK          = 1,  /* 市场行情 Tick */
    MSG_TYPE_ORDER_REQ     = 2,  /* 订单请求 (Python -> C++) */
    MSG_TYPE_ORDER_ACK     = 3,  /* 订单确认 (C++ -> Python) */
    MSG_TYPE_RISK_SIGNAL   = 4,  /* 风控信号 (硬止损/熔断) */
    MSG_TYPE_HEARTBEAT     = 5,  /* 心跳 */
    MSG_TYPE_CONFIG_SYNC   = 6,  /* 配置同步 */
    MSG_TYPE_LOG           = 7,  /* 日志 */
    MSG_TYPE_MAX           = 255
};

/* ======================== 消息头 (16 字节) ======================== */
typedef struct {
    uint8_t  type;            /* FlatMsgType 之一 */
    uint8_t  version;         /* 协议版本 (当前为 1) */
    uint16_t payload_len;     /* 有效负载实际长度 (字节, 最大 240) */
    uint32_t seq;             /* 消息序列号 (单调递增) */
    uint32_t timestamp_ms;    /* 消息生产时间戳 (毫秒, epoch 基准) */
    uint32_t crc32;           /* 负载的 CRC32 校验值 (0 表示未使用) */
} FlatMsgHeader;

/* 静态断言：头部大小为 16 */
_Static_assert(sizeof(FlatMsgHeader) == 16, "FlatMsgHeader must be 16 bytes");

/* ======================== 消息载体 (256 字节) ======================== */
typedef struct {
    FlatMsgHeader header;
    uint8_t       payload[FLAT_MSG_PAYLOAD_SIZE];   /* 240 字节 */
} FlatMsg;

_Static_assert(sizeof(FlatMsg) == FLAT_MSG_MAX_SIZE, "FlatMsg must be 256 bytes");

/* ======================== 常用负载结构 ======================== */

/* ---- 市场 Tick 数据 ---- */
typedef struct {
    char     symbol[16];      /* 交易对，如 "BTCUSDT" */
    double   last_price;
    double   bid;
    double   ask;
    double   volume;
    uint64_t exchange_ts;     /* 交易所时间戳 (微秒) */
} TickPayload;

/* ---- 订单请求 ---- */
typedef struct {
    char     symbol[16];
    uint8_t  side;            /* 0:BUY, 1:SELL */
    uint8_t  order_type;      /* 0:LIMIT, 1:MARKET ... */
    double   price;
    double   quantity;
    uint64_t client_id;
} OrderReqPayload;

/* ---- 订单确认 ---- */
typedef struct {
    char     order_id[32];
    uint8_t  status;          /* 0:FILLED, 1:CANCELLED, 2:REJECTED ... */
    double   fill_price;
    double   fill_qty;
    double   slippage_bps;
} OrderAckPayload;

/* ---- 风控信号 ---- */
typedef struct {
    uint8_t  signal_type;     /* 0:STOP_LOSS, 1:FORCE_CLOSE, 2:LIQ_WARNING */
    char     symbol[16];
    double   trigger_price;
    double   current_price;
    uint32_t reserved;
} RiskSignalPayload;

/* ---- 心跳 ---- */
typedef struct {
    uint32_t ping_seq;
    uint32_t pong_seq;
} HeartbeatPayload;

/* ---- 日志 ---- */
typedef struct {
    uint8_t  level;           /* 0:DEBUG, 1:INFO, 2:WARN, 3:ERROR */
    char     message[239];    /* 短日志 */
} LogPayload;

/* ======================== 构造函数与工具函数 ======================== */

/** 清零整个消息 */
static inline void flat_msg_init(FlatMsg* msg) {
    if (msg) memset(msg, 0, sizeof(FlatMsg));
}

/** 设置消息头 (不计算 CRC) */
static inline void flat_msg_set_header(FlatMsg* msg, uint8_t type,
                                       uint16_t payload_len,
                                       uint32_t seq,
                                       uint32_t timestamp_ms) {
    if (!msg) return;
    msg->header.type         = type;
    msg->header.version      = 1;
    msg->header.payload_len  = payload_len;
    msg->header.seq          = seq;
    msg->header.timestamp_ms = timestamp_ms;
    msg->header.crc32        = 0;  /* 默认不校验 */
}

/** 获取负载指针 (void*) */
static inline void* flat_msg_payload(FlatMsg* msg) {
    return (msg) ? msg->payload : NULL;
}

/** 获取负载指针 (const) */
static inline const void* flat_msg_payload_const(const FlatMsg* msg) {
    return (msg) ? msg->payload : NULL;
}

/** 简单 CRC32 校验 (用于负载) */
static inline uint32_t flat_msg_calc_crc32(const uint8_t* data, uint16_t len) {
    uint32_t crc = 0xFFFFFFFF;
    for (uint16_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int j = 0; j < 8; j++) {
            if (crc & 1)
                crc = (crc >> 1) ^ 0xEDB88320;
            else
                crc >>= 1;
        }
    }
    return ~crc;
}

/** 为消息负载计算并写入 CRC32 */
static inline void flat_msg_set_crc32(FlatMsg* msg) {
    if (!msg || msg->header.payload_len == 0) return;
    uint32_t crc = flat_msg_calc_crc32(msg->payload, msg->header.payload_len);
    msg->header.crc32 = crc;
}

/** 验证消息负载的 CRC32 (返回 1 表示通过) */
static inline int flat_msg_validate_crc32(const FlatMsg* msg) {
    if (!msg || msg->header.payload_len == 0) return 1;
    uint32_t calc = flat_msg_calc_crc32(msg->payload, msg->header.payload_len);
    return (calc == msg->header.crc32) ? 1 : 0;
}

/** 便捷：构建一个 Tick 消息 */
static inline void flat_msg_build_tick(FlatMsg* msg,
                                       const char* symbol,
                                       double last, double bid, double ask,
                                       double vol, uint64_t exch_ts,
                                       uint32_t seq, uint32_t ts_ms) {
    flat_msg_init(msg);
    TickPayload* tick = (TickPayload*)flat_msg_payload(msg);
    strncpy(tick->symbol, symbol, sizeof(tick->symbol) - 1);
    tick->last_price    = last;
    tick->bid           = bid;
    tick->ask           = ask;
    tick->volume        = vol;
    tick->exchange_ts   = exch_ts;
    flat_msg_set_header(msg, MSG_TYPE_TICK, sizeof(TickPayload), seq, ts_ms);
}

/** 便捷：构建订单请求消息 */
static inline void flat_msg_build_order_req(FlatMsg* msg,
                                            const char* symbol,
                                            uint8_t side, uint8_t otype,
                                            double price, double qty,
                                            uint64_t client_id,
                                            uint32_t seq, uint32_t ts_ms) {
    flat_msg_init(msg);
    OrderReqPayload* ord = (OrderReqPayload*)flat_msg_payload(msg);
    strncpy(ord->symbol, symbol, sizeof(ord->symbol) - 1);
    ord->side       = side;
    ord->order_type = otype;
    ord->price      = price;
    ord->quantity   = qty;
    ord->client_id  = client_id;
    flat_msg_set_header(msg, MSG_TYPE_ORDER_REQ, sizeof(OrderReqPayload), seq, ts_ms);
}

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* FIRESEED_FLAT_MSG_H */
