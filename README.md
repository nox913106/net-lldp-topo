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
- [API 文件](docs/API.md)
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

完整的量測、容量評估與「要不要改用 Rust」的評估見
[docs/PERFORMANCE.md](docs/PERFORMANCE.md)。

## 授權

MIT License
