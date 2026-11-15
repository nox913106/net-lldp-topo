"""
The settings an operator changes from the web UI.

They live in a file rather than in the environment because they are edited
at run time, and in a file rather than in the database because two processes
read them - the API and the collector - and the collector must be able to
honour the subnet restriction even on a cycle where the database is slow.

Only things the running system really acts on belong here. The poll
interval, the SNMP timeout and the retry count used to be on the same form
and were saved nowhere and read by nothing: the collector takes those from
the environment at start-up (app/config.py). A field that can be edited and
does nothing is worse than no field, so they are gone from here and shown
read-only in the UI instead.
"""
import ipaddress
import json
import logging
import os
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

from app.config import get_settings

logger = logging.getLogger(__name__)

SETTINGS_FILE = os.path.join(
    get_settings().app_data_dir
    or os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "settings.json"
)


class SNMPSettings(BaseModel):
    # Used by the subnet sweep, and offered as the default on the forms.
    default_community: str = "public"

    # Where this system is allowed to send SNMP. With the restriction on,
    # the SNMP test tools, manual discovery, the subnet sweep and the
    # collector's own neighbour discovery all refuse an address outside
    # these networks - which is what stops a community string from being
    # offered to whatever an uplink happens to advertise over LLDP.
    allowed_subnets: List[str] = []
    enable_subnet_restriction: bool = False

    # The periodic sweep of allowed_subnets. Off unless asked for: filling
    # in the subnets to *restrict* discovery should not start probing every
    # address in them.
    discovery_enabled: bool = False
    discovery_interval: int = Field(default=3600, ge=300, le=86400)

    @field_validator("allowed_subnets")
    @classmethod
    def _networks(cls, value: List[str]) -> List[str]:
        cleaned = []
        for entry in value:
            entry = (entry or "").strip()
            if not entry:
                continue
            try:
                network = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                raise ValueError(f"'{entry}' is not a network in CIDR form")
            if str(network) not in cleaned:
                cleaned.append(str(network))
        return cleaned

    @field_validator("default_community")
    @classmethod
    def _community(cls, value: str) -> str:
        value = (value or "").strip()
        return value or "public"


def load_settings() -> SNMPSettings:
    """The saved settings, or the defaults when there are none (or they are unreadable)"""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            return SNMPSettings(**json.load(handle))
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.error("Could not read %s (%s) - using defaults", SETTINGS_FILE, exc)
    return SNMPSettings()


def save_settings(settings_data: SNMPSettings) -> None:
    """
    Write the settings, or raise.

    Written to a temporary file and renamed, so the collector can never read
    half a file; and 0600, because the community string is in it.
    """
    temporary = SETTINGS_FILE + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(settings_data.model_dump(), handle, indent=2)
    os.replace(temporary, SETTINGS_FILE)


# (mtime, settings) - the collector asks once per neighbour per cycle
_cached: Tuple[Optional[float], Optional[SNMPSettings]] = (None, None)


def current_settings() -> SNMPSettings:
    """load_settings(), re-read only when the file has changed"""
    global _cached
    try:
        mtime: Optional[float] = os.stat(SETTINGS_FILE).st_mtime
    except OSError:
        mtime = None
    if _cached[1] is None or _cached[0] != mtime:
        _cached = (mtime, load_settings())
    return _cached[1]  # type: ignore[return-value]


def address_allowed(address: str, settings_data: Optional[SNMPSettings] = None) -> bool:
    """
    Whether SNMP may be sent to this address.

    Always True while the restriction is off or no network is listed - an
    empty list means "not configured", not "nothing is allowed". Something
    that is not an IP address at all is refused once the restriction is on.
    """
    settings_data = settings_data or current_settings()
    if not settings_data.enable_subnet_restriction or not settings_data.allowed_subnets:
        return True
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return False
    return any(
        ip in ipaddress.ip_network(network, strict=False)
        for network in settings_data.allowed_subnets
    )


def refusal(address: str) -> str:
    """The sentence shown when address_allowed() said no"""
    return (
        f"{address} is outside the allowed subnets "
        f"({', '.join(current_settings().allowed_subnets)}). "
        "Change them under Settings > SNMP, or switch the restriction off."
    )
