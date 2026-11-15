#!/usr/bin/env bash
#
# mf-topomon 一行安裝（Debian 12 / 13、Ubuntu 24.04）
#
#   curl -fsSL https://raw.githubusercontent.com/nox913106/net-lldp-topo/main/scripts/bootstrap.sh | sudo bash
#
# 最精簡的系統連 curl 都沒有，那就先：
#
#   sudo apt-get update && sudo apt-get install -y curl
#
# 這個腳本只做一件事：把程式碼放到 /opt/mf-topomon，然後把後面的事交給 repo
# 裡的 scripts/mf-topomon.sh install。真正的安裝步驟只有那一份 —— 這裡不重複，
# 所以「一行安裝」與「之後手動重跑 install」走的是同一段程式。
#
# 預設是原生安裝：PostgreSQL、nginx 與兩個 systemd 服務直接裝在主機上。要用
# Docker 的話：
#
#   curl -fsSL .../scripts/bootstrap.sh | sudo MF_TOPOMON_MODE=docker bash
#
# 可以用環境變數改的東西：
#   MF_TOPOMON_DIR=/opt/mf-topomon   安裝位置
#   MF_TOPOMON_BRANCH=main           要跟的分支
#   MF_TOPOMON_REPO=https://...      來源（fork 或內部鏡像）
#   MF_TOPOMON_MODE=native|docker    部署方式
#
# 重複執行是安全的：已經裝好的會變成一次更新，設定、密碼、憑證與資料庫都不會
# 被動到。
#
# 整個腳本包在函式裡、最後一行才呼叫 main —— curl 到一半斷線的話，bash 會
# 執行它已經收到的那半個檔案，而一個只有定義沒有呼叫的半個檔案什麼也不會做。

set -euo pipefail

MF_TOPOMON_DIR="${MF_TOPOMON_DIR:-/opt/mf-topomon}"
MF_TOPOMON_REPO="${MF_TOPOMON_REPO:-https://github.com/nox913106/net-lldp-topo.git}"
MF_TOPOMON_BRANCH="${MF_TOPOMON_BRANCH:-main}"
MF_TOPOMON_MODE="${MF_TOPOMON_MODE:-native}"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; BOLD=$'\033[1m'; NC=$'\033[0m'
info() { echo "${GREEN}[INFO]${NC} $*"; }
warn() { echo "${YELLOW}[WARN]${NC} $*" >&2; }
fail() { echo "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step() { echo; echo "${BOLD}==> $*${NC}"; }

check_host() {
    [[ $EUID -eq 0 ]] || fail "需要 root：把這一行的結尾改成 | sudo bash"
    [[ "$(uname -s)" == "Linux" ]] || fail "只支援 Linux"
    command -v apt-get >/dev/null 2>&1 || fail \
        "這個安裝程式只處理 apt 系的發行版（Debian / Ubuntu）。"
    case "$MF_TOPOMON_MODE" in
        native|docker) ;;
        *) fail "MF_TOPOMON_MODE 只能是 native 或 docker（現在是 $MF_TOPOMON_MODE）" ;;
    esac
}

install_prerequisites() {
    local missing=() tool
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

# 上一個名字的安裝。兩個不會互相干擾，但也不會自己合併，所以要講出來 ——
# 否則同一台機器上會有兩套各自在輪詢同一批設備。
note_previous_install() {
    if [[ -d /opt/topomon/.git && "$MF_TOPOMON_DIR" != "/opt/topomon" ]]; then
        warn "這台機器上有舊名稱的安裝（/opt/topomon，Docker）。這次安裝不會動它。
       要搬過來：先在舊的那邊 \`sudo topomon backup\`，裝好新的之後
       \`sudo mf-topomon restore <那份 .sql.gz>\`，確認無誤再
       \`sudo topomon uninstall\`。步驟在 docs/DEPLOYMENT.md。"
    fi
}

fetch_source() {
    if [[ -d "$MF_TOPOMON_DIR/.git" ]]; then
        step "更新 $MF_TOPOMON_DIR"
        git -C "$MF_TOPOMON_DIR" fetch --quiet origin "$MF_TOPOMON_BRANCH" \
            || fail "git fetch 失敗 —— 這台機器連得到 $MF_TOPOMON_REPO 嗎？"
        if [[ -n "$(git -C "$MF_TOPOMON_DIR" status --porcelain --untracked-files=no)" ]]; then
            warn "$MF_TOPOMON_DIR 有未提交的改動，跳過更新程式碼（設定與資料不受影響）"
        else
            git -C "$MF_TOPOMON_DIR" checkout --quiet "$MF_TOPOMON_BRANCH" 2>/dev/null \
                || git -C "$MF_TOPOMON_DIR" checkout --quiet -b "$MF_TOPOMON_BRANCH" \
                   --track "origin/$MF_TOPOMON_BRANCH"
            git -C "$MF_TOPOMON_DIR" merge --ff-only "origin/$MF_TOPOMON_BRANCH" >/dev/null \
                || warn "無法 fast-forward，維持現在的版本"
        fi
    elif [[ -e "$MF_TOPOMON_DIR" ]]; then
        fail "$MF_TOPOMON_DIR 已經存在而且不是 git checkout。換一個位置：
    curl ... | sudo MF_TOPOMON_DIR=/opt/mf-topomon2 bash"
    else
        step "取得原始碼到 $MF_TOPOMON_DIR"
        git clone --quiet --branch "$MF_TOPOMON_BRANCH" "$MF_TOPOMON_REPO" "$MF_TOPOMON_DIR" \
            || fail "git clone 失敗 —— 這台機器連得到 $MF_TOPOMON_REPO 嗎？
    （內網的話用 MF_TOPOMON_REPO 指到你們自己的鏡像。）"
    fi
    info "版本 $(git -C "$MF_TOPOMON_DIR" rev-parse --short HEAD)"
}

main() {
    echo
    echo "${BOLD}mf-topomon 安裝${NC}  →  $MF_TOPOMON_DIR  （$MF_TOPOMON_MODE）"
    check_host
    install_prerequisites
    note_previous_install
    fetch_source

    # 之後的每一件事都是 repo 裡那支工具的工作。這裡不重複一份。
    if [[ "$MF_TOPOMON_MODE" == "docker" ]]; then
        exec "$MF_TOPOMON_DIR/scripts/mf-topomon-docker.sh" deploy
    fi
    exec "$MF_TOPOMON_DIR/scripts/mf-topomon.sh" install
}

main "$@"
