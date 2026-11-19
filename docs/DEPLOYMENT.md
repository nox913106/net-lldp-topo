# mf-topomon 部署指南

## 一行安裝

一台乾淨的 Debian 12／13 或 Ubuntu 24.04：

```bash
curl -fsSL https://raw.githubusercontent.com/nox913106/net-lldp-topo/main/scripts/bootstrap.sh | sudo bash
```

最精簡的系統連 `curl` 都沒有，那就先
`sudo apt-get update && sudo apt-get install -y curl`。

這是**原生安裝**：PostgreSQL、nginx 與兩個 systemd 服務（外加一個後台工作
用的 path 單元）直接裝在主機上，沒有容器。裝完會印出網址與第一個管理員的
帳密。重複執行是安全的 —— 已經裝好的會變成一次
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
4. 產生自簽 TLS 憑證。
5. 在主機的 PostgreSQL 裡建立角色與資料庫，都叫 `mftopomon`。
6. 建立 Python 環境 `/opt/mf-topomon/.venv`，照 `requirements.txt` 的 hash
   鎖定安裝。
7. 建立第一個管理員帳號 `admin`（隨機密碼，第一次登入時要求換掉）。舊版留下
   的 `htpasswd` 會被匯入，帳號密碼不變。
8. 寫入 systemd 單元（應用程式、收集器、後台工作的 path 單元）與 nginx 站台，
   啟動。
9. 等 `/health/ready` 回答「資料庫也連得上」，再確認登入真的有在要求（沒登入
   的 API 要是 401，登入頁要用的那一條要是 200）。
10. 把自己連到 `/usr/local/bin/mf-topomon`。

沒有互動式問題 —— 它要能從管線被餵進 bash。

### 東西在哪裡

| 什麼 | 位置 | 誰的 |
|---|---|---|
| 程式碼 | `/opt/mf-topomon` | root；服務帳號只讀得到 |
| Python 環境 | `/opt/mf-topomon/.venv` | root |
| 設定 | `/etc/mf-topomon/mf-topomon.env` | root:mftopomon 640 |
| HTTPS 憑證 | `/etc/mf-topomon/auth/` | 目錄 root:www-data 750、`tls.key` root 600 |
| 介面上存的設定 | `/var/lib/mf-topomon/settings.json` | mftopomon |
| 後台送出的工作請求 | `/var/lib/mf-topomon/requests/` | mftopomon |
| 後台工作的結果 | `/var/lib/mf-topomon-runner/` | root:mftopomon 750（服務帳號只讀） |
| 資料庫（含帳號、稽核紀錄、通知設定） | 主機的 PostgreSQL，資料庫 `mftopomon` | postgres |
| 服務 | `mf-topomon-app.service`、`mf-topomon-collector.service` | 以 mftopomon 執行 |
| 後台工作 | `mf-topomon-runner.path` → `mf-topomon-runner.service` | root，只做固定的幾種工作 |
| 反向代理 | `/etc/nginx/sites-available/mf-topomon` | 主機的 nginx |
| 備份 | `/var/backups/mf-topomon/` | 目錄 root:mftopomon 750；dump 640，設定封存 600 |

兩個長駐服務都不是 root。應用程式只聽 `127.0.0.1:8080`；收集器為了能綁
udp/162 收 trap，只拿到 `CAP_NET_BIND_SERVICE` 這一項。

後台的備份、還原、升級、換憑證需要 root，應用程式沒有，也不該有。所以它只在
`requests/` 放一個請求檔；`mf-topomon-runner.path` 看到之後啟動一次
`mf-topomon run-requests`（root），由它**重新驗證每一個欄位**
（`scripts/runner_helper.py`：不跟隨符號連結、檢查檔案擁有者、只接受固定的
七種工作與固定格式的參數）再執行，結果寫在只有 root 寫得進去的
`/var/lib/mf-topomon-runner/`。這和在主機上打 `sudo mf-topomon backup` 是
同一段程式。

### 安裝時的密碼

印在畫面上一次，另外存一份在 `/etc/mf-topomon/.admin-initial-password`
（root 才讀得到）—— 安裝的輸出被管線吃掉的時候，「只印一次」等於沒印。

第一次登入時介面會要求換掉它；換完之後把那個檔案刪掉
（`sudo rm /etc/mf-topomon/.admin-initial-password`），`mf-topomon doctor`
在它還在的時候會提醒你。

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
mf-topomon password [帳號] [密碼]   建立或重設本機管理員（省略密碼＝隨機產生，登入時要換）
mf-topomon backup [目錄]       備份資料庫與設定
mf-topomon restore <檔案> [--yes]  從備份復原（沒有 --yes 會先要你打字確認）
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
  [ OK ] 登入有在要求（沒登入的 API 回 401，登入頁可用）
  [ OK ] 啟用中的管理員帳號 1 個
  [ OK ] 背景執行器待命中（mf-topomon-runner.path）
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
* **登入有在要求** —— 一個不用登入的管理介面長得跟要登入的一模一樣，直到
  有人逛進來。
* **管理員帳號** —— 所有管理員都被停用或刪掉的話，後台就沒有人進得去
  （修法：`sudo mf-topomon password`）。
* **背景執行器** —— 它沒在跑的時候，後台的「立即備份」「升級」按下去只會
  一直排隊。
* **執行中的就是磁碟上的版本** —— `git pull` 之後沒有重啟的話，跑的還是
  舊的程式碼。

exit code 是 0（全過）或 1，所以可以直接接到監控：

```bash
# /etc/cron.d/mf-topomon-doctor
*/5 * * * * root /usr/local/bin/mf-topomon doctor >/tmp/mf-topomon-doctor.log 2>&1 || \
    logger -t mf-topomon "doctor found a problem"
```

---

## 登入、帳號與憑證

登入在**應用程式**裡（`app/core/security_middleware.py`）：帳號、角色、AD
登入、稽核紀錄。nginx 守的是另外兩件事：

* 應用程式只聽 `127.0.0.1:8080`，從網路上連不到。唯一的入口是 nginx 的
  80／443。
* 80 一律轉到 443 —— 登入表單送的是密碼、登入之後的 cookie 是鑰匙，兩者都
  不該出現在明文的線路上。應用程式從 nginx 得知請求是 HTTPS，cookie 就會
  設成 `Secure`。

規則：

* `/health`、`/health/ready` 與登入頁不用登入；其餘全部要，**包含 `/docs`**
  （那是可以直接打 API 的互動式介面）。沒登入的頁面被轉到 `/login`，API 回 401。
* 會改東西的請求（POST／PUT／DELETE）要帶 `X-Requested-With` 標頭，否則
  403 —— 這是防跨站請求偽造（CSRF）：別的網站的表單帶得到 cookie，但帶不了
  自訂標頭。介面的每一個呼叫都會自動帶。
* 四種角色：**系統管理員**（全部，含後台）、**維運人員**（改設備、群組、告警
  設定檔、SNMP 設定、確認告警）、**檢視者**（只能看）、**訪客**（AD 帳號第一
  次登入，等管理員給角色之前什麼都看不到）。
* 連續登入失敗 5 次鎖 15 分鐘；閒置 8 小時、或登入滿 7 天自動登出。都可以在
  「後台 → 帳號管理」改。

這一層有測試在守：`tests/test_proxy_auth.py` 用真的 nginx 跑
`deploy/nginx.conf`、後面接真的應用程式，確認沒登入到不了任何東西、cookie
是 `Secure; HttpOnly; SameSite`、沒有 CSRF 標頭的變更被拒絕、明文 HTTP 在
送出任何東西之前就被轉走。CI 每次都跑。

### AD／LDAP 登入

「後台 → AD 驗證」：伺服器（可以填多台，連不上時換下一台）、網域、Base DN、
允許登入的群組（含巢狀群組）、LDAPS 用的公司 CA。**先按「測試」再儲存**：
測試會一步一步回報網路、TLS、帳號、群組哪一步過、哪一步沒過，而且不會儲存
任何東西。

使用者用自己的 AD 密碼登入，系統不保存 AD 密碼。**本機帳號一律先比對**，所以
網域控制站掛掉的那一天，本機管理員照樣登得進去 —— 至少保留一個；後台會擋住
「刪掉或降級最後一個本機管理員」。

### 忘記密碼、帳號被鎖、所有管理員都被降級

主機上這一行是最後手段，一定能回來：

```bash
sudo mf-topomon password              # 重設 admin，密碼隨機，登入時要換
sudo mf-topomon password noc          # 建立或重設 noc 這個本機管理員
sudo mf-topomon password noc 'mypass' # 自己指定密碼
```

立即生效；那個帳號原本的登入都會被登出。

### 換成真的憑證

預設是自簽的（10 年），所以瀏覽器第一次會警告。換掉最簡單的方法是
「後台 → HTTPS 憑證」上傳憑證、私鑰與中繼憑證：網頁先檢查一次（PEM 格式、
私鑰與憑證成對、在有效期內），背景執行器以 root 再檢查一次，換上之前先
`nginx -t`，nginx 不接受就換回原本的，換上之後確認 443 送出的真的是新的那
一張。到期前 30 天健康檢查會開始警告。

也可以在主機上手動換：

```bash
sudo cp fullchain.pem /etc/mf-topomon/auth/tls.crt
sudo cp privkey.pem   /etc/mf-topomon/auth/tls.key
sudo chmod 600 /etc/mf-topomon/auth/tls.key
sudo systemctl reload nginx
```

Let's Encrypt 需要這台機器從外面連得到 80 埠，管理網段通常沒有 —— 內部 CA
或公司憑證比較實際。

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

或在「後台 → 版本與升級」按「檢查更新」看有哪些新版本，再按「升級到最新版」
—— 背景執行器跑的就是同一個指令，頁面會顯示它的輸出，服務重啟時自己重新
連線。

1. `git fetch` + fast-forward 到 `origin/main`（有本地改動就停下來，不會蓋掉）；
2. 換成新版的程式之後，**由新版的這支腳本接手**接下來的每一步，所以新版加的
   安裝步驟在這一次升級就會生效；
3. 有新版本的話，先備份資料庫與設定；
4. 跑跟 `install` 同一串步驟（套件、Python 依賴、帳號、單元檔、nginx 站台）並重啟；
5. 健康檢查，包含「登入有在要求」；
6. **沒過就自動回到升級前的 commit**，重啟，然後把失敗的日誌印出來。

### 從沒有登入功能的舊版升級

舊版的認證是 nginx 上的一組 basic auth 密碼。升級之後：

* 那個帳號會被匯入成本機管理員，**密碼不變**（第一次登入時自動改存成 scrypt），
  `htpasswd` 改名為 `htpasswd.imported`；
* nginx 不再問密碼，改由登入頁登入。

舊版的 `mf-topomon upgrade` 不知道有「匯入帳號」這一步，所以這一步也放在
應用程式每次啟動前（`mf-topomon-app.service` 的 `ExecStartPre`，以 root
執行）—— 不管是哪一版的工具做的升級，帳號都會在。後台工作的 path 單元則要
等一次新版的 `install`：升級完之後跑一次 `sudo mf-topomon install`
（健康檢查與 `doctor` 都會提醒）。

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

或在「後台 → 備份還原」：設定排程（每天或每週、幾點、保留幾份）、立即備份、
下載（要再輸入一次自己的密碼）、還原（要輸入 RESTORE 與自己的密碼；還原前會
先自動備份現在的狀態）、刪除。預設每天 02:30 自動備份、保留 14 份。

每次備份是兩個檔案：

* `mf-topomon-<時間>.sql.gz` —— 資料庫，640 root:mftopomon（後台列得出來、
  管理員下載得到；裡面沒有服務帳號本來就讀不到的東西）；
* `mf-topomon-<時間>-config.tar.gz` —— `/etc/mf-topomon`（設定、資料庫密碼、
  TLS 私鑰）與 `settings.json`，600 root，只留在主機上。少了它，一份完好的
  dump 換到另一台機器復原之後連不上資料庫。

主機指令的保留份數是 14（`MF_TOPOMON_BACKUP_KEEP`）；後台的排程用後台設定的
份數。

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

**這份安裝流程實際驗過的範圍**（PERFORMANCE.md 第 44、45 節）：Ubuntu
24.04、Python 3.12／3.13、PostgreSQL 16、nginx 1.24，從一行安裝一路到登入、
健檢、備份、復原、升級、刻意弄壞的升級自動回滾、解除安裝、在留下的資料上
重裝、以及 `--purge`。後台的部分在**真的 systemd**（PID 1）上驗過：從舊版
（nginx basic auth）以舊版的 `mf-topomon upgrade` 升上來、舊密碼照樣登得
進去；網頁上的完整健檢、立即備份、下載、換 HTTPS 憑證（443 送出的指紋確實
換了）、檢查更新、一鍵升級、還原，每一個都由 path 單元觸發 root 工作完成，
單元檔的沙箱設定（`ProtectSystem=full`、`NoNewPrivileges` 等）也都在生效。

安裝之後主機與瀏覽器都不需要連得到網際網路：介面用的檔案都隨程式一起送
（`static/vendor/`）。升級需要連得到 git 來源與 PyPI（或它們的內部鏡像）。

---

## 疑難排解

**先跑 `sudo mf-topomon doctor`** —— 它會指出是哪一層，並給出指令。

| 現象 | 看哪裡 |
|---|---|
| 安裝停在「Python 環境」 | 連不到 PyPI，或這台機器的 Python 沒有某個套件的 wheel。pip 的訊息會說是哪一個 |
| 安裝停在 nginx 的設定檢查 | 80／443 被別的站台佔用。換埠（上面「換埠」一節） |
| 登入不了 | `sudo mf-topomon password` 重設一個本機管理員 |
| 後台按了備份／升級一直在排隊 | 背景執行器沒在跑：`sudo systemctl enable --now mf-topomon-runner.path`，或 `sudo mf-topomon install` |
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
* 後台沒有背景執行器：網頁上的備份、還原、升級、換憑證會說明要改用主機上的
  `mf-topomon-docker` 指令。帳號、AD、稽核紀錄、通知、健康檢查都一樣能用。

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
確認它讀得到。登入帳號是新安裝建立的那一個（舊的 Docker 部署的 htpasswd 不在
這台機器上）；在舊介面的設定頁存過
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
