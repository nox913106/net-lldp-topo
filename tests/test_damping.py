"""
Alert damping, as pure logic - no network, no database.

Every default has to reproduce the behaviour from before damping existed,
because an upgrade must not quietly change when someone gets paged.
"""
import pytest

from app.core.damping import FlapDamper, OfflineConfirm, TRAP_KEY, trap_requires_poll
from app.core.poll_scheduler import OfflineBackoff


class Clock:
    """A monotonic clock a test can move."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# -- OfflineConfirm --------------------------------------------------------

def test_default_confirms_on_the_first_failure():
    """cycles=1 is the old behaviour: one unanswered poll is offline."""
    confirm = OfflineConfirm()
    assert not confirm.enabled
    assert confirm.record_failure(1).confirmed


def test_three_cycles_needs_three_consecutive_failures():
    confirm = OfflineConfirm(cycles=3)
    assert confirm.enabled
    assert [confirm.record_failure(1).confirmed for _ in range(3)] == [False, False, True]


def test_one_success_resets_the_count():
    """Four failures with a success between them must never confirm."""
    confirm = OfflineConfirm(cycles=3)
    confirm.record_failure(1)
    confirm.record_failure(1)
    confirm.record_success(1)
    assert [confirm.record_failure(1).confirmed for _ in range(2)] == [False, False]
    assert confirm.failures(1) == 2


def test_devices_are_counted_separately():
    confirm = OfflineConfirm(cycles=2)
    confirm.record_failure(1)
    assert not confirm.record_failure(2).confirmed
    assert confirm.record_failure(1).confirmed


def test_verdict_describes_itself_for_the_log():
    verdict = OfflineConfirm(cycles=3).record_failure(1)
    assert verdict.describe() == "1/3 consecutive failures"


def test_prune_forgets_devices_this_process_no_longer_owns():
    confirm = OfflineConfirm(cycles=3)
    confirm.record_failure(1)
    confirm.record_failure(2)
    confirm.prune({2})
    assert confirm.failures(1) == 0
    assert confirm.failures(2) == 1


def test_zero_or_negative_cycles_means_one():
    """A misconfigured value must not disable offline alerting altogether."""
    for value in (0, -5):
        assert OfflineConfirm(cycles=value).record_failure(1).confirmed


def test_pending_devices_report_their_progress():
    """
    What the API shows an operator: which devices are mid-confirmation and
    how far, so damping is distinguishable from a collector that stopped
    noticing.
    """
    confirm = OfflineConfirm(cycles=3)
    confirm.record_failure(7)
    confirm.record_failure(7)
    confirm.record_failure(9)
    assert confirm.pending_devices() == [(7, 2), (9, 1)]
    assert confirm.pending() == 2


def test_a_confirmed_device_is_no_longer_pending():
    """Once it has alerted it is not "waiting" any more, it is offline."""
    confirm = OfflineConfirm(cycles=2)
    confirm.record_failure(7)
    assert confirm.pending_devices() == [(7, 1)]
    confirm.record_failure(7)
    assert confirm.pending_devices() == []
    assert confirm.pending() == 0


def test_the_pending_list_is_bounded():
    """A site outage puts every device behind the failed uplink in here."""
    confirm = OfflineConfirm(cycles=3)
    for device_id in range(100):
        confirm.record_failure(device_id)
    assert len(confirm.pending_devices(limit=10)) == 10
    assert confirm.pending() == 100


def test_flapping_links_report_their_change_count():
    damper = FlapDamper(transitions=2, window_seconds=600)
    damper.observe(1, down=True)
    damper.observe(1, down=False)
    damper.observe(1, down=True)
    damper.observe(2, down=True)
    damper.observe(2, down=False)
    assert damper.flapping_links() == [(1, 2)]
    assert damper.flapping_count() == 1


def test_nothing_is_flapping_when_damping_is_off():
    damper = FlapDamper()
    damper.observe(1, down=True)
    damper.observe(1, down=False)
    assert damper.flapping_links() == []
    assert damper.flapping_count() == 0


# -- the backoff has to know about the confirmation window ----------------

def test_backoff_keeps_full_cadence_while_confirming():
    """
    Otherwise "three consecutive failures" on a 60s loop takes 60+120+240 =
    7 minutes instead of 3, and the window becomes a function of the backoff
    curve rather than the number that was configured.
    """
    backoff = OfflineBackoff(60, 3600, confirm_after=3)
    assert [backoff.delay_for(n) for n in range(1, 7)] == [60, 60, 60, 120, 240, 480]


def test_backoff_unchanged_when_confirmation_is_off():
    backoff = OfflineBackoff(60, 3600, confirm_after=1)
    assert [backoff.delay_for(n) for n in range(1, 6)] == [60, 120, 240, 480, 960]


# -- FlapDamper ------------------------------------------------------------

def test_damping_is_off_by_default():
    damper = FlapDamper()
    assert not damper.enabled
    for _ in range(10):
        damper.observe(1, down=True)
        damper.observe(1, down=False)
    assert not damper.flapping(1)


def test_the_first_sighting_is_a_baseline_not_a_change():
    """
    Nothing is known about a cable until it has been seen twice, so the first
    observation can never count towards flapping - otherwise every cable in a
    freshly started process would begin one change in.
    """
    damper = FlapDamper(transitions=3, window_seconds=600)
    assert damper.observe(1, down=True) is False
    assert damper.count(1) == 0


def test_only_changes_count_not_repeats():
    """Ten cycles of seeing the same down port is one event, not ten."""
    damper = FlapDamper(transitions=3, window_seconds=600)
    damper.observe(1, down=True)                      # baseline
    for _ in range(9):
        assert damper.observe(1, down=True) is False
    assert damper.count(1) == 0
    assert damper.observe(1, down=False) is True      # change 1
    assert damper.count(1) == 1
    assert not damper.flapping(1)


def test_flapping_once_the_threshold_is_reached():
    damper = FlapDamper(transitions=3, window_seconds=600)
    damper.observe(1, down=True)       # baseline
    damper.observe(1, down=False)      # change 1
    damper.observe(1, down=True)       # change 2
    assert not damper.flapping(1)
    damper.observe(1, down=False)      # change 3
    assert damper.flapping(1)
    assert damper.count(1) == 3


def test_changes_outside_the_window_are_forgotten():
    clock = Clock()
    damper = FlapDamper(transitions=2, window_seconds=600, clock=clock)
    damper.observe(1, down=True)       # baseline
    damper.observe(1, down=False)      # change 1
    clock.advance(601)
    damper.observe(1, down=True)       # change 2, but change 1 has aged out
    assert damper.count(1) == 1
    assert not damper.flapping(1)


def test_cables_are_tracked_separately():
    """One flapping fibre must not make the next port on the same switch
    look like it is flapping too."""
    damper = FlapDamper(transitions=2, window_seconds=600)
    damper.observe(1, down=True)
    damper.observe(2, down=True)
    damper.observe(1, down=False)      # cable 1, change 1
    damper.observe(1, down=True)       # cable 1, change 2
    assert damper.flapping(1)
    assert not damper.flapping(2)
    assert damper.count(2) == 0


def test_prune_keeps_the_cable_that_is_still_down():
    """
    A cable that is down and quiet is still alerting, so its history has to
    survive; one that came back up and went quiet can be dropped.
    """
    clock = Clock()
    damper = FlapDamper(transitions=2, window_seconds=600, clock=clock)
    damper.observe(1, down=True)
    damper.observe(1, down=False)
    damper.observe(1, down=True)       # cable 1: down, with two changes
    damper.observe(2, down=True)
    damper.observe(2, down=False)      # cable 2: back up, one change
    clock.advance(601)
    damper.prune()

    # Both windows have emptied, so neither counts as flapping any more -
    # but cable 1 is still down, so the damper must still know its state:
    # seeing it come up is a change. Cable 2 was dropped, so its next
    # sighting starts a fresh baseline instead.
    assert damper.count(1) == 0
    assert damper.observe(1, down=False) is True
    assert damper.observe(2, down=True) is False


def test_a_single_transition_threshold_is_treated_as_off():
    """"Flapping" needs at least a down and an up; 1 would damp the first down."""
    assert not FlapDamper(transitions=1).enabled


# -- per-profile trap policy ----------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False),
    ("1", True), ("true", True), ("yes", True), ("on", True), ("TRUE", True),
    ("0", False), ("false", False), ("", False), ("banana", False),
])
def test_trap_requires_poll_reads_the_profile(value, expected):
    assert trap_requires_poll({TRAP_KEY: {"requires_poll": value}}, False) is expected


def test_trap_requires_poll_falls_back_to_the_global_default():
    for default in (True, False):
        assert trap_requires_poll(None, default) is default
        assert trap_requires_poll({}, default) is default
        assert trap_requires_poll({"cpu": {"warning": 80}}, default) is default
        assert trap_requires_poll({TRAP_KEY: {}}, default) is default
