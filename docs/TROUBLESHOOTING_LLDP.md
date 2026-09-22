# 收不到 LLDP 鄰居 — 排查指南

## 先看這裡：v1.x 部署版本的已知問題

2026-09 之前部署的版本，**不論設備設定多正確都一定收不到 LLDP**。

原因：`requirements.txt` 寫的是 `pysnmp>=4.4.12,<7.0.0`，實際安裝的是 pysnmp 6.x，
而 collector 的 SNMP WALK 是照 pysnmp 4.x 的 API 寫的。在 pysnmp 6.x 底下，
`nextCmd()` 回傳的是「每列 varBind 的 table」而不是單一 varBind，程式取到的
`str(var_bind[0])` 變成 `LLDP-MIB::lldpRemSysName... = xxx` 這種字串，
跟數字 OID 比對永遠不相等，於是 **每一次 WALK 都回傳空結果**。

症狀完全符合：

- 設備列表看得到設備、status 是 managed、CPU/記憶體/uptime 都有值（GET 正常）
- 但是拓撲圖永遠空白、`raw_links` 資料表沒有任何資料（WALK 全掛）
- 介面流量也是空的（同一個 WALK 函式）
- 設定頁的「SNMP Walk」按鈕回報失敗（`'coroutine' object is not async iterable`）

修正方式是改用 pysnmp 6.x 的 `bulkWalkCmd()` / `walkCmd()` 非同步產生器，
並把 `pysnmp` 版本鎖在 `>=6.2.6,<7.0.0`。

### 更新方式

```bash
cd /path/to/net-lldp-topo
git pull
docker compose build --no-cache          # 一定要重建，requirements.txt 有變
docker compose up -d
docker compose logs -f collector
```

正常的 collector log 長這樣：

```
Polled core-sw01 (192.168.1.1): 4 LLDP + 0 CDP neighbors, 52 interfaces
Updating merged links...
```

若 LLDP 數量是 0，再往下看。

## 診斷工具

```bash
# 基本檢查
docker compose exec collector python -m tools.diag_lldp 192.168.1.1 public

# 印出每一筆 OID
docker compose exec collector python -m tools.diag_lldp 192.168.1.1 public -v

# SNMPv3
docker compose exec collector python -m tools.diag_lldp 192.168.1.1 --v3 \
    --user snmpuser --auth-proto SHA --auth-pass xxxxxx \
    --priv-proto AES --priv-pass yyyyyy
```

也可以用 API：

```bash
curl -s -X POST http://localhost:8080/api/snmp/lldp \
  -H 'Content-Type: application/json' \
  -d '{"ip":"192.168.1.1","community":"public"}' | jq
```

## 各種情況的判讀

### 1. SNMP 完全沒回應

```
✗ 沒有 SNMP 回應
```

- collector 用的是 `network_mode: host`，設備看到的來源 IP 是**主機 IP**，
  不是 docker 網段。設備上的 SNMP ACL 要放行主機 IP。
- 確認 UDP/161 沒有被防火牆擋掉：`nc -vzu 192.168.1.1 161`
- community / SNMP 版本是否和設備上的設定一致（v2c vs v3）。

### 2. SNMP 通，但 `lldpRemTable` 是 0 rows

設備本身沒有 LLDP 鄰居資料，程式端沒有問題。檢查設備設定：

| 廠牌 | 開啟方式 | 檢查指令 |
|------|----------|----------|
| Cisco IOS / IOS-XE | `lldp run` | `show lldp neighbors` |
| Cisco NX-OS | `feature lldp` | `show lldp neighbors` |
| HP ProCurve / Aruba | `lldp run` | `show lldp info remote-device` |
| FortiGate | `config system interface` → `set lldp-transmission enable` | `get system lldp neighbors` |
| Palo Alto | Network → LLDP → Enable | `show lldp neighbors all` |

注意：

- 介面層級也可能被關掉（Cisco：`no lldp transmit` / `no lldp receive`）。
- 對端設備若只跑 CDP，就要在兩邊都開 LLDP，或改看 CDP（本系統也會收 CDP）。
- LLDP 鄰居表要等一個 LLDP 週期（預設 30 秒）才會出現。

### 3. SNMP 通、設備上 `show lldp neighbors` 有資料，但 walk 是 0 rows

多半是 SNMP view / ACL 只放行了部分 MIB。LLDP-MIB 的根是 `1.0.8802.1.1.2`，
要確認這個子樹在允許的 view 內。Cisco 範例：

```
snmp-server view TOPOMON iso included
snmp-server view TOPOMON 1.0.8802.1.1.2 included
snmp-server community public view TOPOMON RO 20
```

驗證：

```bash
docker compose exec collector python -m tools.diag_lldp 192.168.1.1 public -v
```

若 `ifDescr` 也是 0 rows，代表整個 WALK 被擋（或設備不支援 GETBULK，
此時把設備的 SNMP 版本設成 v1，系統會自動改用 GETNEXT）。

### 4. 有鄰居，但拓撲圖上沒有連線

拓撲圖只畫**兩端都在設備清單裡**的連線。`raw_links` 會記錄所有鄰居，
但 `merged_links` 只保留能對應到已知設備的。

- 鄰居的 hostname 必須和資料庫中的 `hostname` 一致（設備的 sysName）。
  LLDP 送出的常是 FQDN（`sw2.example.com`），而設備清單裡可能只有 `sw2`。
- 用「手動探索」並勾選遞迴，會依 LLDP 廣告的管理位址自動把鄰居加進來：

```bash
curl -s -X POST http://localhost:8080/api/discovery/manual \
  -H 'Content-Type: application/json' \
  -d '{"ip":"192.168.1.1","community":"public","recursive":true}' | jq
```

### 5. 鄰居名稱是一串 MAC

AP、IP 電話等設備常常不送 sysName TLV。這種情況會改用 chassis ID（MAC）當名稱，
連線仍然會被記錄下來，只是沒有可讀的名字。

## 手動用 net-snmp 驗證

若手邊有 `snmpwalk`，可以直接比對：

```bash
snmpwalk -v2c -c public 192.168.1.1 1.0.8802.1.1.2.1.4.1.1.9   # lldpRemSysName
snmpwalk -v2c -c public 192.168.1.1 1.0.8802.1.1.2.1.4.1.1.7   # lldpRemPortId
snmpwalk -v2c -c public 192.168.1.1 1.0.8802.1.1.2.1.3.7.1.4   # lldpLocPortDesc
```

`snmpwalk` 有資料而系統沒有，請附上 `tools/diag_lldp.py -v` 的輸出回報。
