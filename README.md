# mf-topomon

LLDP/CDP Topology Monitor - 網路拓撲監控系統

## 專案簡介

基於 LLDP/CDP 協定的網路拓撲自動發現與監控系統，提供乾淨的視覺化拓撲圖和完整的告警機制。

### 核心功能

- 🔍 **自動發現**：透過 LLDP/CDP 自動探索網路設備
- 🗺️ **拓撲視覺化**：乾淨的網路拓撲圖（合併重複 link）
- 📊 **流量監控**：即時顯示 link 頻寬使用率
- 🚨 **智慧告警**：可自訂閾值的告警系統
- 📝 **Log 整合**：支援輸出至 Elasticsearch/Graylog

### 支援設備

- Cisco (IOS, NX-OS)
- HP ProCurve
- Aruba
- Fortinet FortiGate
- Palo Alto Networks
- Ruckus (AP, SmartZone, Unleashed)

## 系統需求

- Ubuntu 22.04 / 24.04 LTS（或 Debian）
- Docker & Docker Compose v2 —— `install.sh` 會幫你裝
- 2 core / 4 GB 起（500 台約吃 24% CPU，見 [docs/PERFORMANCE.md](docs/PERFORMANCE.md)）
- 連得到設備管理位址的 udp/161；要收 trap 再加 udp/162

PostgreSQL、Redis、Python 都在容器裡，主機上不用先裝。
不用 docker 的裝法（systemd + venv）見 [docs/DEPLOYMENT.md 附錄 A](docs/DEPLOYMENT.md)。

裝完之後**管理主機與瀏覽器都不需要連得到網際網路**。介面要用的檔案都隨程式
一起送（d3 在 `static/vendor/`，見那裡的 README），所以放在沒有對外路由的
管理網段也會正常顯示 —— 這點由 `tests/test_static_assets.py` 守著，因為少
一個檔案的話瀏覽器是靜靜地畫不出來，不會報錯。

## 一鍵部署

一台乾淨的 Ubuntu，一行。**這個 repo 是私有的**，所以權杖要給兩次 —— 一次
拿這個腳本，一次讓它 clone：

```bash
export GITHUB_TOKEN=ghp_xxx          # 要有這個 repo 的 Contents: Read
curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://raw.githubusercontent.com/nox913106/net-lldp-topo/main/install.sh \
  | sudo GITHUB_TOKEN="$GITHUB_TOKEN" bash
```

權杖會被寫進 `/opt/topomon/.git/config`（chmod 600，只有 root 讀得到），
`topomon update` 之後才能自己更新。不想留權杖的話用唯讀 deploy key，
見 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

repo 轉成公開之後就不用權杖：

```bash
curl -fsSL https://raw.githubusercontent.com/nox913106/net-lldp-topo/main/install.sh | sudo bash
```

裝 docker、取原始碼到 `/opt/topomon`、產生隨機密鑰與登入密碼、起服務、等到
`/health/ready` 說資料庫也連得上為止，然後印出網址與帳密（**密碼只顯示這一
次**）。重複執行等同一次更新，不會動到 `.env`、憑證與資料庫。

之後都用 `topomon`：

```bash
sudo topomon health      # 逐層檢查：容器、代理、認證、資料庫、收集器
sudo topomon update      # 拉最新版、重建、健康檢查（沒過自動回滾）
sudo topomon status      # 容器狀態 + 收集器回報
sudo topomon logs        # 跟著看日誌（app / collector / proxy / db）
sudo topomon backup      # 備份資料庫
sudo topomon password    # 重設介面密碼
```

完整清單與每一項的細節見 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

### 介面是有密碼的

這個系統沒有使用者的概念，認證整個落在反向代理上：8080（應用程式）與
5432（資料庫）只綁 `127.0.0.1`，唯一的入口是 443，而 80 一律轉到 443。
`/health` 與 `/health/ready` 不要密碼（給負載平衡器與 `topomon health`），
其餘全部要，包含可以直接打 API 的 `/docs`。

憑證預設是自簽的，所以瀏覽器第一次會警告 —— 換成真憑證是覆蓋
`auth/tls.crt` 與 `auth/tls.key` 再 `topomon restart proxy`。

這一層有測試守著：`tests/test_proxy_auth.py` 用真的 nginx 跑
`deploy/nginx.conf`，確認沒帶密碼的請求**根本到不了應用程式**，而兩個健康
檢查路徑仍然通。CI 每次都跑。

### 開發用的起法

```bash
git clone https://github.com/nox913106/net-lldp-topo.git
cd net-lldp-topo
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload       # http://127.0.0.1:8000
.venv/bin/python -m app.collector             # 另一個行程
```

預設資料庫是本地的 SQLite 檔，沒有代理也沒有密碼 —— 只適合本機。

## 文件

- [專案介紹網站](docs/index.html) —— 功能、實際畫面與部署方式，可切換繁中／簡中／英文；把 GitHub Pages 設成由 `/docs` 發佈就是這一頁
- [部署與維護](docs/DEPLOYMENT.md) —— 一鍵安裝、`topomon` 每一個指令、認證與憑證、備份
- [設定說明](docs/CONFIGURATION.md) —— 每個環境變數，以及設備端要不要改（不用）
- [使用手冊](docs/USER_MANUAL.md) —— 介面怎麼用
- [收不到 LLDP 排查指南](docs/TROUBLESHOOTING_LLDP.md) —— 拓撲空白時從這裡查
- [效能與記憶體評估](docs/PERFORMANCE.md) —— 500 台的實測，以及每一次改動的量測
- [系統設計文件 (SDD)](docs/SDD.md) / [API](docs/SDD-API.md)

## 疑難排解

收不到 LLDP 鄰居 / 拓撲圖空白時，先執行診斷工具：

```bash
docker compose exec collector python -m tools.diag_lldp <設備IP> <community> -v
```

詳細說明見 [docs/TROUBLESHOOTING_LLDP.md](docs/TROUBLESHOOTING_LLDP.md)。
開了 trap 卻沒有任何反應，見 [docs/CONFIGURATION.md 第 5.4 節](docs/CONFIGURATION.md#54-開了-trap-卻沒反應)。

## 兩個輪詢迴圈

收集器把兩件週期完全不同的事分開跑：

| 迴圈 | 間隔 | 做什麼 |
|---|---|---|
| 盤點 | `COLLECTOR_INTERVAL`（300s） | 誰接到誰、埠速、自動發現。走訪 LLDP/CDP 與介面表 |
| 快迴圈 | `COLLECTOR_FAST_INTERVAL`（60s） | 設備存活、每條線的 up/down、流量，以及**告警檢查** |

快迴圈不走訪整張表 —— 盤點那一輪已經記下每條線的 ifIndex，所以它只問那幾
個埠（每台約 61 個 varbind，而不是 500），一輪的成本約是盤點的 1/5。
告警檢查跟著快迴圈走，因為告警不會比評估它的迴圈新。

另外可以收 SNMP trap（`COLLECTOR_TRAP_ENABLED`，udp/162）：linkDown／linkUp
會在**毫秒內**更新那條線的狀態並發告警，而不是等到下一次快迴圈。trap 永遠
不是唯一的事實來源 —— 快迴圈還是會重新確認一次。

要不要在設備上加設定？**都不用。** 輪詢用的是你本來就在用的管理位址，拓樸
是靠 sysName 對名字跟 IP 無關，而 trap —— 以前唯一的例外 —— 現在會比對
設備**自己回報的所有位址**（盤點時走訪它的 IP 位址表，每台預設每小時一次），所以從出口介面送
的 trap 也認得出來，不必在每台設備加 `snmp-server trap-source`。
見 [docs/CONFIGURATION.md 第 5 節](docs/CONFIGURATION.md#5-設備端需要改什麼以及為什麼不需要-loopback)。

### 500 台的實測

| | 之前 | 現在 |
|---|---|---|
| 使用率告警平均延遲 | 6.4 分 | **1.3 分** |
| 能觸發 70% 門檻的最短事件 | 210 s | **42 s** |
| 設備斷線告警 | 平均 190 s | 平均 **45 s** |
| 單一線路 up/down | 沒有這個告警 | **毫秒（trap）/ ≤60 s（輪詢）** |
| CPU 佔用 | 39% | **24%** |

佔用會下降是因為 SNMP engine 改成依憑證共用（每台省 162 ms 的 CPU），
而且請求用的 OID 不再每輪重新對 MIB 解析一次。

無回應的設備會依連續失敗次數退避（兩個迴圈各自算），不再每輪都等滿
`SNMP_TIMEOUT × (SNMP_RETRIES + 1)`。

### 告警抑制（避免掉包就誤報）

每個告警都來自一次 UDP 交握，所以三個旋鈕可以用「晚一點但確定」換掉
「掉包一次就叫人起床」。預設值都維持原本的行為。

| 設定 | 預設 | 作用 |
|---|---|---|
| `ALERT_OFFLINE_CONFIRM_CYCLES` | 1 | 連續 n 次沒回應才判離線。3 配 60 秒快迴圈 ≈ 3 分鐘；實測（20 秒快迴圈）1 是 24 s、3 是 99 s |
| `ALERT_LINK_FLAP_TRANSITIONS` / `_WINDOW` | 0 / 600 | 一條線在窗內變化 n 次就按住它的告警並標記 flapping，而不是每抖一次一組告警＋恢復（實測 8 次抖動 → 2 列告警） |
| `ALERT_TRAP_REQUIRES_POLL` | false | trap 照寫狀態（毫秒），但把告警交給下一次快迴圈確認。可在 alert profile 裡按設備覆寫 |

`SNMP_RETRIES`（預設 2 ＝ 每個請求打三次）仍然是第一道防線：掉一個封包
本來就不會變成告警。第一次 down、以及輪詢讀到的 down 都不延遲 —— 埠狀態
跟「設備活著」是同一個 GET 回來的，所以那是設備自己說的，不是掉包推論的。

抑制一旦打開，「沒有告警」就有兩種意思：真的沒事，或者正在確認中。設定頁的
**收集器狀態**會把後者寫出來 —— 哪一台在確認中（含「3 次裡的第 2 次」）、
哪一條線被按住、哪一片沒有在回報。資料是收集器行程自己回報的，不是 API 行程
照自己的環境變數猜的（`GET /api/v1/collector/status`，見
[docs/SDD-API.md](docs/SDD-API.md) 第 8 節）。

## 分片（多個 collector 行程）

一輪的瓶頸是 SNMP 解碼的 CPU，而 GIL 讓多執行緒幫不上忙，所以要跨過單核
的上限只能多開行程。設 `COLLECTOR_SHARDS=N`，起 N 個 collector，各自給
一個 `COLLECTOR_SHARD_ID`（0 到 N-1），每一片負責
`devices.id % N == COLLECTOR_SHARD_ID` 的設備 —— 條件下在 SQL 裡，所以
一片只讀自己那些設備與線路。

分片後**不用**任何新元件。每台設備剛好屬於一片，所以離線退避、輪詢節奏
都不需要跨行程協調。唯一不能分的是「整個機群」的工作：重建合併線路與
告警檢查。那由搶到 lease 的**一個**行程做（lease 放在既有的 PostgreSQL
裡，不是 Redis —— 沒有資料庫 collector 本來就不能運作，但多一個 Redis
就多一種「設備都正常輪詢卻沒有告警」的故障）。那個行程掛掉會被接手。

| 500 台 | 一片 | 兩片 | 四片 |
|---|---|---|---|
| 盤點一輪的 CPU（每片） | 33 s | **18 s** | **11 s** |
| 快迴圈一輪的 CPU（每片） | 6.9 s | **3.4 s** | **2.1 s** |
| CPU 佔用（每片） | 22% | **12%** | **7%** |
| 資料庫連線數（合計） | 20 | 28 | 81 |

加速是 1.86×（兩片）與 3.05×（四片），不是 2× 與 4×：每個行程都有自己的
固定成本，整台主機要多付 7%～27%。要的是前者 —— 單核的上限才是問題。
**1000 台用四片等於 500 台用兩片**，所以往上一個數量級是改設定。

0 到 N-1 每個號碼都要有行程在跑：少跑一個，那些設備就沒有人輪詢。
拿到 lease 的行程會警告這件事（`Shard(s) 2 of 4 are not reporting`），
設定頁的**收集器狀態**也會，不用去翻 log。
`COLLECTOR_TRAP_ENABLED` 一台主機只能開一個行程 —— udp/162 只有一個綁得到，
而綁到的那個會處理整個機群的 trap，不只自己那一片。

完整的量測、容量評估與「要不要改用 Rust」的評估見
[docs/PERFORMANCE.md](docs/PERFORMANCE.md)。

## 開發與 CI

依賴是鎖定的：`requirements.in` 是人維護的那一份（直接依賴與版本範圍），
`requirements.txt` 由 `pip-compile` 產生 —— 每個套件釘到確切版本並帶 hash，
`Dockerfile` 與 native 的安裝步驟都用 `--require-hashes` 安裝。這個專案被依賴漂移
咬過一次（pysnmp 的進入點換掉，LLDP 走訪變成安靜地回傳 0 筆），所以範圍不夠，
要的是同一份原始碼每次裝出同一棵樹。

```bash
# 測試：純邏輯那幾組不需要任何東西
pip install --require-hashes -r requirements-dev.txt
pytest

# 反向代理那一組要一個 nginx（裝了就好，不用啟動 —— 測試會自己起一個在
# 隨機埠上）。沒有的話那 8 項會 skip，CI 設 REQUIRE_NGINX=1 讓它變失敗。
sudo apt install -y nginx-light

# 連「真的走一次 SNMP」與端對端那幾組（模擬器刻意裝在另一個 venv，原因見
# requirements-sim.in：它會拉 pysnmp 7.x，正是這裡鎖著不要的版本）
python -m venv .venv-sim
.venv-sim/bin/pip install --require-hashes -r requirements-sim.txt
SNMPSIM_BIN=.venv-sim/bin/snmpsim-command-responder pytest

# 對著真的 PostgreSQL 再跑一次（CI 兩種都跑）
DATABASE_URL=postgresql+asyncpg://topomon:topomon@127.0.0.1:5432/topomon \
SNMPSIM_BIN=.venv-sim/bin/snmpsim-command-responder pytest

# 型別檢查（規則集很窄，原因寫在 pyrightconfig.json 裡）
npx pyright@1.1.414

# 改了依賴之後重新產生 lock
pip install pip-tools
pip-compile --generate-hashes --strip-extras --no-header requirements.in
```

`.github/workflows/ci.yml` 跑三個工作：**tests**（照 lock 安裝，起一個 SNMP
模擬器與一個 nginx，跑 189 項測試 —— 而且跑兩次，一次 SQLite、一次
`postgres:15`，因為有兩類錯只在 PostgreSQL 發作，見
[docs/PERFORMANCE.md 第 39.3 節](docs/PERFORMANCE.md)）、
**lock**（就地重編三份 lock 再 `git diff --exit-code`，抓「改了 `.in` 沒重編」
和手改 `.txt`）、**typecheck**（pyright）。測試在沒有模擬器時會 skip 而不是
fail，但 CI 設 `REQUIRE_SNMPSIM=1` 把那個 skip 變成失敗 —— 否則 CI 會在什麼
都沒測的情況下變綠。

那 189 項裡有 22 項是端對端的：真的跑一輪盤點與快迴圈、真的 trap 封包進真的
接收器、租約與分片心跳對真的資料庫（`tests/test_*_e2e.py`，說明在
`tests/README.md`）。在那之前這些證明都只活在開發容器的腳本裡。

另外 8 項跑的是**部署出去的那個 nginx 設定**（`tests/test_proxy_auth.py`）：
沒帶密碼的請求到不了應用程式、`/docs` 也要密碼、明文 HTTP 在問密碼之前就被
轉走、而兩個健康檢查路徑仍然免認證。那個檔案是這個專案唯一的認證，而一個
只有人會讀的設定檔是最容易漂掉的東西。

型別檢查抓不到當初那場事故（原因見 [docs/PERFORMANCE.md 第 32.2 節](docs/PERFORMANCE.md)），
真正守住它的是 `tests/test_snmp_collector.py` —— 那幾項斷言的是**筆數**，
不是型別。

## 授權

MIT License
