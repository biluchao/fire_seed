#!/bin/bash
# =============================================================================
# 火种系统 一键部署脚本 (Fire Seed Deploy Script)
# 适用环境: Ubuntu 22.04 LTS, 4核8G
# 执行方式: chmod +x deploy.sh && sudo ./deploy.sh
# =============================================================================

set -euo pipefail

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 日志文件
LOGFILE="/var/log/fire_seed_deploy.log"

# 系统用户 (推荐非root运行，但脚本需root权限进行部分操作)
SERVICE_USER="fire_seed"
SERVICE_GROUP="fire_seed"
INSTALL_DIR="/opt/fire_seed"
VENV_DIR="${INSTALL_DIR}/venv"
CONFIG_DIR="${INSTALL_DIR}/config"
DATA_DIR="/var/lib/fire_seed"
LOG_DIR="/var/log/fire_seed"

# 需要的系统包
SYSTEM_PACKAGES=(
    python3.10 python3.10-venv python3.10-dev
    cmake gcc g++ make
    libboost-all-dev libeigen3-dev
    libpython3.10-dev
    docker.io docker-compose
    supervisor nginx
    curl wget git
    libssl-dev
)

echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}       火种系统 一键部署脚本开始${NC}"
echo -e "${BLUE}============================================${NC}"

# ---------------------------------------------------------------------------
# 1. 环境检查
# ---------------------------------------------------------------------------
check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo -e "${RED}[错误] 请使用 sudo 运行此脚本。${NC}"
        exit 1
    fi
}

check_ubuntu_version() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        if [[ "$ID" != "ubuntu" || "$VERSION_ID" != "22.04" ]]; then
            echo -e "${YELLOW}[警告] 推荐 Ubuntu 22.04，当前为 $ID $VERSION_ID，可能不兼容。${NC}"
        fi
    else
        echo -e "${YELLOW}[警告] 无法检测操作系统版本。${NC}"
    fi
}

check_resources() {
    CPU_CORES=$(nproc)
    TOTAL_MEM=$(free -m | awk '/^Mem:/{print $2}')
    DISK_FREE=$(df -BG / | awk 'NR==2 {print $4}' | sed 's/G//')
    echo -e "${GREEN}[信息] CPU核心: $CPU_CORES${NC}"
    echo -e "${GREEN}[信息] 内存: ${TOTAL_MEM}MB${NC}"
    echo -e "${GREEN}[信息] 可用磁盘: ${DISK_FREE}GB${NC}"

    if [[ $CPU_CORES -lt 4 ]]; then
        echo -e "${YELLOW}[警告] 推荐4核以上CPU，当前为$CPU_CORES核，可能影响性能。${NC}"
    fi
    if [[ $TOTAL_MEM -lt 7500 ]]; then
        echo -e "${YELLOW}[警告] 推荐8GB以上内存，当前为${TOTAL_MEM}MB。${NC}"
    fi
    if [[ $DISK_FREE -lt 20 ]]; then
        echo -e "${YELLOW}[警告] 可用磁盘空间不足20GB，请清理后再试。${NC}"
    fi
}

# ---------------------------------------------------------------------------
# 2. 安装系统依赖
# ---------------------------------------------------------------------------
install_system_deps() {
    echo -e "${BLUE}[步骤 1/7] 更新系统包并安装依赖...${NC}"
    apt-get update -qq
    apt-get install -y -qq "${SYSTEM_PACKAGES[@]}"

    # 启动Docker服务
    systemctl start docker
    systemctl enable docker

    echo -e "${GREEN}[完成] 系统依赖已安装。${NC}"
}

# ---------------------------------------------------------------------------
# 3. 创建系统用户和目录
# ---------------------------------------------------------------------------
create_user_and_dirs() {
    echo -e "${BLUE}[步骤 2/7] 创建服务用户和目录...${NC}"

    # 创建服务用户（如果不存在）
    if ! id -u $SERVICE_USER >/dev/null 2>&1; then
        useradd -r -m -d /home/$SERVICE_USER -s /bin/bash $SERVICE_USER
        echo -e "${GREEN}[信息] 用户 $SERVICE_USER 已创建。${NC}"
    fi

    # 创建必要目录
    mkdir -p $INSTALL_DIR $CONFIG_DIR $DATA_DIR $LOG_DIR
    mkdir -p ${INSTALL_DIR}/cpp/build
    mkdir -p ${INSTALL_DIR}/strategies
    mkdir -p ${INSTALL_DIR}/tests

    # 设置权限
    chown -R $SERVICE_USER:$SERVICE_GROUP $INSTALL_DIR
    chown -R $SERVICE_USER:$SERVICE_GROUP $DATA_DIR
    chown -R $SERVICE_USER:$SERVICE_GROUP $LOG_DIR

    echo -e "${GREEN}[完成] 目录结构已创建。${NC}"
}

# ---------------------------------------------------------------------------
# 4. 部署应用代码
# ---------------------------------------------------------------------------
deploy_code() {
    echo -e "${BLUE}[步骤 3/7] 部署应用代码...${NC}"

    # 假设脚本在项目根目录执行
    SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
    if [[ "$SCRIPT_DIR" != "$INSTALL_DIR" ]]; then
        cp -r "$SCRIPT_DIR"/* "$INSTALL_DIR"/
    fi

    # 确保关键脚本存在
    if [[ ! -f "$INSTALL_DIR/start.sh" ]]; then
        echo -e "${YELLOW}[警告] start.sh 不存在，将自动创建。${NC}"
        cat > "$INSTALL_DIR/start.sh" << 'EOF'
#!/bin/bash
cd /opt/fire_seed
source venv/bin/activate
python core/engine.py &
EOF
        chmod +x "$INSTALL_DIR/start.sh"
    fi

    chown -R $SERVICE_USER:$SERVICE_GROUP "$INSTALL_DIR"
    echo -e "${GREEN}[完成] 代码部署至 $INSTALL_DIR。${NC}"
}

# ---------------------------------------------------------------------------
# 5. 配置Python虚拟环境与依赖
# ---------------------------------------------------------------------------
setup_python_env() {
    echo -e "${BLUE}[步骤 4/7] 配置Python虚拟环境...${NC}"

    # 创建虚拟环境
    python3.10 -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"

    # 升级pip
    pip install --upgrade pip setuptools wheel

    # 安装Python依赖
    if [[ -f "$INSTALL_DIR/requirements.txt" ]]; then
        pip install -r "$INSTALL_DIR/requirements.txt"
    else
        echo -e "${YELLOW}[警告] requirements.txt 未找到，安装核心依赖。${NC}"
        pip install numpy pandas scipy scikit-learn torch pybind11 \
            ccxt fastapi uvicorn websockets pyyaml python-dotenv bcrypt
    fi

    deactivate
    echo -e "${GREEN}[完成] Python环境配置完毕。${NC}"
}

# ---------------------------------------------------------------------------
# 6. 编译C++模块
# ---------------------------------------------------------------------------
compile_cpp() {
    echo -e "${BLUE}[步骤 5/7] 编译C++模块...${NC}"

    BUILD_DIR="$INSTALL_DIR/cpp/build"
    mkdir -p "$BUILD_DIR"
    cd "$BUILD_DIR"

    cmake .. -DCMAKE_BUILD_TYPE=Release -DPython3_ROOT_DIR="$VENV_DIR"
    make -j$(nproc)

    cd "$INSTALL_DIR"
    echo -e "${GREEN}[完成] C++模块编译完成。${NC}"
}

# ---------------------------------------------------------------------------
# 7. 配置系统参数
# ---------------------------------------------------------------------------
configure_system() {
    echo -e "${BLUE}[步骤 6/7] 配置系统参数...${NC}"

    # 交互式设置API密钥（如果尚未配置）
    SETTINGS_FILE="$CONFIG_DIR/settings.yaml"
    if [[ ! -f "$SETTINGS_FILE" ]]; then
        echo -e "${YELLOW}[配置] 未发现 settings.yaml，创建默认配置。${NC}"
        cat > "$SETTINGS_FILE" << EOF
# 火种全局配置
exchange:
  binance:
    api_key: "YOUR_API_KEY"
    api_secret: "YOUR_API_SECRET"
    testnet: false
  bybit:  # 备用
    api_key: ""
    api_secret: ""
strategy_mode: "moderate"   # aggressive / moderate
learning_time:
  start_hour: 1
  start_minute: 30
  end_hour: 4
  end_minute: 30
risk_limits:
  max_drawdown: 0.15
  daily_loss_limit: 0.05
EOF
        echo -e "${YELLOW}[注意] 请编辑 $SETTINGS_FILE 填入API密钥。${NC}"
    fi

    # 设置前端操作密码
    AUTH_FILE="$CONFIG_DIR/auth.yaml"
    if [[ ! -f "$AUTH_FILE" ]]; then
        read -sp "请设置前端操作密码: " FRONT_PASS
        echo
        # 使用Python生成bcrypt哈希
        source "$VENV_DIR/bin/activate"
        HASH=$(python -c "import bcrypt; print(bcrypt.hashpw('$FRONT_PASS'.encode(), bcrypt.gensalt()).decode())")
        deactivate
        cat > "$AUTH_FILE" << EOF
# 前端操作密码哈希
password_hash: "$HASH"
EOF
        echo -e "${GREEN}[完成] 密码已设置。${NC}"
    fi

    # 启用日志轮转
    cat > /etc/logrotate.d/fire_seed << EOF
/var/log/fire_seed/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 640 $SERVICE_USER $SERVICE_GROUP
    postrotate
        systemctl reload fire_seed 2>/dev/null || true
    endscript
}
EOF

    # 内核参数优化 (大页内存需要硬件支持)
    if grep -q "hugepages" /proc/cpuinfo; then
        echo "vm.nr_hugepages = 512" >> /etc/sysctl.conf
        sysctl -p
    fi

    echo -e "${GREEN}[完成] 系统配置完成。${NC}"
}

# ---------------------------------------------------------------------------
# 8. 启动服务
# ---------------------------------------------------------------------------
setup_service() {
    echo -e "${BLUE}[步骤 7/7] 配置systemd服务并启动...${NC}"

    # 创建 systemd 服务文件
    cat > /etc/systemd/system/fire_seed.service << EOF
[Unit]
Description=Fire Seed Trading System
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_DIR
ExecStart=/bin/bash $INSTALL_DIR/start.sh
ExecStop=/bin/bash $INSTALL_DIR/stop.sh
Restart=on-failure
RestartSec=10
LimitNOFILE=65536
CPUQuota=80%
MemoryMax=7G

[Install]
WantedBy=multi-user.target
EOF

    # 重载systemd
    systemctl daemon-reload
    systemctl enable fire_seed.service
    systemctl start fire_seed.service

    echo -e "${GREEN}[完成] 火种系统已启动。${NC}"
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
main() {
    check_root
    check_ubuntu_version
    check_resources

    install_system_deps
    create_user_and_dirs
    deploy_code
    setup_python_env
    compile_cpp
    configure_system
    setup_service

    # 输出访问信息
    IP_ADDR=$(hostname -I | awk '{print $1}')
    echo -e "\n${GREEN}============================================${NC}"
    echo -e "${GREEN}       火种系统部署成功！${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo -e "${BLUE}访问地址: http://${IP_ADDR}:8000${NC}"
    echo -e "${BLUE}日志路径: $LOG_DIR${NC}"
    echo -e "${BLUE}可用命令:${NC}"
    echo -e "${BLUE}  查看状态: systemctl status fire_seed${NC}"
    echo -e "${BLUE}  停止服务: systemctl stop fire_seed${NC}"
    echo -e "${BLUE}  启动服务: systemctl start fire_seed${NC}"
    echo -e "${BLUE}  查看日志: journalctl -u fire_seed -f${NC}"
    echo -e "${GREEN}============================================${NC}"
}

# 执行主函数
main "$@" 2>&1 | tee "$LOGFILE"
