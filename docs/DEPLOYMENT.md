# mf-topomon 部署指南

## 一行安裝

一台乾淨的 Debian 12／13 或 Ubuntu 24.04：

```bash
curl -fsSL https://raw.githubusercontent.com/nox913106/net-lldp-topo/main/scripts/bootstrap.sh | sudo bash
```

最精簡的系統連 `curl` 都沒有，那就先
`sudo apt-get update && sudo apt-get install -y curl`。

這是**原生安裝**：PostgreSQL、nginx 與兩個 systemd 服務直接裝在主機上，沒有
容器。裝完會印出網址與登入帳密。重複執行是安全的 —— 已經裝好的會變成一次
更新，設定、密碼、憑證與資料庫都不會被動到。

可以用環境變數改的東西（放在 `sudo` 後面）：

| 變數 | 預設 | 用途 |
|---|---|---|
| `MF_TOPOMON_DIR` | `/opt/mf-topomon` | 程式碼放哪裡 |
| `MF_TOPOMON_BRANCH` | `main` | 要跟的分支 |
| `MF_TOPOMON_REPO` | 這個 repo | 來源（fork 或內部鏡像） |
| `MF_TOPOMON_MODE` | `native` | `docker` 改用 Docker Compose，見[最後一節](#用-docker-部署次要方式) |

```bash
# 例：內網，從自己的鏡像裝
curl -fsSL https://git.example.lan/mirror/net-lldp-topo/raw/main/scripts/bootstrap.sh \
  | sudo MF_TOPOMON_REPO=https://git.example.lan/mirror/net-lldp-topo.git bash
```

---

## 那一行做了什麼

`bootstrap.sh` 只做兩件事：補齊 `git`／`curl`／`openssl`，把 repo clone 到
`/opt/mf-topomon`。之後交給 repo 裡的 `scripts/mf-topomon.sh install`，它才是
真正做事的那一個 —— 所以「一行安裝」與「之後手動重跑 install」是同一段程式：

1. `apt` 安裝 `python3`、`python3-venv`、`postgresql`、`nginx`（已經有的不動）。
2. 建立系統帳號 `mftopomon`：不能登入，兩個服務用它跑。
3. 產生 `/etc/mf-topomon/mf-topomon.env`，裡面是**隨機**的 `APP_SECRET_KEY`
   與資料庫密碼。
4. 產生登入用的 `htpasswd`（隨機密碼）與自簽 TLS 憑證，並讓 nginx 的 worker
   讀得到 `htpasswd`。
5. 在主機的 PostgreSQL 裡建立角色與資料庫，都叫 `mftopomon`。
6. 建立 Python 環境 `/opt/mf-topomon/.venv`，照 `requirements.txt` 的 hash
   鎖定安裝。
7. 寫入兩個 systemd 單元與 nginx 站台，啟動。
8. 等 `/health/ready` 回答「資料庫也連得上」，再帶一組錯的帳密去問一次，
   確認 nginx 真的驗得了密碼（要 401，不是 500）。
9. 把自己連到 `/usr/local/bin/mf-topomon`。

沒有互動式問題 —— 它要能從管線被餵進 bash。

### 東西在哪裡

| 什麼 | 位置 | 誰的 |
|---|---|---|
| 程式碼 | `/opt/mf-topomon` | root；服務帳號只讀得到 |
| Python 環境 | `/opt/mf-topomon/.venv` | root |
| 設定 | `/etc/mf-topomon/mf-topomon.env` | root:mftopomon 640 |
| 登入與憑證 | `/etc/mf-topomon/auth/` | `htpasswd` root:www-data 640、`tls.key` root 600 |
| 介面上存的設定 | `/var/lib/mf-topomon/settings.json` | mftopomon |
| 資料庫 | 主機的 PostgreSQL，資料庫 `mftopomon` | postgres |
| 服務 | `mf-topomon-app.service`、`mf-topomon-collector.service` | 以 mftopomon 執行 |
| 反向代理 | `/etc/nginx/sites-available/mf-topomon` | 主機的 nginx |
| 備份 | `/var/backups/mf-topomon/` | root 700 |

兩個服務都不是 root。應用程式只聽 `127.0.0.1:8080`；收集器為了能綁 udp/162
收 trap，只拿到 `CAP_NET_BIND_SERVICE` 這一項。

### 安裝時的密碼

印在畫面上一次，另外存一份在 `/etc/mf-topomon/.admin-initial-password`
（root 才讀得到）—— 安裝的輸出被管線吃掉的時候，「只印一次」等於沒印。

登入之後請換掉，檔案會跟著被刪除：

```bash
sudo mf-topomon password
```

`mf-topomon doctor` 在那個檔案還在的時候會提醒你。

---

## 日常維護：`mf-topomon`

```
mf-topomon install             安裝，或修復一個裝到一半／被改壞的安裝
mf-topomon upgrade             拉最新版、備份、更新、健康檢查（沒過會自動回滾）
mf-topomon doctor              逐層健檢；沒過的每一條都附一行修復指令
mf-topomon status              服務狀態與收集器完整回報
mf-topomon logs [服務]         跟著看日誌（app / collector / proxy）
mf-topomon restart             重啟兩個服務並重新載入 nginx
mf-topomon start | stop        啟動／停止兩個服務（資料留著）
mf-topomon password [帳號] [密碼]   重設介面登入密碼（省略密碼＝隨機產生）
mf-topomon backup [目錄]       備份資料庫與設定
mf-topomon restore <檔案>      從備份復原（會先要你打字確認）
mf-topomon rollback            回到上一個健康檢查通過的版本
mf-topomon version             版本與路徑
mf-topomon uninstall [--purge] 移除服務（--purge 連資料與設定一起）
```

改了 `/etc/mf-topomon/mf-topomon.env` 之後 `sudo mf-topomon restart`。
每一個設定的說明在 [CONFIGURATION.md](CONFIGURATION.md)。

### `mf-topomon doctor` 檢查什麼

```
$ sudo mf-topomon doctor
mf-topomon doctor  (/opt/mf-topomon @ a1b2c3d)

  [ OK ] 設定檔 /etc/mf-topomon/mf-topomon.env
  [ OK ] PostgreSQL 在接受連線
  [ OK ] mf-topomon-app.service 在執行
  [ OK ] mf-topomon-collector.service 在執行
  [ OK ] 應用程式就緒，資料庫連得上
  [ OK ] nginx 在聽 :80 與 :443
  [ OK ] 認證有在要求（/ 回 401）
  [ OK ] nginx 驗得了密碼（錯的帳密被拒絕，而不是 500）
  [ OK ] 收集器 live=1/1
        12.3s 前回報  盤點 300s / 快 60s / 位址 3600s
  [ OK ] 執行中的就是磁碟上的版本
  [ OK ] 最近的備份：mf-topomon-20261115-141255.sql.gz

一切正常
```

沒過的時候，每一條後面都跟著可以直接貼上的指令：

```
  [FAIL] mf-topomon-collector.service 沒有在執行
         → sudo systemctl restart mf-topomon-collector.service && sudo journalctl -u mf-topomon-collector.service -n 40 --no-pager
```

每一條都是「會無聲壞掉」的東西：

* **收集器沒在跑** —— UI 還是會好好地顯示一份越來越舊的拓撲，這是最難看
  出來的壞法。
* **`/health` 與 `/health/ready`** —— 前者只證明行程活著，後者才會真的
  `SELECT 1`。
* **認證有在要求** —— 一個沒有密碼的管理介面長得跟有密碼的一模一樣，直到
  有人逛進來。
* **nginx 驗得了密碼** —— 上一條分不出「會驗密碼」與「讀不到密碼檔」：沒帶
  帳密的請求在 nginx 打開 `htpasswd` 之前就回 401 了。所以這一條帶一組一定
  錯的帳密去問，401 才是對的；500 表示沒有任何人登入得進去，而其他每一條都
  還是綠的。
* **執行中的就是磁碟上的版本** —— `git pull` 之後沒有重啟的話，跑的還是
  舊的程式碼。

exit code 是 0（全過）或 1，所以可以直接接到監控：

```bash
# /etc/cron.d/mf-topomon-doctor
*/5 * * * * root /usr/local/bin/mf-topomon doctor >/tmp/mf-topomon-doctor.log 2>&1 || \
    logger -t mf-topomon "doctor found a problem"
```

---

## 登入與憑證

這個系統**沒有使用者的概念**，認證整個落在 nginx 上（`deploy/nginx.conf`）：

* 應用程式只聽 `127.0.0.1:8080`，從網路上連不到。唯一的入口是 nginx 的
  80／443。
* 80 一律轉到 443 —— basic auth 的密碼是 base64 不是加密，走明文等於把管理
  密碼送上線路。
* `/health` 與 `/health/ready` 不要密碼（負載平衡器與 `doctor` 要用），其餘
  全部要，**包含 `/docs`**：那是可以直接打 API 的互動式介面。

這一層有測試在守：`tests/test_proxy_auth.py` 用真的 nginx 跑
`deploy/nginx.conf`，確認沒帶密碼的請求根本到不了應用程式；裝到主機上的
那一份是同一個檔案，只換了上游位址、憑證目錄與埠
（`tests/test_deploy.py`）。CI 每次都跑。

### 換成真的憑證

預設是自簽的（10 年），所以瀏覽器第一次會警告。換掉：

```bash
sudo cp fullchain.pem /etc/mf-topomon/auth/tls.crt
sudo cp privkey.pem   /etc/mf-topomon/auth/tls.key
sudo chmod 600 /etc/mf-topomon/auth/tls.key
sudo systemctl reload nginx
```

Let's Encrypt 需要這台機器從外面連得到 80 埠，管理網段通常沒有 —— 內部 CA
或公司憑證比較實際。

### 換密碼

```bash
sudo mf-topomon password              # 隨機產生一組，印出來
sudo mf-topomon password noc          # 換帳號，密碼隨機
sudo mf-topomon password noc 'mypass' # 兩個都自己指定
```

立即生效，不用重啟：nginx 每次請求都重讀 `htpasswd`。

### 換埠，或與別的站台共用 nginx

在 `/etc/mf-topomon/mf-topomon.env` 設 `TOPOMON_HTTP_PORT`／
`TOPOMON_HTTPS_PORT`，然後 `sudo mf-topomon install`（它會重寫站台設定並
檢查 `nginx -t`）。

安裝會停用發行版附的預設站台（`sites-enabled/default`）—— 它也宣告自己是
80 埠的 `default_server`，兩個同時啟用 nginx 會拒絕啟動。檔案還留在
`sites-available/default`。

---

## 升級

```bash
sudo mf-topomon upgrade
```

1. `git fetch` + fast-forward 到 `origin/main`（有本地改動就停下來，不會蓋掉）；
2. 有新版本的話，先備份資料庫與設定；
3. 跑跟 `install` 同一串步驟（套件、Python 依賴、單元檔、nginx 站台）並重啟；
4. 健康檢查，包含「nginx 驗得了密碼」；
5. **沒過就自動回到升級前的 commit**，重啟，然後把失敗的日誌印出來。

回滾只回程式碼。資料庫的結構是往前相容地補的（新增欄位與索引），所以舊程式
碼對著新結構可以跑；真的需要回到升級前的資料，用第 2 步那份備份
`mf-topomon restore`。

`mf-topomon rollback` 手動回到上一個健康檢查通過的版本。

---

## 備份與復原

```bash
sudo mf-topomon backup                       # → /var/backups/mf-topomon/
sudo mf-topomon restore /var/backups/mf-topomon/mf-topomon-20261115-141255.sql.gz
```

每次備份是兩個檔案，權限都是 600：

* `mf-topomon-<時間>.sql.gz` —— 資料庫；
* `mf-topomon-<時間>-config.tar.gz` —— `/etc/mf-topomon`（設定、資料庫密碼、
  `htpasswd`、憑證）與 `settings.json`。少了它，一份完好的 dump 復原之後
  既連不上資料庫、也進不了介面。

保留最近 14 份（`MF_TOPOMON_BACKUP_KEEP`）。排程：

```bash
# /etc/cron.d/mf-topomon-backup
0 3 * * * root /usr/local/bin/mf-topomon backup >/dev/null 2>&1
```

備份在同一台機器上，等於沒有備份 —— 請把 `/var/backups/mf-topomon` 納入你們
本來的異地備份。

---

## 防火牆

```bash
sudo apt install -y ufw
sudo ufw allow ssh          # 先開這個，否則會把自己鎖在外面
sudo ufw allow 443/tcp      # Web UI
sudo ufw allow 80/tcp       # 只為了把人轉到 443
sudo ufw allow 162/udp      # 只有要收 SNMP trap 才需要
sudo ufw enable
```

**不要**開 8080 與 5432：它們只聽 127.0.0.1，開了也沒用。

---

## 系統需求

| 項目 | 最低 | 建議 |
|---|---|---|
| OS | Debian 12／13、Ubuntu 24.04（amd64） | 同左 |
| CPU | 2 cores | 4 cores（500 台約吃 24% 的 CPU，見 PERFORMANCE.md） |
| RAM | 4 GB | 8 GB |
| Disk | 20 GB | 50 GB SSD |
| 網路 | 連得到設備的管理位址（udp/161） | 另外收 trap 要 udp/162 |

清單以外的系統安裝程式會停下來。要在別的 Debian 系發行版上試，加上
`MF_TOPOMON_ALLOW_UNSUPPORTED=1`；那表示沒有人跑過，不表示一定不行。

**這份安裝流程實際驗過的範圍**（PERFORMANCE.md 第 44 節）：Ubuntu 24.04、
Python 3.13、PostgreSQL 16、nginx 1.24，從一行安裝一路到登入、健檢、備份、
復原、升級、刻意弄壞的升級自動回滾、解除安裝、在留下的資料上重裝、以及
`--purge`。有一件事是用替身驗的：那個環境沒有 systemd，所以「systemd 照
單元檔把行程起起來」這一段是由一個讀同一份單元檔的替身做的。單元檔的沙箱
設定（`ProtectSystem` 等）因此沒有被執行過。**第一次在真的機器上安裝之後，
請跑一次 `sudo mf-topomon doctor`。**

安裝之後主機與瀏覽器都不需要連得到網際網路：介面用的檔案都隨程式一起送
（`static/vendor/`）。升級需要連得到 git 來源與 PyPI（或它們的內部鏡像）。

---

## 疑難排解

**先跑 `sudo mf-topomon doctor`** —— 它會指出是哪一層，並給出指令。

| 現象 | 看哪裡 |
|---|---|
| 安裝停在「Python 環境」 | 連不到 PyPI，或這台機器的 Python 沒有某個套件的 wheel。pip 的訊息會說是哪一個 |
| 安裝停在 nginx 的設定檢查 | 80／443 被別的站台佔用。換埠（上面「換埠」一節） |
| 輸入對的密碼得到 500 | nginx 讀不到 `htpasswd`。`sudo mf-topomon install` 會把權限修好 |
| 拓撲不更新 | `sudo mf-topomon logs collector` |
| 收不到任何鄰居 | [TROUBLESHOOTING_LLDP.md](TROUBLESHOOTING_LLDP.md) |
| 要更多細節 | `/etc/mf-topomon/mf-topomon.env` 設 `APP_LOG_LEVEL=DEBUG`，`sudo mf-topomon restart` |

日誌：

```bash
sudo mf-topomon logs               # 兩個服務，跟著跑
sudo mf-topomon logs collector     # 只看收集器
sudo journalctl -u mf-topomon-app --since "1 hour ago"
sudo tail -f /var/log/nginx/error.log
```

---

## 解除安裝

```bash
sudo mf-topomon uninstall            # 移除服務與 nginx 站台；資料、設定留著
sudo mf-topomon uninstall --purge    # 連資料庫、/etc/mf-topomon、服務帳號一起
```

nginx 與 PostgreSQL 是主機的套件，可能還有別的東西在用，所以兩種都不會移除
它們。沒有 `--purge` 的解除安裝之後重跑一行安裝，會接回原本的資料與密碼。

---

## 用 Docker 部署（次要方式）

主機不能裝套件、已經是一台 Docker 主機、或要在同一台機器上隔離幾套東西的
時候用這個：

```bash
curl -fsSL https://raw.githubusercontent.com/nox913106/net-lldp-topo/main/scripts/bootstrap.sh \
  | sudo MF_TOPOMON_MODE=docker bash
```

它會裝 docker（還沒有的話）、clone 到 `/opt/mf-topomon`，然後
`docker compose up`：五個容器（nginx、應用程式、收集器、PostgreSQL、Redis）。
管理用另一支工具，指令是對應的：

| 原生 | Docker |
|---|---|
| `mf-topomon install` | `mf-topomon-docker deploy` |
| `mf-topomon upgrade` | `mf-topomon-docker update` |
| `mf-topomon doctor` | `mf-topomon-docker health` |
| 其餘（status、logs、restart、password、backup、restore、rollback、uninstall） | 同名 |

差別：

* 設定在 `/opt/mf-topomon/.env`，憑證在 `/opt/mf-topomon/auth/`，備份在
  `/opt/mf-topomon/backups/`，資料庫在 docker volume `pgdata` 裡。
* 收集器用 host 網路（收 trap 需要看到設備真正的來源位址），其餘在 bridge。
* 初始密碼只印一次，不另外存檔。

### 從舊名稱的 Docker 部署（`/opt/topomon`）搬過來

舊的部署不用動也能繼續用：`sudo topomon update` 之後，`topomon` 這個指令會
轉到新的 `mf-topomon-docker`。它**不會**自己變成原生安裝。要搬：

```bash
# 1. 在舊的那一套留一份資料
sudo topomon backup                                   # → /opt/topomon/backups/

# 2. 停掉舊的，讓出 80／443（資料還在 volume 裡）
sudo topomon stop

# 3. 裝新的
curl -fsSL https://raw.githubusercontent.com/nox913106/net-lldp-topo/main/scripts/bootstrap.sh | sudo bash

# 4. 把資料倒進去
sudo mf-topomon restore /opt/topomon/backups/topomon-<時間>.sql.gz

# 5. 確認沒問題之後再移除舊的
sudo mf-topomon doctor
sudo topomon uninstall
```

第 4 步會在 `/var/backups/mf-topomon/.last-restore.log` 留下幾行
`role "topomon" does not exist` —— dump 裡寫著每張表屬於舊的資料庫角色，
而這裡沒有那個角色。可以不管：`restore` 是以新的角色把資料倒進去的，倒完會
確認它讀得到。登入密碼是新安裝產生的那一組，不是舊的；在舊介面的設定頁存過
的 SNMP 設定（`/opt/topomon/settings.json`）要自己複製到
`/var/lib/mf-topomon/`。

這條路徑驗過的是第 4 步 —— 一份 Docker 部署的備份倒進原生安裝，八台設備
都在、`doctor` 全過。前三步與第五步是兩套工具各自驗過的指令。

---

## 另外幾份文件

* [CONFIGURATION.md](CONFIGURATION.md) —— 每一個設定是什麼、什麼時候該動
* [USER_MANUAL.md](USER_MANUAL.md) —— 介面怎麼用
* [TROUBLESHOOTING_LLDP.md](TROUBLESHOOTING_LLDP.md) —— 收不到 LLDP 鄰居
* [PERFORMANCE.md](PERFORMANCE.md) —— 量測、設計決定與它們的理由
