/**
 * 火种系统 (FireSeed) 通用市场数据类型 (Header-Only)
 * ====================================================
 * 统一所有 C++ 模块共用的数据结构定义，避免重复声明。
 *
 * 包含：
 * - MarketTick   : 市场行情 Tick 快照
 * - PositionInfo : 持仓信息（用于风控与共享内存）
 * - Heartbeat    : 心跳信号（主进程与 C++ 守护进程间保活）
 *
 * 所有结构体均使用标准 C 布局，可直接被 ctypes 或 pybind11 映射。
 */

#ifndef FIRESEED_MARKET_TYPES_H
#define FIRESEED_MARKET_TYPES_H

#include <cstdint>

#pragma pack(push, 1)

// ======================== 市场行情 Tick ========================
struct MarketTick {
    char     symbol[16];         // 交易对，如 "BTCUSDT"
    double   last_price;         // 最新成交价
    double   bid;                // 买一价
    double   ask;                // 卖一价
    double   volume;             // 累计成交量 (base)
    uint64_t timestamp_us;       // 交易所时间戳 (微秒)
    uint64_t local_stamp_us;     // 本地接收时间戳 (微秒)
    char     raw_data[128];      // 原始数据载荷 (JSON 片段)
};

// ======================== 持仓信息 ========================
struct PositionInfo {
    char     symbol[16];         // 交易对
    double   entry_price;        // 开仓均价
    double   stop_loss;          // 当前止损价 (Python 侧动态更新)
    double   take_profit;        // 止盈价 (可选)
    double   size;               // 持仓数量 (正=多，负=空)
    int      position_id;        // 持仓唯一标识 (与 Python 侧一致)
    uint32_t update_seq;         // Python 侧写入时递增的序列号，用于新鲜度校验
    char     reserved[32];       // 预留，可能用于扩展杠杆等信息
};

// ======================== 心跳信号 ========================
struct Heartbeat {
    uint64_t python_counter;    // Python 主进程每 Tick 递增的心跳计数
    uint64_t cpp_counter;       // C++ 守护进程每循环递增的心跳计数
    char     _pad[48];          // 填充至 64 字节，避免伪共享
};

#pragma pack(pop)

#endif // FIRESEED_MARKET_TYPES_H
