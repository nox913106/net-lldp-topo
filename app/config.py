"""
Application configuration management
"""
import logging

from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    # Application
    app_name: str = "DEV-lldp-topomon"
    app_debug: bool = False
    app_log_level: str = "INFO"
    app_secret_key: str = "change-this-to-random-string"
    
    # Database (默認使用 SQLite 方便本地測試)
    database_url: str = "sqlite+aiosqlite:///./topomon.db"
    
    # Redis
    redis_url: str = "redis://localhost:6379"
    
    # SNMP Settings
    snmp_default_community: str = "public"
    snmp_timeout: int = 5
    snmp_retries: int = 2
    
    # Collector Settings
    #
    # The poll interval is also the alert latency and the averaging window of
    # every traffic rate, which is why it is not just a load knob:
    #   * a utilisation figure is the mean over one interval, so the shortest
    #     event that can cross a 70% threshold is 0.70 * interval - at 300 s
    #     that is 210 s of line rate traffic, and shorter bursts are invisible
    #   * a cycle costs the same whatever the interval (~50 s at 500 devices),
    #     so halving the interval doubles the CPU share
    # docs/PERFORMANCE.md section 18 has the measured budget.
    collector_interval: int = 300  # 5 minutes
    # How many devices wait on SNMP at once. This is an SNMP knob only: the
    # database half of a cycle is two statements per device since the writes
    # were batched, so it barely responds to this (PERFORMANCE.md 17.4).
    # Both loops use it.
    collector_concurrent: int = 20

    # The fast loop. collector_interval above is the *inventory* interval -
    # what is cabled to what, which changes when somebody moves a cable. This
    # is the interval for the things that change by the second: whether a
    # device answers, whether each cable's port is up, and how much traffic it
    # is carrying. It reads three values per cable instead of walking the
    # interface tables, so it costs roughly an eighth of an inventory cycle
    # per run (PERFORMANCE.md section 20).
    #
    # It is also the interval the alert checks run on, so it is what decides:
    #   * how late an alert can be (about half of this, plus the exchange)
    #   * the averaging window of every bps, and therefore the shortest event
    #     that can cross a 70% threshold: 0.70 * this
    # Set to 0 to switch it off and leave everything on the inventory loop,
    # which is what the collector did before. Per-cable link state is then
    # never polled - only traps can establish it.
    collector_fast_interval: int = 60

    # Receive SNMP linkDown/linkUp notifications (udp/162) and update the
    # affected cable's state from them, in milliseconds rather than at the
    # next fast cycle. Off by default: it opens a listening port, needs the
    # privilege to bind one below 1024, and does nothing until the devices are
    # configured to send traps here. A trap is never the only source of truth -
    # see app/core/trap_receiver.py.
    collector_trap_enabled: bool = False
    collector_trap_bind: str = "0.0.0.0"
    collector_trap_port: int = 162
    # Community strings accepted on notifications, comma separated. Empty
    # means "the same as snmp_default_community".
    collector_trap_communities: str = ""

    # Split the fleet across several collector processes. A cycle is CPU
    # bound on SNMP decoding and the GIL means threads do not help, so this
    # is how the collector scales past one core: start COLLECTOR_SHARDS
    # processes, each with its own COLLECTOR_SHARD_ID, and each polls the
    # devices whose id modulo the count is its own id.
    #
    # Every shard must agree on the count, and every id from 0 to count-1
    # must be running: with COLLECTOR_SHARDS=4 and three processes started,
    # a quarter of the fleet is never polled. The process holding the
    # fleet-wide lease warns about exactly that (app/core/lease.py), but the
    # warning is a safety net, not a substitute for starting them all.
    #
    # 1 (the default) is a single collector and behaves exactly as before:
    # no shard filter on any query, and no coordination with anybody.
    # PERFORMANCE.md section 24 has the measured scaling.
    collector_shards: int = 1
    collector_shard_id: int = 0

    # Back off devices that do not answer SNMP instead of probing them every
    # cycle: after n consecutive failures the next attempt waits
    # collector_interval * 2 ** (n - 1), capped at the value below.
    collector_offline_backoff: bool = True
    collector_offline_backoff_max: int = 1800  # 30 minutes

    # -- Alert damping ----------------------------------------------------
    #
    # Every alert in this project is raised from a UDP exchange, so every one
    # of them can be raised by a lost or forged datagram rather than by a
    # fault. SNMP_RETRIES is the first line of defence (three attempts per
    # request), but all three datagrams take the same path, so a congested
    # uplink can swallow the lot and make a healthy device look dead for one
    # cycle. These three knobs are the second line. Every default below
    # reproduces the behaviour that existed before they were added.
    # app/core/damping.py has the reasoning; PERFORMANCE.md section 28 has the
    # measured latencies each one costs.

    # Consecutive unanswered polls before a device is called offline. 1 is the
    # original behaviour: the first unanswered poll writes status=offline and
    # the alert cycle raises device_offline from it. 3 with the default fast
    # interval means a device has to be silent for about 3 minutes, and a
    # momentary loss costs nothing. The price is paid only by a device that
    # really has gone: its alert is (n-1) fast intervals later.
    #
    # Both loops share the counter, so whichever asks next does the
    # confirming, and the offline backoff is told the same number so that the
    # confirming polls are not themselves backed off.
    alert_offline_confirm_cycles: int = 1

    # Flap damping for per-cable link_down alerts. A cable that changes state
    # this many times inside ALERT_LINK_FLAP_WINDOW seconds is flapping, and
    # its alert is then held open through the ups instead of being resolved
    # and re-raised once per flap. The first down is never delayed.
    # 0 (or 1) switches this off, which is the original behaviour: one alert
    # and one recovery per flap.
    alert_link_flap_transitions: int = 0
    alert_link_flap_window: int = 600  # 10 minutes

    # Whether a linkDown/linkUp trap waits for the next fast cycle to agree
    # with it before an alert is raised. False (the default) is what the trap
    # receiver has always done - the notification itself raises the alert, in
    # milliseconds, which is the whole reason to turn traps on. True keeps the
    # state write (the topology and the UI are current immediately) and lets
    # the poll decide about the alert, which costs up to one fast interval of
    # latency and is the right trade for devices whose traps cross a network
    # you do not control.
    #
    # Per device it is overridden in the alert profile's thresholds JSON:
    #   {"trap": {"requires_poll": true}}
    # It needs the fast loop: with COLLECTOR_FAST_INTERVAL=0 nothing ever
    # polls per-cable state, so there is nothing to corroborate a trap with
    # and the collector logs a warning and keeps alerting immediately.
    alert_trap_requires_poll: bool = False

    # Read ifHCInOctets/ifHCOutOctets so links carry a real traffic rate.
    # Turning this off keeps the real per-port speed (ifHighSpeed) and the
    # cabling inventory, and drops two table walks per device per cycle.
    collector_traffic_stats: bool = True

    # Walk each device's own IP address table during the inventory poll and
    # remember what it holds, so a trap can be matched to the device that
    # sent it even when it left by an interface other than the one the
    # device is polled on - which is most platforms' default, and otherwise
    # means an `snmp-server trap-source` line on every device in an existing
    # network (docs/CONFIGURATION.md section 5.3).
    #
    # Deliberately NOT tied to COLLECTOR_TRAP_ENABLED. Only one process per
    # host can hold udp/162, so in a sharded deployment the shard with the
    # receiver needs the addresses of devices the *other* shards poll, and
    # those processes have traps switched off.
    #
    # Measured cost: see docs/PERFORMANCE.md section 37.
    collector_device_addresses: bool = True

    # How stale a device's address list is allowed to get. The walk above
    # costs a measured 5% of a device's poll, and addressing is a slow
    # variable: an interface's IP changes when someone changes it, not every
    # five minutes. So each device is walked at most this often rather than
    # every cycle, which divides that cost by however many cycles fit in
    # here (docs/PERFORMANCE.md section 38).
    #
    # In seconds rather than cycles, so that raising COLLECTOR_INTERVAL
    # cannot quietly make the addresses staler than this. A device polled
    # for the first time by this process is always walked, so the staleness
    # is on *changes*, not on new devices: an address that moves can keep
    # matching its old device for up to this long - which the trap receiver
    # still checks against that device's cables before applying anything.
    #
    # 0 (or less) restores the old behaviour, a walk on every cycle.
    collector_address_interval: int = 3600  # 1 hour

    # How long a neighbour that could not be turned into a device (no
    # management address, no DNS record, no SNMP answer) is left alone before
    # it is probed again.
    collector_discovery_cache_ttl: int = 3600  # 1 hour
    
    # Discovery Settings
    discovery_enabled: bool = True
    discovery_interval: int = 3600  # 1 hour
    allowed_subnets: str = ""  # Comma-separated CIDR list
    
    # Log Export (Optional)
    log_export_enabled: bool = False
    log_export_type: str = "elasticsearch"
    elasticsearch_url: Optional[str] = None
    elasticsearch_index_prefix: str = "topomon-"
    
    # Graylog Settings (Optional)
    graylog_host: str = "localhost"
    graylog_port: int = 12201
    graylog_protocol: str = "udp"
    
    def trap_community_list(self) -> list:
        """Communities accepted on notifications, defaulted and de-duplicated"""
        raw = self.collector_trap_communities or self.snmp_default_community
        seen = []
        for community in raw.split(","):
            community = community.strip()
            if community and community not in seen:
                seen.append(community)
        return seen


    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # 忽略額外的環境變數


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()


def log_level() -> int:
    """
    APP_LOG_LEVEL as a logging level.

    It had been in .env.example and the docs from the start while both entry
    points hardcoded INFO, so DEBUG - the level the "why was this neighbour
    not discovered" lines are logged at - could not actually be turned on.

    getLevelName() answers with an int for a known name and with a string
    ("Level banana") for anything else, which is the whole validation: a
    typo, or a value that happens to name something else in the logging
    module, falls back to INFO instead of failing the process over a log
    setting.
    """
    level = logging.getLevelName(str(get_settings().app_log_level).strip().upper())
    return level if isinstance(level, int) else logging.INFO
