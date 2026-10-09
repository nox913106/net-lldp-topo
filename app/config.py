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
    collector_concurrent: int = 20

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
    
    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # 忽略額外的環境變數


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()
