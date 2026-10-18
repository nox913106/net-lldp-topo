# topomon 部署指南

## 一鍵安裝

一台乾淨的 Ubuntu 22.04／24.04（或 Debian），一行。**這個 repo 目前是私
有的**，所以權杖要給兩次 —— 一次拿這個腳本（`curl` 用），一次給腳本
clone（`sudo` 傳進去）：

```bash
export GITHUB_TOKEN=ghp_xxx          # 要有這個 repo 的 Contents: Read
curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://raw.githubusercontent.com/nox913106/net-lldp-topo/main/install.sh \
  | sudo GITHUB_TOKEN="$GITHUB_TOKEN" bash
```

repo 若轉成公開，就是乾淨的一行：

```bash
curl -fsSL https://raw.githubusercontent.com/nox913106/net-lldp-topo/main/install.sh | sudo bash
```

> **私有 repo 的 raw 內容對未認證的請求回 404，不是 401。** 所以「curl 回
> 404」第一個要懷疑的是權杖，不是路徑打錯。

### 權杖要留在機器上嗎

預設會留：git 會把它寫進 `/opt/topomon/.git/config`，而 install.sh 把那個
檔案收成 `600`（只有 root 讀得到）。這是取捨 —— 留著，`topomon update` 才
能自己去 fetch；不留，每次更新都要手動帶一次權杖。

**不想留權杖**就用唯讀的 deploy key：

```bash
# 在這台機器上產生一把，公鑰貼到 GitHub 的 repo → Settings → Deploy keys
sudo ssh-keygen -t ed25519 -N '' -f /root/.ssh/topomon_deploy -C topomon@$(hostname)
sudo cat /root/.ssh/topomon_deploy.pub         # 貼上去，不要勾 write access

sudo tee -a /root/.ssh/config >/dev/null <<'EOF'
Host github.com
  IdentityFile /root/.ssh/topomon_deploy
  IdentitiesOnly yes
EOF

# 然後用 SSH 的來源安裝（不需要 GITHUB_TOKEN）
sudo TOPOMON_REPO=git@github.com:nox913106/net-lldp-topo.git bash install.sh
```

deploy key 綁單一 repo、可以隨時撤銷、而且不會出現在任何 `.git/config`
裡 —— 代價是 install.sh 本身要先用別的方法弄到這台機器上（`scp` 或
`gh api ... /contents/install.sh`）。

跑完會印出網址與一組登入帳密 —— **那組密碼只會出現這一次**（忘了就
`sudo topomon password` 重設一組）。

重複執行是安全的：已經裝過的機器上它等同一次更新，`.env`、`auth/`（密鑰、
密碼、憑證）與資料庫都不會被動到。

想先看它要做什麼再決定，就分兩步：

```bash
curl -fsSL https://raw.githubusercontent.com/nox913106/net-lldp-topo/main/install.sh -o install.sh
less install.sh
sudo bash install.sh
```

可以改的環境變數：

| 變數 | 預設 | 作用 |
|---|---|---|
| `TOPOMON_DIR` | `/opt/topomon` | 裝在哪 |
| `TOPOMON_BRANCH` | `main` | 跟哪個分支 |
| `TOPOMON_REPO` | GitHub 上的本專案 | 換成 fork 或內部鏡像 |

```bash
# 例：裝到別的位置，並且跟內部鏡像
curl -fsSL .../install.sh | sudo TOPOMON_DIR=/srv/topomon TOPOMON_REPO=https://git.example/topo.git bash
```

---

## 那一行做了什麼

1. 確認是 root、是 Linux、有 apt。
2. 補齊 `git` / `curl` / `openssl`。
3. 裝 docker（先試官方源，連不到就退回發行版的 `docker.io` +
   `docker-compose-v2`）。
4. 把 repo clone 到 `/opt/topomon`。
5. 之後交給 `tools/topomon deploy`，它才是真正做事的那一個：
   * 從 `.env.example` 產生 `.env`，裡面放**隨機**的 `APP_SECRET_KEY` 與
     資料庫密碼；
   * 產生 `auth/htpasswd`（隨機密碼）與自簽 TLS 憑證；
   * `docker compose up -d --build`；
   * 等 `/health/ready` 回答「資料庫也連得上」為止，最多 180 秒；
   * 把自己連到 `/usr/local/bin/topomon`。

沒有互動式問題 —— 它要能從管線被餵進 bash。

---

## 日常維護：`topomon`

```bash
sudo topomon <指令>
```

| 指令 | 做什麼 |
|---|---|
| `deploy` | 第一次部署，或改了 `.env`／`docker-compose.yml` 之後重新套用 |
| `update` | 拉最新版 → 重建 → 健康檢查。**沒過會自動回滾到更新前那個 commit** |
| `rollback` | 手動回到上一個健康檢查通過的版本 |
| `health` | 逐層檢查（見下），全過才 exit 0 —— 可以直接丟給監控 |
| `status` | 容器狀態 ＋ 收集器完整回報 |
| `logs [服務]` | 跟著看日誌：`app` / `collector` / `proxy` / `db` |
| `restart [服務]` / `start` / `stop` | 如字面 |
| `password [帳號] [密碼]` | 重設介面登入密碼，省略密碼＝隨機產生，立即生效 |
| `backup [目錄]` | 備份資料庫（預設 `./backups`，保留最近 14 份） |
| `restore <檔案>` | 從備份復原（要打字確認） |
| `version` | 版本、分支、路徑 |
| `uninstall [--purge]` | 移除容器；`--purge` 連資料庫內容一起刪 |

`topomon` 是指向 `/opt/topomon/tools/topomon` 的**連結**，不是複本 ——
`topomon update` 之後，下一次執行的就是新版的它。

### `topomon health` 檢查什麼

```
$ sudo topomon health
topomon health  (/opt/topomon @ a1b2c3d)

  [ OK ] 容器：app collector db proxy redis
  [ OK ] 代理在聽 :80
  [ OK ] 應用程式就緒，資料庫連得上
  [ OK ] 認證有在要求（/ 回 401）
  [ OK ] 收集器 live=1/1
     shard 0/1  12.3s 前回報  盤點 300s / 快 60s / 位址 3600s

一切正常
```

五條都是「會無聲壞掉」的東西：

* **容器** —— 少了 `collector`，UI 還是會好好地顯示一份越來越舊的拓撲；
* **代理在聽** —— 設定寫錯 nginx 會起不來，而 app 還活著；
* **資料庫連得上** —— `/health` 只證明行程活著，`/health/ready` 才會真的
  `SELECT 1`；
* **認證有在要求** —— 一個沒有密碼的管理介面長得跟有密碼的一模一樣，直到
  有人逛進來。這一條直接去打 `/` 看是不是 401；
* **收集器在回報** —— API 與收集器是兩個行程，`live=0/1` 表示沒有人在輪詢，
  而那時候畫面上什麼都不會變紅（docs/SDD-API.md 第 8 節）。

要接到監控系統：

```bash
*/5 * * * * root /usr/local/bin/topomon health >/tmp/topomon-health.log 2>&1 || \
    logger -t topomon "health check failed"
```

---

## 登入與憑證

這個系統**沒有使用者的概念**，認證整個落在反向代理上（`deploy/nginx.conf`）：

* 8080（應用程式）與 5432（資料庫）只綁在 `127.0.0.1`，從網路上連不到。
  唯一的入口是代理的 80／443。
* 80 一律轉到 443 —— basic auth 的密碼是 base64 不是加密，走明文等於把管理
  密碼送上線路。
* `/health` 與 `/health/ready` 不要密碼（負載平衡器與 `topomon health` 要
  用），其餘全部要，**包含 `/docs`**：那是可以直接打 API 的互動式介面。

這一層有測試在守：`tests/test_proxy_auth.py` 會用真的 nginx 跑
`deploy/nginx.conf`，確認沒帶密碼的請求根本到不了應用程式。CI 每次都跑。

### 換成真的憑證

預設是自簽的（10 年），所以瀏覽器第一次會警告。換掉：

```bash
sudo cp fullchain.pem /opt/topomon/auth/tls.crt
sudo cp privkey.pem   /opt/topomon/auth/tls.key
sudo chmod 600 /opt/topomon/auth/tls.key
sudo topomon restart proxy
```

Let's Encrypt 需要這台機器從外面連得到 80 埠，管理網段通常沒有 —— 內部 CA
或公司憑證比較實際。

### 換密碼

```bash
sudo topomon password              # 隨機產生一組，印出來
sudo topomon password noc          # 換帳號，密碼隨機
sudo topomon password noc 'mypass' # 兩個都自己指定
```

立即生效，不用重啟：nginx 每次請求都重讀 `htpasswd`。

### 還要擋更多

代理可以再加來源限制（例如只讓管理網段連）。在
`/opt/topomon/deploy/nginx.conf` 的 `location /` 裡加：

```nginx
allow 10.0.0.0/8;
deny  all;
```

然後 `sudo topomon restart proxy`。注意這是版控裡的檔案，`topomon update`
會因為本地改動而拒絕更新 —— 改完後 commit 到你自己的 fork，或用
`TOPOMON_REPO` 指到那個 fork。

---

## 更新

```bash
sudo topomon update
```

它會：fetch → `--ff-only` 合併 → 重建 → 等 `/health/ready`。**健康檢查沒過
就自動 `git reset --hard` 回更新前的 commit 再起一次**，並且把失敗的日誌印
出來。理由很簡單：這個指令最可能是在沒有人盯著的時候跑的，一個「更新完就壞
掉而且不說」的部署指令比沒有這個指令更糟。

有本地未提交的改動時它會拒絕動作（`git pull` 會蓋掉它們）。`.env` 與
`auth/` 不在版控裡，所以不受影響。

資料庫的 schema：新增的**可為 NULL 的欄位**與**索引**會在啟動時自動補上
（`app/db/database.py` 的 `_sync_upgrade_schema`）。改欄位型別、刪欄位、加
NOT NULL 欄位不會自動做 —— 那種改動會在日誌裡留下一行 warning，需要人工
處理。

---

## 備份與復原

```bash
sudo topomon backup                    # → /opt/topomon/backups/topomon-YYYYmmdd-HHMMSS.sql.gz
sudo topomon backup /mnt/nas/topomon   # 放別的地方
sudo topomon restore /opt/topomon/backups/topomon-20260101-030000.sql.gz
```

每天一次：

```bash
0 3 * * * root /usr/local/bin/topomon backup >/dev/null 2>&1
```

**備份裡沒有 `.env` 與 `auth/`**（密鑰、資料庫密碼、憑證、htpasswd）。那兩個
目錄要自己另外收好，否則復原之後連不上資料庫、也進不了介面：

```bash
sudo tar czf /mnt/nas/topomon-secrets.tgz -C /opt/topomon .env auth
```

---

## 防火牆

```bash
sudo apt install -y ufw
sudo ufw allow ssh          # 先開這個，否則會把自己鎖在外面
sudo ufw allow 443/tcp      # Web UI
sudo ufw allow 80/tcp       # 只為了把人轉到 443
sudo ufw allow 162/udp      # 只有要收 SNMP trap 才需要
sudo ufw enable
sudo ufw status
```

**不要**開 8080 與 5432：它們只綁在 127.0.0.1，開了也沒用，而且那是在對自己
宣告一件不成立的事。

---

## 從舊版升級（原本用 deploy.sh 裝的）

`deploy.sh` 已經移除，由 `install.sh` + `topomon` 取代。原本的 Docker 部署
升級方式：

```bash
cd /opt/DEV-lldp-topomon      # 或你原本的目錄
sudo git remote set-url origin https://github.com/nox913106/net-lldp-topo.git
sudo git fetch origin main && sudo git merge --ff-only origin/main
sudo ./tools/topomon deploy
```

`deploy` 會補上缺的東西（代理、憑證、htpasswd），而且**不會**動你現有的
`.env` 與資料庫。要注意兩件事：

1. 原本 8080 是對外開的，現在不是了 —— 網址換成
   `https://<ip>/`，並且要輸入它印出來的帳密。
2. 原本的資料庫密碼（預設 `topomon`）會留著。`topomon health` 會提醒你，但
   它只綁在 127.0.0.1，所以不急 —— 真要換得連 `pgdata` volume 一起重建。

Native（systemd）部署不受影響，見附錄 A。

---

## 系統需求

| 項目 | 最低 | 建議 |
|---|---|---|
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04／24.04 LTS |
| CPU | 2 cores | 4 cores（500 台約吃 24% 的 CPU，見 docs/PERFORMANCE.md） |
| RAM | 4 GB | 8 GB |
| Disk | 20 GB | 50 GB SSD |
| 網路 | 連得到設備的管理位址（udp/161） | 另外收 trap 要 udp/162 |

docker 部署不需要主機上先有 Python、PostgreSQL 或 Redis —— 都在容器裡。

---

## 疑難排解

### 1. 資料庫連線失敗

```bash
# 檢查 PostgreSQL 狀態
sudo systemctl status postgresql

# 檢查連線
sudo -u postgres psql -c "SELECT 1;"

# 檢查 pg_hba.conf 設定
sudo nano /etc/postgresql/15/main/pg_hba.conf
# 確保有以下行：
# local   all   topomon   md5
# host    all   topomon   127.0.0.1/32   md5
```

### 2. Redis 連線失敗

```bash
# 檢查 Redis 狀態
sudo systemctl status redis-server

# 測試連線
redis-cli ping
```

### 3. Port 衝突

```bash
# 檢查 8080 port 使用情況
sudo lsof -i :8080

# 如需更換 port，修改 .env 或 docker-compose.yml
```

### 4. 權限問題

```bash
# 修正目錄權限
sudo chown -R $USER:$USER /opt/topomon

# 確認虛擬環境權限
chmod -R 755 /opt/topomon/venv
```

### 5. Docker 官方源連線失敗（網路限制）

如果遇到無法連接 `download.docker.com` 的問題：

**方案 A：使用 Ubuntu 官方倉庫（建議）**

```bash
# 使用 Ubuntu 官方倉庫的 docker.io 套件
sudo apt update
sudo apt install -y docker.io docker-compose-v2

# 啟用並啟動服務
sudo systemctl enable docker
sudo systemctl start docker

# 驗證安裝
docker --version
```

> **注意**：Ubuntu 官方倉庫的 Docker 版本可能較舊，但功能完整，適合內網環境。

**方案 B：使用鏡像源**

```bash
# 使用阿里雲鏡像（中國區域適用）
curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://mirrors.aliyun.com/docker-ce/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

**方案 C：離線安裝**

1. 在可連網的電腦下載 Docker 安裝包：
   - 前往 https://download.docker.com/linux/ubuntu/dists/
   - 下載對應版本的 `.deb` 檔案

2. 使用 USB 或 SCP 傳輸到目標伺服器

3. 手動安裝：
```bash
sudo dpkg -i containerd.io_*.deb
sudo dpkg -i docker-ce-cli_*.deb
sudo dpkg -i docker-ce_*.deb
sudo dpkg -i docker-compose-plugin_*.deb
```

### 6. 日誌在哪裡

| 想看什麼 | 指令 |
|---|---|
| 全部 | `topomon logs` |
| API（含 HTTP 錯誤） | `topomon logs app` |
| 收集器（輪詢、告警、trap） | `topomon logs collector` |
| 代理（誰連進來、401） | `topomon logs proxy` |
| 資料庫 | `topomon logs db` |
| Native 部署 | `journalctl -u topomon-web` / `-u topomon-collector` |

`topomon logs` 預設是跟著跑的（`-f`），Ctrl-C 離開。要多一點細節把
`APP_LOG_LEVEL` 設成 `DEBUG` 再 `topomon restart` —— 收不到 LLDP 鄰居的
排查要的就是那些行（docs/TROUBLESHOOTING_LLDP.md）。

---

## 附錄 A：Native 安裝（systemd，不被 topomon 管理）

容器裡不能跑、或是公司政策不給 docker 的時候用這個。**`topomon` 這個指令
不管 native 部署** —— 一個每個子指令都要分兩種情況的工具，等於兩種情況都
只有一半被測過。下面的步驟要自己維護，更新與備份也是。

同樣的三件事在 native 下自己補：

* 反向代理與認證：系統的 nginx，把 `deploy/nginx.conf` 當範本（`upstream`
  改成 `127.0.0.1:8080`），並且讓 uvicorn 只綁 `127.0.0.1`；
* 健康檢查：`curl -fsS http://127.0.0.1:8080/health/ready`；
* 更新：`git pull` 之後 `systemctl restart topomon-web topomon-collector`。

### A.1 系統依賴、資料庫與應用程式

#### 1. 安裝系統依賴

```bash
# 更新系統
sudo apt update && sudo apt upgrade -y

# 安裝 Python 3.11
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev

# 安裝建置工具
sudo apt install -y build-essential libpq-dev git
```

#### 2. 安裝 PostgreSQL 15

```bash
# 新增 PostgreSQL 儲存庫
sudo sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo apt-key add -

# 安裝 PostgreSQL
sudo apt update
sudo apt install -y postgresql-15

# 啟動並啟用 PostgreSQL
sudo systemctl enable postgresql
sudo systemctl start postgresql

# 建立資料庫和使用者
sudo -u postgres psql << EOF
CREATE USER topomon WITH PASSWORD 'your-strong-password';
CREATE DATABASE topomon OWNER topomon;
GRANT ALL PRIVILEGES ON DATABASE topomon TO topomon;
EOF
```

#### 3. 安裝 Redis 7

```bash
# 安裝 Redis
sudo apt install -y redis-server

# 編輯配置（可選：設定密碼）
sudo nano /etc/redis/redis.conf

# 啟動並啟用 Redis
sudo systemctl enable redis-server
sudo systemctl start redis-server

# 驗證 Redis
redis-cli ping
```

#### 4. 部署應用程式

```bash
# 建立應用程式目錄
sudo mkdir -p /opt/topomon
sudo chown -R $USER:$USER /opt/topomon
cd /opt/topomon

# Clone 專案
git clone https://github.com/nox913106/net-lldp-topo.git .

# 建立虛擬環境
python3.11 -m venv venv
source venv/bin/activate

# 安裝 Python 套件。requirements.txt 是 pip-compile 產生的 lock（每個套件
# 釘到確切版本並帶 hash），--require-hashes 讓少一個 hash 的手改在這裡就失敗，
# 而不是裝到當天 PyPI 上的任何版本
pip install --upgrade pip
pip install --require-hashes -r requirements.txt

# 建立環境設定檔
cp .env.example .env
nano .env
```

#### 5. 設定環境變數

編輯 `/opt/topomon/.env`：

```env
# 應用程式
APP_NAME=mf-topomon
APP_DEBUG=false
APP_LOG_LEVEL=INFO
APP_SECRET_KEY=your-random-secret-key-here

# 資料庫（使用本地連線）
DATABASE_URL=postgresql+asyncpg://topomon:your-strong-password@localhost:5432/topomon

# Redis
REDIS_URL=redis://localhost:6379

# SNMP 設定
SNMP_DEFAULT_COMMUNITY=your-community-string
SNMP_TIMEOUT=5
SNMP_RETRIES=2

# Collector 設定
COLLECTOR_INTERVAL=300
COLLECTOR_CONCURRENT=20
```

---

### A.2 Systemd 服務

#### 建立 Web 應用服務

```bash
sudo tee /etc/systemd/system/topomon-web.service << EOF
[Unit]
Description=LLDP Topology Monitor Web Service
After=network.target postgresql.service redis-server.service

[Service]
Type=exec
User=$USER
Group=$USER
WorkingDirectory=/opt/topomon
Environment="PATH=/opt/topomon/venv/bin"
EnvironmentFile=/opt/topomon/.env
ExecStart=/opt/topomon/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

#### 建立 Collector 服務

```bash
sudo tee /etc/systemd/system/topomon-collector.service << EOF
[Unit]
Description=LLDP Topology Monitor Collector Service
After=network.target postgresql.service redis-server.service topomon-web.service

[Service]
Type=exec
User=$USER
Group=$USER
WorkingDirectory=/opt/topomon
Environment="PATH=/opt/topomon/venv/bin"
EnvironmentFile=/opt/topomon/.env
ExecStart=/opt/topomon/venv/bin/python -m app.collector
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

#### 啟動服務

```bash
# 重新載入 systemd
sudo systemctl daemon-reload

# 啟用並啟動服務
sudo systemctl enable topomon-web topomon-collector
sudo systemctl start topomon-web topomon-collector

# 檢查服務狀態
sudo systemctl status topomon-web topomon-collector

# 查看日誌
sudo journalctl -u topomon-web -f
sudo journalctl -u topomon-collector -f
```

---

## 另外幾份文件

* [專案介紹網站](index.html) —— 功能與畫面，可以直接給別人看
* [設定說明](CONFIGURATION.md) —— 每一個環境變數，以及設備端要不要改
* [使用手冊](USER_MANUAL.md) —— 介面怎麼用
* [收不到 LLDP 鄰居](TROUBLESHOOTING_LLDP.md) —— 拓撲空白時從這裡查
* [效能與記憶體](PERFORMANCE.md) —— 500 台的實測與每一次改動的量測
