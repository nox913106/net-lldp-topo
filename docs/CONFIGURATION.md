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
# 所以一輪的成本大約是盤點那一輪的 1/8。它同時決定：
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
