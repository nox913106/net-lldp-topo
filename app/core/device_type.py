"""
A first guess at what a newly discovered device is.

Discovery used to file everything it found as "access". The overview only
draws the backbone (core, distribution, routers, firewalls), so on a new
installation - one seed switch, everything else discovered from it - the
overview showed that one switch on its own and the rest of the network
looked as if it had not been found.

The guess is from the two things known at that moment, the name and the
vendor, and it is only ever a starting point: the operator can change the
type on the Devices page and nothing here will change it back, because it is
applied when a device is created and never again.
"""
import re
from typing import Optional

# Checked in order; the first token that matches decides.
_NAME_HINTS = (
    (("fw", "firewall", "fgt"), "firewall"),
    # Before camera and ap: an NVR is named for the cameras it records and a
    # WLC for the access points it runs, and both are what a focus path from
    # one of those ends at
    (("nvr", "dvr"), "nvr"),
    (("wlc", "smartzone", "vsz", "wifictl"), "wlc"),
    (("cam", "cctv", "camera", "ipc"), "camera"),
    (("lb", "slb", "adc", "loadbalancer"), "loadbalancer"),
    (("core", "spine"), "core"),
    (("dist", "distribution", "agg", "aggregation"), "distribution"),
    (("rtr", "router", "gw"), "router"),
    (("ap", "wap"), "ap"),
    (("tor", "leaf"), "tor"),
    (("srv", "server", "esx", "esxi", "pve"), "server"),
    (("vm",), "vm"),
)

_VENDOR_TYPES = {
    "fortinet": "firewall",
    "paloalto": "firewall",
    "ruckus": "ap",
}


def guess_device_type(hostname: Optional[str], vendor: Optional[str] = None) -> str:
    """core / distribution / access / router / firewall / ap / wlc / tor /
    camera / nvr / loadbalancer / server / vm"""
    # Whole tokens only, with any trailing number dropped: "core-sw-01" and
    # "dist2" say what they are, "decorator" and "scoreboard" do not.
    tokens = [
        re.sub(r"\d+$", "", token)
        for token in re.split(r"[^a-z0-9]+", (hostname or "").lower())
    ]
    for hints, device_type in _NAME_HINTS:
        if any(token in hints for token in tokens):
            return device_type
    return _VENDOR_TYPES.get(vendor or "", "access")
