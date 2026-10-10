"""
Sharding, as pure logic.

The failure to protect against is a silent one: with COLLECTOR_SHARDS=4 and
three processes running, a quarter of the fleet is never polled and the only
symptom is those devices slowly alerting as offline. So a count or id that
cannot describe a slice has to raise at startup, and every device id must
belong to exactly one shard.
"""
import pytest

from app.core.sharding import Shard, ShardConfigError


class Settings:
    def __init__(self, count, index):
        self.collector_shards = count
        self.collector_shard_id = index


def test_unsharded_owns_everything_and_filters_nothing():
    shard = Shard()
    assert not shard.enabled
    assert all(shard.owns(i) for i in range(100))
    assert shard.criterion(object()) is None
    assert shard.label() == "single process"


@pytest.mark.parametrize("count", [2, 3, 4, 8])
def test_every_device_belongs_to_exactly_one_shard(count):
    shards = [Shard(count, i) for i in range(count)]
    for device_id in range(500):
        owners = [s for s in shards if s.owns(device_id)]
        assert len(owners) == 1, f"device {device_id} has {len(owners)} owners"


def test_a_sharded_process_does_not_own_an_unsaved_device():
    """None means "no id yet", which belongs to nobody rather than everybody."""
    assert Shard(2, 0).owns(None) is False
    assert Shard().owns(None) is True


@pytest.mark.parametrize("count,index", [(0, 0), (-1, 0), (2, 2), (2, -1), (4, 4)])
def test_impossible_slices_raise(count, index):
    with pytest.raises(ShardConfigError):
        Shard(count, index)


def test_from_settings_reads_the_environment():
    shard = Shard.from_settings(Settings(4, 2))
    assert (shard.count, shard.index) == (4, 2)
    assert shard.name == "shard-2-of-4"
    assert shard.label() == "shard 2 of 4"


def test_from_settings_raises_rather_than_clamping():
    with pytest.raises(ShardConfigError):
        Shard.from_settings(Settings(4, 9))


def test_from_settings_defaults_to_unsharded():
    class Empty:
        pass

    assert not Shard.from_settings(Empty()).enabled


def test_shards_compare_by_identity_not_by_object():
    assert Shard(4, 1) == Shard(4, 1)
    assert Shard(4, 1) != Shard(4, 2)
    assert Shard(4, 1) != Shard(2, 1)
    assert len({Shard(4, 1), Shard(4, 1), Shard(4, 2)}) == 2
