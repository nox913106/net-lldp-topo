# tests

What CI runs. Two kinds, and the split matters:

| 檔案 | 需要什麼 | 守住什麼 |
|---|---|---|
| `test_damping.py`、`test_sharding.py`、`test_config.py` | 什麼都不用 | 純邏輯：告警抑制、分片、設定解析 |
| `test_device_addresses.py`、`test_trap_receiver.py`、`test_collector_api.py` | 一個資料庫（預設是暫存的 SQLite） | 位址對照、trap 認設備的順序、狀態 API |
| `test_snmp_collector.py` | 一個 snmpsim 代理 | **真的走一次 SNMP**，而且斷言「有抓到東西」 |
| `test_loops_e2e.py` | 代理 + 資料庫 | **真的跑一輪盤點與快迴圈**：線路寫進去了、合併成一條、告警出現、流量是速率不是計數器 |
| `test_trap_e2e.py` | 代理 + 資料庫 + 一個 UDP 埠 | **真的 trap 封包**：從設備自己持有的位址送來會被套用、誰都不持有的位址被忽略、v1 的宣稱只是最後手段、偽造的宣稱蓋不過來源位址、位址走訪的間隔內新位址還不認得 |
| `test_lease_e2e.py` | 一個資料庫 | 整機群的工作只有一個行程做、過期才接手、分片心跳回報的狀態、少一片會被指名 |
| `test_proxy_auth.py` | 一個 nginx（裝了就好） | **真的跑 `deploy/nginx.conf`，後面接真的應用程式**：沒登入到不了任何東西（API 401、頁面轉登入頁、`/docs` 也是）、cookie 是 `Secure; HttpOnly; SameSite`、沒有 CSRF 標頭的變更被拒絕、明文 HTTP 在送出任何東西前就被轉走、兩個健康檢查路徑仍然免登入 |
| `test_admin.py` | 不用 | 後台：每個角色能做的剛好是它那一份、最後一個管理員刪不掉、失敗鎖定、AD 帳號第一次是訪客、舊版 `$apr1$` 密碼照樣登入、每個會改東西的路由都進稽核紀錄且不含密碼、告警變成有簽章的 Webhook、大量告警合併、送不出去會重試、密碼不回傳給瀏覽器、背景執行器拒絕符號連結／別人的檔案／名單外的工作、健康檢查、憑證與私鑰不成對被拒 |
| `test_http_surface.py` | 一個資料庫 | 兩個健康檢查的語意（ready 真的 `SELECT 1`、失敗是 503 而且不洩漏連線字串）、被刪掉的空殼端點真的是 404、CORS 預設關閉 |
| `test_deploy.py` | 什麼都不用（有 nginx 的話多驗一項） | 三支腳本（`scripts/bootstrap.sh`、`scripts/mf-topomon.sh`、`scripts/mf-topomon-docker.sh`）與舊路徑那個殼的語法；每個指令都有被 dispatch 也有被寫進說明；兩支工具的指令是對應的；systemd 單元不是 root、應用程式只聽 127.0.0.1；裝到主機 nginx 的站台就是被測試的那一份只換三樣東西；doctor 每一條沒過的檢查都附修復指令；compose 沒有把 8080／5432 開到網路上；密碼檔讓代理的 worker 讀得到。**執行腳本的測試都在只有 PATH 與 HOME 的環境下跑** —— 作者 shell 裡的環境變數曾經遮掉一個 `set -u` 的錯（docs/PERFORMANCE.md 第 41.5 節） |

最後三個檔案以前是 scratchpad 裡的腳本 —— 也就是這個專案最強的證據活在一個
會被回收的容器裡，CI 測到的東西比我手上少。現在它們在 CI 裡，而且跑兩次：
一次 SQLite、一次真的 PostgreSQL。那不是多餘的，有兩個已經發生過的錯只在
PostgreSQL 發作：連線池裡的連線綁在開它的 event loop 上，以及 Python 的
`None` 被當成 JSON 的 `"null"` 存進去（`JSON(none_as_null=True)`）——
SQLite 兩個都不會抱怨。

第二類是這個專案被咬過的地方：部署出去的 v1.x 在 pysnmp 介面換掉之後，
LLDP 走訪變成**回傳 0 筆、不丟任何錯誤**，從外面看就是拓撲圖空白
（`docs/TROUBLESHOOTING_LLDP.md`）。型別檢查抓不到那件事（那些進入點藏在
`from ... import *` 後面，而且現在程式自己用 try/except 包著兩個版本），
鎖定版本可以讓它不再自己變動，但真正能證明「走訪還活著」的只有一個會問
真agent 的測試。所以 `test_snmp_collector.py` 斷言的不是型別，是**筆數**。

## 跑法

```bash
# 純邏輯的部分，什麼都不用裝
python -m pytest tests -q -k "not snmp and not e2e"

# 連 SNMP 那一組一起（snmpsim 裝在另一個 venv，見下）
python -m venv .venv-sim && .venv-sim/bin/pip install -r requirements-sim.txt
SNMPSIM_BIN=.venv-sim/bin/snmpsim-command-responder python -m pytest tests -q
```

```bash
# 反向代理那一組（需要一個 nginx 在 PATH 上，或 NGINX_BIN 指給它）
sudo apt install -y nginx-light
python -m pytest tests/test_proxy_auth.py -q
```

`SNMPSIM_BIN` 沒設或那個檔不存在時，SNMP 那一組會 **skip**（不是 fail）——
在沒有模擬器的機器上跑純邏輯測試不應該變成紅燈。`nginx` 也是同一個規矩
（`NGINX_BIN`）。兩個 skip 在 CI 都被 `REQUIRE_SNMPSIM=1` 與
`REQUIRE_NGINX=1` 變成失敗，否則 CI 會在什麼都沒測的情況下變綠。

nginx 只要「裝了」就夠，不需要啟動：測試會自己在一個隨機埠上起一個，用的是
`deploy/nginx.conf` 本身（只替換 upstream 位址與那兩個埠）。比 1.25.1 舊的
nginx 不認得 `http2 on;`，測試會把那一行註解掉再跑 —— 部署用的映像釘在
1.27（`docker-compose.yml`）。

## 為什麼 snmpsim 要裝在另一個 venv

snmpsim 依賴 `pysnmp-lextudio`，而它提供的 import 名稱就是 `pysnmp` ——
跟這個專案鎖定的 `pysnmp` 是同一個 import 名稱的兩個發行版。裝在同一個環境
裡，最後哪一份檔案留下來取決於安裝順序，而測試就不再是在測鎖定的那一份。
分成兩個 venv 之後，模擬器只是一個另一個行程，透過 UDP 對話，不可能蓋掉
被測的程式。

## 對著真的 PostgreSQL 跑

```bash
docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=topomon \
    -e POSTGRES_USER=topomon -e POSTGRES_DB=topomon --name topomon-test postgres:16
DATABASE_URL=postgresql+asyncpg://topomon:topomon@127.0.0.1:5432/topomon \
SNMPSIM_BIN=.venv-sim/bin/snmpsim-command-responder python -m pytest
```

`DATABASE_URL` 沒設（或是空字串）時，測試會自己開一個暫存的 SQLite 檔，
不會碰到開發機上的 `topomon.db`。

## 端對端那幾個測試裡的兩個手法

* **一個模擬器當好幾台設備。** snmpsim 按 community 挑錄音檔，而上層是用
  sysName 認設備的，所以「兩個 community 互相把對方寫成鄰居」在拓樸引擎
  看起來就是兩台接在一起的交換機 —— 合併那段邏輯只有這樣才測得到。
  同一個代理也綁了第二個位址（127.0.0.4），這樣 trap 才能從一台**受管
  設備**送出去，而不是只能從陌生位址送。
* **換 community 等於換位址。** `test_trap_e2e` 要驗「設備多一個位址之後，
  要等下一次走訪才認得」，而中途重啟模擬器會把測試變成另一件事。改成把設備
  的 community 指到另一個錄音檔：對收集器來說，下一次走訪就是看到了不一樣的
  一組位址 —— 這正是改位址的樣子。時間用的是注入的假時鐘，所以「一小時後」
  不用真的等一小時。
