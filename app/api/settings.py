"""
Settings API endpoints
"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.core.app_settings import (
    SNMPSettings, load_settings, save_settings
)
from app.core.discovery_scheduler import (
    get_discovery_scheduler, start_discovery_scheduler, stop_discovery_scheduler
)

logger = logging.getLogger(__name__)

router = APIRouter()

__all__ = ["router", "load_settings", "SNMPSettings"]


async def apply_discovery(settings_data: SNMPSettings) -> None:
    """Start, restart or stop the subnet sweep to match the settings"""
    if settings_data.discovery_enabled and settings_data.allowed_subnets:
        await start_discovery_scheduler(
            subnets=settings_data.allowed_subnets,
            community=settings_data.default_community,
            interval=settings_data.discovery_interval
        )
    else:
        stop_discovery_scheduler()


@router.get("", response_model=SNMPSettings)
async def get_snmp_settings():
    """Get current SNMP and discovery settings"""
    return load_settings()


@router.post("")
async def save_snmp_settings(settings_data: SNMPSettings):
    """Save SNMP and discovery settings"""
    try:
        save_settings(settings_data)
    except OSError as exc:
        # Reporting success here is how a setting gets "saved" and is gone
        # after the next restart.
        logger.error("Saving settings failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Could not write settings: {exc}")

    await apply_discovery(settings_data)
    return {"success": True, "message": "Settings saved", "settings": settings_data}


@router.get("/runtime")
async def get_runtime_settings():
    """
    What is configured in the environment, read-only.

    These are start-up settings (.env): changing them means editing that
    file and restarting, so the UI shows them rather than offering a form
    that would save nothing. The collector reports the intervals it really
    runs with on /api/v1/collector/status; this is the rest.
    """
    env = get_settings()
    exporting = bool(env.log_export_enabled)
    kind = (env.log_export_type or "").lower()
    if kind == "graylog":
        target = f"{env.graylog_protocol}://{env.graylog_host}:{env.graylog_port}"
    else:
        target = env.elasticsearch_url or ""
    return {
        "snmp": {
            "timeout": env.snmp_timeout,
            "retries": env.snmp_retries,
        },
        "log_export": {
            "enabled": exporting,
            "type": kind if exporting else None,
            "target": target if exporting else None,
        },
    }


@router.get("/discovery/status")
async def get_discovery_status():
    """Get current discovery scheduler status"""
    scheduler = get_discovery_scheduler()

    if scheduler and scheduler.running:
        return {
            "running": True,
            "subnets": scheduler.allowed_subnets,
            "interval": scheduler.interval,
            "last_run": scheduler.last_run,
        }

    return {"running": False}


@router.post("/discovery/trigger")
async def trigger_discovery():
    """Manually trigger a discovery cycle"""
    scheduler = get_discovery_scheduler()

    if scheduler and scheduler.running:
        scheduler.trigger()
        return {"success": True, "message": "Discovery cycle started"}

    return {"success": False, "message": "Discovery scheduler not running"}
