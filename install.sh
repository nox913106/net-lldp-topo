#!/usr/bin/env bash
#
# topomon 一鍵安裝（Ubuntu / Debian）
#
#   curl -fsSL https://raw.githubusercontent.com/nox913106/net-lldp-topo/main/install.sh | sudo bash
#
# 這個腳本只做「把機器準備好」：裝 docker、把 repo 放到 /opt/topomon，然後
# 把後面的事交給 repo 裡的 tools/topomon。日常維護都用那一個：
#
#   topomon health | status | logs | update | backup | password
#
# 可以用環境變數改的東西：
#   TOPOMON_DIR=/opt/topomon     安裝位置
#   TOPOMON_BRANCH=main          要跟的分支
#   TOPOMON_REPO=https://...     來源（fork 或內部鏡像）
#
# 重複執行是安全的：已經裝好的會變成一次更新，而 .env、auth/（密鑰、密碼、
# 憑證）與資料庫都不會被動到。
#
# 整個腳本包在函式裡、最後一行才呼叫 main —— curl 到一半斷線的話，bash 會
# 執行它已經收到的那半個檔案，而一個只有定義沒有呼叫的半個檔案什麼也不會做。

set -euo pipefail

TOPOMON_DIR="${TOPOMON_DIR:-/opt/topomon}"
TOPOMON_REPO="${TOPOMON_REPO:-https://github.com/nox913106/net-lldp-topo.git}"
TOPOMON_BRANCH="${TOPOMON_BRANCH:-main}"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; BOLD=$'\033[1m'; NC=$'\033[0m'
info() { echo "${GREEN}[INFO]${NC} $*"; }
warn() { echo "${YELLOW}[WARN]${NC} $*" >&2; }
fail() { echo "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step() { echo; echo "${BOLD}==> $*${NC}"; }

check_host() {
    [[ $EUID -eq 0 ]] || fail "需要 root：把這一行改成
    curl -fsSL <url>/install.sh | sudo bash"

    command -v apt-get >/dev/null 2>&1 || fail \
        "這個腳本只處理 apt 系的發行版（Ubuntu / Debian）。其他系統請照
    docs/DEPLOYMENT.md 手動裝 docker 與 git，然後跑 tools/topomon deploy。"

    # 收 trap 要綁 udp/162，分片要靠 host 網路 —— 兩個都是 Linux 的前提
    [[ "$(uname -s)" == "Linux" ]] || fail "只支援 Linux"
}

install_prerequisites() {
    local missing=()
    for tool in git curl openssl; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        return
    fi
    step "安裝 ${missing[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        ca-certificates "${missing[@]}"
}

install_docker() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        info "docker 已安裝：$(docker --version | sed 's/Docker version //')"
        return
    fi

    step "安裝 docker"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        ca-certificates curl gnupg

    # 先試 Docker 官方源（版本比較新，compose v2 一定有）。內網或代理後面
    # 拿不到的話退回發行版自己的套件 —— 那個版本舊，但功能是完整的。
    install -m 0755 -d /etc/apt/keyrings
    if curl -fsSL --max-time 30 https://download.docker.com/linux/ubuntu/gpg \
         -o /tmp/docker.gpg 2>/dev/null; then
        gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg < /tmp/docker.gpg
        rm -f /tmp/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
            > /etc/apt/sources.list.d/docker.list
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            docker-ce docker-ce-cli containerd.io docker-compose-plugin
    else
        warn "連不到 download.docker.com，改用發行版的套件"
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            docker.io docker-compose-v2
    fi

    systemctl enable --now docker

    docker compose version >/dev/null 2>&1 || fail \
        "docker 裝好了但沒有 compose v2。手動裝 docker-compose-plugin 之後重跑。"
    info "docker $(docker --version | sed 's/Docker version //')"
}

fetch_source() {
    if [[ -d "$TOPOMON_DIR/.git" ]]; then
        step "更新 $TOPOMON_DIR"
        git -C "$TOPOMON_DIR" remote set-url origin "$TOPOMON_REPO"
        git -C "$TOPOMON_DIR" fetch --quiet origin "$TOPOMON_BRANCH"
        if [[ -n "$(git -C "$TOPOMON_DIR" status --porcelain --untracked-files=no)" ]]; then
            warn "$TOPOMON_DIR 有未提交的改動，跳過更新程式碼（設定與資料不受影響）"
        else
            git -C "$TOPOMON_DIR" checkout --quiet "$TOPOMON_BRANCH" 2>/dev/null \
                || git -C "$TOPOMON_DIR" checkout --quiet -b "$TOPOMON_BRANCH" \
                   --track "origin/$TOPOMON_BRANCH"
            git -C "$TOPOMON_DIR" merge --ff-only "origin/$TOPOMON_BRANCH" \
                || warn "無法 fast-forward，維持現在的版本"
        fi
    elif [[ -e "$TOPOMON_DIR" ]]; then
        fail "$TOPOMON_DIR 已經存在而且不是 git checkout。換一個位置：
    TOPOMON_DIR=/opt/topomon2 curl ... | sudo bash"
    else
        step "取得原始碼到 $TOPOMON_DIR"
        git clone --quiet --branch "$TOPOMON_BRANCH" "$TOPOMON_REPO" "$TOPOMON_DIR" \
            || fail "git clone 失敗（網路？分支名稱？）"
    fi
    info "版本 $(git -C "$TOPOMON_DIR" rev-parse --short HEAD)"
}

main() {
    echo
    echo "${BOLD}topomon 安裝${NC}  →  $TOPOMON_DIR"
    check_host
    install_prerequisites
    install_docker
    fetch_source

    # 之後的每一件事都是 topomon 的工作，包含產生密鑰與密碼、起容器、等健康
    # 檢查通過、把自己連到 /usr/local/bin。這裡不重複一份。
    exec "$TOPOMON_DIR/tools/topomon" deploy
}

main "$@"
