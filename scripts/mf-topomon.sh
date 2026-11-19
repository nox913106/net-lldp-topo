#!/usr/bin/env bash
#
# mf-topomon —— 原生安裝（apt + systemd）的安裝、升級、健檢與日常維護。
#
# 這個檔案在 repo 裡，/usr/local/bin/mf-topomon 只是一個指向它的連結。所以
# `mf-topomon upgrade` 做完 git pull 之後，下一次執行的就是新版的這個腳本。
#
# 用法：mf-topomon <指令>，不帶指令會列出全部指令。
#
# 裝在主機上的東西，以及它們各自在哪裡：
#
#   程式碼        /opt/mf-topomon            root 的；服務帳號只讀得到
#   Python 環境   /opt/mf-topomon/.venv      照 requirements.txt 的 hash 鎖定安裝
#   設定          /etc/mf-topomon/mf-topomon.env        root:mftopomon 640
#   HTTPS 憑證    /etc/mf-topomon/auth/                  tls.crt、tls.key
#   執行期資料    /var/lib/mf-topomon/       介面上存的設定、後台送出的工作請求
#   工作結果      /var/lib/mf-topomon-runner/  root 的；服務帳號只讀得到
#   資料庫        主機自己的 PostgreSQL，資料庫與角色都叫 mftopomon
#                 （登入帳號、稽核紀錄、通知設定也在裡面）
#   服務          mf-topomon-app.service、mf-topomon-collector.service、
#                 mf-topomon-runner.path（後台要求的備份／升級／憑證）
#   反向代理      主機自己的 nginx，/etc/nginx/sites-available/mf-topomon
#   備份          /var/backups/mf-topomon/
#
# 三個決定：
#
#   1. 每個步驟都是「確保它是這個樣子」，不是「做一次」。所以 install 可以
#      重跑，重跑就是修復；upgrade 做的也是同一串步驟，只是前面多一次
#      git pull、後面多一次失敗時的回滾。
#
#   2. 登入在應用程式裡（帳號、角色、AD），nginx 只負責 TLS。應用程式只聽
#      127.0.0.1，所以唯一的入口還是 nginx。doctor 需要讀資料庫的時候不走
#      HTTP，而是以服務帳號跑 `python -m app.cli`（app/cli.py）。
#
#   3. doctor 每一條沒過的檢查，後面都跟著一行可以直接複製貼上的指令。
#      「哪裡壞了」沒有附上「所以要打什麼」，等於只講了一半。
#
# Docker 的部署方式還在，是次要的：scripts/mf-topomon-docker.sh。

set -euo pipefail

# --------------------------------------------------------------------------
# 基本設定
# --------------------------------------------------------------------------

# readlink -f 是為了讓 /usr/local/bin/mf-topomon 這個連結也找得到 repo 根目錄
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
cd "$DIR"

BRANCH="${MF_TOPOMON_BRANCH:-main}"
LINK="${MF_TOPOMON_LINK:-/usr/local/bin/mf-topomon}"
ETC_DIR="${MF_TOPOMON_ETC:-/etc/mf-topomon}"
ENV_FILE="$ETC_DIR/mf-topomon.env"
AUTH_DIR="$ETC_DIR/auth"
DATA_DIR="${MF_TOPOMON_DATA:-/var/lib/mf-topomon}"
BACKUP_DIR="${MF_TOPOMON_BACKUP_DIR:-/var/backups/mf-topomon}"
# 後台工作的結果。root 的、服務帳號只讀：見 deploy/systemd/mf-topomon-runner.path
RUNNER_DIR="${MF_TOPOMON_RUNNER_DIR:-/var/lib/mf-topomon-runner}"
REQUEST_DIR="$DATA_DIR/requests"
VENV="$DIR/.venv"
LAST_GOOD="$ETC_DIR/.last-good"
# 安裝時產生的管理密碼也存在這裡（root 才讀得到），因為「只印一次」的東西
# 在一個被管線吃掉輸出的安裝過程裡等於沒印。登入之後就該刪掉，doctor 會提醒。
INITIAL_PASSWORD_FILE="$ETC_DIR/.admin-initial-password"

RUN_USER="${MF_TOPOMON_USER:-mftopomon}"
DB_NAME="${MF_TOPOMON_DB:-mftopomon}"
DB_USER="${MF_TOPOMON_DB_USER:-mftopomon}"
APP_PORT="${MF_TOPOMON_APP_PORT:-8080}"

APP_UNIT="mf-topomon-app.service"
COLLECTOR_UNIT="mf-topomon-collector.service"
RUNNER_PATH_UNIT="mf-topomon-runner.path"
RUNNER_UNIT="mf-topomon-runner.service"
UNIT_DIR="${MF_TOPOMON_UNIT_DIR:-/etc/systemd/system}"
NGINX_AVAILABLE="/etc/nginx/sites-available/mf-topomon"
NGINX_ENABLED="/etc/nginx/sites-enabled/mf-topomon"

# 健康檢查最多等多久（秒）。第一次安裝要建資料表，比重啟一次久。
WAIT_SECONDS="${MF_TOPOMON_WAIT:-120}"
BACKUP_KEEP="${MF_TOPOMON_BACKUP_KEEP:-14}"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'; BOLD=$'\033[1m'; NC=$'\033[0m'

info()  { echo "${GREEN}[INFO]${NC} $*"; }
warn()  { echo "${YELLOW}[WARN]${NC} $*" >&2; }
fail()  { echo "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()  { echo; echo "${BOLD}==> $*${NC}"; }

# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

# 設定檔的某一個值。刻意不用 `source`：那會把設定檔當成程式碼執行，而這個
# 檔案裡有密碼，一個不小心的引號就會變成指令。
env_value() {
    local key="$1" default="${2:-}" line
    line="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
    if [[ -z "$line" ]]; then
        echo "$default"
        return
    fi
    line="${line#*=}"
    line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    line="${line%\"}"; line="${line#\"}"
    line="${line%\'}"; line="${line#\'}"
    [[ -n "$line" ]] && echo "$line" || echo "$default"
}

# 設定檔的某一個值設成指定內容：有就改，沒有就加在最後
env_set() {
    local key="$1" value="$2"
    if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$ENV_FILE"; then
        # | 當分隔符：值裡面會有 / （URL），不會有 |
        sed -i "s|^[[:space:]]*${key}[[:space:]]*=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

http_port()  { env_value TOPOMON_HTTP_PORT 80; }
https_port() { env_value TOPOMON_HTTPS_PORT 443; }

require_root() {
    [[ $EUID -eq 0 ]] || fail "這個指令需要 root：sudo mf-topomon $*"
}

require_tools() {
    local tool
    for tool in "$@"; do
        command -v "$tool" >/dev/null 2>&1 \
            || fail "需要 $tool。sudo apt-get install -y $tool"
    done
}

require_installed() {
    [[ -f "$ENV_FILE" ]] || fail "這台機器還沒有安裝（找不到 $ENV_FILE）。
    先跑：sudo mf-topomon install"
}

random_password() {
    # 不含 shell 與 URL 會有意見的字元：這組密碼會被貼進瀏覽器，資料庫那一組
    # 還會被放進 DATABASE_URL。cut 而不是 head：head 讀夠了就關掉管線，上游
    # 拿到 SIGPIPE，而 `set -o pipefail` 會把那個當成失敗。
    openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-20
}

# 以 postgres 這個系統帳號執行 psql。不用密碼：主機上的 postgres 帳號走
# unix socket 的 peer 認證，這是 Debian/Ubuntu 的 PostgreSQL 預設。
#
# 先 cd 到 /：這個腳本的工作目錄是程式碼所在的地方，postgres 帳號未必進得去
# （它可以被裝在某個人的家目錄底下），而 psql 會抱怨 "could not change
# directory" —— 抱怨在 stderr，但夾在輸出裡就會被當成查詢的結果。
as_postgres() {
    (cd / && runuser -u postgres -- "$@")
}

psql_value() {
    as_postgres psql -tAc "$1" 2>/dev/null | head -n 1 | tr -d '[:space:]'
}

# nginx 的 worker 用哪個帳號跑（auth/ 目錄交給它的群組讀，secure_auth_dir）。
# Debian/Ubuntu 是 www-data。
nginx_user() {
    local user
    user="$(grep -E '^[[:space:]]*user[[:space:]]+' /etc/nginx/nginx.conf 2>/dev/null \
        | head -n 1 | awk '{print $2}' | tr -d ';' || true)"
    echo "${user:-www-data}"
}

# --------------------------------------------------------------------------
# 主機
# --------------------------------------------------------------------------

# 驗過的組合。清單以外的系統不是「一定不行」，而是沒有人跑過 —— 尤其是
# Python 版本：依賴是照 hash 鎖定的，某個版本的 Python 沒有對應的 wheel 時
# 會在 ensure_venv 那一步失敗，而不是裝出一個半套的環境。
SUPPORTED_OS="debian:12 debian:13 ubuntu:24.04"

check_host() {
    command -v apt-get >/dev/null 2>&1 || fail \
        "這個安裝程式只處理 apt 系的發行版（Debian / Ubuntu）。"

    local id version
    # shellcheck disable=SC1091
    id="$(. /etc/os-release && echo "${ID:-}")"
    # shellcheck disable=SC1091
    version="$(. /etc/os-release && echo "${VERSION_ID:-}")"
    if [[ " $SUPPORTED_OS " != *" ${id}:${version} "* ]]; then
        if [[ "${MF_TOPOMON_ALLOW_UNSUPPORTED:-}" == "1" ]]; then
            warn "${id} ${version} 不在驗過的清單裡（$SUPPORTED_OS），照你的要求繼續"
        else
            fail "${id} ${version} 不在驗過的清單裡：$SUPPORTED_OS
    要在這台機器上試，加上 MF_TOPOMON_ALLOW_UNSUPPORTED=1 再跑一次。
    不想動主機的話用 Docker：sudo bash $DIR/scripts/mf-topomon-docker.sh deploy"
        fi
    fi

    # 服務是 systemd 單元。容器、WSL1、或用別的 init 的系統沒有它。
    if [[ ! -d /run/systemd/system && "${MF_TOPOMON_NO_SYSTEMD_CHECK:-}" != "1" ]]; then
        fail "這台機器沒有在跑 systemd（找不到 /run/systemd/system）。
    原生安裝的兩個服務是 systemd 單元。在容器裡請用 Docker 的部署方式：
        sudo bash $DIR/scripts/mf-topomon-docker.sh deploy"
    fi
}

install_packages() {
    local missing=() package
    # nginx 與 postgresql 是主機自己的服務；python3-venv 在 Debian/Ubuntu 是
    # 跟 python3 分開的套件，少了它 `python3 -m venv` 會做出一個沒有 pip 的環境
    for package in python3 python3-venv postgresql nginx openssl curl git ca-certificates; do
        dpkg -s "$package" >/dev/null 2>&1 || missing+=("$package")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        info "需要的套件都在"
        return
    fi
    info "安裝 ${missing[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
}

ensure_user() {
    if id "$RUN_USER" >/dev/null 2>&1; then
        return
    fi
    # 系統帳號、不能登入、家目錄就是它唯一寫得到的地方
    useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home \
        --shell /usr/sbin/nologin "$RUN_USER"
    info "建立系統帳號 $RUN_USER"
}

ensure_dirs() {
    install -d -m 750 -o root -g "$RUN_USER" "$ETC_DIR"
    install -d -m 750 -o "$RUN_USER" -g "$RUN_USER" "$DATA_DIR" "$REQUEST_DIR"
    # 服務帳號讀得到資料庫的 dump（後台列出與下載備份用）—— 裡面沒有它本來
    # 就讀不到的東西。旁邊的設定封存檔有 TLS 私鑰，維持只有 root 讀得到。
    install -d -m 750 -o root -g "$RUN_USER" "$BACKUP_DIR"
    local dump
    for dump in "$BACKUP_DIR"/mf-topomon-*.sql.gz; do
        [[ -f "$dump" ]] || continue
        chown "root:$RUN_USER" "$dump"
        chmod 640 "$dump"
    done
    install -d -m 750 -o root -g "$RUN_USER" "$RUNNER_DIR" "$RUNNER_DIR/jobs"
}

# --------------------------------------------------------------------------
# 設定、資料庫、Python 環境
# --------------------------------------------------------------------------

ensure_env() {
    if [[ ! -f "$ENV_FILE" ]]; then
        info "建立 $ENV_FILE（從 .env.example）"
        install -m 640 -o root -g "$RUN_USER" "$DIR/.env.example" "$ENV_FILE"

        local db_password
        db_password="$(random_password)"
        env_set APP_SECRET_KEY "$(openssl rand -hex 32)"
        # 資料庫密碼放兩個地方：DATABASE_URL 是應用程式讀的，POSTGRES_PASSWORD
        # 是這個腳本讀的 —— ensure_database 每次都把角色的密碼設成它，所以
        # 兩邊不會對不上。
        env_set POSTGRES_PASSWORD "$db_password"
        env_set DATABASE_URL \
            "postgresql+asyncpg://${DB_USER}:${db_password}@127.0.0.1:5432/${DB_NAME}"
        env_set APP_DATA_DIR "$DATA_DIR"
        env_set APP_RUNNER_DIR "$RUNNER_DIR"
        env_set APP_BACKUP_DIR "$BACKUP_DIR"
        info "已產生隨機的 APP_SECRET_KEY 與資料庫密碼"
        return
    fi

    # 這兩個是這個腳本自己的路徑，不是使用者的設定：舊的安裝沒有的話補上，
    # 否則後台的備份、升級、憑證會說「沒有背景執行器」
    grep -qE '^[[:space:]]*APP_RUNNER_DIR[[:space:]]*=' "$ENV_FILE" || env_set APP_RUNNER_DIR "$RUNNER_DIR"
    grep -qE '^[[:space:]]*APP_BACKUP_DIR[[:space:]]*=' "$ENV_FILE" || env_set APP_BACKUP_DIR "$BACKUP_DIR"

    # 裡面有資料庫密碼，所以每次都收一次權限
    chown "root:$RUN_USER" "$ENV_FILE"
    chmod 640 "$ENV_FILE"

    # 既有的設定檔：不要動它，但要講出這個版本多了哪些設定。少一個設定不會
    # 讓它壞掉（程式有預設值），可是「有一個新旋鈕而你不知道」會。
    local missing=() key
    while IFS= read -r key; do
        grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$ENV_FILE" || missing+=("$key")
    done < <(grep -oE '^[A-Z][A-Z0-9_]*=' "$DIR/.env.example" | tr -d '=')

    if [[ ${#missing[@]} -gt 0 ]]; then
        warn "$ENV_FILE 沒有這些新設定（都有預設值，不設也能跑）："
        printf '         %s\n' "${missing[@]}" >&2
        warn "說明在 .env.example 與 docs/CONFIGURATION.md"
    fi
}

ensure_database() {
    systemctl enable --now postgresql >/dev/null 2>&1 \
        || fail "PostgreSQL 起不來。看：journalctl -u postgresql -n 50"

    local waited=0
    until as_postgres pg_isready -q >/dev/null 2>&1; do
        waited=$((waited + 1))
        if (( waited > 30 )); then
            fail "PostgreSQL 在 30 秒內沒有開始接受連線。看：
        journalctl -u postgresql -n 50"
        fi
        sleep 1
    done

    local password
    password="$(env_value POSTGRES_PASSWORD)"
    [[ -n "$password" ]] || fail "$ENV_FILE 裡沒有 POSTGRES_PASSWORD"
    # 密碼只有英數字（random_password），所以可以直接放進單引號
    [[ "$password" =~ ^[A-Za-z0-9]+$ ]] || fail \
        "POSTGRES_PASSWORD 只能是英數字（它會被放進 SQL 與 DATABASE_URL）"

    if [[ "$(psql_value "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'")" != "1" ]]; then
        as_postgres psql -qc "CREATE ROLE ${DB_USER} LOGIN PASSWORD '${password}'" >/dev/null
        info "建立資料庫角色 $DB_USER"
    else
        # 每次都重設：設定檔是密碼的來源，角色跟著它走
        as_postgres psql -qc "ALTER ROLE ${DB_USER} LOGIN PASSWORD '${password}'" >/dev/null
    fi

    if [[ "$(psql_value "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'")" != "1" ]]; then
        as_postgres createdb -O "$DB_USER" "$DB_NAME"
        info "建立資料庫 $DB_NAME"
    fi
}

ensure_venv() {
    if [[ ! -x "$VENV/bin/python" ]]; then
        info "建立 Python 環境 $VENV（$(python3 --version 2>&1)）"
        python3 -m venv "$VENV"
    fi
    # --require-hashes：requirements.txt 的每一行都帶 hash，少一個就失敗，而
    # 不是靜靜地裝下 PyPI 那天剛好提供的版本（requirements.in 說明為什麼鎖）
    if ! "$VENV/bin/pip" install --quiet --disable-pip-version-check \
            --require-hashes -r "$DIR/requirements.txt"; then
        fail "Python 依賴裝不起來。最常見的兩個原因：
      * 連不到 PyPI（代理、防火牆）
      * 這台機器的 $(python3 --version 2>&1) 沒有某個套件的現成 wheel
    上面 pip 的訊息會說是哪一個。"
    fi
}

# auth/ 的權限。tls.key 是 nginx 的 master 行程在啟動時以 root 讀的，600 root
# 就夠；目錄交給 nginx 的群組（750）。以前這裡還有 htpasswd —— 登入搬進應用
# 程式之後，舊安裝留下的那一份會在第一次升級時被匯入（ensure_accounts）。
secure_auth_dir() {
    local group
    group="$(id -gn "$(nginx_user)" 2>/dev/null || echo www-data)"
    # nginx 要走進 auth/ 得先走得進 /etc/mf-topomon，而那一層是服務帳號的
    # 群組。所以只給「其他人」通過的權限（x），不給列出內容的權限（r）。
    chmod 751 "$ETC_DIR"
    chown "root:$group" "$AUTH_DIR"
    chmod 750 "$AUTH_DIR"
    if [[ -f "$AUTH_DIR/htpasswd" ]]; then
        chown "root:$group" "$AUTH_DIR/htpasswd"
        chmod 640 "$AUTH_DIR/htpasswd"
    fi
    if [[ -f "$AUTH_DIR/tls.key" ]]; then
        chown root:root "$AUTH_DIR/tls.key"
        chmod 600 "$AUTH_DIR/tls.key"
    fi
}

# 剛產生的登入帳密，印一次就清掉。由 EXIT trap 呼叫（cmd_install），因為
# 帳號在 ensure_accounts 就寫進資料庫了：之後任何一步失敗，腳本都會在印出
# 摘要之前結束。
print_new_credentials() {
    if [[ -z "${NEW_CREDENTIALS:-}" ]]; then return 0; fi
    echo
    echo "  ${YELLOW}${BOLD}登入帳密：${NC}"
    echo "      ${BOLD}${NEW_CREDENTIALS}${NC}"
    echo
    echo "  也存了一份在 $INITIAL_PASSWORD_FILE（root 才讀得到）。"
    echo "  第一次登入時會要求換掉這組密碼；換完之後刪掉那個檔案："
    echo "      sudo rm $INITIAL_PASSWORD_FILE"
    echo
    NEW_CREDENTIALS=""
}

# 以服務帳號執行 app/cli.py。用服務帳號而不是 root：它讀的就是服務讀的那份
# 設定、連的就是服務連的那個資料庫角色，表格也就屬於服務。
app_cli() {
    (cd "$DIR" && runuser -u "$RUN_USER" -- env -i PATH=/usr/bin:/bin \
        PYTHONDONTWRITEBYTECODE=1 HOME="$DATA_DIR" LANG=C.UTF-8 \
        "$VENV/bin/python" -m app.cli --env-file "$ENV_FILE" "$@")
}

# 登入帳號。三種情況：
#   * 舊版留下的 htpasswd：匯入那個帳號，密碼不變（app/core/passwords.py 認得
#     $apr1$），第一次登入時自動改存成 scrypt
#   * 資料庫裡沒有任何管理員：建一個，密碼隨機，第一次登入要改
#   * 已經有管理員：什麼都不做
ensure_accounts() {
    if [[ -f "$AUTH_DIR/htpasswd" ]]; then
        local imported
        # root 開檔、從 stdin 交給服務帳號：auth/ 是 nginx 的群組才讀得到
        imported="$(app_cli import-htpasswd - < "$AUTH_DIR/htpasswd" 2>/dev/null)" \
            || fail "匯入舊的登入帳號失敗（$AUTH_DIR/htpasswd）"
        mv -f "$AUTH_DIR/htpasswd" "$AUTH_DIR/htpasswd.imported"
        rm -f "$AUTH_DIR/.admin-user"
        info "登入改由應用程式處理：舊的帳號已匯入，密碼不變（${imported}）"
    fi
    local out user password
    out="$(app_cli ensure-admin "${MF_TOPOMON_ADMIN_USER:-admin}")" \
        || fail "建立管理員帳號失敗。看：sudo journalctl -u $APP_UNIT -n 40"
    if [[ "$out" == created* ]]; then
        read -r _ user password <<< "$out"
        (umask 077 && printf '%s / %s\n' "$user" "$password" > "$INITIAL_PASSWORD_FILE")
        NEW_CREDENTIALS="$user / $password"
        info "建立管理員帳號 $user"
    fi
}

ensure_auth() {
    install -d -m 750 "$AUTH_DIR"

    if [[ ! -f "$AUTH_DIR/tls.crt" || ! -f "$AUTH_DIR/tls.key" ]]; then
        info "產生自簽 TLS 憑證（10 年）"
        local host ip
        host="$(hostname -f 2>/dev/null || hostname)"
        ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
            -subj "/CN=${host}" \
            -addext "subjectAltName=DNS:${host},DNS:localhost,IP:127.0.0.1${ip:+,IP:$ip}" \
            -keyout "$AUTH_DIR/tls.key" -out "$AUTH_DIR/tls.crt" 2>/dev/null \
            || fail "產生憑證失敗（openssl 太舊不支援 -addext？）"
        chmod 644 "$AUTH_DIR/tls.crt"
    fi

    secure_auth_dir
}

# --------------------------------------------------------------------------
# systemd 與 nginx
# --------------------------------------------------------------------------

install_units() {
    local unit
    for unit in "$APP_UNIT" "$COLLECTOR_UNIT" "$RUNNER_PATH_UNIT" "$RUNNER_UNIT"; do
        sed -e "s|@DIR@|$DIR|g" \
            -e "s|@ENV_FILE@|$ENV_FILE|g" \
            -e "s|@DATA_DIR@|$DATA_DIR|g" \
            -e "s|@RUNNER_DIR@|$RUNNER_DIR|g" \
            -e "s|@RUN_USER@|$RUN_USER|g" \
            -e "s|@APP_PORT@|$APP_PORT|g" \
            "$DIR/deploy/systemd/$unit" > "$UNIT_DIR/$unit"
        chmod 644 "$UNIT_DIR/$unit"
    done
    systemctl daemon-reload
    systemctl enable "$APP_UNIT" "$COLLECTOR_UNIT" >/dev/null 2>&1 || true
    # 由 path 單元觸發，不 enable 那個 service 本身
    systemctl enable --now "$RUNNER_PATH_UNIT" >/dev/null 2>&1 || true
}

# nginx 的版本，例如 "1.24.0" -> 1 24 0
nginx_version() {
    nginx -v 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1
}

# deploy/nginx.conf 就是被測試的那一份（tests/test_proxy_auth.py），這裡只把
# 三樣跟環境有關的東西換掉：上游位址、憑證所在的目錄、要聽的埠。
render_nginx_site() {
    local version major minor patch http2_ok=1
    version="$(nginx_version)"
    IFS=. read -r major minor patch <<< "${version:-0.0.0}"
    # `http2 on;` 是 1.25.1 才有的寫法；比它舊的要寫在 listen 那一行上
    if (( major < 1 || (major == 1 && minor < 25) || (major == 1 && minor == 25 && patch < 1) )); then
        http2_ok=0
    fi

    sed -e "s|server app:8080;|server 127.0.0.1:${APP_PORT};|" \
        -e "s|/etc/nginx/auth|${AUTH_DIR}|g" \
        -e "s|listen 80 default_server;|listen $(http_port) default_server;|" \
        -e "s|listen 443 ssl default_server;|listen $(https_port) ssl default_server;|" \
        "$DIR/deploy/nginx.conf" \
    | if (( http2_ok == 0 )); then
        sed -e "s|listen $(https_port) ssl default_server;|listen $(https_port) ssl http2 default_server;|" \
            -e '/^[[:space:]]*http2 on;[[:space:]]*$/d'
      else
        cat
      fi
}

install_nginx_site() {
    local tmp
    tmp="$(mktemp)"
    render_nginx_site > "$tmp"
    install -m 644 "$tmp" "$NGINX_AVAILABLE"
    rm -f "$tmp"
    ln -sfn "$NGINX_AVAILABLE" "$NGINX_ENABLED"

    # 發行版附的預設站台也宣告自己是 80 埠的 default_server，兩個同時啟用
    # nginx 會拒絕啟動。只拿掉那個連結；檔案留在 sites-available 裡。
    if [[ -L /etc/nginx/sites-enabled/default ]]; then
        rm -f /etc/nginx/sites-enabled/default
        info "停用 nginx 的預設站台（/etc/nginx/sites-available/default 還在）"
    fi

    nginx -t >/dev/null 2>&1 || {
        nginx -t >&2 || true
        fail "nginx 的設定檢查沒過（上面是它的訊息）。最常見的原因是 80 或 443
    已經被別的站台佔用 —— 在 $ENV_FILE 設 TOPOMON_HTTP_PORT /
    TOPOMON_HTTPS_PORT 換一組埠，再跑一次 sudo mf-topomon install"
    }
}

start_services() {
    systemctl enable nginx >/dev/null 2>&1 || true
    systemctl restart "$APP_UNIT" "$COLLECTOR_UNIT"
    # reload 而不是 restart：這台主機的 nginx 可能還在服務別的站台。沒在跑
    # 的時候 reload 會失敗，那就是該 start 的時候。
    systemctl reload nginx >/dev/null 2>&1 || systemctl restart nginx
}

# --------------------------------------------------------------------------
# 等服務起來，以及它是不是真的能用
# --------------------------------------------------------------------------

wait_healthy() {
    local deadline=$((SECONDS + WAIT_SECONDS)) body
    printf '%s' "等服務就緒 "
    while (( SECONDS < deadline )); do
        body="$(curl -fsS --max-time 5 -k \
            "https://127.0.0.1:$(https_port)/health/ready" 2>/dev/null || true)"
        if [[ "$body" == *'"status":"ready"'* ]]; then
            echo " ${GREEN}就緒${NC}"
            return 0
        fi
        printf '.'
        sleep 3
    done
    echo " ${RED}逾時${NC}"
    return 1
}

# 登入有在要求嗎。經過 nginx 問兩件事：
#
#   /api/v1/devices   沒有登入要是 401 —— 不是 200（任何人都進得去），也不是
#                     502（nginx 到不了應用程式）
#   /api/v1/auth/info 登入頁要用的這一條要是 200，否則沒有人登入得進去
auth_probe() {
    local api info
    api="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -k \
        "https://127.0.0.1:$(https_port)/api/v1/devices" 2>/dev/null || true)"
    info="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -k \
        "https://127.0.0.1:$(https_port)/api/v1/auth/info" 2>/dev/null || true)"
    echo "$api/$info"
}

require_login_works() {
    local code
    code="$(auth_probe)"
    if [[ "$code" == "401/200" ]]; then
        return 0
    fi
    tail -n 5 /var/log/nginx/error.log >&2 2>/dev/null || true
    fail "服務起來了，但登入不是預期的樣子（沒登入的 API 回 ${code%/*}、登入頁的
    資訊回 ${code#*/}，應該是 401 與 200）。上面是 nginx 最近的錯誤。"
}

api_get() {
    curl -fsS --max-time 10 "http://127.0.0.1:${APP_PORT}$1" 2>/dev/null
}

# 給後台讀的狀態：憑證與版本。寫在 RUNNER_DIR（root 的），應用程式只讀。
write_status_files() {
    install -d -m 750 -o root -g "$RUN_USER" "$RUNNER_DIR" "$RUNNER_DIR/jobs"
    if [[ -f "$AUTH_DIR/tls.crt" ]]; then
        python3 -I "$DIR/scripts/runner_helper.py" tls-info "$AUTH_DIR/tls.crt" \
            | python3 -I "$DIR/scripts/runner_helper.py" write-json "$RUNNER_DIR/tls.json" || true
    fi
    python3 -I "$DIR/scripts/runner_helper.py" git-info "$DIR" \
        | python3 -I "$DIR/scripts/runner_helper.py" write-json "$RUNNER_DIR/version.json" || true
    local last_good=""
    [[ -f "$LAST_GOOD" ]] && last_good="$(cut -c1-10 "$LAST_GOOD")"
    printf '{"last_good": "%s"}' "$last_good" \
        | python3 -I "$DIR/scripts/runner_helper.py" merge-json "$RUNNER_DIR/version.json" || true
    printf '{"installed": true, "updated_at": "%s"}' "$(date -Is)" \
        | python3 -I "$DIR/scripts/runner_helper.py" merge-json "$RUNNER_DIR/runner.json" || true
    chown -R "root:$RUN_USER" "$RUNNER_DIR"
    chmod 640 "$RUNNER_DIR"/*.json 2>/dev/null || true
}

# --------------------------------------------------------------------------
# 指令：install
# --------------------------------------------------------------------------

# install 與 upgrade 共用的那一串。每一步都是「確保」，所以順序才是重點：
# 設定檔要先有，資料庫才知道密碼；Python 環境要先有，服務才起得來。
converge() {
    step "套件與帳號"
    install_packages
    ensure_user
    ensure_dirs

    step "設定"
    ensure_env
    ensure_auth

    step "資料庫"
    ensure_database

    step "Python 環境"
    ensure_venv

    step "登入帳號"
    ensure_accounts

    step "服務與反向代理"
    install_units
    install_nginx_site
    start_services
}

cmd_install() {
    require_root install
    check_host
    NEW_CREDENTIALS=""
    # 不管這個指令怎麼結束，剛產生的密碼都要被印出來
    trap print_new_credentials EXIT

    converge

    step "健康檢查"
    if ! wait_healthy; then
        warn "服務在 ${WAIT_SECONDS}s 內沒有就緒。最近的日誌："
        journalctl -u "$APP_UNIT" -n 30 --no-pager >&2 2>/dev/null || true
        fail "安裝沒有完成。跑 \`sudo mf-topomon doctor\` 看是哪一層"
    fi
    require_login_works

    git rev-parse HEAD > "$LAST_GOOD" 2>/dev/null || true
    write_status_files
    install_link
    print_summary
}

install_link() {
    # 連結而不是複製：見檔案開頭的說明
    if [[ "$(readlink -f "$LINK" 2>/dev/null || true)" != "$DIR/scripts/mf-topomon.sh" ]]; then
        ln -sf "$DIR/scripts/mf-topomon.sh" "$LINK"
        info "已安裝 $LINK"
    fi
}

print_summary() {
    local ip port base
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    ip="${ip:-127.0.0.1}"
    port="$(https_port)"
    base="https://${ip}"
    if [[ "$port" != "443" ]]; then base="${base}:${port}"; fi
    echo
    echo "${BOLD}=========================================${NC}"
    echo "${BOLD}  mf-topomon 已就緒${NC}"
    echo "${BOLD}=========================================${NC}"
    echo
    echo "  Web UI    ${base}/"
    echo "  API 文件  ${base}/docs"
    print_new_credentials
    echo
    echo "  憑證是自己簽的，所以瀏覽器會先警告一次。要換成真憑證：登入後到"
    echo "  「後台 → HTTPS 憑證」上傳；帳號、AD、通知、備份排程也都在後台設定。"
    echo
    echo "  接下來：到「設備」頁新增一台交換機（名稱、IP、community），"
    echo "  它的鄰居會在下一輪盤點自動長出來。細節見"
    echo "  ${BLUE}docs/USER_MANUAL.md${NC}。設備端不需要任何改動"
    echo "  （要收 trap 的話見 docs/CONFIGURATION.md 第 5 節）。"
    echo
    echo "  常用指令：mf-topomon doctor / status / logs / upgrade"
    echo
}

# --------------------------------------------------------------------------
# 指令：upgrade / rollback
# --------------------------------------------------------------------------

cmd_upgrade() {
    require_root upgrade
    require_installed
    check_host
    NEW_CREDENTIALS=""
    trap print_new_credentials EXIT

    local before after
    if [[ "${1:-}" == "--resume" ]]; then
        # 第二段：程式碼已經換成新版，由新版的這個腳本接手（見下面的 exec）
        before="$2"
        after="$(git rev-parse HEAD)"
    else
        before="$(git rev-parse HEAD)"

        step "取得更新"
        if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
            git status --short --untracked-files=no >&2
            fail "$DIR 有未提交的改動，升級會蓋掉它們。先處理上面這些檔案。
    （設定在 $ETC_DIR，不在這個目錄裡，不會被升級碰到。）"
        fi
        git fetch --quiet origin "$BRANCH" || fail "git fetch 失敗 —— 這台機器連得到
    $(git remote get-url origin 2>/dev/null || echo 'origin') 嗎？"
        if ! git merge --ff-only "origin/$BRANCH" >/dev/null 2>&1; then
            fail "無法 fast-forward 到 origin/$BRANCH —— 這個 checkout 有自己的
    commit。要丟掉本地的改動用：
        sudo git -C $DIR reset --hard origin/$BRANCH && sudo mf-topomon install"
        fi
        after="$(git rev-parse HEAD)"
        if [[ "$before" != "$after" ]]; then
            # bash 已經把這個腳本的舊版讀進記憶體了：接下來的步驟（安裝、
            # 健康檢查）要由新版來做，否則新版加的安裝步驟要等到下一次升級才會
            # 生效。trap 先拿掉：密碼由新版那一段負責印。
            trap - EXIT
            exec /bin/bash "$DIR/scripts/mf-topomon.sh" upgrade --resume "$before"
        fi
    fi

    if [[ "$before" == "$after" ]]; then
        info "已經是最新版（$(git rev-parse --short HEAD)）"
    else
        info "$(git rev-parse --short "$before") → $(git rev-parse --short "$after")"
        git --no-pager log --oneline "$before..$after" | sed 's/^/         /'
        # 換版本之前先留一份資料庫。升級會改 schema（app/db/database.py 會
        # 自動補欄位與索引），而回滾程式碼不會把資料庫改回去。
        step "升級前備份"
        cmd_backup >/dev/null
        info "備份在 $BACKUP_DIR"
    fi

    converge

    step "健康檢查"
    if wait_healthy && [[ "$(auth_probe)" == "401/200" ]]; then
        echo "$after" > "$LAST_GOOD"
        write_status_files
        info "升級完成"
        return 0
    fi

    # 升級後起不來：回到升級前的 commit。不這樣做的話，一個壞掉的版本會一直
    # 留在機器上，而這個指令最可能是在沒有人盯著的時候跑的。
    warn "升級後健康檢查沒過，回滾到 $(git rev-parse --short "$before")"
    journalctl -u "$APP_UNIT" -n 30 --no-pager >&2 2>/dev/null || true
    git reset --hard --quiet "$before"
    ensure_venv
    install_units
    start_services
    if wait_healthy; then
        write_status_files
        fail "已回滾到升級前的版本，服務是好的。失敗的原因在上面的日誌裡。"
    fi
    fail "回滾之後仍然起不來。跑 \`sudo mf-topomon doctor\`。"
}

# 應用程式每次啟動前（systemd 以 root 執行，見 mf-topomon-app.service 的
# ExecStartPre）。只做「補齊」：這一版需要的目錄與設定、登入帳號、給後台讀的
# 狀態檔。任何一步失敗都只記一行，不擋啟動 —— doctor 會指出是哪一步。
cmd_app_prestart() {
    [[ $EUID -eq 0 && -f "$ENV_FILE" && -x "$VENV/bin/python" ]] || return 0
    id "$RUN_USER" >/dev/null 2>&1 || return 0
    ( ensure_dirs ) || warn "app-prestart：目錄沒有補齊"
    grep -qE '^[[:space:]]*APP_RUNNER_DIR[[:space:]]*=' "$ENV_FILE" || env_set APP_RUNNER_DIR "$RUNNER_DIR"
    grep -qE '^[[:space:]]*APP_BACKUP_DIR[[:space:]]*=' "$ENV_FILE" || env_set APP_BACKUP_DIR "$BACKUP_DIR"
    ( NEW_CREDENTIALS=""; ensure_accounts ) || warn "app-prestart：登入帳號沒有補齊"
    ( write_status_files ) >/dev/null 2>&1 || true
    return 0
}

cmd_rollback() {
    require_root rollback
    require_installed
    [[ -f "$LAST_GOOD" ]] || fail "沒有紀錄上一個正常的版本（$LAST_GOOD 不存在）"
    local target
    target="$(cat "$LAST_GOOD")"
    if [[ "$target" == "$(git rev-parse HEAD)" ]]; then
        info "目前就是上一個健康檢查通過的版本（$(git rev-parse --short HEAD)）"
        return 0
    fi
    step "回到 $(git rev-parse --short "$target")"
    git reset --hard --quiet "$target"
    ensure_venv
    install_units
    start_services
    wait_healthy || fail "回滾之後健康檢查沒過。跑 \`sudo mf-topomon doctor\`。"
    write_status_files
    info "已回滾（資料庫沒有動；需要的話用 mf-topomon restore）"
}

# --------------------------------------------------------------------------
# 指令：doctor
# --------------------------------------------------------------------------

DOCTOR_FAILED=0
ok()     { echo "  ${GREEN}[ OK ]${NC} $*"; }
notice() { echo "  ${YELLOW}[WARN]${NC} $*"; }
# 沒過的檢查：說是什麼，然後給一行可以直接貼上的指令
bad() {
    echo "  ${RED}[FAIL]${NC} $1"
    if [[ -n "${2:-}" ]]; then echo "         ${BOLD}→ $2${NC}"; fi
    DOCTOR_FAILED=1
}

unit_active() { systemctl is-active --quiet "$1" 2>/dev/null; }

cmd_doctor() {
    require_tools curl
    echo "${BOLD}mf-topomon doctor${NC}  ($DIR @ $(git rev-parse --short HEAD 2>/dev/null || echo '?'))"
    echo

    # 1. 設定檔。其他每一條都讀它，所以它不在的話後面的結果沒有意義。
    if [[ ! -f "$ENV_FILE" ]]; then
        bad "找不到 $ENV_FILE —— 這台機器還沒有安裝" "sudo mf-topomon install"
        echo
        echo "${RED}${BOLD}有檢查沒過${NC}"
        return 1
    fi
    ok "設定檔 $ENV_FILE"

    # 2. 資料庫
    if as_postgres pg_isready -q >/dev/null 2>&1; then
        ok "PostgreSQL 在接受連線"
    else
        bad "PostgreSQL 沒有在接受連線" "sudo systemctl restart postgresql"
    fi

    # 3. 兩個服務。少了收集器，UI 還是會好好地顯示一份越來越舊的拓撲 ——
    #    這是最難看出來的壞法。
    local unit
    for unit in "$APP_UNIT" "$COLLECTOR_UNIT"; do
        if unit_active "$unit"; then
            ok "$unit 在執行"
        else
            bad "$unit 沒有在執行" \
                "sudo systemctl restart $unit && sudo journalctl -u $unit -n 40 --no-pager"
        fi
    done

    # 4. 應用程式自己說它連得上資料庫（直接問它，不經過 nginx）
    local ready
    ready="$(api_get /health/ready || true)"
    if [[ "$ready" == *'"status":"ready"'* ]]; then
        ok "應用程式就緒，資料庫連得上"
    else
        bad "應用程式沒有就緒${ready:+：$ready}" \
            "sudo journalctl -u $APP_UNIT -n 40 --no-pager"
    fi

    # 5. nginx：在聽、而且登入有在要求
    if curl -fsS --max-time 5 "http://127.0.0.1:$(http_port)/health" >/dev/null 2>&1; then
        ok "nginx 在聽 :$(http_port) 與 :$(https_port)"
    else
        bad "nginx 沒有回應 http://127.0.0.1:$(http_port)/health" \
            "sudo nginx -t && sudo systemctl restart nginx"
    fi

    local code
    code="$(auth_probe)"
    if [[ "$code" == "401/200" ]]; then
        ok "登入有在要求（沒登入的 API 回 401，登入頁可用）"
    elif [[ "${code%/*}" == "200" ]]; then
        bad "沒登入的 API 回 200 —— 這個介面現在不用登入就進得去" "sudo mf-topomon install"
    else
        bad "沒登入的 API 回 ${code%/*}、登入頁資訊回 ${code#*/}（應該是 401 與 200）" \
            "sudo tail -n 20 /var/log/nginx/error.log && sudo journalctl -u $APP_UNIT -n 40 --no-pager"
    fi

    # 管理員帳號。全部停用或刪光的話，後台就沒有人進得去
    local accounts admins
    accounts="$(app_cli users 2>/dev/null || true)"
    admins="$(echo "$accounts" | awk -F'\t' '$3=="admin" && $4=="active"' | grep -c . || true)"
    if (( admins > 0 )); then
        ok "啟用中的管理員帳號 $admins 個"
    else
        bad "沒有啟用中的管理員帳號，沒有人進得了後台" "sudo mf-topomon password"
    fi

    # 背景執行器：後台的備份、還原、升級、憑證都靠它
    if systemctl is-active --quiet "$RUNNER_PATH_UNIT" 2>/dev/null; then
        ok "背景執行器待命中（$RUNNER_PATH_UNIT）"
    else
        bad "$RUNNER_PATH_UNIT 沒有在執行 —— 後台的備份、升級、憑證都不會做" \
            "sudo systemctl enable --now $RUNNER_PATH_UNIT"
    fi

    # 6. 收集器自己的回報。API 與收集器是兩個行程，所以這是唯一能回答
    #    「有人在輪詢嗎」的地方（docs/SDD-API.md 第 8 節）。API 要登入，所以
    #    以服務帳號直接讀資料庫（app/cli.py collector-status）。
    local status summary waited=0
    status="$(app_cli collector-status 2>/dev/null || true)"
    # 收集器啟動後先等資料庫幾秒才寫第一筆回報。剛重啟完就跑 doctor 會撞上
    # 那段空窗，而「服務在跑、只是還沒回報」不該被說成壞掉 —— 所以只在服務
    # 確實在跑的時候，多等它一下。
    while [[ "$status" == *'"reporting":false'* ]] && unit_active "$COLLECTOR_UNIT" \
            && (( waited < 20 )); do
        sleep 2
        waited=$((waited + 2))
        status="$(app_cli collector-status 2>/dev/null || true)"
    done
    if [[ -z "$status" ]]; then
        bad "讀不到收集器狀態" "sudo journalctl -u $APP_UNIT -n 40 --no-pager"
    else
        summary="$(python3 -c "
import json, sys
d = json.loads(sys.argv[1])
procs = d.get('processes') or []
live = [p for p in procs if p.get('live')]
print('live=%d/%d' % (len(live), len(procs)))
for p in live:
    iv = p.get('intervals') or {}
    print('   %ss 前回報  盤點 %ss / 快 %ss / 位址 %ss' % (
        p.get('age_seconds'), iv.get('inventory'), iv.get('fast'), iv.get('addresses')))
for w in d.get('warnings') or []:
    print('!! ' + (w.get('message') or w.get('code', '')))
" "$status" 2>/dev/null || true)"
        local live_line
        live_line="$(echo "$summary" | head -n 1)"
        if [[ -z "$summary" ]]; then
            notice "收集器狀態讀到了但解析不出來"
        elif [[ "$live_line" == "live=0/"* ]]; then
            bad "沒有收集器在回報 —— 拓撲與告警都不會更新" \
                "sudo systemctl restart $COLLECTOR_UNIT && sudo journalctl -u $COLLECTOR_UNIT -n 40 --no-pager"
        else
            ok "收集器 $live_line"
            echo "$summary" | tail -n +2 | grep -v '^!!' | sed 's/^/     /' || true
            local line
            while IFS= read -r line; do
                if [[ -n "$line" ]]; then notice "${line#!! }"; fi
            done < <(echo "$summary" | grep '^!!' || true)
        fi
    fi

    # 7. 執行中的程式是不是磁碟上的這一版。git pull 之後沒有重啟的話，
    #    兩個服務跑的還是舊的程式碼，而 git log 說的是新的。
    local head started changed
    head="$(git log -1 --format=%ct 2>/dev/null || echo 0)"
    started="$(systemctl show -p ActiveEnterTimestamp --value "$APP_UNIT" 2>/dev/null || true)"
    changed="$(date -d "$started" +%s 2>/dev/null || echo 0)"
    if [[ "$changed" != "0" && "$head" != "0" ]]; then
        local checkout
        checkout="$(stat -c %Y "$DIR/.git/HEAD" 2>/dev/null || echo 0)"
        if (( checkout > changed )); then
            bad "程式碼在服務啟動之後更新過，但服務沒有重啟" "sudo mf-topomon restart"
        else
            ok "執行中的就是磁碟上的版本"
        fi
    fi

    # 8. 備份
    local newest
    newest="$(ls -1t "$BACKUP_DIR"/mf-topomon-*.sql.gz 2>/dev/null | head -n 1 || true)"
    if [[ -z "$newest" ]]; then
        notice "還沒有任何備份"
        echo "         ${BOLD}→ sudo mf-topomon backup${NC}"
    else
        ok "最近的備份：$(basename "$newest")"
    fi

    # 9. 能跑但不該這樣留著的東西
    if [[ -f "$INITIAL_PASSWORD_FILE" ]]; then
        notice "安裝時的管理密碼還存在 $INITIAL_PASSWORD_FILE"
        echo "         ${BOLD}→ 登入並換掉密碼之後：sudo rm $INITIAL_PASSWORD_FILE${NC}"
    fi
    if [[ "$(env_value APP_SECRET_KEY)" == "change-this-to-random-string" ]]; then
        notice "APP_SECRET_KEY 還是範例值"
        echo "         ${BOLD}→ sudo sed -i \"s|^APP_SECRET_KEY=.*|APP_SECRET_KEY=\$(openssl rand -hex 32)|\" $ENV_FILE && sudo mf-topomon restart${NC}"
    fi

    echo
    if [[ $DOCTOR_FAILED -eq 0 ]]; then
        echo "${GREEN}${BOLD}一切正常${NC}"
    else
        echo "${RED}${BOLD}有檢查沒過${NC}（照上面 → 後面的指令處理）"
    fi
    return $DOCTOR_FAILED
}

# --------------------------------------------------------------------------
# 指令：日常
# --------------------------------------------------------------------------

cmd_status() {
    step "服務"
    systemctl --no-pager --lines=0 status "$APP_UNIT" "$COLLECTOR_UNIT" "$RUNNER_PATH_UNIT" nginx postgresql 2>/dev/null \
        | grep -E '^(●|○|×|\*)|Active:' || true
    step "收集器"
    local status
    status="$(app_cli collector-status 2>/dev/null || true)"
    if [[ -n "$status" ]]; then
        echo "$status" | python3 -m json.tool 2>/dev/null || echo "$status"
    else
        warn "讀不到收集器狀態（$APP_UNIT 沒在跑？）"
    fi
    step "版本"
    cmd_version
}

cmd_version() {
    echo "  commit   $(git rev-parse --short HEAD 2>/dev/null || echo '?') \
($(git log -1 --format=%cd --date=short 2>/dev/null || echo '?'))"
    echo "  branch   $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    echo "  程式     $DIR"
    echo "  設定     $ENV_FILE"
    echo "  python   $("$VENV/bin/python" --version 2>/dev/null || echo '（還沒有環境）')"
}

cmd_logs() {
    # 預設跟著跑：會打 logs 的時候通常是「現在發生了什麼事」
    local unit
    case "${1:-}" in
        app)        unit=(-u "$APP_UNIT") ;;
        collector)  unit=(-u "$COLLECTOR_UNIT") ;;
        proxy|nginx) exec tail -n 200 -f /var/log/nginx/error.log /var/log/nginx/access.log ;;
        "")         unit=(-u "$APP_UNIT" -u "$COLLECTOR_UNIT") ;;
        *)          fail "不認得的服務：$1（app / collector / proxy）" ;;
    esac
    exec journalctl "${unit[@]}" -n 200 -f
}

cmd_restart() {
    require_root restart
    require_installed
    start_services
    wait_healthy || warn "重啟後健康檢查沒過，跑 \`sudo mf-topomon doctor\` 看哪一條"
}

cmd_start() {
    require_root start
    require_installed
    systemctl start postgresql nginx "$APP_UNIT" "$COLLECTOR_UNIT"
    wait_healthy || warn "健康檢查沒過，跑 \`sudo mf-topomon doctor\` 看哪一條"
}

cmd_stop() {
    require_root stop
    systemctl stop "$APP_UNIT" "$COLLECTOR_UNIT"
    info "已停止兩個服務（nginx 與 PostgreSQL 是主機的服務，沒有動；資料都在）"
}

# 本機管理員帳號的密碼：建立或重設，而且一定是管理員、一定是啟用的。這是
# 「後台進不去了」的最後手段 —— 忘記密碼、帳號被鎖、AD 掛了、有人把所有
# 管理員都降級了，都從這裡回來。
cmd_password() {
    require_root password
    require_installed
    local user="${1:-admin}" out password
    if [[ -n "${2:-}" ]]; then
        # 密碼走 stdin，不放在 app_cli 的參數上（ps 看得到參數）
        out="$(printf '%s\n' "$2" | app_cli set-password "$user" -)" \
            || fail "密碼不符合規則（至少 10 個字元、不能包含帳號名稱）"
        password="$2"
    else
        out="$(app_cli set-password "$user")" || fail "重設失敗。看：sudo journalctl -u $APP_UNIT -n 40"
        password="$(echo "$out" | awk '{print $3}')"
    fi
    # 換過密碼之後，安裝時存的那一份就是錯的了
    rm -f "$INITIAL_PASSWORD_FILE"
    echo
    echo "  ${BOLD}${user} / ${password}${NC}"
    echo
    if [[ -z "${2:-}" ]]; then
        info "這是隨機產生的，第一次登入時會要求換掉。這個帳號原本的登入都已登出。"
    else
        info "立刻生效。這個帳號原本的登入都已登出。"
    fi
}

cmd_backup() {
    require_root backup
    require_installed
    local dir="$BACKUP_DIR" keep="$BACKUP_KEEP" trigger="cli" by="${SUDO_USER:-root}"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --keep)    keep="$2"; shift 2 ;;
            --trigger) trigger="$2"; shift 2 ;;
            --by)      by="$2"; shift 2 ;;
            *)         dir="$1"; shift ;;
        esac
    done
    [[ "$keep" =~ ^[0-9]+$ ]] && (( keep >= 1 )) || fail "--keep 要是正整數"
    local stamp file config
    if [[ "$dir" == "$BACKUP_DIR" ]]; then
        install -d -m 750 -o root -g "$RUN_USER" "$dir"
    else
        install -d -m 700 "$dir"
    fi
    stamp="$(date +%Y%m%d-%H%M%S)"
    # 同一秒內的第二份（排程剛好撞上手動）不能蓋掉第一份
    while [[ -e "$dir/mf-topomon-${stamp}.sql.gz" ]]; do
        sleep 1
        stamp="$(date +%Y%m%d-%H%M%S)"
    done
    file="$dir/mf-topomon-${stamp}.sql.gz"
    config="$dir/mf-topomon-${stamp}-config.tar.gz"

    info "備份資料庫到 $file"
    # --no-owner：dump 裡不寫「這張表是誰的」，復原時誰倒進去就是誰的。這樣
    # 同一份備份可以倒進角色名稱不同的資料庫（例如從 Docker 的部署搬過來）。
    (umask 077 && as_postgres pg_dump --clean --if-exists --no-owner "$DB_NAME" | gzip > "$file") \
        || { rm -f "$file"; fail "備份失敗"; }
    if [[ "$dir" == "$BACKUP_DIR" ]]; then
        # 後台列得出來、管理員下載得到（ensure_dirs 說明為什麼可以）
        chown "root:$RUN_USER" "$file"
        chmod 640 "$file"
        if [[ -d "$RUNNER_DIR" ]]; then
            printf '{"%s": {"trigger": "%s", "by": "%s"}}' "$(basename "$file")" "$trigger" "$by" \
                | python3 -I "$DIR/scripts/runner_helper.py" merge-json "$RUNNER_DIR/backups.json" || true
            chown "root:$RUN_USER" "$RUNNER_DIR/backups.json" 2>/dev/null || true
        fi
    fi
    info "$(du -h "$file" | cut -f1)"

    # 資料庫備份復原之後，還需要連得上它的密碼、TLS 憑證、
    # 以及介面上存的設定 —— 少了這些，一份完好的 dump 也用不起來。
    (umask 077 && tar -czf "$config" \
        -C / "${ETC_DIR#/}" \
        $([[ -f "$DATA_DIR/settings.json" ]] && echo "${DATA_DIR#/}/settings.json") \
        2>/dev/null) || warn "設定沒有備份到（$config）"
    info "設定與憑證：$config（裡面有密碼，權限是 600）"

    # 只留最近幾份：沒有人會去刪它們
    local old
    old="$(ls -1t "$dir"/mf-topomon-*.sql.gz 2>/dev/null | tail -n +$((keep + 1)) || true)"
    if [[ -n "$old" ]]; then
        local dump
        while IFS= read -r dump; do
            rm -f "$dump" "${dump%.sql.gz}-config.tar.gz"
        done <<< "$old"
        info "刪掉 $(echo "$old" | wc -l) 份舊備份（保留最近 $keep 份）"
    fi
}

cmd_restore() {
    require_root restore
    require_installed
    local file="" yes=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --yes) yes=1; shift ;;
            *)     file="$1"; shift ;;
        esac
    done
    [[ -n "$file" ]] || fail "用法：mf-topomon restore <備份檔.sql.gz> [--yes]"
    [[ -f "$file" ]] || fail "找不到 $file"

    if [[ $yes -eq 0 ]]; then
        echo "${RED}${BOLD}這會用 $file 蓋掉現在資料庫裡的全部內容。${NC}"
        echo "確定的話輸入 RESTORE："
        local answer
        read -r answer
        [[ "$answer" == "RESTORE" ]] || fail "取消"
    fi

    step "停掉會寫入的服務"
    systemctl stop "$APP_UNIT" "$COLLECTOR_UNIT"
    step "復原"
    # dump 是 --clean --if-exists 做的，所以它自己會先 DROP 再建
    #
    # 以服務的角色倒進去（SET ROLE），所以每一張表都是它的。不這樣做的話，
    # 表會屬於 postgres，而應用程式會在第一個查詢得到 permission denied ——
    # 尤其是從別的部署搬來的 dump，裡面的擁有者是一個這裡沒有的角色
    # （那幾行 ALTER ... OWNER 會失敗，可以不管）。
    { echo "SET ROLE ${DB_USER};"; gunzip -c "$file"; } \
        | as_postgres psql -q -d "$DB_NAME" >/dev/null 2>"$BACKUP_DIR/.last-restore.log" \
        || fail "復原失敗。資料庫現在可能是半套的 —— 不要啟動，先看
    $BACKUP_DIR/.last-restore.log"
    # 倒進去的東西要真的讀得到，否則「復原完成」是假的
    if ! (cd / && runuser -u postgres -- psql -tAq -d "$DB_NAME" \
            -c "SET ROLE ${DB_USER}; SELECT count(*) FROM devices" >/dev/null 2>&1); then
        fail "資料倒進去了，但服務的角色讀不到 devices 這張表。看
    $BACKUP_DIR/.last-restore.log"
    fi
    step "啟動"
    start_services
    wait_healthy || fail "復原後健康檢查沒過。跑 \`sudo mf-topomon doctor\`"
    info "復原完成"
}

cmd_uninstall() {
    require_root uninstall
    local purge=0
    if [[ "${1:-}" == "--purge" ]]; then purge=1; fi

    echo "${RED}${BOLD}這會停掉並移除 mf-topomon 的兩個服務與 nginx 站台。${NC}"
    if [[ $purge -eq 1 ]]; then
        echo "${RED}${BOLD}--purge：資料庫、設定、憑證與服務帳號也會一起刪掉，無法復原。${NC}"
    else
        echo "資料庫、$ETC_DIR 與 $DATA_DIR 會留下來。"
    fi
    echo "確定的話輸入 UNINSTALL："
    local answer
    read -r answer
    [[ "$answer" == "UNINSTALL" ]] || fail "取消"

    systemctl disable --now "$APP_UNIT" "$COLLECTOR_UNIT" "$RUNNER_PATH_UNIT" >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/$APP_UNIT" "$UNIT_DIR/$COLLECTOR_UNIT" \
        "$UNIT_DIR/$RUNNER_PATH_UNIT" "$UNIT_DIR/$RUNNER_UNIT"
    systemctl daemon-reload || true
    rm -f "$NGINX_ENABLED" "$NGINX_AVAILABLE"
    # nginx 與 PostgreSQL 是主機的套件，可能還有別人在用，所以只重新載入
    systemctl reload nginx >/dev/null 2>&1 || true

    if [[ $purge -eq 1 ]]; then
        as_postgres dropdb --if-exists "$DB_NAME" || true
        as_postgres dropuser --if-exists "$DB_USER" || true
        rm -rf "$ETC_DIR" "$DATA_DIR" "$RUNNER_DIR"
        if id "$RUN_USER" >/dev/null 2>&1; then userdel "$RUN_USER" || true; fi
    fi
    if [[ "$(readlink -f "$LINK" 2>/dev/null || true)" == "$DIR/scripts/mf-topomon.sh" ]]; then
        rm -f "$LINK"
    fi

    info "已移除。留在原地的："
    echo "         $DIR            （程式碼）"
    echo "         $BACKUP_DIR     （備份）"
    if [[ $purge -eq 0 ]]; then
        echo "         $ETC_DIR、$DATA_DIR、資料庫 $DB_NAME"
    fi
    echo "         nginx 與 PostgreSQL 這兩個套件（主機的，沒有移除）"
}

# --------------------------------------------------------------------------
# 指令：run-requests（後台要求的工作，由 mf-topomon-runner.path 觸發）
# --------------------------------------------------------------------------
#
# 請求檔是服務帳號寫的，所以這裡把它當成不可信的輸入：讀檔、檢查擁有者、
# 驗證每個欄位都由 scripts/runner_helper.py 做（不跟隨符號連結），而且
# 驗過的值在這裡用之前還會再比對一次格式。結果寫在 $RUNNER_DIR/jobs ——
# root 的目錄，服務帳號只讀得到。

RUNNER_HELPER() { python3 -I "$DIR/scripts/runner_helper.py" "$@"; }

job_status() {
    # job_status <id> <json>：合併進 jobs/<id>.json
    local id="$1" json="$2" file="$RUNNER_DIR/jobs/$1.json"
    printf '%s' "$json" | RUNNER_HELPER merge-json "$file"
    chown "root:$RUN_USER" "$file"
    chmod 640 "$file"
}

json_str() {
    python3 -I -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
}

# 一個工作。在子 shell 裡跑：cmd_* 失敗時呼叫的 fail 會 exit，只結束這個子 shell
run_one_job() {
    local id="$1" kind="$2" by="$3" args="$4" stage="$5"
    local log="$RUNNER_DIR/jobs/$id.log" rc=0 arg_keep arg_trigger arg_name
    arg_keep="$(python3 -I -c 'import json,sys; print(json.loads(sys.argv[1]).get("keep", ""))' "$args")"
    arg_trigger="$(python3 -I -c 'import json,sys; print(json.loads(sys.argv[1]).get("trigger", "manual"))' "$args")"
    arg_name="$(python3 -I -c 'import json,sys; print(json.loads(sys.argv[1]).get("name", ""))' "$args")"

    : > "$log"
    chown "root:$RUN_USER" "$log"
    chmod 640 "$log"
    job_status "$id" "{\"state\": \"running\", \"started_at\": \"$(date -Is)\"}"

    (
        set -euo pipefail
        # 第二道檢查：helper 已經驗過，這裡在用之前再比對一次
        case "$kind" in
            backup)
                [[ "$arg_keep" =~ ^[0-9]{1,3}$ ]] || fail "keep 格式不對"
                [[ "$arg_trigger" =~ ^(manual|scheduled)$ ]] || fail "trigger 格式不對"
                cmd_backup --keep "$arg_keep" --trigger "$arg_trigger" --by "$by"
                ;;
            delete-backup)
                [[ "$arg_name" =~ ^mf-topomon-[0-9]{8}-[0-9]{6}\.sql\.gz$ ]] || fail "備份名稱格式不對"
                [[ -f "$BACKUP_DIR/$arg_name" ]] || fail "找不到 $arg_name"
                rm -f "$BACKUP_DIR/$arg_name" "$BACKUP_DIR/${arg_name%.sql.gz}-config.tar.gz"
                info "已刪除 $arg_name"
                ;;
            restore)
                [[ "$arg_name" =~ ^mf-topomon-[0-9]{8}-[0-9]{6}\.sql\.gz$ ]] || fail "備份名稱格式不對"
                [[ -f "$BACKUP_DIR/$arg_name" ]] || fail "找不到 $arg_name"
                step "還原前先備份現在的資料"
                cmd_backup --keep 365 --trigger manual --by "$by"
                cmd_restore "$BACKUP_DIR/$arg_name" --yes
                ;;
            upgrade)
                cmd_upgrade
                ;;
            check-update)
                git fetch --quiet origin "$BRANCH" || fail "git fetch 失敗 —— 這台機器連得到 $(git remote get-url origin) 嗎？"
                RUNNER_HELPER git-info "$DIR" "origin/$BRANCH" | RUNNER_HELPER write-json "$RUNNER_DIR/update.json"
                chown "root:$RUN_USER" "$RUNNER_DIR/update.json"
                chmod 640 "$RUNNER_DIR/update.json"
                local behind
                behind="$(git rev-list --count "HEAD..origin/$BRANCH")"
                if [[ "$behind" == "0" ]]; then
                    info "已經是最新版（$(git rev-parse --short HEAD)）"
                else
                    info "有 $behind 個新的版本可以升級（$(git rev-parse --short HEAD) → $(git rev-parse --short "origin/$BRANCH")）"
                fi
                ;;
            tls)
                apply_tls "$stage"
                ;;
            doctor)
                cmd_doctor
                ;;
            *)
                fail "不認得的工作 $kind"
                ;;
        esac
    ) >> "$log" 2>&1 || rc=$?

    # 給畫面看的一行：最後一行有內容的輸出，去掉顏色碼
    local summary
    summary="$(sed -e 's/\x1b\[[0-9;]*m//g' "$log" | grep -v '^[[:space:]]*$' | tail -n 1 | cut -c1-300)"
    sed -i -e 's/\x1b\[[0-9;]*m//g' "$log"
    if [[ $rc -eq 0 ]]; then
        job_status "$id" "{\"state\": \"ok\", \"exit_code\": 0, \"finished_at\": \"$(date -Is)\", \"summary\": $(json_str "$summary")}"
    else
        job_status "$id" "{\"state\": \"failed\", \"exit_code\": $rc, \"finished_at\": \"$(date -Is)\", \"summary\": $(json_str "$summary")}"
    fi
}

# 上傳的憑證：再驗一次、換上、nginx -t、reload；任何一步失敗就換回原本的
apply_tls() {
    local stage="$1" cert="$1/cert.pem" key="$1/key.pem" stamp
    [[ -f "$cert" && -f "$key" ]] || fail "請求裡沒有憑證或私鑰"
    openssl x509 -in "$cert" -noout >/dev/null 2>&1 || fail "讀不出憑證（不是 PEM？）"
    openssl pkey -in "$key" -noout >/dev/null 2>&1 || fail "讀不出私鑰（有密碼保護或不是 PEM？）"
    if [[ "$(openssl x509 -in "$cert" -noout -pubkey)" != "$(openssl pkey -in "$key" -pubout)" ]]; then
        fail "私鑰與憑證不是一對"
    fi
    openssl x509 -in "$cert" -noout -checkend 0 >/dev/null 2>&1 || fail "憑證已經過期"
    info "新憑證：$(openssl x509 -in "$cert" -noout -subject | sed 's/^subject=//')"
    info "到期：$(openssl x509 -in "$cert" -noout -enddate | sed 's/^notAfter=//')"

    stamp="$(date +%Y%m%d-%H%M%S)"
    cp -p "$AUTH_DIR/tls.crt" "$AUTH_DIR/tls.crt.bak-$stamp"
    cp -p "$AUTH_DIR/tls.key" "$AUTH_DIR/tls.key.bak-$stamp"
    install -m 644 -o root -g root "$cert" "$AUTH_DIR/tls.crt"
    install -m 600 -o root -g root "$key" "$AUTH_DIR/tls.key"
    if ! nginx -t >/dev/null 2>&1 || ! systemctl reload nginx; then
        nginx -t || true
        cp -p "$AUTH_DIR/tls.crt.bak-$stamp" "$AUTH_DIR/tls.crt"
        cp -p "$AUTH_DIR/tls.key.bak-$stamp" "$AUTH_DIR/tls.key"
        systemctl reload nginx || true
        fail "nginx 不接受新的憑證，已換回原本的（上面是 nginx 的訊息）"
    fi
    # reload 是非同步的：等新的 worker 起來，確認對外送出的就是這張
    local want got="" tries=0
    want="$(openssl x509 -in "$cert" -noout -fingerprint -sha256)"
    while (( tries < 10 )); do
        got="$(echo | openssl s_client -connect "127.0.0.1:$(https_port)" 2>/dev/null \
            | openssl x509 -noout -fingerprint -sha256 2>/dev/null || true)"
        [[ "$got" == "$want" ]] && break
        tries=$((tries + 1))
        sleep 1
    done
    [[ "$got" == "$want" ]] || warn "nginx 已重新載入，但 :$(https_port) 送出的還不是新憑證（${got:-連不上}）"
    secure_auth_dir
    write_status_files
    # 舊的留最近 5 份
    ls -1t "$AUTH_DIR"/tls.crt.bak-* 2>/dev/null | tail -n +6 | while IFS= read -r old; do
        rm -f "$old" "${old/tls.crt.bak-/tls.key.bak-}"
    done
    info "HTTPS 憑證已更新"
}

cmd_run_requests() {
    require_root run-requests
    require_installed
    local uid
    uid="$(id -u "$RUN_USER")"
    install -d -m 750 -o root -g "$RUN_USER" "$RUNNER_DIR" "$RUNNER_DIR/jobs"
    printf '{"last_run": "%s"}' "$(date -Is)" | RUNNER_HELPER merge-json "$RUNNER_DIR/runner.json"
    chown "root:$RUN_USER" "$RUNNER_DIR/runner.json"
    chmod 640 "$RUNNER_DIR/runner.json"

    local handled=0 req stage id kind by args error
    # 一次處理到請求目錄空了為止；上限只是防止一個寫不停的服務帳號讓這裡永遠不結束
    while (( handled < 20 )); do
        stage="$(mktemp -d)"
        req="$(RUNNER_HELPER take "$REQUEST_DIR" "$uid" "$stage")" || { rm -rf "$stage"; break; }
        if [[ -z "$req" ]]; then
            rm -rf "$stage"
            break
        fi
        handled=$((handled + 1))
        error="$(python3 -I -c 'import json,sys; print(json.loads(sys.argv[1]).get("error", ""))' "$req")"
        id="$(python3 -I -c 'import json,sys; print(json.loads(sys.argv[1]).get("id") or "")' "$req")"
        if [[ -n "$error" ]]; then
            warn "拒絕一個請求：$error"
            if [[ "$id" =~ ^[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$ ]]; then
                job_status "$id" "{\"id\": \"$id\", \"state\": \"failed\", \"finished_at\": \"$(date -Is)\", \"summary\": $(json_str "請求被拒絕：$error")}"
            fi
            rm -rf "$stage"
            continue
        fi
        kind="$(python3 -I -c 'import json,sys; print(json.loads(sys.argv[1])["kind"])' "$req")"
        by="$(python3 -I -c 'import json,sys; print(json.loads(sys.argv[1])["by"])' "$req")"
        args="$(python3 -I -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1])["args"]))' "$req")"
        [[ "$id" =~ ^[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$ ]] || { rm -rf "$stage"; continue; }
        [[ "$by" =~ ^[A-Za-z0-9._@-]{1,128}$ ]] || by="web"
        job_status "$id" "$(printf '%s' "$req" | python3 -I -c 'import json,sys; d=json.load(sys.stdin); d["state"]="queued"; print(json.dumps(d))')"
        info "工作 $id：$kind（$by）"
        run_one_job "$id" "$kind" "$by" "$args" "$stage"
        rm -rf "$stage"
    done

    # 工作紀錄留最近 200 筆
    ls -1t "$RUNNER_DIR"/jobs/*.json 2>/dev/null | tail -n +201 | while IFS= read -r old; do
        rm -f "$old" "${old%.json}.log"
    done
}

usage() {
    cat <<EOF
${BOLD}mf-topomon${NC} —— LLDP/CDP 拓撲監控的安裝與維護（原生安裝）

  ${BOLD}mf-topomon install${NC}             安裝，或修復一個裝到一半／被改壞的安裝
  ${BOLD}mf-topomon upgrade${NC}             拉最新版、備份、更新、健康檢查（沒過會自動回滾）
  ${BOLD}mf-topomon doctor${NC}              逐層健檢；沒過的每一條都附一行修復指令
  ${BOLD}mf-topomon status${NC}              服務狀態與收集器完整回報
  ${BOLD}mf-topomon logs [服務]${NC}         跟著看日誌（app / collector / proxy）
  ${BOLD}mf-topomon restart${NC}             重啟兩個服務並重新載入 nginx
  ${BOLD}mf-topomon start${NC}               啟動
  ${BOLD}mf-topomon stop${NC}                停止兩個服務（資料留著）
  ${BOLD}mf-topomon password [帳號] [密碼]${NC} 建立或重設本機管理員（省略密碼＝隨機，登入時要換）
  ${BOLD}mf-topomon backup [目錄]${NC}       備份資料庫與設定（預設 $BACKUP_DIR，保留 $BACKUP_KEEP 份）
  ${BOLD}mf-topomon restore <檔案> [--yes]${NC} 從備份復原（沒有 --yes 會先要你打字確認）
  ${BOLD}mf-topomon rollback${NC}            回到上一個健康檢查通過的版本
  ${BOLD}mf-topomon version${NC}             版本與路徑
  ${BOLD}mf-topomon uninstall [--purge]${NC} 移除服務（--purge 連資料與設定一起）

第一次安裝（Debian 12 / 13、Ubuntu 24.04）：

  curl -fsSL https://raw.githubusercontent.com/nox913106/net-lldp-topo/main/scripts/bootstrap.sh | sudo bash

文件：docs/DEPLOYMENT.md（部署）、docs/CONFIGURATION.md（設定）、
      docs/USER_MANUAL.md（使用）、docs/TROUBLESHOOTING_LLDP.md（收不到鄰居）
EOF
}

main() {
    local command="${1:-}"
    [[ $# -gt 0 ]] && shift || true
    case "$command" in
        install)    cmd_install "$@" ;;
        upgrade)    cmd_upgrade "$@" ;;
        doctor)     cmd_doctor "$@" ;;
        status)     cmd_status "$@" ;;
        logs)       cmd_logs "$@" ;;
        restart)    cmd_restart "$@" ;;
        start)      cmd_start "$@" ;;
        stop)       cmd_stop "$@" ;;
        password)   cmd_password "$@" ;;
        backup)     cmd_backup "$@" ;;
        restore)    cmd_restore "$@" ;;
        rollback)   cmd_rollback "$@" ;;
        version)    cmd_version "$@" ;;
        uninstall)  cmd_uninstall "$@" ;;
        run-requests) cmd_run_requests "$@" ;;
        app-prestart) cmd_app_prestart "$@" ;;
        # 不在說明裡：印出 install 會寫進 nginx 的那一份站台設定，不安裝它。
        # 測試用它確認「被測試的設定檔」與「裝上去的設定檔」是同一份。
        render-nginx) render_nginx_site ;;
        -h|--help|help) usage ;;
        "")         usage; exit 2 ;;
        *)          echo "${RED}不認得的指令：$command${NC}" >&2; echo >&2
                    usage >&2; exit 2 ;;
    esac
}

main "$@"
