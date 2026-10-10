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
# DEBUG 會多出「為什麼這個鄰居沒被自動發現」那一類的行（見
# docs/TROUBLESHOOTING_LLDP.md 第 6 種情況）。認不得的值退回 INFO
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
#
# 「來源位址是受管設備」這一條在既有環境裡要注意：很多平台預設是從出口
# 介面送 trap，不是從你管理它的那個位址。三個解法（其中兩個完全不用改
# 設備）見第 5.3 節，收不到時的判讀見 5.4 節。
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

# 抑制打開之後，「沒有告警」就有兩種意思：真的沒事，或者正在確認中。
# 設定頁的「收集器狀態」會把後者寫出來（哪一台在確認中、第幾次、哪一條線
# 被按住、哪一片沒在回報），資料是收集器行程自己回報的，不是這些變數在
# 哪個行程裡被讀到 —— 見 docs/SDD-API.md 第 8 節。
#
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

---

## 5. 設備端需要改什麼（以及為什麼不需要 loopback）

在既有環境裡導入這個專案，最常問的一句是「是不是每一台 switch / router /
firewall 都要加設定」。答案是：**盤點、拓樸、告警都不用**，只有「收 trap」
這一項可能要。

| 用到什麼 | 靠哪個識別 | 設備端要改什麼 |
|---|---|---|
| 輪詢（存活、埠狀態、流量、CPU） | `devices.ip_address` —— 你本來就用來管理那台設備的位址 | 不用（SNMP 本來就得開） |
| **把兩端接成一條線** | **sysName（hostname）** | 不用 |
| 自動發現新設備 | LLDP 管理位址 TLV → DNS → chassis ID | 不用，見 5.2 |
| 收 linkDown/linkUp trap | **trap 封包的來源位址** | 可能要，見 5.3 |

### 5.1 為什麼拓樸不需要任何位址

合併線路是名字對名字：`raw_links.remote_hostname`（鄰居透過 LLDP 告訴我們的
sysName）去對 `devices.hostname`
（`app/core/topology_engine.py:81`、`:102`）。整個合併過程不碰任何 IP，
所以一台設備有幾個位址、管理位址是 loopback 還是 SVI、對拓樸圖毫無影響。

反過來說，拓樸圖缺線的原因是**名字對不上**，不是位址對不上 —— 對不上的
鄰居會留在 `raw_links` 裡，但畫不出連線。判讀見
[docs/TROUBLESHOOTING_LLDP.md](TROUBLESHOOTING_LLDP.md) 第 4 種情況。

### 5.2 自動發現的位址從哪裡來

看到一個還沒進資料庫的鄰居時，要有一個位址才能去輪詢它。依序試四個來源：

1. 那個鄰居透過 LLDP 廣告的管理位址（`lldpRemManAddrTable`，
   `app/core/snmp_oids.py:14`）
2. 廣告的 sysName 拿去查 DNS（`app/collector.py:170`）
3. chassis ID 本身就是一個 IPv4 的話，直接用它
   （`app/core/snmp_collector.py:829`）
4. 都不行 → 記進 dead-end 快取，等你手動把它加進來

所以 LLDP 管理位址只是**自動發現的捷徑**，不是必要條件；而且它是設備
「本來就有」的那個位址 —— 很多路由器預設廣告的是 loopback，純粹因為廠商
預設如此，不是這個專案要求的。完全不廣告管理位址、設備全部手動加，之後
所有功能一模一樣。

那個廣告出來的位址**連不到**（例如一個沒有路由到監控主機的 loopback）也
不會留下爛資料：加進資料庫之前會先用 SNMP 探一次，探不到就記進 dead-end
快取（`app/collector.py:230`），`COLLECTOR_DISCOVERY_CACHE_TTL` 之後才會
再試一次。結果是「沒有被自動發現」，不是「多出一台永遠離線的設備」——
手動用那個真的連得到的位址把它加進來就行，拓樸圖一樣會把線接上，因為接線
靠的是 sysName 而不是那個位址。

### 5.3 trap 是唯一的例外

`COLLECTOR_TRAP_ENABLED=true` 時，收到的 trap 是用**來源位址**去對
`devices.ip_address` 認設備的（`app/core/trap_receiver.py:547`）。對不上就
丟掉，並且每個來源記一次：

```
Ignoring traps from 10.1.2.3: not a managed device (it is matched on its management address)
```

麻煩的是不少平台預設用**出口介面**的位址送 trap，而那不一定是你拿來管理它
的那個位址。三個解法，由省事到費事：

1. **不開 trap**（預設值）。每條線的 up/down 還是有，只是延遲從毫秒變成
   最多一個 `COLLECTOR_FAST_INTERVAL`（預設 60 秒）。
2. **改監控這一邊**：把 `devices.ip_address` 設成設備實際送 trap 的那個
   位址（前提是那個位址也能輪詢）。零工單，但你得知道每台是從哪個介面送的。
3. **改設備那一邊**：指定 trap 的來源位址。這就是「每台都要加一行」的那個
   方案，也是 loopback 在這個題目裡唯一出現的理由：

   | 平台 | 設定 |
   |---|---|
   | Cisco IOS / IOS-XE | `snmp-server trap-source <interface>` |
   | Cisco NX-OS | `snmp-server source-interface traps <interface>` |
   | Junos | `set snmp trap-options source-address <address>` |

   其他平台找它自己的 trap source / source-interface 設定，確切語法以該
   平台的文件為準。

**這是實作選擇，不是協定要求。** 只比對來源位址是目前的做法；改成比對
「那台設備所有已知的位址」（v1 trap 自帶的 agent-address 欄位、LLDP 管理
位址、設備自己的 `ipAddressIfIndex`）之後，第 3 個解法就不需要了。那一項
列在 [docs/PERFORMANCE.md](PERFORMANCE.md) 第 31 節，還沒做。

### 5.4 開了 trap 卻沒反應

trap 的失敗是安靜的 —— 沒有任何東西會主動告訴你「我在等 trap」。盤點迴圈
每輪會印一行：

```
Traps: 0 received, 0 applied, 0 ignored, 0 dropped
```

| 看到的 | 意思 |
|---|---|
| `0 received` | 封包根本沒到。查設備端的 trap 目的地、路由、防火牆的 udp/162，以及容器有沒有綁到那個埠（需要 `network_mode: host`，見 `docker-compose.yml` 的註解） |
| `received` 在跳、`ignored` 跟著跳 | 封包到了但認不出設備 —— 就是 5.3 的來源位址問題，log 裡會有上面那行 warning |
| `applied` 在跳但沒有告警 | 那個埠上沒有記錄到線（只有有線的埠才算），或者 `ALERT_TRAP_REQUIRES_POLL=true` 正在等輪詢佐證（那時這一行會多一段 `left to a poll to alert on`） |
| `dropped` 在跳 | 一瞬間超過 2000 個 trap 在排隊（機箱重開、光纖狂抖）。丟掉的是延遲不是正確性，快迴圈照樣會把狀態撿回來 |
