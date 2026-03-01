#!/bin/bash
# Cronjob 服务部署脚本
# 使用方式: curl -sSL https://raw.githubusercontent.com/bezhai/chiwei-platform/main/apps/cloud/cronjob/deploy.sh | bash

set -e

# 配置
DOCKER_IMAGE_NAME="chiwiio/cronjob-work:latest"
DOCKER_CONTAINER_NAME="chiwei_cronjob"
NETWORK="cloud_default"
ENV_FILE=".env"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查 .env 文件
check_env() {
    if [ ! -f "$ENV_FILE" ]; then
        log_error ".env 文件不存在，请先创建 .env 文件"
        exit 1
    fi
    log_info ".env 文件检查通过"
}

# 拉取最新镜像
pull_image() {
    log_info "拉取最新镜像: $DOCKER_IMAGE_NAME"
    docker pull "$DOCKER_IMAGE_NAME"
}

# 停止并删除旧容器
stop_container() {
    if [ "$(docker ps -q -f name=$DOCKER_CONTAINER_NAME)" ]; then
        log_info "停止运行中的容器..."
        docker stop "$DOCKER_CONTAINER_NAME"
    fi
    if [ "$(docker ps -aq -f name=$DOCKER_CONTAINER_NAME)" ]; then
        log_info "删除旧容器..."
        docker rm "$DOCKER_CONTAINER_NAME"
    fi
}

# 运行新容器
run_container() {
    log_info "启动新容器..."
    docker run -d \
        --env-file "$ENV_FILE" \
        --name "$DOCKER_CONTAINER_NAME" \
        --network "$NETWORK" \
        --restart unless-stopped \
        "$DOCKER_IMAGE_NAME"
    log_info "容器启动成功"
}

# 显示容器状态
show_status() {
    log_info "容器状态:"
    docker ps -f name="$DOCKER_CONTAINER_NAME" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
}

# 主流程
main() {
    log_info "开始部署 Cronjob 服务..."
    check_env
    pull_image
    stop_container
    run_container
    show_status
    log_info "部署完成!"
}

main "$@"
