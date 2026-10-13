# DEV-lldp-topomon API 設計文件

## 1. API 總覽

| 端點 | 方法 | 說明 |
|------|------|------|
| `/api/v1/devices` | GET, POST | 設備管理 |
| `/api/v1/devices/{id}` | GET, PUT, DELETE | 單一設備操作 |
| `/api/v1/topology` | GET | 取得拓撲資料 |
| `/api/v1/topology/exclude-rules` | GET, POST, DELETE | 排除規則 |
| `/api/v1/alerts` | GET | 告警列表 |
| `/api/v1/alerts/active` | GET | 活躍告警 |
| `/api/v1/alerts/{id}/acknowledge` | POST | 確認告警 |
| `/api/v1/profiles` | GET, POST | Profile 管理 |
| `/api/v1/profiles/{id}` | GET, PUT, DELETE | 單一 Profile |
| `/api/v1/discovery/start` | POST | 啟動發現 |
| `/api/v1/discovery/status` | GET | 發現狀態 |
| `/api/v1/collector/status` | GET | 收集器狀態：分片、告警抑制、trap |

---

## 2. 設備 API

### GET /api/v1/devices

列出所有設備

**查詢參數：**
| 參數 | 類型 | 說明 |
|------|------|------|
| `skip` | int | 跳過筆數 (分頁) |
| `limit` | int | 回傳筆數 (0=全部) |
| `status` | string | 狀態過濾 (managed/offline/unknown) |
| `vendor` | string | 廠牌過濾 |
| `search` | string | 搜尋 hostname 或 IP (伺服器端) |
| `sort_by` | string | 排序欄位 (hostname/ip_address/vendor/status/last_seen) |
| `sort_order` | string | 排序方向 (asc/desc) |

**Response:**
```json
{
  "devices": [
    {
      "id": 1,
      "hostname": "Core-1",
      "ip_address": "10.0.0.1",
      "vendor": "cisco_nxos",
      "model": "N9K-C93180YC-EX",
      "firmware_version": "10.2(5)",
      "device_type": "core",
      "parent_device_id": null,
      "status": "managed",
      "cpu_percent": 45.2,
      "memory_percent": 68.5,
      "alert_profile": {"id": 1, "name": "Core Device"},
      "last_seen": "2025-12-10T23:50:00+08:00"
    }
  ],
  "total": 50
}
```

### POST /api/v1/devices

新增設備

**Request:**
```json
{
  "hostname": "Core-1",
  "ip_address": "10.0.0.1",
  "snmp_community": "public",
  "alert_profile_id": 1,
  "auto_discover": true
}
```

---

## 3. 拓撲 API

### GET /api/v1/topology

取得拓撲資料（用於前端 D3.js）

**Response:**
```json
{
  "nodes": [
    {
      "id": "1",
      "hostname": "Core-1",
      "ip_address": "10.0.0.1",
      "device_type": "switch",
      "vendor": "cisco",
      "status": "online",
      "cpu_percent": 45.2,
      "memory_percent": 68.5,
      "alert_count": 1
    }
  ],
  "links": [
    {
      "id": "1",
      "source": "1",
      "target": "2",
      "total_bandwidth_mbps": 2000,
      "utilization_in_percent": 45.5,
      "utilization_out_percent": 32.1,
      "status": "normal",
      "port_details": [
        {"local": "Gi0/1", "remote": "Gi0/1", "bandwidth": 1000},
        {"local": "Gi0/2", "remote": "Gi0/2", "bandwidth": 1000}
      ]
    }
  ],
  "last_updated": "2025-12-10T23:50:00+08:00"
}
```

### POST /api/v1/topology/exclude-rules

新增排除規則

**Request:**
```json
{
  "rule_type": "hostname_pattern",
  "pattern": "^AP-.*"
}
```

或

```json
{
  "rule_type": "device_pair",
  "device_a_id": 1,
  "device_b_id": 5
}
```

---

## 4. 告警 API

### GET /api/v1/alerts/active

取得活躍告警

**查詢參數：**
| 參數 | 類型 | 說明 |
|------|------|------|
| `device_id` | int | 過濾特定設備的告警 |

**Response:**
```json
{
  "alerts": [
    {
      "id": 1,
      "alert_type": "link_high_utilization",
      "severity": "warning",
      "hostname": "Core-1",
      "ip_address": "10.0.0.1",
      "device_id": 1,
      "link_id": 1,
      "message": "Link utilization 78.5% exceeds warning threshold 70%",
      "current_value": 78.5,
      "threshold_value": 70,
      "is_active": true,
      "triggered_at": "2025-12-10T23:45:00+08:00",
      "recovered_at": null,
      "acknowledged_at": null
    }
  ],
  "total": 1
}
```

### POST /api/v1/alerts/{id}/acknowledge

確認告警

**Request:**
```json
{
  "acknowledged_by": "admin"
}
```

---

## 5. Profile API

### GET /api/v1/profiles

列出所有 Profile

**Response:**
```json
{
  "profiles": [
    {
      "id": 1,
      "name": "Core Device",
      "description": "核心設備 - 嚴格閾值",
      "thresholds": {
        "link_utilization": {"warning": 60, "critical": 80, "recovery_buffer": 10},
        "cpu": {"warning": 70, "critical": 85, "recovery_buffer": 10},
        "memory": {"warning": 75, "critical": 90, "recovery_buffer": 10}
      },
      "is_default": false,
      "device_count": 5
    }
  ]
}
```

### POST /api/v1/profiles

新增 Profile

**Request:**
```json
{
  "name": "Custom Profile",
  "description": "自訂閾值",
  "thresholds": {
    "link_utilization": {"warning": 75, "critical": 90, "recovery_buffer": 10},
    "cpu": {"warning": 80, "critical": 95, "recovery_buffer": 10},
    "memory": {"warning": 80, "critical": 95, "recovery_buffer": 10},
    "device_offline_retry": 3
  }
}
```

---

## 6. 發現 API

### POST /api/v1/discovery/start

啟動設備發現

**Request:**
```json
{
  "seed_device_id": 1
}
```

**Response:**
```json
{
  "job_id": "abc123",
  "status": "started",
  "message": "Discovery started from device Core-1"
}
```

### GET /api/v1/discovery/status

查詢發現狀態

**Response:**
```json
{
  "job_id": "abc123",
  "status": "running",
  "progress": {
    "discovered": 45,
    "pending": 12,
    "failed": 2
  },
  "started_at": "2025-12-10T23:50:00+08:00"
}
```

---

## 7. WebSocket API

### /ws/topology

即時拓撲更新

**Message Format:**
```json
{
  "type": "topology_update",
  "data": {
    "links": [
      {"id": "1", "utilization_in_percent": 48.2}
    ]
  }
}
```

### /ws/alerts

即時告警推送

**Message Format:**
```json
{
  "type": "new_alert",
  "data": {
    "id": 5,
    "alert_type": "device_offline",
    "severity": "critical",
    "device": {"hostname": "Acc-3"},
    "message": "Device offline"
  }
}
```

---

## 8. 收集器 API

### GET /api/v1/collector/status

收集器行程自己回報的狀態。

這個端點跟其他端點不一樣的地方：**資料不是這個行程算出來的**。收集器跟 API
是兩個行程、不共用記憶體，而「哪些設備正在確認中」「哪些線被按住」是刻意留在
收集器行程記憶體裡的（`app/core/damping.py`）。所以每個收集器把它正在做的事
寫在自己本來就要續約的 heartbeat 那一列上（`collector_leases.status`），這裡
只是把那些列讀回來、把 id 換成看得懂的名字。

API 行程**不會**用自己的環境變數去填這些值：docker-compose 裡 `ALERT_*` 只設
在 collector 服務上，照自己的環境回答會變成「很有自信地顯示一組沒有人在用的
預設值」。

**Response:**
```json
{
  "reporting": true,
  "sharded": true,
  "shards_configured": 4,
  "shards_reporting": [0, 1, 3],
  "shards_missing": [2],
  "processes": [
    {
      "shard": 0, "shards": 4, "owner": "shard-0-of-4", "live": true,
      "renewed_at": "2026-10-10T19:06:39", "expires_at": "2026-10-10T19:16:39",
      "age_seconds": 12.2,
      "intervals": {"inventory": 300, "fast": 60, "addresses": 3600},
      "damping": {
        "offline_confirm_cycles": 3,
        "link_flap_transitions": 4,
        "link_flap_window": 600.0,
        "trap_requires_poll": true,
        "pending_offline": 1,
        "pending_offline_devices": [{"id": 3, "failures": 2}],
        "flapping_links": 1,
        "flapping_link_ids": [{"id": 1, "transitions": 7}]
      },
      "traps": {
        "listening": 162, "received": 412, "applied": 395, "ignored": 17,
        "dropped": 0, "deferred": 3, "requires_poll": true,
        "known_addresses": 1203, "unknown_sources": ["10.9.9.9"]
      }
    }
  ],
  "fleet_wide": {
    "owner": "shard-1-of-4", "live": true,
    "renewed_at": "2026-10-10T19:06:39", "takeovers": 0
  },
  "damping": {
    "offline_confirm_cycles": 3,
    "link_flap_transitions": 4,
    "link_flap_window": 600.0,
    "trap_requires_poll": true,
    "pending_offline": 1,
    "flapping_links": 1
  },
  "traps": {
    "receivers": 1, "received": 412, "applied": 395, "ignored": 17,
    "dropped": 0, "deferred": 3, "requires_poll": true,
    "known_addresses": 1203, "unknown_sources": ["10.9.9.9"]
  },
  "pending_offline_devices": [
    {"id": 3, "hostname": "core-sw-3", "ip_address": "10.0.0.3", "failures": 2}
  ],
  "flapping_links": [
    {"id": 1, "device_id": 1, "hostname": "core-sw-1",
     "local_port": "GigabitEthernet1/0/24", "remote_hostname": "dist-sw-7",
     "remote_port": "TenGigabitEthernet1/1/2", "transitions": 7}
  ],
  "warnings": [
    {
      "code": "shards_missing",
      "message": "Shard(s) 2 of 4 are not reporting: about 25% of the fleet is not being polled by anyone.",
      "params": {"shards": "2", "configured": 4, "percent": 25}
    }
  ]
}
```

**幾個欄位的意思：**

| 欄位 | 意思 |
|---|---|
| `reporting` | 有沒有**活著的**收集器在回報。false 可能是沒有在跑，也可能是收集器版本比這個欄位舊 —— 那樣它還是在輪詢，只是不會說自己的事 |
| `processes[].live` | 由**資料庫的時鐘**判定（`app/core/lease.py`），不是 API 行程的時鐘 |
| `processes[].age_seconds` | 用 API 行程的時鐘算的近似值，只是給 UI 顯示「12 秒前」用；權威答案是 `live` |
| `damping.<設定>` | 所有活著的行程回報的值。**如果各片不一樣，這裡會是一個陣列**，同時產生一個 `damping_disagree` 警告 |
| `damping.pending_offline` / `flapping_links` | 所有片加總 |
| `pending_offline_devices` | id 已經換成 hostname/IP，並附上「已經失敗幾次」。已經被刪掉的設備不會出現（不會留下一個光禿禿的 id） |
| `traps.known_addresses` | trap 可以比對的位址總數（不是加總：每個接收器都比對整個機群）。等於設備數就表示位址收集沒有生效，見 CONFIGURATION 第 5.3 節 |
| `processes[].intervals.addresses` | 那個行程走訪設備位址表的間隔（秒）。`0` = `COLLECTOR_DEVICE_ADDRESSES` 關掉了，所以「從不走訪」與「每輪走訪」不會長得一樣 |
| `warnings[]` | `code` 給 UI 翻譯用，`message` 是給不認得那個 code 的客戶端的英文退路，`params` 是要代入的數字 |

**警告的 code：**

| code | 什麼時候出現 |
|---|---|
| `shards_missing` | 設定了 N 片但有些號碼沒有行程在回報 —— 那些設備沒有任何人在輪詢 |
| `shards_disagree` | 各行程的 `COLLECTOR_SHARDS` 不一致，會有設備被輪詢兩次、有些完全沒人輪詢 |
| `damping_disagree` | 各片的抑制設定不一致，同一個機群裡的設備判斷標準不同 |
| `trap_unknown_sources` | 有 trap 從不是受管設備的位址送來 —— 幾乎都是 trap 來源位址的問題，見 CONFIGURATION 第 5.3 節 |
