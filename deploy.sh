#!/usr/bin/env bash
# ================================================================
# 火种系统 (FireSeed) 一键部署脚本
# 适用于 Ubuntu 22.04 / 24.04 LTS x86_64
# 功能：
#   1. 环境检测与系统依赖安装
#   2. Python 虚拟环境创建与 pip 依赖安装
#   3. C++ 高性能模块编译 (pybind11)
#   4. 配置文件初始化 (含智能体世界观的默认配置)
#   5. SQLite 数据库初始化
#   6. 日志轮转配置
#   7. 系统服务安装 (systemd)
#   8. 最终健康检查
# ================================================================
set -euo pipefail

# ----- 颜色定义 -----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# ----- 全局变量 -----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_NAME="fire_seed"
PROJECT_USER="${SUDO_USER:-$USER}"
PYTHON_BIN="python3.12"
VENV_DIR="$SCRIPT_DIR/venv"
LOG_DIR="/var/log/$PROJECT_NAME"
CONFIG_DIR="$SCRIPT_DIR/config"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"
CMAKE_BUILD_DIR="$SCRIPT_DIR/cpp/build"

# ----- 辅助函数 -----
log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "${CYAN}[STEP]${NC} $*"; }

# 判断是否以 root 运行
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "请使用 sudo 运行此脚本，例如: sudo bash deploy.sh"
        exit 1
    fi
}

# 检查 Ubuntu 版本
check_os() {
    if [[ ! -f /etc/os-release ]]; then
        log_error "无法识别操作系统，需要 Ubuntu 22.04 或 24.04"
        exit 1
    fi
    source /etc/os-release
    if [[ "$ID" != "ubuntu" ]]; then
        log_error "此脚本仅支持 Ubuntu，当前系统: $ID"
        exit 1
    fi
    local major_ver="${VERSION_ID%%.*}"
    if [[ "$major_ver" -lt 22 ]]; then
        log_warn "推荐 Ubuntu 22.04 或更高版本，当前版本: $VERSION_ID"
        read -p "是否继续？(y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then exit 1; fi
    fi
    log_info "操作系统检查通过: $PRETTY_NAME"
}

# 检查硬件资源 (内存至少 6GB，CPU 至少 4 核)
check_hardware() {
    local mem_total_kb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    local mem_total_gb=$((mem_total_kb / 1024 / 1024))
    local cpu_cores=$(nproc)
    if [[ $mem_total_gb -lt 6 ]]; then
        log_warn "内存为 ${mem_total_gb}GB，建议至少 8GB。继续可能出现性能问题。"
    fi
    if [[ $cpu_cores -lt 4 ]]; then
        log_warn "CPU 核心数为 ${cpu_cores}，建议至少 4 核。"
    fi
    log_info "硬件资源: CPU ${cpu_cores} 核, 内存 ${mem_total_gb}GB"
}

# 安装系统包
install_system_deps() {
    log_step "更新 apt 源并安装系统依赖..."
    apt-get update -qq
    apt-get install -y -qq \
        build-essential \
        cmake \
        g++ \
        gdb \
        make \
        pkg-config \
        python3.12 \
        python3.12-dev \
        python3.12-venv \
        libssl-dev \
        libffi-dev \
        libbz2-dev \
        libreadline-dev \
        libsqlite3-dev \
        libncursesw5-dev \
        libgdbm-dev \
        liblzma-dev \
        zlib1g-dev \
        uuid-dev \
        libpython3.12 \
        docker.io \
        redis-server \
        logrotate \
        curl \
        git \
        ufw \
        > /dev/null
    log_info "系统依赖安装完成。"
    # 确保 docker 服务启动
    systemctl enable --now docker || true
    systemctl enable --now redis-server || true
}

# 配置 Python 环境
setup_python() {
    log_step "配置 Python 环境..."
    if ! command -v python3.12 &> /dev/null; then
        log_error "Python 3.12 未安装，请检查。"
        exit 1
    fi
    log_info "Python: $(python3.12 --version)"

    # 创建虚拟环境
    if [[ ! -d "$VENV_DIR" ]]; then
        python3.12 -m venv "$VENV_DIR"
        log_info "虚拟环境创建于 $VENV_DIR"
    else
        log_info "虚拟环境已存在，跳过创建。"
    fi

    # 激活并升级 pip
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip setuptools wheel
    log_info "pip 升级完成。"
}

# 安装 Python 依赖
install_python_deps() {
    log_step "安装 Python 依赖..."
    source "$VENV_DIR/bin/activate"
    if [[ -f "$REQUIREMENTS" ]]; then
        pip install -r "$REQUIREMENTS"
        log_info "Python 依赖安装完成。"
    else
        log_error "找不到 requirements.txt"
        exit 1
    fi
}

# 编译 C++ 模块
build_cpp() {
    log_step "编译 C++ 高性能模块..."
    mkdir -p "$CMAKE_BUILD_DIR"
    cd "$CMAKE_BUILD_DIR"
    cmake .. -DCMAKE_BUILD_TYPE=Release
    make -j$(nproc)
    log_info "C++ 模块编译完成。"
    cd "$SCRIPT_DIR"
}

# 初始化配置文件
init_config() {
    log_step "初始化配置文件..."
    # .env 文件
    if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
        if [[ -f "$SCRIPT_DIR/.env.example" ]]; then
            cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
            log_warn ".env 文件已从 .env.example 创建，请编辑填入实际 API 密钥。"
        else
            touch "$SCRIPT_DIR/.env"
            log_warn "生成空 .env 文件，请手动配置 API 密钥。"
        fi
    else
        log_info ".env 文件已存在，跳过。"
    fi

    # 确保配置目录存在
    mkdir -p "$CONFIG_DIR"

    # settings.yaml (如果不存在则从例子复制)
    if [[ ! -f "$CONFIG_DIR/settings.yaml" ]]; then
        if [[ -f "$CONFIG_DIR/settings.yaml.example" ]]; then
            cp "$CONFIG_DIR/settings.yaml.example" "$CONFIG_DIR/settings.yaml"
        else
            # 生成默认 settings.yaml
            cat > "$CONFIG_DIR/settings.yaml" << 'YAMLEOF'
# 火种系统默认配置 (自动生成)
system:
  mode: virtual
  strategy_mode: moderate
  timezone: "Asia/Shanghai"
  log_level: INFO

learning:
  enabled: true
  start_time: "01:30"
  end_time: "04:30"

adversarial_council:
  enabled: true
  cooling_off_minutes: 30
  anti_consensus_boost: 2.0
  min_jury_members: 3
YAMLEOF
        fi
        log_info "已生成默认 settings.yaml"
    else
        # 检查是否需要补充缺失的配置段
        if ! grep -q 'adversarial_council' "$CONFIG_DIR/settings.yaml"; then
            cat >> "$CONFIG_DIR/settings.yaml" << 'YAMLEOF'

# 对抗式议会配置 (自动追加)
adversarial_council:
  enabled: true
  cooling_off_minutes: 30
  anti_consensus_boost: 2.0
  min_jury_members: 3
YAMLEOF
            log_info "已将对抗式议会配置追加到 settings.yaml"
        fi
    fi

    # agent_rewards.yaml (默认)
    if [[ ! -f "$CONFIG_DIR/agent_rewards.yaml" ]]; then
        cat > "$CONFIG_DIR/agent_rewards.yaml" << 'YAMLEOF'
# 火种智能体极端化奖励函数 (自动生成)
global:
  performance_window_days: 20
  adaptive_weights: false
  min_voting_weight: 0.03

agents:
  sentinel:
    description: "监察者·机械唯物主义"
    reward_components:
      - name: detection_rate
        weight: 0.5
        goal: maximize
      - name: false_alarm_rate
        weight: -0.5
        goal: minimize

  alchemist:
    description: "炼金术士·进化论"
    reward_components:
      - name: sharpe_ratio
        weight: 0.4
        goal: maximize
      - name: strategy_novelty
        weight: 0.2
        goal: maximize

  guardian:
    description: "守护者·存在主义"
    reward_components:
      - name: max_drawdown
        weight: -0.6
        goal: minimize
      - name: cvar_99
        weight: -0.4
        goal: minimize

  devils_advocate:
    description: "魔鬼代言人·怀疑论"
    reward_components:
      - name: adversarial_success
        weight: 1.0
        goal: maximize

  godel_watcher:
    description: "哥德尔监视者·不完备定理"
    reward_components:
      - name: doubt_accuracy
        weight: 0.7
        goal: maximize
      - name: opportunity_cost
        weight: -0.3
        goal: minimize

  env_inspector:
    description: "环境检察官·物理主义"
    reward_components:
      - name: uptime_pct
        weight: 0.3
        goal: maximize
      - name: avg_latency_ms
        weight: -0.4
        goal: minimize

  redundancy_auditor:
    description: "冗余审计官·奥卡姆剃刀"
    reward_components:
      - name: dead_code_ratio
        weight: -0.5
        goal: minimize
      - name: unused_config_keys
        weight: -0.3
        goal: minimize

  weight_calibrator:
    description: "权重校准师·贝叶斯主义"
    reward_components:
      - name: out_of_sample_sharpe
        weight: 0.6
        goal: maximize
      - name: in_sample_sharpe_gap
        weight: -0.4
        goal: minimize

  narrator:
    description: "叙事官·诠释学"
    reward_components:
      - name: human_feedback_score
        weight: 0.7
        goal: maximize
      - name: report_generation_time
        weight: -0.3
        goal: minimize

  diversity_enforcer:
    description: "多样性强制者·多元主义"
    reward_components:
      - name: agent_weight_entropy
        weight: 0.6
        goal: maximize
      - name: prediction_diversity
        weight: 0.4
        goal: minimize

  archive_guardian:
    description: "归档审计官·历史主义"
    reward_components:
      - name: archived_ratio
        weight: 0.8
        goal: maximize
      - name: missing_backups
        weight: -0.2
        goal: minimize

  copy_trade_coordinator:
    description: "跟单协调官·整体论"
    reward_components:
      - name: sync_success_rate
        weight: 0.9
        goal: maximize
      - name: sync_delay_sec
        weight: -0.1
        goal: minimize
YAMLEOF
        log_info "已生成默认 agent_rewards.yaml"
    fi

    # 确保其他配置文件存在（若无则提供默认）
    for cfg_file in risk_limits.yaml strategy_params.yaml weights.yaml cloud_storage.yaml multi_account.yaml human_constraints.yaml; do
        if [[ ! -f "$CONFIG_DIR/$cfg_file" ]]; then
            touch "$CONFIG_DIR/$cfg_file"
            log_warn "$cfg_file 缺失，已生成空文件，请手动配置。"
        fi
    done
}

# 初始化数据库
init_database() {
    log_step "初始化 SQLite 数据库..."
    source "$VENV_DIR/bin/activate"
    python3 -c "
import sqlite3, os
os.makedirs('$SCRIPT_DIR/data', exist_ok=True)
db_path = os.path.join('$SCRIPT_DIR', 'data', 'fire_seed.db')
conn = sqlite3.connect(db_path)
conn.execute('''CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    ts REAL,
    level TEXT,
    event_type TEXT,
    module TEXT,
    content TEXT,
    snapshot TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)''')
conn.execute('''CREATE TABLE IF NOT EXISTS archive_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT UNIQUE,
    cloud_key TEXT,
    uploaded_at REAL
)''')
conn.execute('''CREATE TABLE IF NOT EXISTS elo_rankings (
    strategy_id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    elo REAL DEFAULT 1500.0,
    matches INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    draws INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    streak INTEGER DEFAULT 0,
    best_elo REAL DEFAULT 1500.0,
    last_updated TEXT DEFAULT (datetime('now')),
    created_at TEXT DEFAULT (datetime('now'))
)''')
conn.execute('''CREATE TABLE IF NOT EXISTS match_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_a TEXT NOT NULL,
    strategy_b TEXT NOT NULL,
    score_a REAL NOT NULL,
    score_b REAL NOT NULL,
    elo_change_a REAL NOT NULL,
    elo_change_b REAL NOT NULL,
    timestamp TEXT DEFAULT (datetime('now'))
)''')
conn.commit()
conn.close()
print('数据库初始化完成:', db_path)
" || log_warn "数据库初始化失败，请检查 Python 环境。"
}

# 配置日志轮转
setup_logrotate() {
    log_step "配置日志轮转..."
    mkdir -p "$LOG_DIR"
    chown -R "$PROJECT_USER:$PROJECT_USER" "$LOG_DIR" 2>/dev/null || true
    cat > /etc/logrotate.d/fire_seed <<EOF
$LOG_DIR/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    maxsize 100M
}
EOF
    log_info "日志轮转配置已写入 /etc/logrotate.d/fire_seed"
}

# 安装 systemd 服务
install_service() {
    log_step "安装 systemd 服务..."
    cat > /etc/systemd/system/fire_seed.service <<EOF
[Unit]
Description=FireSeed Quant Trading System
After=network.target redis-server.service docker.service
Requires=redis-server.service

[Service]
Type=simple
User=$PROJECT_USER
WorkingDirectory=$SCRIPT_DIR
EnvironmentFile=$SCRIPT_DIR/.env
ExecStart=$VENV_DIR/bin/python $SCRIPT_DIR/core/main.py
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable fire_seed.service
    log_info "systemd 服务已安装并启用 (fire_seed.service)"
}

# 创建数据目录并调整权限
setup_permissions() {
    log_step "设置文件权限..."
    chown -R "$PROJECT_USER:$PROJECT_USER" "$SCRIPT_DIR" 2>/dev/null || true
    mkdir -p "$SCRIPT_DIR/data"
    chmod 755 "$SCRIPT_DIR"
    log_info "权限设置完成。"
}

# 最后健康检查
final_check() {
    log_step "执行最终健康检查..."
    local errors=0

    # 检查 Python 虚拟环境
    if [[ ! -f "$VENV_DIR/bin/python" ]]; then
        log_error "Python 虚拟环境缺失"
        ((errors++))
    fi

    # 检查 C++ 编译产物（若有 CMakeLists.txt 则检查编译结果）
    if [[ -f "$SCRIPT_DIR/CMakeLists.txt" ]]; then
        if [[ ! -f "$CMAKE_BUILD_DIR/fire_seed_cpp.so" ]] && [[ ! -f "$CMAKE_BUILD_DIR/libfire_seed_cpp.so" ]]; then
            log_warn "未检测到 C++ 编译产物，请检查编译是否成功。"
        fi
    fi

    # 检查 .env 是否已配置
    if [[ -f "$SCRIPT_DIR/.env" ]]; then
        source "$SCRIPT_DIR/.env"
        if [[ -z "${BINANCE_API_KEY:-}" ]]; then
            log_warn "BINANCE_API_KEY 未设置，系统将无法连接交易所。"
        fi
    else
        log_error ".env 文件不存在"
        ((errors++))
    fi

    if [[ $errors -eq 0 ]]; then
        log_info "所有检查通过，系统已准备就绪。"
    else
        log_warn "存在 $errors 个问题，请检查日志并修正。"
    fi
}

# ----- 主流程 -----
main() {
    echo -e "${CYAN}"
    echo "======================================="
    echo "  火种量化系统 - 一键部署"
    echo "  FireSeed v3.0.0-spartan"
    echo "======================================="
    echo -e "${NC}"

    check_root
    check_os
    check_hardware

    # 询问是否继续
    read -p "按回车开始部署，或 Ctrl+C 取消..."

    install_system_deps
    setup_python
    install_python_deps
    build_cpp
    init_config
    init_database
    setup_logrotate
    setup_permissions
    install_service
    final_check

    echo ""
    echo -e "${GREEN}======================================"
    echo "  部署完成！"
    echo "  请编辑 .env 填入交易所 API 密钥"
    echo "  启动服务: sudo systemctl start fire_seed"
    echo "  查看状态: sudo systemctl status fire_seed"
    echo "  访问面板: http://<服务器IP>:8000"
    echo "======================================${NC}"
}

main "$@"
