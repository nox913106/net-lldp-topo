# DEV-lldp-topomon 設定說明

## 1. 環境變數

複製 `.env.example` 為 `.env` 並修改：

```bash
cp .env.example .env
```

### 基本設定

```env
# 應用程式
APP_NAME=DEV-lldp-topomon
APP_DEBUG=false
APP_LOG_LEVEL=INFO

# 資料庫
DATABASE_URL=postgresql://topomon:password@localhost:5432/topomon

# Redis
REDIS_URL=redis://localhost:6379

# SNMP 預設設定
SNMP_DEFAULT_COMMUNITY=public
SNMP_TIMEOUT=5
SNMP_RETRIES=2

# Collector 設定
#
# 兩個迴圈，兩個間隔。下面這個是「盤點」的間隔 —— 誰接到誰、埠速多少、
# 有沒有新設備。這件事只有在有人動線才會變，所以它可以慢。
COLLECTOR_INTERVAL=300
#
# 這個是「快迴圈」的間隔 —— 設備有沒有回應、每條線的埠是不是 up、
# 流量多少，以及**告警檢查**。它只讀每條線 3 個值（不走訪整張表），
# 所以一輪的成本大約是盤點那一輪的 1/5（實測；第 18.4 節原本估 1/8）。
# 它同時決定：
#   1. 告警最慢多久（約 0.5 × 這個值，加上那次交握本身）
#   2. 每個 bps 的平均窗，也就是能觸發 70% 門檻的最短事件＝ 0.70 × 這個值
#      （60 秒 → 42 秒的滿線速流量）
# 設 0 就關掉快迴圈，所有事情回到盤點迴圈上（和舊版行為一致），
# 但那樣每條線的 up/down 就只能靠 trap。詳見 docs/PERFORMANCE.md 第 20 節
COLLECTOR_FAST_INTERVAL=60
# 同時有幾台設備在等 SNMP 回應。兩個迴圈共用這個值。
# 這已經只是 SNMP 的旋鈕 —— 資料庫那半邊從批次化之後幾乎不受它影響
# （PERFORMANCE.md 17.4）
COLLECTOR_CONCURRENT=20
#
# 分片：把機群拆給幾個 collector 行程。這個行程負責
# devices.id % COLLECTOR_SHARDS == COLLECTOR_SHARD_ID 的設備。
# 一輪的瓶頸是 SNMP 解碼的 CPU，而 GIL 讓多執行緒幫不上忙，所以要跨過
# 單核的上限只能多開行程。1 ＝ 單一行程，所有查詢都不帶分片條件、
# 也完全不跟別人協調（和沒有這個功能時一模一樣）。
#
# 用之前要知道三件事（docs/PERFORMANCE.md 第 24 節）：
#   1. 每一片的 COLLECTOR_SHARDS 必須一樣，而且 0 到 COLLECTOR_SHARDS-1
#      每個號碼都要有行程在跑。少跑一個，那些設備就沒有人輪詢，而唯一的
#      症狀是它們慢慢開始報 device_offline。拿到 fleet-wide lease 的那個
#      行程會警告這件事，但那是安全網，不是替代品。
#   2. 合併線路與告警檢查是「整個機群」的工作，由拿到 lease 的那**一個**
#      行程做。誰拿到是搶的（不是固定 0 號），所以那個行程掛掉會被接手，
#      實測 TTL 內（3 × 快迴圈間隔）完成交接。
#   3. COLLECTOR_TRAP_ENABLED 一台主機只能有一個行程設 true：udp/162 只有
#      一個行程綁得到。綁到的那一個會處理整個機群的 trap，不只自己那一片。
#
# 實測（400 台直接量、折算到 500 台）：盤點一輪的 CPU 一片 33s、兩片各
# 18s、四片各 11s —— 加速 1.86× 與 3.05×，代價是整台主機的 CPU 總量多
# 7% 與 27%。資料庫連線數 20 / 28 / 81 條（PostgreSQL 預設上限 100）。
COLLECTOR_SHARDS=1
COLLECTOR_SHARD_ID=0
# 連續無回應的設備改用指數退避，不要每輪都等滿 timeout
COLLECTOR_OFFLINE_BACKOFF=true
COLLECTOR_OFFLINE_BACKOFF_MAX=1800
# 讀取 ifHCIn/OutOctets，讓每條線路有真實 bps 與使用率。
# 設為 false 仍會讀 ifHighSpeed（真實埠速），只是不量流量
COLLECTOR_TRAFFIC_STATS=true
# 轉不成設備的鄰居（IP 話機、印表機、SNMP 關閉的 AP）隔多久才再探測一次
COLLECTOR_DISCOVERY_CACHE_TTL=3600

# 收 SNMP trap（udp/162）。開了之後 linkDown/linkUp 會在毫秒內更新那條線
# 的狀態並發告警，而不是等到下一次快迴圈。預設關閉：它會開一個監聽埠、
# 需要能綁 1024 以下的埠（root 或 CAP_NET_BIND_SERVICE），而且在設備端
# 設好 trap 目的地之前不會收到任何東西。
#
# trap 永遠不是唯一的事實來源 —— 一個 UDP 封包可能遺失、重複、延遲或偽造，
# 所以快迴圈還是會把狀態重新確認一次。只接受來源位址是受管設備、
# community 正確、而且那個埠上確實有線的 trap；v3 notification 不支援。
COLLECTOR_TRAP_ENABLED=false
COLLECTOR_TRAP_BIND=0.0.0.0
COLLECTOR_TRAP_PORT=162
# 接受的 community，逗號分隔。留空＝沿用 SNMP_DEFAULT_COMMUNITY
COLLECTOR_TRAP_COMMUNITIES=

# -- 告警抑制（avoid false alerts）------------------------------------------
# 這個專案的每一個告警都來自一次 UDP 交握，所以掉一個封包、或收到一個偽造的
# 封包，都可能變成一個告警。SNMP_RETRIES 是第一道（每個請求打三次），
# 下面三個是第二道。預設值都維持加入它們之前的行為，所以要不要用
# 「晚一點但確定」換「立刻但可能誤報」，是一個明確的選擇。

# 連續幾次沒回應才把設備判為離線。1 ＝ 第一次就判（原本的行為）。
# 3 配 60 秒的快迴圈 ＝ 設備要安靜大約 3 分鐘，所以一次把三個 SNMP 重試
# 全部吃掉的壅塞不會再叫人起床。
# 實測（快迴圈 20 秒）：設備真的死掉之後，1 是 24 秒發告警，3 是 99 秒。
# 兩個迴圈共用同一個計數器；離線退避也知道這個數字，所以用來確認的那幾次
# 輪詢維持正常間隔，不會被退避曲線拉長（詳見 PERFORMANCE.md 第 28 節）
ALERT_OFFLINE_CONFIRM_CYCLES=1

# 一條線在 ALERT_LINK_FLAP_WINDOW 秒內變化這麼多次就算 flapping：
# 它的 link_down 告警會在「又 up 了」的時候被按住不解除，而不是每抖一次
# 就一組告警＋恢復，並且在訊息裡說明它在 flapping。
# 第一次 down 永遠不延遲。0 ＝ 關閉（每抖一次一組告警＋恢復，原本的行為）
ALERT_LINK_FLAP_TRANSITIONS=0
ALERT_LINK_FLAP_WINDOW=600

# linkDown/linkUp trap 要不要等下一次快迴圈同意之後才發告警。
# false（預設）就是 trap 的本意：收到通知就在毫秒內發告警。
# true 保留狀態寫入（拓樸和介面立刻是最新的），但把「要不要告警」交給輪詢，
# 代價是最多一個快迴圈的延遲，換來的是遺失、過期或偽造的 trap 永遠叫不醒人。
# 可以在告警設定檔（alert profile）裡按設備覆寫：
#   {"trap": {"requires_poll": true}}
# 它需要快迴圈：COLLECTOR_FAST_INTERVAL=0 時沒有任何東西在輪詢每條線的狀態，
# 也就沒有東西可以佐證 trap，這時 collector 會警告一次並維持立刻告警。
ALERT_TRAP_REQUIRES_POLL=false

# Log 輸出 (可選)
LOG_EXPORT_ENABLED=false
LOG_EXPORT_TYPE=elasticsearch
ELASTICSEARCH_URL=http://localhost:9200
```

---

## 2. 設定檔

### config.yaml

```yaml
app:
  name: "DEV-lldp-topomon"
  debug: false
  log_level: "INFO"

snmp:
  default_community: "public"
  timeout: 5
  retries: 2

collector:
  interval_seconds: 300
  concurrent_connections: 20
  
discovery:
  enabled: true
  auto_add_neighbors: true

log_export:
  enabled: false
  type: "elasticsearch"
  elasticsearch:
    url: "http://localhost:9200"
    index_prefix: "topomon-"
  graylog:
    host: "localhost"
    port: 12201
    protocol: "udp"
```

---

## 3. SNMP Community 管理

可以在 Web UI 或設定檔中設定多組 SNMP community：

```yaml
snmp:
  communities:
    - name: "public"
      priority: 1
    - name: "private"
      priority: 2
    - name: "custom_community"
      priority: 3
```

發現設備時會依優先序嘗試連線。

---

## 4. 廠商自動偵測

系統會根據 sysDescr 自動判斷設備廠商：

| 關鍵字 | 廠商 |
|--------|------|
| `Cisco` | Cisco |
| `ProCurve`, `Aruba` | HP/Aruba |
| `FortiGate` | Fortinet |
| `Palo Alto` | Palo Alto |

可自訂額外規則：

```yaml
vendor_detection:
  rules:
    - pattern: ".*Custom-OS.*"
      vendor: "custom"
      cpu_oid: "1.3.6.1.4.1.xxx"
      memory_oid: "1.3.6.1.4.1.xxx"
```
