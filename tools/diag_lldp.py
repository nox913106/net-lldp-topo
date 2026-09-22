"""
LLDP 診斷工具 / LLDP diagnostic tool

在部署主機上檢查某台設備為什麼收不到 LLDP 鄰居：

    docker compose exec collector python -m tools.diag_lldp 192.168.1.1 public
    docker compose exec collector python -m tools.diag_lldp 192.168.1.1 --v3 \
        --user snmpuser --auth-proto SHA --auth-pass xxx --priv-proto AES --priv-pass yyy

它會逐步印出：SNMP 是否通、設備有沒有實作 LLDP-MIB、
lldpLocPortTable / lldpRemTable 的原始內容，以及解析後的鄰居清單。
"""
import argparse
import asyncio
import logging
import sys

from app.core.snmp_collector import SNMPCollector, snmp_value_to_text
from app.core.snmp_oids import (
    LLDP_REM_TABLE, LLDP_REM_CHASSIS_ID, LLDP_REM_PORT_ID, LLDP_REM_SYS_NAME,
    LLDP_LOC_PORT_DESC, LLDP_LOC_PORT_ID, LLDP_REM_MAN_ADDR_IF_SUBTYPE,
    CDP_CACHE_DEVICE_ID, IF_DESCR, SYS_NAME, SYS_DESCR
)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


async def diagnose(args) -> int:
    collector = SNMPCollector(
        community=args.community,
        timeout=args.timeout,
        retries=args.retries,
        snmp_version="v3" if args.v3 else args.version,
        v3_username=args.user,
        v3_auth_protocol=args.auth_proto,
        v3_auth_password=args.auth_pass,
        v3_priv_protocol=args.priv_proto,
        v3_priv_password=args.priv_pass,
        port=args.port
    )

    section(f"1. SNMP 連線測試 {args.ip}:{args.port}")
    sys_name = await collector._snmp_get(args.ip, SYS_NAME)
    if sys_name is None:
        print("✗ 沒有 SNMP 回應。請檢查：")
        print("  - IP / UDP 161 是否可達（防火牆、路由）")
        print("  - community / SNMP 版本是否正確")
        print("  - 設備上的 SNMP ACL 是否允許本機來源 IP")
        print("  - collector 容器使用 host 網路，來源 IP 會是主機 IP")
        return 1

    sys_descr = await collector._snmp_get(args.ip, SYS_DESCR)
    print(f"✓ sysName  : {snmp_value_to_text(sys_name)}")
    print(f"  sysDescr : {snmp_value_to_text(sys_descr)}")

    section("2. LLDP-MIB 原始資料")
    tables = {
        "lldpLocPortId   (1.0.8802.1.1.2.1.3.7.1.3)": LLDP_LOC_PORT_ID,
        "lldpLocPortDesc (1.0.8802.1.1.2.1.3.7.1.4)": LLDP_LOC_PORT_DESC,
        "lldpRemChassisId(1.0.8802.1.1.2.1.4.1.1.5)": LLDP_REM_CHASSIS_ID,
        "lldpRemPortId   (1.0.8802.1.1.2.1.4.1.1.7)": LLDP_REM_PORT_ID,
        "lldpRemSysName  (1.0.8802.1.1.2.1.4.1.1.9)": LLDP_REM_SYS_NAME,
        "lldpRemManAddr  (1.0.8802.1.1.2.1.4.2.1.3)": LLDP_REM_MAN_ADDR_IF_SUBTYPE,
    }

    walked = {}
    for label, oid in tables.items():
        rows = await collector._snmp_walk(args.ip, oid)
        walked[oid] = rows
        print(f"  {label}: {len(rows)} rows")
        if args.verbose:
            for index, value in rows.items():
                print(f"      .{index} = {snmp_value_to_text(value)}")

    if not walked[LLDP_REM_CHASSIS_ID] and not walked[LLDP_REM_SYS_NAME]:
        print("\n✗ 設備沒有回報任何 LLDP 鄰居 (lldpRemTable 為空)。常見原因：")
        print("  - 設備未啟用 LLDP：Cisco 'lldp run'、HP/Aruba 'lldp run'、")
        print("    FortiGate 'config system interface -> set lldp-transmission enable'")
        print("  - 介面層級關閉了 LLDP receive/transmit")
        print("  - SNMP view 未包含 1.0.8802.1.1.2（LLDP-MIB）")
        print("  - 對端設備沒有發送 LLDP（例如只跑 CDP）")

    section("3. CDP 原始資料")
    cdp_rows = await collector._snmp_walk(args.ip, CDP_CACHE_DEVICE_ID)
    print(f"  cdpCacheDeviceId: {len(cdp_rows)} rows")
    if args.verbose:
        for index, value in cdp_rows.items():
            print(f"      .{index} = {snmp_value_to_text(value)}")

    section("4. 介面表")
    if_rows = await collector._snmp_walk(args.ip, IF_DESCR)
    print(f"  ifDescr: {len(if_rows)} rows")
    if not if_rows:
        print("  ✗ 連 ifDescr 都讀不到，代表 WALK 被 SNMP view 擋住或裝置不支援 GETBULK")

    section("5. 解析後的鄰居")
    lldp = await collector.get_lldp_neighbors(args.ip)
    cdp = await collector.get_cdp_neighbors(args.ip)

    for neighbor in lldp + cdp:
        print(
            f"  [{neighbor.protocol}] {neighbor.local_port} "
            f"(ifIndex {neighbor.local_port_index}) -> "
            f"{neighbor.remote_hostname} / {neighbor.remote_port} "
            f"chassis={neighbor.remote_chassis_id} mgmt_ip={neighbor.remote_mgmt_ip or '-'}"
        )

    print(f"\n總計: LLDP {len(lldp)} 筆, CDP {len(cdp)} 筆")
    return 0 if (lldp or cdp) else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="診斷設備的 LLDP/CDP 鄰居收集")
    parser.add_argument("ip", help="設備 IP")
    parser.add_argument("community", nargs="?", default="public", help="SNMP community (v1/v2c)")
    parser.add_argument("--version", default="v2c", choices=["v1", "v2c", "v3"])
    parser.add_argument("--v3", action="store_true", help="使用 SNMPv3")
    parser.add_argument("--user", help="SNMPv3 使用者名稱")
    parser.add_argument("--auth-proto", help="MD5 / SHA / SHA224 / SHA256 / SHA384 / SHA512")
    parser.add_argument("--auth-pass")
    parser.add_argument("--priv-proto", help="DES / 3DES / AES / AES192 / AES256")
    parser.add_argument("--priv-pass")
    parser.add_argument("--port", type=int, default=161)
    parser.add_argument("--timeout", type=int, default=5)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("-v", "--verbose", action="store_true", help="印出每一筆 OID")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(diagnose(args))


if __name__ == "__main__":
    sys.exit(main())
