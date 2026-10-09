"""
Application configuration management
"""
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

    # Read ifHCInOctets/ifHCOutOctets so links carry a real traffic rate.
    # Turning this off keeps the real per-port speed (ifHighSpeed) and the
    # cabling inventory, and drops two table walks per device per cycle.
    collector_traffic_stats: bool = True

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
