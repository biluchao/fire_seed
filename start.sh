#!/usr/bin/env bash
# ================================================================
# 火种系统 (FireSeed) 启动脚本
# 用法:
#   ./start.sh              # 使用 systemd 启动（推荐）
#   ./start.sh --direct     # 直接启动（前台运行，适用于调试）
#   ./start.sh --status     # 查看服务状态
# ================================================================
set -euo pipefail

# ----- 颜色定义 -----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ----- 全局变量 -----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_NAME="fire_seed"
SYSTEMD_SERVICE="${PROJECT_NAME}.service"
LOG_DIR="/var/log/${PROJECT_NAME}"
VENV_DIR="${SCRIPT_DIR}/venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
MAIN_SCRIPT="${SCRIPT_DIR}/core/main.py"
ENV_FILE="${SCRIPT_DIR}/.env"

# ----- 辅助函数 -----
log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# 加载环境变量
load_env() {
    if [[ -f "$ENV_FILE" ]]; then
        set -a
        source "$ENV_FILE"
        set +a
    else
        log_error ".env 文件不存在，请先运行 deploy.sh 或手动创建。"
        exit 1
    fi
}

# 检查必需的环境变量
check_env() {
    if [[ -z "${BINANCE_API_KEY:-}" ]]; then
        log_error "BINANCE_API_KEY 未设置，请编辑 .env 文件。"
        exit 1
    fi
}

# 确保依赖服务运行
start_dependencies() {
    log_info "检查并启动依赖服务 (Redis, Docker)..."
    # Redis
    if systemctl is-active --quiet redis-server; then
        log_info "Redis 已运行。"
    else
        log_info "启动 Redis..."
        sudo systemctl start redis-server || {
            log_error "Redis 启动失败，请检查安装。"
            exit 1
        }
    fi

    # Docker (用于进化沙箱)
    if systemctl is-active --quiet docker; then
        log_info "Docker 已运行。"
    else
        log_info "启动 Docker..."
        sudo systemctl start docker || {
            log_error "Docker 启动失败，请检查安装。"
            exit 1
        }
    fi
}

# 检查 Python 虚拟环境
check_venv() {
    if [[ ! -f "$PYTHON_BIN" ]]; then
        log_error "Python 虚拟环境未找到: $PYTHON_BIN。请先运行 deploy.sh。"
        exit 1
    fi
}

# 通过 systemd 启动
start_with_systemd() {
    log_info "通过 systemd 启动服务..."
    if systemctl is-active --quiet "$SYSTEMD_SERVICE"; then
        log_warn "服务已在运行。如需重启，请使用: sudo systemctl restart $SYSTEMD_SERVICE"
        exit 0
    fi
    sudo systemctl start "$SYSTEMD_SERVICE" || {
        log_error "服务启动失败，请检查日志: sudo journalctl -u $SYSTEMD_SERVICE -f"
        exit 1
    }
    sleep 2
    if systemctl is-active --quiet "$SYSTEMD_SERVICE"; then
        log_info "火种系统已启动 (PID: $(systemctl show --property=MainPID --value $SYSTEMD_SERVICE))"
    else
        log_error "服务启动后异常退出。"
        sudo journalctl -u $SYSTEMD_SERVICE --no-pager -n 50
        exit 1
    fi
}

# 直接启动 (用于调试)
start_direct() {
    load_env
    check_env
    start_dependencies
    check_venv

    log_info "直接启动火种系统 (前台模式)..."
    mkdir -p "$LOG_DIR"

    # 启动主进程，日志同时输出到控制台和日志文件
    exec "$PYTHON_BIN" "$MAIN_SCRIPT" 2>&1 | tee -a "$LOG_DIR/fire_seed.log"
}

# 查看状态
show_status() {
    echo "=== 火种系统状态 ==="
    # systemd 服务状态
    if systemctl is-active --quiet "$SYSTEMD_SERVICE"; then
        log_info "systemd 服务: 运行中 (PID: $(systemctl show --property=MainPID --value $SYSTEMD_SERVICE))"
        echo "最近日志:"
        sudo journalctl -u "$SYSTEMD_SERVICE" --no-pager -n 10 --output=cat
    else
        log_warn "systemd 服务: 未运行"
    fi
    echo ""
    # Redis
    if systemctl is-active --quiet redis-server; then
        log_info "Redis: 运行中"
    else
        log_warn "Redis: 未运行"
    fi
    # Docker
    if systemctl is-active --quiet docker; then
        log_info "Docker: 运行中"
    else
        log_warn "Docker: 未运行"
    fi
    # 端口占用
    if ss -tuln | grep -q ':8000'; then
        log_info "Web 面板端口 8000: 监听中"
    else
        log_warn "Web 面板端口 8000: 未监听"
    fi
}

# ----- 主流程 -----
main() {
    local mode="${1:-systemd}"

    case "$mode" in
        --direct)
            load_env
            # 直接模式也需要加载环境
            start_direct
            ;;
        --status)
            show_status
            ;;
        systemd|--systemd)
            load_env
            check_env
            start_dependencies
            start_with_systemd
            ;;
        *)
            echo "用法: $0 [--direct|--status]"
            echo "  (无参数)   使用 systemd 启动 (推荐)"
            echo "  --direct   直接前台运行"
            echo "  --status   查看服务状态"
            exit 1
            ;;
    esac
}

main "$@"
