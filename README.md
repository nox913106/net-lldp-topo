# DEV-lldp-topomon

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

- Ubuntu 22.04 LTS
- Python 3.11+
- PostgreSQL 15+
- Redis 7+
- Docker & Docker Compose (建議)

## 快速開始

```bash
# Clone 專案
git clone https://github.com/nox913106/DEV-lldp-topomon.git
cd DEV-lldp-topomon

# 使用 Docker Compose 啟動
docker-compose up -d

# 訪問 Web UI
open http://localhost:8080
```

## 快速部署

```bash
# 自動化部署（需要 root 權限）
sudo bash deploy.sh docker   # Docker 部署（建議）
sudo bash deploy.sh native   # Native 部署
```

## 文件

- [系統設計文件 (SDD)](docs/SDD.md)
- [API 設計文件](docs/SDD-API.md)
- [Ubuntu 部署指南](docs/DEPLOYMENT.md) ⭐ 新增
- [設定說明](docs/CONFIGURATION.md)
- [收不到 LLDP 排查指南](docs/TROUBLESHOOTING_LLDP.md) ⭐ 新增
- [效能與記憶體評估](docs/PERFORMANCE.md) ⭐ 新增

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
`Dockerfile` 與 `deploy.sh` 都用 `--require-hashes` 安裝。這個專案被依賴漂移
咬過一次（pysnmp 的進入點換掉，LLDP 走訪變成安靜地回傳 0 筆），所以範圍不夠，
要的是同一份原始碼每次裝出同一棵樹。

```bash
# 測試：純邏輯那幾組不需要任何東西
pip install --require-hashes -r requirements-dev.txt
pytest

# 連「真的走一次 SNMP」那一組（模擬器刻意裝在另一個 venv，原因見
# requirements-sim.in：它會拉 pysnmp 7.x，正是這裡鎖著不要的版本）
python -m venv .venv-sim
.venv-sim/bin/pip install --require-hashes -r requirements-sim.txt
SNMPSIM_BIN=.venv-sim/bin/snmpsim-command-responder pytest

# 型別檢查（規則集很窄，原因寫在 pyrightconfig.json 裡）
npx pyright@1.1.414

# 改了依賴之後重新產生 lock
pip install pip-tools
pip-compile --generate-hashes --strip-extras --no-header requirements.in
```

`.github/workflows/ci.yml` 跑三個工作：**tests**（照 lock 安裝，起一個 SNMP
模擬器，跑 69 項測試）、**lock**（就地重編三份 lock 再 `git diff --exit-code`，
抓「改了 `.in` 沒重編」和手改 `.txt`）、**typecheck**（pyright）。測試在沒有
模擬器時會 skip 而不是 fail，但 CI 設 `REQUIRE_SNMPSIM=1` 把那個 skip 變成
失敗 —— 否則 CI 會在什麼都沒測的情況下變綠。

型別檢查抓不到當初那場事故（原因見 [docs/PERFORMANCE.md 第 32.2 節](docs/PERFORMANCE.md)），
真正守住它的是 `tests/test_snmp_collector.py` —— 那幾項斷言的是**筆數**，
不是型別。

## 授權

MIT License
