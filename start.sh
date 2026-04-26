#!/usr/bin/env bash
#
# 火种系统生产级启动脚本
# 使用方式: ./start.sh [start|stop|restart|status]
#

set -euo pipefail

# ------------------------- 系统配置 -------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 项目名称
PROJECT_NAME="fire_seed"

# Python 虚拟环境路径（若不存在则使用系统 Python）
VENV_DIR="${SCRIPT_DIR}/.venv"
PYTHON="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

# 日志目录
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

# PID 文件目录
PID_DIR="${SCRIPT_DIR}/pids"
mkdir -p "${PID_DIR}"

# 各服务 PID 文件
API_PID="${PID_DIR}/api.pid"
ENGINE_PID="${PID_DIR}/engine.pid"
SHADOW_PID="${PID_DIR}/shadow.pid"
FEED_PID="${PID_DIR}/feed.pid"

# 日志文件
API_LOG="${LOG_DIR}/api.log"
ENGINE_LOG="${LOG_DIR}/engine.log"
SHADOW_LOG="${LOG_DIR}/shadow.log"
FEED_LOG="${LOG_DIR}/feed.log"

# ------------------------- 环境准备 -------------------------
setup_environment() {
    echo "[火种] 检查运行环境..."

    # 检查 Python 版本 (要求 3.10+)
    if ! command -v python3 &> /dev/null; then
        echo "[错误] 未找到 python3，请安装 Python 3.10+"
        exit 1
    fi

    PY_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

    if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10 ]]; then
        echo "[错误] Python 版本过低 ($PY_VERSION)，需要 3.10+"
        exit 1
    fi

    # 创建虚拟环境（若不存在）
    if [[ ! -d "$VENV_DIR" ]]; then
        echo "[火种] 创建 Python 虚拟环境..."
        python3 -m venv "$VENV_DIR"
    fi

    echo "[火种] 安装/更新 Python 依赖..."
    "$PIP" install --upgrade pip -q
    "$PIP" install -r requirements.txt -q

    # 检查并编译 C++ 模块
    BUILD_DIR="${SCRIPT_DIR}/cpp/build"
    if [[ ! -f "${BUILD_DIR}/libfire_core.so" ]] && [[ ! -f "${BUILD_DIR}/fire_core*.so" ]]; then
        echo "[火种] C++ 模块未编译，正在构建..."
        mkdir -p "$BUILD_DIR"
        cd "$BUILD_DIR"
        cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc) || {
            echo "[错误] C++ 模块编译失败，请检查 CMakeLists.txt 和依赖"
            exit 1
        }
        cd "$SCRIPT_DIR"
    fi

    # 初始化数据库（如果需要）
    if [[ ! -f "${SCRIPT_DIR}/data/fire_seed.db" ]]; then
        echo "[火种] 初始化数据库..."
        "$PYTHON" -c "from core.models import init_db; init_db()" || {
            echo "[错误] 数据库初始化失败"
            exit 1
        }
    fi

    # 检查关键配置文件
    if [[ ! -f "config/settings.yaml" ]]; then
        echo "[错误] 缺少 config/settings.yaml，请从 settings.yaml.example 复制并配置"
        exit 1
    fi

    echo "[火种] 环境准备完成"
}

# ------------------------- 启动服务 -------------------------
start_service() {
    local name="$1"
    local cmd="$2"
    local pid_file="$3"
    local log_file="$4"

    if [[ -f "$pid_file" ]]; then
        local old_pid=$(cat "$pid_file")
        if kill -0 "$old_pid" 2>/dev/null; then
            echo "[警告] $name 已在运行 (PID $old_pid)"
            return 1
        else
            rm -f "$pid_file"
        fi
    fi

    echo "[火种] 启动 $name..."
    nohup $cmd >> "$log_file" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$pid_file"
    sleep 0.5
    if kill -0 "$new_pid" 2>/dev/null; then
        echo "[火种] $name 已启动 (PID $new_pid)"
    else
        echo "[错误] $name 启动失败，检查日志: $log_file"
        rm -f "$pid_file"
        return 1
    fi
}

start_api() {
    start_service "API服务" \
        "$PYTHON -m uvicorn api.server:app --host 0.0.0.0 --port 8000 --log-level info" \
        "$API_PID" "$API_LOG"
}

start_engine() {
    start_service "交易引擎" \
        "$PYTHON core/engine.py --config config/settings.yaml" \
        "$ENGINE_PID" "$ENGINE_LOG"
}

start_feed() {
    start_service "数据采集" \
        "$PYTHON core/data_feed.py --config config/settings.yaml" \
        "$FEED_PID" "$FEED_LOG"
}

start_shadow() {
    start_service "幽灵影子验证" \
        "$PYTHON ghost/shadow_manager.py --config config/settings.yaml" \
        "$SHADOW_PID" "$SHADOW_LOG"
}

# ------------------------- 停止服务 -------------------------
stop_service() {
    local name="$1"
    local pid_file="$2"
    local timeout=10

    if [[ -f "$pid_file" ]]; then
        local pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "[火种] 停止 $name (PID $pid)..."
            kill "$pid"
            # 等待进程退出
            for i in $(seq 1 $timeout); do
                if ! kill -0 "$pid" 2>/dev/null; then
                    break
                fi
                sleep 1
            done
            # 强制杀死
            if kill -0 "$pid" 2>/dev/null; then
                echo "[警告] $name 未响应，强制终止..."
                kill -9 "$pid"
            fi
        fi
        rm -f "$pid_file"
    fi
}

stop_all() {
    echo "[火种] 停止所有服务..."
    stop_service "API服务" "$API_PID"
    stop_service "交易引擎" "$ENGINE_PID"
    stop_service "数据采集" "$FEED_PID"
    stop_service "幽灵影子验证" "$SHADOW_PID"
    echo "[火种] 所有服务已停止"
}

# ------------------------- 状态检查 -------------------------
status_service() {
    local name="$1"
    local pid_file="$2"
    if [[ -f "$pid_file" ]]; then
        local pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "[运行] $name (PID $pid)"
        else
            echo "[停止] $name (PID 文件残留)"
        fi
    else
        echo "[停止] $name"
    fi
}

status_all() {
    echo "火种系统服务状态："
    status_service "API服务" "$API_PID"
    status_service "交易引擎" "$ENGINE_PID"
    status_service "数据采集" "$FEED_PID"
    status_service "幽灵影子验证" "$SHADOW_PID"
}

# ------------------------- 主逻辑 -------------------------
case "${1:-start}" in
    start)
        setup_environment
        start_feed       # 先启动数据源，保证引擎有行情
        sleep 2
        start_engine
        start_shadow
        start_api
        echo "================================================"
        echo " 火种系统启动完成"
        echo " 面板地址: http://$(hostname -I | awk '{print $1}'):8000"
        echo " 日志路径: $LOG_DIR"
        echo " 运行 ./start.sh stop 停止系统"
        echo "================================================"
        ;;
    stop)
        stop_all
        ;;
    restart)
        stop_all
        sleep 2
        setup_environment
        start_feed
        sleep 2
        start_engine
        start_shadow
        start_api
        ;;
    status)
        status_all
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
