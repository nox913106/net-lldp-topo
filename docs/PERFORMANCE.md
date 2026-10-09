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

> **注意：這一節的容量推估已被第 7 節取代。**
> 下面的「~170 台」是用「每台都是 1000 埠機框」外推的，那種機隊在實體上
> 不存在。以真實接線架構重算的結果見第 7 節（單一行程 ~3000 台接取交換機），
> 以及第 8 節 —— 先撞牆的其實是資料庫，不是 CPU。


改動後，20 台大型設備併發花 34.46s **CPU**（wall 44.70s，CPU 佔 77%）。
也就是一台 1000 介面的設備每輪要燒掉約 **1.7 秒的 CPU** 來做 ASN.1 解析和
物件配置。這部分無法用 asyncio 疊起來——它是單執行緒的實際運算。

用 `COLLECTOR_INTERVAL=300` 推估**單一 Python 行程**的容量：

| 設備類型 | 每輪 CPU/台 | 300 秒內大約可容納 |
|---|---|---|
| 小型接取交換機（約 48 埠） | 0.13 s | ~2300 台 |
| 大型機框（1000 埠、500 鄰居） | 1.7 s | **~170 台** |

（留 10～20% 餘裕給 topology/alert engine 和 DB 寫入。）

這就是判斷要不要上 Rust 的分界線。（修正後的分界線見第 10 節。）

---

## 4. Python + Rust 拆分評估

### 結論：現在不需要，而且不要整個重寫

> 這一節的結論在第 10 節重新驗證過，結論不變但理由換了：原本寫的
> 「反正是 I/O-bound」是錯的（實測是 CPU 佔 77%），真正的理由是
> SNMP 那一段可以靠行程分片線性擴展。


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

---

# 以 500+ 台為基準點的重新評估

前面第 3、4 節的容量推估是用「單台設備外推」做的，那個做法會高估，因為它
假設每台設備都是 1000 埠 / 500 鄰居的機框。實際的實體接線架構不可能長這樣
（500 台這種設備等於 25 萬條線）。這一節改用**整個機隊**的量測重算一次，
並且把資料庫那一段也納進來 —— 結論和前面不一樣。

## 6. 唯一真正重要的常數：每列 138 µs

把 collector 用到的 15 張表逐一計時（大型機框，1000 介面 / 500 鄰居）：

| 走訪 | 列數 | CPU ms | µs/列 |
|---|---|---|---|
| lldpRemChassisId | 500 | 65.9 | 132 |
| lldpRemChassisIdSubtype | 500 | 78.8 | 158 |
| lldpRemPortId | 500 | 79.7 | 159 |
| lldpRemPortIdSubtype | 500 | 76.3 | 153 |
| lldpRemPortDesc | 500 | 66.7 | 133 |
| lldpRemSysName | 500 | 67.8 | 136 |
| lldpRemManAddr | 500 | 69.2 | 138 |
| lldpLocPortDesc | 500 | 66.2 | 132 |
| lldpLocPortId | 500 | 67.3 | 135 |
| lldpLocPortIdSubtype | 500 | 64.2 | 128 |
| ifName | 1000 | 124.7 | 125 |
| ifDescr | 1000 | 137.1 | 137 |
| ifHighSpeed | 1000 | 159.3 | 159 |
| ifHCInOctets | 1000 | 124.5 | 124 |
| ifHCOutOctets | 1000 | 137.1 | 137 |
| **合計** | **10000** | **1384.8** | **138** |

成本和表的種類無關，平得像一條線。這就是 pysnmp 解碼一列的硬成本。於是：

```
每台每輪 CPU ≈ 30 ms（固定）+ 138 µs × 走訪列數
走訪列數      = 10 × 鄰居數 + 5 × 介面數
```

驗證（兩種設備，誤差 < 3%）：

| 設備 | 介面 | 鄰居 | 預測列數 | 預測 CPU | 實測 CPU |
|---|---|---|---|---|---|
| 大型機框 | 1000 | 500 | 10000 | 1410 ms | 1381 ms |
| 接取交換機 | 48 | 4 | 280 | 69 ms | 70 ms |

**關鍵在於這個公式沒有一項跟設備數有關，只跟「實體線數」和「埠數」有關。**
`10 × 鄰居數` 是你機房裡實際插了幾條線決定的，不是 CPU 速度決定的。

## 7. 500 台實際要燒多少

機隊組成取一個真實的校園／園區網路：1 台核心 + 8 台匯聚（96 埠 / 62 鄰居）
+ 491 台接取（48 埠 / 20 鄰居，含 IP 電話與 AP）。

| 項目 | 每輪 |
|---|---|
| 走訪列數（全機隊） | ~225,000 |
| SNMP 解碼 CPU | 31.1 s |
| 每台固定成本 × 500 | 15.0 s |
| **SNMP 合計** | **46 s** |

300 秒週期裡佔 **15% 的一顆核心**。Python 在 500 台這個量級完全不吃緊。

前面第 3 節說「單一行程 ~170 台」是錯的推估基準 —— 那是 500 台全都是
1000 埠機框的假設。以實際接線架構來算，單一 Python 行程在 300 秒週期內
可以處理 **~3000 台**接取交換機。

## 8. 真正先撞牆的是資料庫，不是 Python

> **這一節描述的是改動前的狀態。索引、每台一個 session 與發現快取已經
> 做掉了（第 13 節），改前/改後的實測數字在第 14 節。**

`poll_single_device()` 對**每個鄰居每一輪**都發一次查詢：

```python
select(RawLink).where(RawLink.local_device_id == device.id,
                      RawLink.local_port == neighbor.local_port,
                      RawLink.remote_hostname == neighbor.remote_hostname)
```

而 `raw_links` **除了主鍵之外沒有任何索引**（`app/models/link.py`），
`merged_links` 的 `(device_a_id, device_b_id)` 也沒有 —— 而
`update_merged_links_in_db()` 每輪對每條 merged link 查一次。
所以每次查詢都是全表掃描，而查詢次數又和表的列數同步成長：**平方級**。

另外 `auto_discover_neighbor()` 預設開啟（`auto_discover` 預設 `True`），
每個鄰居每輪再加兩次 `devices` 查詢。

真實 PostgreSQL 16 實測一輪的資料庫時間（20 鄰居/台）：

| 設備數 | raw_links 列數 | 查詢次數 | 無索引 | 加索引 | 改善 |
|---|---|---|---|---|---|
| 50 | 1,042 | 2,084 | 1.30 s | 1.12 s | 1.2× |
| 100 | 2,042 | 4,084 | 2.63 s | 2.26 s | 1.2× |
| 250 | 5,168 | 10,336 | 9.79 s | 5.53 s | 1.8× |
| 500 | 10,336 | 20,672 | **23.04 s** | 13.23 s | 1.7× |
| 1000 | 20,672 | 41,344 | **56.58 s** | 26.68 s | 2.1× |

設備數 20 倍（50→1000），無索引的時間變成 **43.5 倍** —— 平方項確認存在。
加了索引之後成長回到接近線性（23.8 倍 / 20 倍）。

### 8.1 併發對資料庫那一段完全無效

`poll_all_devices()` 把**同一個 `db` session** 傳給所有併發的 device poll。
SQLAlchemy 的 `AsyncSession` 不支援併發操作，它會在內部序列化（實測不會報錯，
但也完全不會平行）。500 台、已加索引：

| COLLECTOR_CONCURRENT | 共用 session（現狀） | 每台一個 session |
|---|---|---|
| 1 | 10.60 s | 11.43 s |
| 10 | 12.23 s | 7.40 s |
| 20 | 11.74 s | 7.52 s |
| 40 | 12.64 s | 8.24 s |

**把併發從 1 開到 40，資料庫那一段一秒都沒有變快。** 這是設計上的硬序列化點。
改成每台一個 session 之後併發才開始有作用（1.6×）。

### 8.2 500 台的完整帳

| 項目 | 每輪（現狀） | 每輪（修掉索引+session） |
|---|---|---|
| SNMP 解碼 | 46 s | 46 s |
| 資料庫 | 23 s | 7.5 s |
| 合計 / 300 s 週期 | **69 s（23%）** | **54 s（18%）** |

## 9. 有一段 CPU 是純浪費

> **已修正（第 13.3 節）。** 那 9.8 s/輪不再是浪費：`ifHighSpeed` 現在
> 真的接到每條線路的頻寬上，`ifHCIn/OutOctets` 算成真實的 bps 與使用率。
> 只要接線盤點、不要流量的部署可以用 `COLLECTOR_TRAFFIC_STATS=false`
> 關掉那兩張表的走訪，而仍然保留真實埠速。

`get_interface_stats()` 每輪在每台設備上走訪 `ifDescr`、`ifHighSpeed`、
`ifHCInOctets`、`ifHCOutOctets`，**而它的回傳值沒有任何人使用** ——
`app/collector.py:291` 只拿它的 `len()` 印一行 log。全專案沒有其他地方讀它
（沒有 InterfaceStats 資料表，API 也沒有這個端點）。

而 `topology_engine.py` 計算連結頻寬的方式是**從埠名字串猜**：

```python
bandwidth = self._estimate_bandwidth(link.local_port)
port_details.append(PortInfo(..., in_bps=0,   # Will be updated by collector
                                  out_bps=0))
```

那行註解裡的 "will be updated by collector" 從來沒有發生。所以現在的狀況是：
**真正的 `ifHighSpeed` 被抓回來丟掉，然後用埠名去猜一個值；流量使用率永遠是 0。**

成本：500 台機隊每輪 `3 × 48 × 491 ≈ 71,000` 列 = **9.8 s CPU**，
佔 SNMP 總時間的 21%，換到的資料是沒人看的。

這一點直接牴觸這個專案「最簡單最快速**最準確**」的初衷 —— 而且它不是語言問題，
Rust 重寫一遍只會讓同一筆沒人要的資料算得更快。

## 10. 修正後的 Rust 結論

以 500 台為基準點，把兩邊放在一起看：

| | Rust 能改善的 | Rust 改善不了的 |
|---|---|---|
| 500 台 | 31 s 解碼 → ~2 s | 23 s 資料庫、15 s 固定成本 |
| 1000 台 | 62 s 解碼 → ~3 s | 57 s 資料庫（且是平方成長） |

**在 500 台，用 Rust 重寫 collector 能拿回 300 秒預算裡的 10%；
補兩個索引加改 session 生命週期能拿回 5%；刪掉沒人用的那段走訪能拿回 3%。**
後兩項合起來跟 Rust 同一個量級，但一個是幾十行，一個是連 SNMPv3 都要重做的重寫。

更重要的是成長的形狀：SNMP 那一段是**線性**的，而且 collector 已經是獨立容器，
**起 N 個行程各管 1/N 台設備就直接線性擴展，不用改一行程式** —— 這正是 Rust
會給你的東西。資料庫那一段是**平方**的，而且分片分不掉（共用同一個 Postgres），
所以它才是那個會先撞牆的。

結論沒變但理由換了：**不是因為「反正是 I/O-bound」（那個說法我前面講錯了），
而是因為 Python 這一段可以靠分片線性擴展，而先撞牆的那一段換成 Rust 也一樣會撞。**

真的該考慮 Rust 的時機：已經分片到多台主機、資料庫那層也修完了、而你想減少
主機數量 —— 大約在 2000 台以上。即使那時候，也只換 collector 執行檔
（它只透過 Postgres 跟其他元件講話，可以和 Python 版並存漸進遷移），
API、topology engine、alert engine 留在 Python。

## 11. 依投資報酬率排序的下一步

| # | 動作 | 效果 | 風險 | 狀態 |
|---|---|---|---|---|
| 1 | `raw_links(local_device_id, local_port, remote_hostname)` 與 `merged_links(device_a_id, device_b_id)` 加索引 | 500 台少 10 s/輪，解決平方成長 | 低 | **已完成**（13.1） |
| 2 | 每台一個 DB session | 併發開始對 DB 有效，再少 6 s/輪 | 低 | **已完成**（13.2） |
| 3 | 把 `ifHighSpeed` 真的接到 merged link（取代字串猜測），`ifHCIn/OutOctets` 改成可關閉 | 準確度提升；關掉時少 10 s/輪 | 低 | **已完成**（13.3） |
| 4 | `auto_discover_neighbor` 只對沒見過的 hostname 做（加一層行程內快取） | 省掉每輪 2 × 鄰居數 次查詢與重複 DNS 查詢 | 低 | **已完成**（13.4） |
| 5 | collector 行程分片（compose 起 N 個，各管 1/N） | SNMP 那一段線性擴展 | 中 | 未做 |
| 6 | 鎖定依賴（pip-compile + hash）、pyright 進 CI | 防止下一次 LLDP 事故 | 低 | 未做 |

### 索引有一個部署上的陷阱

本專案沒有 migration 機制，只用 `Base.metadata.create_all()`。實測確認：

```
既有資料表 + 模型新增索引 → create_all() 不會建立該索引
全新資料庫              → 會建立
```

`create_all(checkfirst=True)` 看到表已存在就整張跳過，不會比對索引。所以第 1 項
**不能只改模型**，否則你現有的部署升級後會繼續用全表掃描而且毫無徵兆。必須在
啟動時發明確的 `CREATE INDEX IF NOT EXISTS` DDL。

> 這一點已經做掉了，而且範圍比索引更大 —— 新增的欄位也有同樣的問題。
> 做法與升級路徑實測見第 13.1 節。

## 12. 這次量測的侷限

- 全部在 loopback 上對 snmpsim 模擬器量測，沒有真實網路延遲。這對 CPU 數字沒有
  影響（CPU 時間和封包延遲無關），但 wall time 會比真實環境樂觀。
- 本沙箱的 UDP/161 被過濾，模擬器跑在 1161。應用程式碼未因此改動。
- 500 台的機隊組成是估計值（1 核心 + 8 匯聚 + 491 接取，20 鄰居/台）。
  如果你的實際鄰居數差很多，用第 6 節的公式重算即可。

---

# 第二輪：把第 11 節的 1～4 項做掉，並接上真實流量

> 這一段是實作紀錄與改前/改後的實測。量測方式和前面不同：這次不是模擬查詢，
> 而是**把真實的 `poll_all_devices()` 跑在真實 PostgreSQL 16 上**，只把 SNMP
> 那一層換成不送封包的替身，所以量到的就是「一輪裡面資料庫那半邊」。
> 兩個版本（改前 `eb5bae1` 與改後）用的是同一支驅動程式與同一個機隊，
> 數字可以直接相比。

## 13. 這次改了什麼

### 13.1 兩個索引，以及既有資料庫怎麼真的拿到它

模型上加了：

```python
# raw_links —— poll_single_device() 每個鄰居每輪就查這三欄一次
Index("ix_raw_links_lookup", "local_device_id", "local_port", "remote_hostname")
# merged_links
Index("ix_merged_links_pair", "device_a_id", "device_b_id")
```

但第 11 節講的陷阱是真的：`create_all(checkfirst=True)` 看到表已存在就整張跳過。
所以 `init_db()` 之後多跑一段 `_sync_upgrade_schema()`，用 SQLAlchemy inspector
比對「模型上有、資料庫裡沒有」的欄位與索引，然後只做兩種能安全就地套用的動作：

- 新的**可為 NULL 欄位** → `ALTER TABLE ... ADD COLUMN`
- 新的**索引** → `CREATE INDEX IF NOT EXISTS`（PostgreSQL ≥ 9.5、SQLite ≥ 3.8 都支援）

其他的（刪欄位、改型別、對已有資料的表加 NOT NULL 欄位）只記一行 warning 就不動，
那種需要真正的 migration。

用「前一版建立的資料庫 + 一列真實資料」實測升級路徑：

```
== raw_links ==
  新增欄位： ['bandwidth_mbps', 'in_bps', 'in_octets', 'out_bps', 'out_octets', 'sampled_at']
  索引 before: ['ix_raw_links_id']
  索引 after : ['ix_raw_links_id', 'ix_raw_links_lookup']
== merged_links ==
  索引 before: ['ix_merged_links_id']
  索引 after : ['ix_merged_links_id', 'ix_merged_links_pair']

既有資料列保留： 1
升級後的查詢計畫： SEARCH raw_links USING INDEX ix_raw_links_lookup
                   (local_device_id=? AND local_port=? AND remote_hostname=?)
```

最後一行是重點：升級前這句是全表掃描，升級後走索引。**升級不需要人工介入，
也不會動到既有資料。** collector 容器自己也會跑這段（`_prepare_database()`，
失敗重試三次），因為它可能比 app 容器先起來，而索引正是讓它不退化成全表掃描的東西。

### 13.2 每台設備一個 DB session

改前：`poll_all_devices()` 把**同一個 `db`** 傳給所有併發的 `poll_single_device()`。
`AsyncSession` 不支援併發，但它不報錯 —— 它內部序列化。所以 `COLLECTOR_CONCURRENT`
對資料庫那半邊完全沒有作用（第 8.1 節）。

改後：每台設備在自己的 session 裡取得、更新、commit。相關的連帶修正：

- 連線池大小改成 `max(10, COLLECTOR_CONCURRENT + 5)`。否則併發上限只是變成
  在連線池上排隊。
- 自動發現的 insert 走**另一個**短命 session，並且用一個大小 4 的 semaphore
  限流。新鄰居的 insert 不應該因為撞到 hostname 唯一鍵就讓那台設備整輪的
  連線資料一起 rollback；而第一輪全機隊都是新設備時，也不該要求兩倍連線數。
- 那個 insert 的 `IntegrityError` 單獨接住：多行程分片時兩個行程會同時發現
  同一台新設備，先到的贏，後到的把它記進快取就好。

### 13.3 真實頻寬與真實流量

資料存在 `raw_links` 上，而不是新開一張表 —— 因為 `raw_links` 的每一列就是
**一條實體線路的一端**，而它每輪本來就會被讀一次寫一次，所以多存這幾欄不增加
任何查詢：

```python
bandwidth_mbps    # ifHighSpeed，設備自己報的埠速
in_octets/out_octets   # 上一次的計數器讀值
in_bps/out_bps         # 由兩次取樣算出來的速率
sampled_at             # 上一次取樣的時間
```

`ifHCInOctets` 是**累計計數器不是速率**，速率只存在於兩次取樣之間。
`app/core/traffic.py` 的 `counter_rate()` 負責這件事，而且在算不出來的時候
回傳 `None` 而不是 `0` —— 「還沒量到」和「沒有流量」是兩件不同的事：

| 情況 | 回傳 |
|---|---|
| 正常：300 s 內 +37.5 MB | `1,000,000` bps |
| 這個埠的第一次取樣 | `None` |
| 計數器倒退（設備重開/清計數器） | `None` |
| 閒置埠，兩次讀值相同 | `0` |
| 兩次取樣間隔 > 4 小時 | `None` |
| 兩次取樣間隔 < 1 秒 | `None` |
| 上一筆時間戳帶時區（PostgreSQL）、這一筆不帶（naive） | 正常計算 |
| 10G 埠跑滿線速 | `10,000,000,000` bps |

計數器倒退一律當成重置，不當成繞回：64 位元 octet 計數器在 100 Gbps 下要
約 46 年才繞一圈。時區那一列是必要的 —— 同一個欄位從 SQLite 讀回來是 naive，
從 PostgreSQL 的 `timestamptz` 讀回來帶時區，兩者相減會直接 `TypeError`。

鄰居對應到介面：**先用 ifIndex**（LLDP 收集階段已經解析出來，不會有歧義），
名稱只是備援（有些設備的 `lldpLocPortTable` 對不回 ifIndex）。`ifName` 和
`ifDescr` 兩種拼法都收（`Gi1/0/1` 與 `GigabitEthernet1/0/1`），而這兩張表
本來就在 per-poll 快取裡，所以這一步沒有多走訪任何東西。

成本面：`ifDescr`/`ifName` 來自快取，所以這一段真正的成本只有 `ifHighSpeed`
加上（開啟流量時）兩張 octet 表。`COLLECTOR_TRAFFIC_STATS=false` 會關掉那兩張，
**但仍然保留真實的 `ifHighSpeed`** —— 只要接線盤點、不要流量的部署，準確度不打折。

### 13.4 auto-discover 快取

改前每個鄰居每輪固定兩次 `devices` 查詢（hostname、ip_address），500 台約
兩萬次，而答案在輪與輪之間幾乎不變。改後在每輪開始時用**一次**
`SELECT hostname, ip_address FROM devices` 把整個機隊載進記憶體（含 excluded
的設備，否則被排除的設備會立刻被重新發現再加回來）。

`DiscoveryCache` 同時記住「轉不成設備的鄰居」（沒有管理位址、DNS 查不到、
SNMP 不回應 —— IP 話機、印表機、關掉 SNMP 的 AP），預設一小時內不再探測，
過期後會再試，所以後來才變得可達的設備不會被永久忽略。它的 `claim()` 也兼作
鎖：兩台設備在同一輪看到同一個新鄰居時，只有一個會去解析、探測、insert。

兩輪的實測（`sw1`/`sw2` 加上一個 AP 與一個對不到的 `Po1` 鄰居）：

```
第 1 輪 DNS 查詢： ['ap-lobby', 'sw9']
第 2 輪 DNS 查詢： []            <- 快取回答，沒有碰網路
```

### 13.5 順手修掉的兩個準確度問題

**一、頻寬被算成兩倍。** 兩端都納管時，同一條線會被兩邊各報一次，而
`merge_links()` 把群組裡所有 raw link 的頻寬相加 —— 於是兩條 1G 線被算成
4000 Mbps，而所有由它推導出來的使用率都只有真值的一半。改成只採信一端
（看到比較多條線的那一端；用埠名去配對兩端的報告不可靠，一端叫 `Gi1/0/1`
另一端叫 `GigabitEthernet1/0/1`）。兩輪端對端實測：

```
兩條 1G 線、兩端都有回報 -> total_bandwidth_mbps=2000（不是 4000）
in=1,995,650 bps（預期 2,000,000）  out=3,991,302（預期 4,000,000）
util_in=0.0998%  util_out=0.1996%
```

差的 0.2% 不是誤差而是正確行為：速率是用**兩次取樣的實際間隔**除的，
這個測試的兩輪相距 300.5 秒而不是 300.0 秒。

**二、「0%」不等於「沒量到」。** 使用率現在可以是 `null`，API 照實傳，
前端顯示 `—` 而不是 `0%`。重開 collector 後的第一輪必然沒有速率
（速率需要兩次取樣），那時候畫 0% 就是在報一個它不知道的數字。
`port_details` 另外多了 `speed_source`（`ifHighSpeed` 或 `port_name`），
所以看得出哪些埠的速度是真讀的、哪些是猜的。

順帶的效果：`alert_engine.check_link_utilization()` 本來就存在，但因為使用率
永遠是 0，**線路使用率告警從來沒有可能觸發過**。現在它有真實輸入了。

HTTP 層實測（`GET /api/v1/topology/?view=full`）：

```json
{
  "total_bandwidth_mbps": 2000,
  "utilization_in_percent": 0.0997825,
  "port_details": [
    {"local_port": "Gi1/0/1", "remote_port": "GigabitEthernet1/0/24",
     "bandwidth_mbps": 1000, "in_bps": 997825, "out_bps": 1995651,
     "utilization_percent": 0.1995651, "speed_source": "ifHighSpeed"}
  ]
}
```

## 14. 500 台實測：改前 vs 改後

機隊：1 核心（96 埠 / 8 鄰居）+ 8 匯聚（96 埠 / 62 鄰居）+ 491 接取
（48 埠 / 20 鄰居，1 條上行 + 19 個端點設備）= 10,324 條 raw link。
PostgreSQL 16.13，SNMP 層換成不送封包的替身，所以表格裡的時間就是
**一輪裡資料庫那半邊的 wall time**。取第 2、3 輪的平均（穩定狀態）。

### 14.1 併發掃描

| `COLLECTOR_CONCURRENT` | 改前 | 改後 | 倍數 |
|---|---|---|---|
| 1 | 37.13 s | **18.61 s** | 2.00× |
| 20 | 29.02 s | **13.18 s** | 2.20× |
| 40 | 29.89 s | **13.60 s** | 2.20× |

每輪發出的 SQL 敘述數：改前 27,561（併發 1 時 37,509），改後 **24,000**。

兩件事要分開看：

- **索引 + 發現快取**本身就給了 2.0×（併發 1：37.13 → 18.61 s）。這是完全
  不依賴併發的那一半。
- **每台一個 session** 讓併發開始有作用。改前從併發 1 開到 20 只快 1.28 倍
  （37.13 → 29.02），改後是 1.41 倍（18.61 → 13.18）。幅度不大，但方向對了。

**併發開到 40 在兩個版本都沒有再變快。** 剩下的序列化點是合併與告警那一段
（它們跑在單一 session 裡），所以 `COLLECTOR_CONCURRENT` 調超過 20 對資料庫
那半邊沒有幫助 —— 這點改完之後依然成立，不要以為調大就會更快。

### 14.2 500 台的新帳

| 項目 | 改前 | 改後 |
|---|---|---|
| SNMP 解碼（第 7 節） | 46 s | 46 s |
| 其中沒人使用的走訪（第 9 節） | 9.8 s | 0 s（資料接上了，不再是浪費） |
| 資料庫 | 29 s | 13 s |
| 每輪合計 | 75 s | **59 s** |
| 佔 300 秒週期 | 25% | **20%** |

SNMP 那一段沒有變快 —— 本來就沒打算讓它變快，第 10 節已經說明理由
（它是線性的，而且靠行程分片就能擴展）。真正的變化是：**那 9.8 s 現在換到了
真實的埠速與真實的流量**，而原本會隨機隊平方成長的那一段不再那樣成長。
