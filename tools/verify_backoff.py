"""
離線設備退避邏輯的自我檢查

退避排程是純邏輯（時間來源可注入），所以不需要任何設備或資料庫就能驗證。

用法:
    python -m tools.verify_backoff
"""
import sys

from app.core.poll_scheduler import OfflineBackoff

FAILURES = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def test_schedule() -> None:
    print("\n1. 退避間隔")
    b = OfflineBackoff(base_interval=300, max_interval=1800, clock=lambda: 0.0)

    actual = [b.delay_for(n) for n in (1, 2, 3, 4, 5, 20)]
    check("300 -> 600 -> 1200 -> 1800 -> 上限",
          actual == [300.0, 600.0, 1200.0, 1800.0, 1800.0, 1800.0], str(actual))
    check("第一次失敗不延遲（等於正常週期）", b.delay_for(1) == 300.0)
    check("失敗次數極大時不會溢出成天文數字",
          b.delay_for(10 ** 6) == 1800.0, str(b.delay_for(10 ** 6)))
    check("max_interval 小於 base_interval 時以 base 為準",
          OfflineBackoff(base_interval=300, max_interval=10).max_interval == 300)


def test_timing() -> None:
    print("\n2. 什麼時候可以再探測")
    now = {"t": 1000.0}
    b = OfflineBackoff(base_interval=300, max_interval=1800, clock=lambda: now["t"])

    check("沒紀錄的設備可以探測", b.should_poll("sw1"))

    b.record_failure("sw1")
    check("剛失敗後不探測", not b.should_poll("sw1"))

    now["t"] += 299
    check("299 秒後還不到（間隔 300）", not b.should_poll("sw1"))
    now["t"] += 1
    check("剛好 300 秒就可以探測", b.should_poll("sw1"))
    check("剩餘等待時間歸零", b.seconds_until_retry("sw1") == 0.0)

    check("第二次失敗計數為 2", b.record_failure("sw1") == 2)
    now["t"] += 599
    check("600 秒的退避走了 599 秒還不行", not b.should_poll("sw1"))
    check("剩餘等待時間為 1 秒", b.seconds_until_retry("sw1") == 1.0,
          str(b.seconds_until_retry("sw1")))
    now["t"] += 1
    check("600 秒後可以探測", b.should_poll("sw1"))

    b.record_success("sw1")
    check("成功後清掉狀態", b.failure_count("sw1") == 0 and b.tracked() == 0)
    check("清掉後立刻可以探測", b.should_poll("sw1"))


def test_housekeeping() -> None:
    print("\n3. 狀態清理（長時間執行不累積）")
    b = OfflineBackoff(base_interval=300, max_interval=1800, clock=lambda: 0.0)
    for host in ("sw1", "sw2", "sw3"):
        b.record_failure(host)
    check("追蹤 3 台", b.tracked() == 3)
    check("pending 回報 3 台", len(b.pending()) == 3)
    check("prune 清掉已刪除的設備", b.prune(["sw1", "sw2"]) == 1)
    check("只剩 2 台", b.tracked() == 2)
    check("prune 沒有要清的回傳 0", b.prune(["sw1", "sw2"]) == 0)
    check("全部清空", b.prune([]) == 2 and b.tracked() == 0)


def test_disabled() -> None:
    print("\n4. 關閉退避")
    b = OfflineBackoff(base_interval=300, max_interval=1800, enabled=False,
                       clock=lambda: 0.0)
    b.record_failure("sw1")
    b.record_failure("sw1")
    check("關閉時永遠可以探測", b.should_poll("sw1"))
    check("關閉時剩餘等待為 0", b.seconds_until_retry("sw1") == 0.0)
    check("關閉時仍然記錄失敗次數（供 log 用）", b.failure_count("sw1") == 2)


def main() -> int:
    test_schedule()
    test_timing()
    test_housekeeping()
    test_disabled()

    if FAILURES:
        print(f"\n✗ {len(FAILURES)} 項失敗: {FAILURES}")
        return 1
    print("\n✓ 全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
