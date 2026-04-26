#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# ==========================================================================
# 火种系统 · 生产级 stop.sh
# 优雅停止所有火种服务（引擎、web面板、C++子进程、学习守卫等）
# ==========================================================================

set -o pipefail

# --------------- 配置 ---------------
PROJECT_NAME="fire_seed"
PID_DIR="/var/run/${PROJECT_NAME}"
LOG_DIR="/var/log/${PROJECT_NAME}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

# 服务组件名称（与 supervisord 或启动脚本对应）
COMPONENTS=(
    "fire-engine"
    "fire-web"
    "fire-ghost"
    "fire-cpp"
    "fire-learning"
    "fire-ota"
)

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# --------------- 函数定义 ---------------
log() {
    echo -e "$(date '+%Y-%m-%d %H:%M:%S') - $1"
}

success() {
    log "${GREEN}[OK]${NC} $1"
}

warn() {
    log "${YELLOW}[WARN]${NC} $1"
}

error() {
    log "${RED}[ERROR]${NC} $1"
}

# 检查进程是否存活
is_running() {
    local component=$1
    if [ -f "${PID_DIR}/${component}.pid" ]; then
        local pid
        pid=$(cat "${PID_DIR}/${component}.pid" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    # 后备：通过进程名查找 (慎用，可能误杀)
    return 1
}

# 优雅停止单个组件
stop_component() {
    local component=$1
    local timeout=${2:-30}  # 默认超时30秒
    local pid

    if [ -f "${PID_DIR}/${component}.pid" ]; then
        pid=$(cat "${PID_DIR}/${component}.pid" 2>/dev/null)
        if [ -z "$pid" ]; then
            warn "${component}: PID 文件为空，尝试通过名称清理"
            pkill -f "${component}" 2>/dev/null && success "${component}: 已通过名称停止" || warn "${component}: 未找到运行实例"
            return
        fi
    else
        warn "${component}: 无 PID 文件，跳过"
        return
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
        warn "${component}: PID ${pid} 不存在，清理残留 PID 文件"
        rm -f "${PID_DIR}/${component}.pid"
        return
    fi

    log "正在停止 ${component} (PID: ${pid})..."

    # 发送 SIGTERM
    kill -TERM "$pid" 2>/dev/null

    # 等待进程退出
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [ $waited -lt $timeout ]; do
        sleep 1
        ((waited++))
    done

    if kill -0 "$pid" 2>/dev/null; then
        error "${component}: 超时未退出，发送 SIGKILL"
        kill -KILL "$pid" 2>/dev/null
        sleep 2
        if kill -0 "$pid" 2>/dev/null; then
            error "${component}: SIGKILL 也无法终止进程，请手动检查"
        else
            success "${component}: 已强制终止"
        fi
    else
        success "${component}: 已停止"
    fi

    # 清理PID文件
    rm -f "${PID_DIR}/${component}.pid"
}

# 清理共享内存段（如果有）
cleanup_ipc() {
    log "清理共享内存..."
    # 删除火种相关的共享内存段 (假设 key 为固定值或通过 ipcs 查找)
    ipcs -m | awk -v user="${USER}" '$3 == user && $5 ~ /fire_seed/ {print $2}' | xargs -r -I {} ipcrm -m {}
    # 清理信号量
    ipcs -s | awk -v user="${USER}" '$3 == user && $5 ~ /fire_seed/ {print $2}' | xargs -r -I {} ipcrm -s {}
    success "共享内存清理完毕"
}

# 最终状态检查
final_status() {
    local all_down=true
    for comp in "${COMPONENTS[@]}"; do
        if is_running "$comp"; then
            error "${comp}: 仍在运行！"
            all_down=false
        fi
    done
    if $all_down; then
        success "火种系统所有组件已停止"
    else
        error "部分组件未能停止，请手动干预"
        return 1
    fi
}

# --------------- 主流程 ---------------
echo ""
echo "=========================================="
echo "  火种系统 · 停止脚本"
echo "=========================================="
echo ""

# 确认操作（可跳过，如果带 -f 参数）
if [[ "$1" != "-f" ]]; then
    echo -n "确定要停止火种所有服务吗？(y/N): "
    read -r confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        log "用户取消操作"
        exit 0
    fi
fi

# 创建日志目录（如果不存在）
mkdir -p "${LOG_DIR}"

log "开始停止火种系统..."

# 按照依赖顺序停止：先停引擎，再停其他
STOP_ORDER=(
    "fire-engine"
    "fire-ghost"
    "fire-cpp"
    "fire-learning"
    "fire-web"
    "fire-ota"
)

for comp in "${STOP_ORDER[@]}"; do
    stop_component "$comp"
    sleep 1
done

# 清理残留（可能通过 start.sh 启动的非守护进程）
log "清理可能残留的进程..."
pkill -f "fire_seed/core/engine.py" 2>/dev/null && warn "发现残留引擎进程，已清理"
pkill -f "uvicorn.*fire_seed" 2>/dev/null && warn "发现残留 web 面板，已清理"

# 清理IPC资源
cleanup_ipc

# 最后检查
final_status
exit $?
