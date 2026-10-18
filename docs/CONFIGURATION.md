# DEV-lldp-topomon 設定說明

## 1. 環境變數

`topomon deploy` 第一次跑的時候會自己從 `.env.example` 產生 `.env`，並且把
`APP_SECRET_KEY` 與資料庫密碼換成隨機值。要自己手動建也可以：

```bash
cp .env.example .env
```

改完 `.env` 之後 `sudo topomon restart` 套用（改到 compose 本身則用
`sudo topomon deploy`）。`topomon health` 會提醒兩個「能跑但不該這樣跑」的
值：還是範例值的 `APP_SECRET_KEY`，以及還是預設 `topomon` 的資料庫密碼。

### 基本設定

```env
# 應用程式
APP_NAME=DEV-lldp-topomon
APP_DEBUG=false
# DEBUG 會多出「為什麼這個鄰居沒被自動發現」那一類的行（見
# docs/TROUBLESHOOTING_LLDP.md 第 6 種情況）。認不得的值退回 INFO
APP_LOG_LEVEL=INFO

# 瀏覽器可以從哪些來源呼叫這個 API，逗號分隔。空的（預設）＝完全不掛 CORS
# middleware，這對目前的部署是對的：介面是同一個行程送出去的，本來就同源。
# 只有「別的地方送出來的頁面要打這個 API」才需要填，而且要指名來源 ——
# 這個 API 讀得到每一台設備的 community。
APP_CORS_ORIGINS=

# 資料庫。驅動不能省：這是 SQLAlchemy 的 async engine，少了 +asyncpg 會在
# 啟動時就失敗（"the asyncio extension requires an async driver"）。
DATABASE_URL=postgresql+asyncpg://topomon:topomon@localhost:5432/topomon

# 資料庫密碼。docker-compose.yml 的 db 容器與上面那兩條 URL 都用它。
# 新安裝時 topomon deploy 會放一組隨機值；既有的部署沒有這一行，會沿用舊的
# 預設值 —— 密碼是在 pgdata 初始化的時候烤進去的，改這裡只會把自己鎖在外面。
POSTGRES_PASSWORD=topomon

# Redis（目前程式還沒在用，見 docs/PERFORMANCE.md 第 42 節第 1 項）
REDIS_URL=redis://localhost:6379

# 反向代理在主機上聽哪兩個埠。80 一律轉到 443；443 後面的每一條路徑都要
# HTTP basic auth 的帳密（`topomon password` 重設）。這兩個埠之外，應用程式
# 的 8080 與資料庫的 5432 只綁 127.0.0.1 —— 見 docs/DEPLOYMENT.md「登入與
# 憑證」。
TOPOMON_HTTP_PORT=80
TOPOMON_HTTPS_PORT=443

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

# 盤點時順便走訪每台設備自己的 IP 位址表，記下它持有哪些位址，這樣它從哪個
# 介面送 trap 都認得出來 —— 也就是不用在每台設備上加 snmp-server trap-source
# （見第 5.3 節）。那次走訪是一次輪詢的約 5% CPU，而且不是每輪都做
# —— 見下面的 COLLECTOR_ADDRESS_INTERVAL 與 PERFORMANCE 第 38 節。
#
# 刻意**不**跟 COLLECTOR_TRAP_ENABLED 綁在一起：一台主機只有一個行程綁得到
# udp/162，所以分片時拿到接收器的那一片需要其他片輪詢的設備的位址，而那些
# 行程的 trap 是關的。不收 trap 的部署可以關掉這個。
COLLECTOR_DEVICE_ADDRESSES=true

# 同一台設備的位址表最多多久走訪一次（秒）。位址是慢變數：介面的 IP 在有人
# 去改它的時候才改，所以每小時問一次跟每輪問一次拿到的是同一張表。寫成秒而
# 不是輪數，這樣調 COLLECTOR_INTERVAL 不會順便讓位址變舊。
#
# 代價：設備「多」一個位址之後，最多約一小時（加一輪盤點、加接收器自己的
# 60 秒快取）那個位址送來的 trap 才會被認得 —— 期間事件照樣由輪詢迴圈發現，
# 丟的是即時性不是正確性。新設備不受影響：第一次成功輪詢一定會走訪。
# 0 = 每輪都走訪（舊行為）。見第 5.5 節與 PERFORMANCE 第 38 節。
COLLECTOR_ADDRESS_INTERVAL=3600

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

### 5.3 trap 的來源位址（**不用**在每台設備加 trap-source）

`COLLECTOR_TRAP_ENABLED=true` 時，收到的 trap 是用**來源位址**認設備的。
麻煩在於不少平台預設是用**出口介面**的位址送 trap，而那不一定是你拿來管理
它的那個位址 —— 以前這表示要在每一台設備上加一行 `snmp-server trap-source`，
在既有環境裡就是一台一張工單。

現在不用了。盤點迴圈會走訪每台設備**自己的 IP 位址表**，把它持有的位址記進
`device_addresses`（`app/core/device_addresses.py`），trap 接收器就拿整組
位址去比對：

```
設備 sw-01   管理位址 10.0.1.1（你輪詢它的位址）
             其他位址 10.255.0.1、192.168.50.1（它自己回報的）
             → 從這三個位址中任何一個送來的 trap 都認得出是 sw-01
```

比對的順序是：

1. **datagram 的來源位址**，對照上面那一整組。這是正常情況。
2. **v1 trap 自己宣稱的位址**（RFC 3584 把 v1 的 agent-addr 欄位轉成
   `snmpTrapAddress.0`）。只在來源位址誰都對不上時才用，給 NAT 後面的設備。
   它在封包裡，所以任何人都可以寫任何值 —— 它只用來**找**設備，不會因此
   跳過任何檢查：那個埠上還是要有線，`ALERT_TRAP_REQUIRES_POLL` 開著時
   還是要等輪詢同意。來源位址已經認出某台設備時，宣稱的位址不會蓋過它。

**位址只從輪詢學，不從 trap 學。** 從 trap 學會讓任何人用一個偽造的封包把
自己的位址掛到某台設備上，然後以那台設備的身分送 trap。

還有什麼情況要自己處理：

* 設備從一個**完全沒出現在它自己位址表裡**的位址送 trap（少見，通常是中間
  有 NAT 而且用 v2c，所以也沒有 agent-addr 可用）。那就回到老方法：在設備
  上指定 trap 來源，或把 `devices.ip_address` 改成它實際送出的位址。

  | 平台 | 設定 |
  |---|---|
  | Cisco IOS / IOS-XE | `snmp-server trap-source <interface>` |
  | Cisco NX-OS | `snmp-server source-interface traps <interface>` |
  | Junos | `set snmp trap-options source-address <address>` |

* `COLLECTOR_DEVICE_ADDRESSES=false` 時不會收集，trap 就只比對管理位址
  （舊行為）。關掉的理由只有一個：那個走訪是一次盤點輪詢的約 5% CPU（實測
  見 [docs/PERFORMANCE.md](PERFORMANCE.md) 第 38.2 節）。不收 trap 的部署
  可以關掉它。
* 走訪**不是每輪都做**：同一台設備最多每 `COLLECTOR_ADDRESS_INTERVAL`
  （預設 3600 秒）問一次，所以預設下那 5% 平均只付 1/12。代價寫在
  下面的 5.5 節。

位址是輪詢時收集的，所以**一台設備要先被成功輪詢過一次**，它的其他位址才會
被認得。新加入的設備在第一次盤點輪詢（`COLLECTOR_INTERVAL`，預設 300 秒）
之前，trap 仍然只比對管理位址。

### 5.4 開了 trap 卻沒反應

trap 的失敗是安靜的 —— 沒有任何東西會主動告訴你「我在等 trap」。盤點迴圈
每輪會印一行：

```
Traps: 0 received, 0 applied, 0 ignored, 0 dropped
```

| 看到的 | 意思 |
|---|---|
| `0 received` | 封包根本沒到。查設備端的 trap 目的地、路由、防火牆的 udp/162，以及容器有沒有綁到那個埠（需要 `network_mode: host`，見 `docker-compose.yml` 的註解） |
| `received` 在跳、`ignored` 跟著跳 | 封包到了但認不出設備。log 裡每個來源會有一行 `Ignoring traps from <ip>: not an address any managed device is known to hold`。設定頁「收集器狀態」的 trap 區會顯示**可比對的位址**有幾個：如果那個數字等於設備數，表示位址收集沒有生效（`COLLECTOR_DEVICE_ADDRESSES`，或那些設備還沒被盤點過一輪）|
| `applied` 在跳但沒有告警 | 那個埠上沒有記錄到線（只有有線的埠才算），或者 `ALERT_TRAP_REQUIRES_POLL=true` 正在等輪詢佐證（那時這一行會多一段 `left to a poll to alert on`） |
| `dropped` 在跳 | 一瞬間超過 2000 個 trap 在排隊（機箱重開、光纖狂抖）。丟掉的是延遲不是正確性，快迴圈照樣會把狀態撿回來 |

### 5.5 位址最多能舊多久

位址是在盤點輪詢時收集的，而且**不是每輪都收**：同一台設備最多每
`COLLECTOR_ADDRESS_INTERVAL`（預設 3600 秒）走訪一次。所以有兩個不同的問題
要分開看。

**新設備：沒有延遲。** 這個行程沒走訪過的設備一律會走訪，所以一台設備加進來
之後，它第一次成功輪詢就把自己的位址都記下來了 —— 不用等一小時。

**設備多一個位址之後：最多約一小時。**

| | 最多 |
|---|---|
| 等到下一次走訪 | `COLLECTOR_ADDRESS_INTERVAL`（3600 秒） |
| 走訪只在盤點時發生 | ＋`COLLECTOR_INTERVAL`（300 秒） |
| 接收器的位址表自己也有快取 | ＋60 秒（有未知來源時最快 5 秒就重讀） |
| 合計 | **約 66 分鐘** |

這段時間裡，從那個新位址送來的 trap 會算成「不屬於任何受管設備」：log 裡
一行、`Traps:` 的 `ignored` 會跳、設定頁的收集器狀態會出現那個警告。
**不會漏掉事件** —— 線路狀態照樣由輪詢迴圈發現（最慢一個
`COLLECTOR_INTERVAL`），少掉的是 trap 那幾秒的即時性。

反過來，位址從一台設備**搬到另一台**之後，舊設備最多也還會匹配那麼久。
接收器仍然要求那個埠上有線、`ALERT_TRAP_REQUIRES_POLL=true` 時還要等輪詢
佐證，所以通常的結果是「忽略」而不是「對錯的設備發告警」。

要縮短就把 `COLLECTOR_ADDRESS_INTERVAL` 調小；`0` 回到每輪都走訪（也就是
每台設備的輪詢多付那 5% CPU）。設定頁「收集器狀態」的**間隔**欄第三個數字
就是這個值，`-` 表示位址收集整個關掉了。

重啟收集器會讓所有設備在第一輪重新走訪一次 —— 排程是行程記憶，刻意的：
它只決定「多久問一次」，剛重啟的行程本來就該重新確認。
