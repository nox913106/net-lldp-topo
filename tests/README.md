# tests

What CI runs. Two kinds, and the split matters:

| 檔案 | 需要什麼 | 守住什麼 |
|---|---|---|
| `test_damping.py`、`test_sharding.py`、`test_config.py` | 什麼都不用 | 純邏輯：告警抑制、分片、設定解析 |
| `test_snmp_collector.py` | 一個 snmpsim 代理 | **真的走一次 SNMP**，而且斷言「有抓到東西」 |

第二類是這個專案被咬過的地方：部署出去的 v1.x 在 pysnmp 介面換掉之後，
LLDP 走訪變成**回傳 0 筆、不丟任何錯誤**，從外面看就是拓撲圖空白
（`docs/TROUBLESHOOTING_LLDP.md`）。型別檢查抓不到那件事（那些進入點藏在
`from ... import *` 後面，而且現在程式自己用 try/except 包著兩個版本），
鎖定版本可以讓它不再自己變動，但真正能證明「走訪還活著」的只有一個會問
真agent 的測試。所以 `test_snmp_collector.py` 斷言的不是型別，是**筆數**。

## 跑法

```bash
# 純邏輯的部分，什麼都不用裝
python -m pytest tests -q -k "not snmp"

# 連 SNMP 那一組一起（snmpsim 裝在另一個 venv，見下）
python -m venv .venv-sim && .venv-sim/bin/pip install -r requirements-sim.txt
SNMPSIM_BIN=.venv-sim/bin/snmpsim-command-responder python -m pytest tests -q
```

`SNMPSIM_BIN` 沒設或那個檔不存在時，SNMP 那一組會 **skip**（不是 fail）——
在沒有模擬器的機器上跑純邏輯測試不應該變成紅燈。

## 為什麼 snmpsim 要裝在另一個 venv

snmpsim 依賴 `pysnmp-lextudio`，而它提供的 import 名稱就是 `pysnmp` ——
跟這個專案鎖定的 `pysnmp` 是同一個 import 名稱的兩個發行版。裝在同一個環境
裡，最後哪一份檔案留下來取決於安裝順序，而測試就不再是在測鎖定的那一份。
分成兩個 venv 之後，模擬器只是一個另一個行程，透過 UDP 對話，不可能蓋掉
被測的程式。
