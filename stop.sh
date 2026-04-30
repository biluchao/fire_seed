#!/usr/bin/env bash
# ================================================================
# 火种系统 (FireSeed) 停止脚本
# 用法:
#   ./stop.sh              # 停止 systemd 服务（默认）
#   ./stop.sh --direct     # 停止直接启动的进程
#   ./stop.sh --all        # 停止所有相关进程（清理残留）
# ================================================================
set -euo pipefail

# ----- 颜色定义 -----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ----- 全局变量 -----
PROJECT_NAME="fire_seed"
SYSTEMD_SERVICE="${PROJECT_NAME}.service"
MAIN_PID_FILE="/tmp/${PROJECT_NAME}.pid"

# ----- 辅助函数 -----
log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# 等待进程退出，超时后强制终止
wait_or_kill() {
    local pid=$1
    local timeout=$2
    local signal=${3:-TERM}
    local count=0
    while kill -0 "$pid" 2>/dev/null && [[ $count -lt $timeout ]]; do
        sleep 0.1
        ((count++))
    done
    if kill -0 "$pid" 2>/dev/null; then
        log_warn "进程 $pid 未响应 SIG${signal}，强制终止..."
        kill -9 "$pid" 2>/dev/null || true
    fi
}

# 停止 systemd 服务
stop_systemd() {
    if systemctl is-active --quiet "$SYSTEMD_SERVICE"; then
        log_info "正在停止 systemd 服务 ${SYSTEMD_SERVICE}..."
        sudo systemctl stop "$SYSTEMD_SERVICE"
        sleep 2
        if systemctl is-active --quiet "$SYSTEMD_SERVICE"; then
            log_error "服务停止失败，请手动检查: sudo systemctl status $SYSTEMD_SERVICE"
            exit 1
        else
            log_info "systemd 服务已停止。"
        fi
    else
        log_info "systemd 服务未在运行。"
    fi
}

# 停止直接启动的进程 (通过 PID 文件)
stop_direct_by_pid() {
    if [[ -f "$MAIN_PID_FILE" ]]; then
        local pid
        pid=$(cat "$MAIN_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            log_info "正在停止直接启动的进程 (PID: $pid)..."
            kill -TERM "$pid" 2>/dev/null || true
            wait_or_kill "$pid" 100 TERM
            log_info "进程 $pid 已终止。"
        else
            log_warn "PID 文件存在但进程 $pid 不存在，可能已退出。"
        fi
        rm -f "$MAIN_PID_FILE"
    else
        log_info "未发现 PID 文件，尝试通过进程名查找..."
        stop_by_name
    fi
}

# 通过进程名查找并停止
stop_by_name() {
    local pids
    pids=$(pgrep -f "core/main.py" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        log_warn "发现残留的火种进程: $pids"
        for pid in $pids; do
            log_info "发送 SIGTERM 到 PID $pid..."
            kill -TERM "$pid" 2>/dev/null || true
            wait_or_kill "$pid" 100 TERM
        done
        log_info "残留进程已清理。"
    else
        log_info "未发现运行中的火种进程。"
    fi
}

# 停止所有相关进程（直接启动 + systemd + 残留）
stop_all() {
    log_info "执行全面停止..."
    # 先尝试 systemd
    if systemctl is-active --quiet "$SYSTEMD_SERVICE"; then
        stop_systemd
    fi
    # 再通过 PID 文件
    if [[ -f "$MAIN_PID_FILE" ]]; then
        stop_direct_by_pid
    fi
    # 最后再清理残留
    stop_by_name
}

# ----- 主流程 -----
main() {
    local mode="${1:-systemd}"

    case "$mode" in
        --direct)
            stop_direct_by_pid
            ;;
        --all)
            stop_all
            ;;
        systemd|--systemd)
            stop_systemd
            ;;
        *)
            echo "用法: $0 [--direct|--all]"
            echo "  (无参数)   停止 systemd 服务"
            echo "  --direct   停止直接启动的进程 (通过 PID 文件)"
            echo "  --all      强制停止所有相关进程"
            exit 1
            ;;
    esac

    # 如果没有任何进程运行，确认状态
    if ! pgrep -f "core/main.py" > /dev/null 2>&1 && ! systemctl is-active --quiet "$SYSTEMD_SERVICE" 2>/dev/null; then
        log_info "火种系统已完全停止。"
    fi
}

main "$@"
