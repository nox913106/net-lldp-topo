# 效能與記憶體評估

本文記錄 collector 的效能瓶頸量測、記憶體使用評估，以及「是否需要改用 Rust」
的結論。所有數字都是實際量到的，不是估算。

## 量測環境

| 項目 | 值 |
|---|---|
| CPU | Intel Xeon @ 2.10GHz, 4 vCPU |
| Python | 3.11.15 |
| pysnmp | 6.2.6 (pyasn1 0.6.0) |
| 被測設備 | snmpsim 模擬器，走 loopback |
| 小型設備 | 3 介面、2 個 LLDP 鄰居 |
| 大型設備 | 1000 介面、500 個 LLDP 鄰居 |

因為走 loopback，網路延遲接近 0。真實環境的 wall time 會比這裡高（要等 UDP
往返），但 **CPU 時間是下限，不會因為網路變快而減少**，所以 CPU 數字可以直接
拿來推估容量。

---

## 1. 瓶頸在哪裡：每個請求都重建一個 SNMP engine

改動前，`_snmp_get()` 和 `_snmp_walk()` 各自 `SnmpEngine()` 建一個新 engine、
用完就 `closeDispatcher()`。一次完整的 `poll_device()` 會發 17～26 個請求，
所以一台設備一輪就建了 20 幾個 engine。

單獨量測 `SnmpEngine()` 的成本：

```
1st engine :    26.0 ms
2nd engine :    24.5 ms   <- MIB 不會跨 engine 快取
100 engines:  +63.55 MiB  -> 約 650 KiB/engine
```

cProfile 顯示 **74% 的時間花在 `builtins.compile`**：pysnmp 每建一個 engine
都重新讀取並重新編譯內建的 MIB 模組原始碼（13 個模組、279 個符號）。這是純
CPU 工作，而且是在 event loop 的執行緒上同步跑的，所以它會**擋住**所有其他
設備的輪詢。

### 改動：每個 collector 共用一個 engine

`SNMPCollector` 改為延遲建立一個 engine 並重複使用，加上 `aclose()` 和
async context manager。`app/collector.py` 改成每台設備一個
`async with SNMPCollector(...)`，所以「一台設備 = 一個 engine = 一個 UDP
socket」，輪詢完立刻釋放。

同時把 LLDP port 對應、CDP、介面統計都要用的 `ifDescr` / `ifName` 在單次
`poll_device()` 內快取起來（只快取這兩張表，見下面的記憶體評估）。

### 實測結果（同一台小型設備，完整 poll_device）

| | 改動前 | 改動後 | 倍數 |
|---|---|---|---|
| 建立的 engine 數 | 23 | **1** | 23× |
| wall time | 1938 ms | **221 ms** | 8.8× |
| user CPU | 1758 ms | **116 ms** | **15×** |
| RSS 增量 | +5.23 MiB | +1.95 MiB | 2.7× |

解析出來的鄰居、介面、管理 IP **完全相同**，沒有行為變化。

注意改動前 1938 ms 裡有 1758 ms 是 CPU——在一個零延遲的 loopback 上。這代表
原本的瓶頸從頭到尾都不是網路，而是重建 engine。`COLLECTOR_CONCURRENT=20` 在
這種情況下幾乎沒有用，因為 20 個協程全部排隊等同一個執行緒做 MIB 編譯。

---

## 2. 記憶體評估

### 2.1 每一列走訪結果的成本

```
  100 rows: 614 B/row
 1000 rows: 500 B/row
 5000 rows: 378 B/row   (OctetString)  /  558 B/row (Counter64)
```

一列 = 索引字串 + pysnmp 的值物件，約 **400～600 bytes**。

這代表 `get_interface_stats()` 對一台 1000 介面的設備走訪 4 個欄位時，同時
握有約 4000 列 ≈ **2 MiB**；5000 介面的機框會到 10 MiB。這比 engine 本身
(650 KiB) 大得多，**走訪結果才是記憶體的主要持有者**。

因此 `ifDescr` / `ifName` 的快取刻意加了兩道限制：

- 只快取 `_CACHEABLE_WALK_OIDS` 這兩張表，不是所有走訪結果
- 超過 `_WALK_CACHE_MAX_ROWS = 2000` 列就用完即丟，不留在快取裡

快取只在 `poll_device()` 執行期間存在，結束就清掉，不會跨輪保留。快取鍵包含
IP，所以不可能把一台設備的介面表餵給另一台。

### 2.2 併發輪詢的尖峰記憶體

| 情境 | wall | CPU | 尖峰 RSS | 相對 idle |
|---|---|---|---|---|
| idle（載入完成） | – | – | 50.9 MiB | – |
| 1× 小型 | 0.25s | 0.13s | 53.0 MiB | +2.1 MiB |
| 20× 小型併發 | 3.49s | 2.69s | 95.7 MiB | +42.6 MiB |
| 1× 大型（1000 介面） | 3.63s | 1.43s | 93.7 MiB | +0.0 MiB |
| 5× 大型併發 | 10.58s | 7.58s | 93.2 MiB | +0.5 MiB |
| 20× 大型併發 | 44.70s | 34.46s | **176.4 MiB** | +84.5 MiB |

最壞情況（20 台 1000 介面設備同時輪詢）尖峰 176 MiB。換算下來每一個併發的
大型設備約 **4.2 MiB**。

### 2.3 有沒有洩漏？

連續 10 輪、每輪 10 台大型設備：

```
cycle  1: 150.2 MiB      cycle  6: 171.5 MiB
cycle  2: 168.2 MiB      cycle  7: 142.6 MiB
cycle  3: 184.3 MiB      cycle  8: 143.5 MiB
cycle  4: 142.6 MiB      cycle  9: 141.6 MiB
cycle  5: 171.5 MiB      cycle 10: 173.5 MiB

第 5 輪 -> 第 10 輪：+1.95 MiB
```

RSS 在 141～184 MiB 之間震盪、**沒有趨勢**（第 7～9 輪比第 1 輪還低）。
tracemalloc 顯示 Python heap 的淨成長只有約 0.8 MiB，而且全部落在 `ply/yacc.py`
（pysmi 的 MIB parser 表，一次性建立）。**沒有記憶體洩漏。**

震盪的原因是 glibc 不會立刻把釋放的 arena 還給 OS——單獨量測
`closeDispatcher()` 時，63 MiB 只有 14 MiB 馬上回到 OS。所以 RSS 看起來高於
實際活躍資料量，這是正常的高水位現象，不是漏。

### 2.4 結論：記憶體不是問題

- collector 容器 **512 MiB 很充裕**，256 MiB 對一般園區網路也夠
- 真要壓低，調 `COLLECTOR_CONCURRENT` 就是線性有效的旋鈕（每個併發大型設備
  約 4.2 MiB）
- 不需要為了記憶體換語言

---

## 3. 真正的上限是 CPU

改動後，20 台大型設備併發花 34.46s **CPU**（wall 44.70s，CPU 佔 77%）。
也就是一台 1000 介面的設備每輪要燒掉約 **1.7 秒的 CPU** 來做 ASN.1 解析和
物件配置。這部分無法用 asyncio 疊起來——它是單執行緒的實際運算。

用 `COLLECTOR_INTERVAL=300` 推估**單一 Python 行程**的容量：

| 設備類型 | 每輪 CPU/台 | 300 秒內大約可容納 |
|---|---|---|
| 小型接取交換機（約 48 埠） | 0.13 s | ~2300 台 |
| 大型機框（1000 埠、500 鄰居） | 1.7 s | **~170 台** |

（留 10～20% 餘裕給 topology/alert engine 和 DB 寫入。）

這就是判斷要不要上 Rust 的分界線。

---

## 4. Python + Rust 拆分評估

### 結論：現在不需要，而且不要整個重寫

理由：

1. **這次真正的 bug 是依賴版本漂移，不是語言問題。** 收不到 LLDP 的原因是
   pysnmp 4.x → 6.x 的 API 破壞性變更（`nextCmd` 的回傳從 varBind 變成
   varBindTable），加上沒有鎖版本、也沒有靜態型別檢查去攔。Rust 的型別系統
   確實會在編譯期抓到這個錯——但 `pyright` + lockfile 也會，而且成本低兩個
   數量級。
2. **效能問題已經解決了 15 倍，而且是改架構不是換語言。** 原本的瓶頸是重建
   engine，不是 Python 慢。
3. **記憶體完全沒問題**（見第 2 節）。
4. **這個專案的價值在各廠牌 MIB 的怪癖處理**（LLDP subtype、`lldpRemLocalPortNum`
   對應、`lldpRemManAddrTable` 索引裡的管理 IP、各家 CPU OID），那是知識而不是
   程式碼，重寫最容易漏掉。Rust 的 SNMP crate 生態明顯比 pysnmp 薄，SNMPv3 的
   auth/priv 套件支援尤其要先確認——從「難搞但完整」換到「乾淨但可能缺功能」
   是真實的風險。
5. **API 層（FastAPI + SQLAlchemy）完全沒有效能問題**，沒有任何理由動它。

### 比 Rust 更划算的下一步（依序）

1. **鎖死依賴**：用 `pip-compile` 產生含 hash 的 lock 檔。這次事故的根因。
2. **靜態型別檢查**：`pyright` 進 CI。
3. **行程分片**：collector 已經是獨立容器，起 N 個各負責 1/N 的設備就繞過 GIL
   了，架構上已經支援，不需要改程式。這是最便宜的水平擴充。
4. **錯開輪詢**：目前是每 300 秒一次把所有設備打出去。改成在週期內均勻分散，
   尖峰記憶體和 CPU 都會下降。
5. **只抓需要的介面**：目前 `get_interface_stats()` 走訪整張 ifTable 的 4 個
   欄位。只抓有 LLDP 鄰居的介面，對大型機框的省幅很大。

### 什麼時候才真的該上 Rust

當以上都做完，而且**量到**單一行程撐不住時——參考第 3 節，大約是
**超過 170 台 1000 埠等級的設備、又要求 5 分鐘輪詢**的規模。

那時候正確的做法是**只把 collector 換成 Rust 單一執行檔**，讀寫同一個
Postgres，API 和前端留在 Python：

- collector 是唯一對效能敏感的部分
- collector 是唯一可以獨立替換的部分（它和 API 之間只透過 DB 溝通）
- 可以漸進遷移：先讓 Rust collector 接管一部分設備，兩者並存

**不要**把 API、topology engine、alert engine 一起重寫——它們不是瓶頸。

---

## 5. 設備不回應時的退避

無回應的設備是輪詢週期裡最貴的東西：每一個 OID 都要等滿
`SNMP_TIMEOUT × (SNMP_RETRIES + 1)`。以預設值（5 秒 × 3）計算，光
`get_device_info()` 的 3 個 GET 就是 **45 秒**，而且每輪都付一次、永遠拿不到
資料。

`app/core/poll_scheduler.py` 的 `OfflineBackoff` 會依連續失敗次數拉長間隔：

| 連續失敗次數 | 下次嘗試間隔（預設） |
|---|---|
| 1 | 300s（等於正常週期，不延遲） |
| 2 | 600s |
| 3 | 1200s |
| 4 以上 | 1800s（上限） |

第一次失敗**不會**造成任何延遲，所以偶發的一次 timeout 沒有代價；只有持續
失敗的設備才會被退避。時間來源是 `time.monotonic()`，不受 NTP 跳時或日光
節約時間影響。

### 設定

| 環境變數 | 預設 | 說明 |
|---|---|---|
| `COLLECTOR_OFFLINE_BACKOFF` | `true` | 設 `false` 可關閉，恢復每輪都探測 |
| `COLLECTOR_OFFLINE_BACKOFF_MAX` | `1800` | 退避間隔上限（秒） |

被跳過的設備狀態維持 `offline`，不會因為沒探測就變成 unknown。每輪的 INFO
log 會顯示跳過幾台，DEBUG log 會顯示每一台還要等多久。

### 狀態存在記憶體裡（刻意的）

這個專案用 `Base.metadata.create_all` 建表、**沒有 migration 機制**
（沒有 alembic 目錄）。在 `devices` 加欄位會讓所有既有部署在下一次 SELECT
就壞掉。所以退避狀態只存在 collector 行程的記憶體裡。

代價：collector 重啟後，每台離線設備會多被探測一輪。可以接受。

記憶體方面，每輪會呼叫 `prune()` 清掉已刪除設備的狀態，所以長時間執行不會
累積。每台失敗設備的狀態是一個只有兩個欄位的 dataclass。
