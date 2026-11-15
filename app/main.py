"""
FastAPI main application entry point
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from contextlib import asynccontextmanager
import logging
import os

from app.config import get_settings, log_level
from app.db.database import engine, init_db
from app.api import (
    alerts, collector, devices, discovery, groups, profiles, settings, snmp,
    topology
)
from app.core.discovery_scheduler import stop_discovery_scheduler
from app.api.settings import apply_discovery, load_settings as load_app_settings

# Configure logging. The level is APP_LOG_LEVEL; see app/config.py for why an
# unknown value falls back to INFO instead of failing to start.
logging.basicConfig(
    level=log_level(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

settings_config = get_settings()

# Get static files path
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events"""
    # Startup
    logger.info(f"Starting {settings_config.app_name}...")
    await init_db()
    logger.info("Database initialized")
    
    # The subnet sweep, if the saved settings ask for one
    app_settings = load_app_settings()
    await apply_discovery(app_settings)
    if app_settings.discovery_enabled and app_settings.allowed_subnets:
        logger.info("Discovery Scheduler started")

    yield
    
    # Shutdown
    stop_discovery_scheduler()
    logger.info(f"Shutting down {settings_config.app_name}...")


# Create FastAPI application
app = FastAPI(
    title=settings_config.app_name,
    description="LLDP/CDP Network Topology Monitor",
    version="1.0.0",
    lifespan=lifespan
)

# CORS: none by default.
#
# The UI is served by this same process, so it is same-origin and needs no
# CORS header at all. What was here before - allow_origins=["*"] together
# with allow_credentials=True - is a combination browsers reject outright, so
# it never worked; what it would have meant if it had is that any page in the
# operator's browser could drive this API with their logged-in session, and
# this API reads every device's SNMP community.
#
# APP_CORS_ORIGINS exists for the one real case: somebody serving the UI, or
# a dashboard of their own, from another origin. It is an explicit list.
_cors_origins = settings_config.cors_origin_list()
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# API routes
app.include_router(devices.router, prefix="/api/v1/devices", tags=["Devices"])
app.include_router(topology.router, prefix="/api/v1/topology", tags=["Topology"])
app.include_router(alerts.router, prefix="/api/v1/alerts", tags=["Alerts"])
app.include_router(profiles.router, prefix="/api/v1/profiles", tags=["Alert Profiles"])
app.include_router(groups.router, prefix="/api/v1/groups", tags=["Device Groups"])
app.include_router(discovery.router, prefix="/api/v1/discovery", tags=["Discovery"])
app.include_router(snmp.router, prefix="/api/v1/snmp", tags=["SNMP Testing"])
app.include_router(settings.router, prefix="/api/v1/settings", tags=["Settings"])
app.include_router(collector.router, prefix="/api/v1/collector", tags=["Collector"])

# Mount static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    """Serve the main web interface"""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/devices")
async def devices_page():
    """Serve the devices management page"""
    return FileResponse(os.path.join(STATIC_DIR, "devices.html"))


@app.get("/alerts")
async def alerts_page():
    """Serve the alerts dashboard page"""
    return FileResponse(os.path.join(STATIC_DIR, "alerts.html"))


@app.get("/groups")
async def groups_page():
    """Serve the groups management page"""
    return FileResponse(os.path.join(STATIC_DIR, "groups.html"))


@app.get("/settings")
async def settings_page():
    """Serve the settings page"""
    return FileResponse(os.path.join(STATIC_DIR, "settings.html"))


@app.get("/health")
async def health():
    """
    Liveness: this process is up and answering.

    Deliberately touches nothing else. A liveness probe that also checks the
    database makes an orchestrator restart a perfectly healthy web process
    because the database is busy - and restarting it does not help. What to do
    about a database that is down is a human decision, so it is reported by
    /health/ready instead.
    """
    return {"status": "healthy"}


@app.get("/health/ready")
async def ready():
    """
    Readiness: can this process actually serve a request?

    Every endpoint but this one and /health needs the database, so "up" and
    "useful" are different questions and this is the second one. It is one
    round trip (SELECT 1), which also proves the pool can hand out a
    connection rather than only that the server is listening.

    Unauthenticated, like /health, so that a load balancer and
    `mf-topomon doctor` can use it (deploy/nginx.conf). That is why the failure
    reason is logged rather than returned: an asyncpg connection error names
    the host, the port and the user it tried.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Readiness check failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "error"},
        )
    return {"status": "ready", "database": "ok"}
