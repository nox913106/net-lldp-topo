"""
The pages and the API have to be talking about the same API.

Every bug this file guards was found by running the system the way it is
deployed and clicking on things, after the test suite had been green for
weeks. None of them raised. Each was the UI and the server disagreeing, and
each looked fine from either side on its own:

  * the settings page called POST /api/v1/snmp/test; the route was mounted
    at /api/v1/snmp/snmp/test. A 404, shown as "failed: undefined".
  * the Edit button on the Groups page sent a PUT that did not exist. A 405,
    and the dialog closed as if it had saved.
  * the device panel asked for /alerts?device_id=7. There was no such
    parameter, unknown parameters are ignored, and every device's panel
    listed the whole fleet's alerts.
  * search, the vendor filter and sorting on the Devices page worked on the
    twenty rows being shown rather than on the fleet.
  * the SNMP settings were saved to the browser's localStorage and read by
    nothing.

The first test is the general one: it reads the pages, collects every API
call they make, and checks each against the routes that exist. The rest pin
the behaviour those calls rely on, on whichever database the suite is
running against.
"""
import asyncio
import json
import pathlib
import re

import pytest
from tests.conftest import authed_client
from sqlalchemy import delete, func, select

import app.core.app_settings as app_settings
import app.main as main_module
from app.core.clock import utcnow
from app.core.device_type import guess_device_type
from app.db.database import async_session_maker, init_db
from app.models.alert import Alert, AlertProfile
from app.models.device import Device
from app.models.group import DeviceGroup, DeviceGroupMember
from app.models.link import MergedLink, RawLink

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"


def run(coro):
    return asyncio.run(coro)


# -- every call the pages make ----------------------------------------------


def _calls_in(text):
    """(METHOD, /api/v1/path/{}) for each fetch()/apiRequest() in a page"""
    calls = []
    pattern = re.compile(
        r"(?:fetch|apiRequest)\(\s*`\$\{(?:API_BASE|this\.apiBase)\}([^`]*)`"
    )
    for match in pattern.finditer(text):
        path = match.group(1).split("?")[0]
        path = re.sub(r"\$\{[^}]*\}", "{}", path)
        # The options follow the URL: {method: 'PUT'} or apiJson('PUT', ...).
        # Only as far as the end of the statement, so the next call's method
        # is not picked up for this one.
        tail = text[match.end():match.end() + 400].split(";")[0]
        method = re.search(r"method:\s*'([A-Z]+)'|apiJson\('([A-Z]+)'", tail)
        if method:
            calls.append(((method.group(1) or method.group(2)), "/api/v1" + path))
        elif re.search(r"\bmethod\b", tail):
            # `method` is a variable: the page picks PUT or POST at run time
            for verb in re.findall(r"'(PUT|POST)'", text[max(0, match.start() - 600):match.end() + 400]):
                calls.append((verb, "/api/v1" + path))
        else:
            calls.append(("GET", "/api/v1" + path))
    return calls


def _routes():
    routes = set()
    for path, item in main_module.app.openapi()["paths"].items():
        for method in item:
            routes.add((method.upper(), re.sub(r"\{[^}]*\}", "{}", path)))
    return routes


def test_every_api_call_a_page_makes_has_a_route():
    routes = _routes()
    made = []
    for page in sorted(STATIC.rglob("*")):
        if page.suffix in (".html", ".js") and "vendor" not in page.parts:
            for call in _calls_in(page.read_text(encoding="utf-8")):
                made.append((page.name, call))

    # If this finds nothing the regex has stopped matching how the pages
    # call the API, and the test would pass having checked nothing.
    assert len(made) >= 25, "found only %d API calls in static/" % len(made)

    missing = sorted({(name, call) for name, call in made if call not in routes})
    assert missing == [], (
        "these pages call an endpoint that does not exist:\n  "
        + "\n  ".join("%s: %s %s" % (name, method, path) for name, (method, path) in missing)
    )


def test_the_calls_that_were_broken_are_among_the_ones_checked():
    """So that the test above is known to cover the cases it was written for"""
    made = set()
    for page in STATIC.rglob("*.html"):
        made.update(_calls_in(page.read_text(encoding="utf-8")))
    assert ("POST", "/api/v1/snmp/test") in made
    assert ("PUT", "/api/v1/groups/{}") in made
    assert ("POST", "/api/v1/settings") in made


def test_no_page_reports_success_without_asking_the_server():
    """
    Two buttons used to answer "saved (demo)" and "connection test: success
    (demo)" from a function that called nothing.
    """
    for page in STATIC.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        assert "(demo)" not in text, "%s still has a demo stub" % page.name
        assert "snmp_allowed_subnets" not in text, (
            "%s keeps the SNMP settings in localStorage again" % page.name
        )


# -- behaviour the calls rely on ---------------------------------------------


async def _clean():
    await init_db()
    async with async_session_maker() as db:
        for model in (Alert, DeviceGroupMember, DeviceGroup, MergedLink,
                      RawLink, Device, AlertProfile):
            await db.execute(delete(model))
        await db.commit()


async def _client():
    return authed_client(main_module.app)


async def _device(db, name, ip, **kwargs):
    device = Device(hostname=name, ip_address=ip, snmp_community="public",
                    status="managed", **kwargs)
    db.add(device)
    await db.flush()
    return device


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Each test gets its own settings.json, never the repository's"""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_FILE", str(path))
    monkeypatch.setattr(app_settings, "_cached", (None, None))
    return path


def test_the_device_list_filters_and_sorts_the_fleet_not_the_page():
    async def check():
        await _clean()
        async with async_session_maker() as db:
            for n in range(30):
                await _device(db, "access-%02d" % n, "10.0.0.%d" % n,
                              vendor="cisco_ios", cpu_percent=float(n))
            await _device(db, "fw-edge", "10.9.0.1", vendor="fortinet", cpu_percent=None)
            await db.commit()
        async with await _client() as client:
            # Page one is 20 rows; the firewall is not on it
            found = (await client.get("/api/v1/devices?search=fw-&limit=20")).json()
            vendor = (await client.get("/api/v1/devices?vendor=fortinet&limit=20")).json()
            busiest = (await client.get(
                "/api/v1/devices?sort_by=cpu_percent&sort_order=desc&limit=3")).json()
            bogus = await client.get("/api/v1/devices?sort_by=raw_links")
        return found, vendor, busiest, bogus

    found, vendor, busiest, bogus = run(check())
    assert [d["hostname"] for d in found["devices"]] == ["fw-edge"]
    assert vendor["total"] == 1
    # Highest first, and the device with no reading is not "the highest"
    assert [d["cpu_percent"] for d in busiest["devices"]] == [29.0, 28.0, 27.0]
    assert bogus.status_code == 200


def test_a_device_without_a_community_does_not_break_the_list():
    """The column is nullable; one such row used to make the whole list a 500"""
    async def check():
        await _clean()
        async with async_session_maker() as db:
            db.add(Device(hostname="v3-only", ip_address="10.0.0.1",
                          snmp_community=None, status="managed"))
            await db.commit()
        async with await _client() as client:
            return await client.get("/api/v1/devices")

    response = run(check())
    assert response.status_code == 200, response.text
    assert response.json()["devices"][0]["snmp_community"] is None


def test_the_auto_discover_checkbox_is_stored_both_ways():
    async def check():
        await _clean()
        async with await _client() as client:
            off = await client.post("/api/v1/devices", json={
                "hostname": "seed-off", "ip_address": "10.0.0.1",
                "snmp_community": "public", "auto_discover": False})
            default = await client.post("/api/v1/devices", json={
                "hostname": "seed-default", "ip_address": "10.0.0.2",
                "snmp_community": "public"})
            flipped = await client.put(
                "/api/v1/devices/%d" % off.json()["id"], json={"auto_discover": True})
        return off.json(), default.json(), flipped.json()

    off, default, flipped = run(check())
    assert off["auto_discover"] is False
    # A seed whose neighbours are not followed discovers nothing
    assert default["auto_discover"] is True
    assert flipped["auto_discover"] is True


def test_renaming_a_device_onto_another_is_refused_not_a_500():
    async def check():
        await _clean()
        async with async_session_maker() as db:
            await _device(db, "a", "10.0.0.1")
            b = await _device(db, "b", "10.0.0.2")
            await db.commit()
            b_id = b.id
        async with await _client() as client:
            return await client.put("/api/v1/devices/%d" % b_id, json={"hostname": "a"})

    response = run(check())
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]


def test_deleting_a_device_that_discovery_went_through():
    """
    Every auto-discovered device points at the one it was found through, and
    that foreign key has no ON DELETE - so on PostgreSQL the seed switch
    could never be deleted.
    """
    async def check():
        await _clean()
        async with async_session_maker() as db:
            seed = await _device(db, "seed", "10.0.0.1")
            child = await _device(db, "child", "10.0.0.2", parent_device_id=seed.id)
            db.add(MergedLink(device_a_id=seed.id, device_b_id=child.id,
                              total_bandwidth_mbps=1000))
            await db.commit()
            seed_id, child_id = seed.id, child.id
        async with await _client() as client:
            deleted = await client.delete("/api/v1/devices/%d" % seed_id)
            survivor = await client.get("/api/v1/devices/%d" % child_id)
        async with async_session_maker() as db:
            links = (await db.execute(select(func.count(MergedLink.id)))).scalar()
        return deleted, survivor, links

    deleted, survivor, links = run(check())
    assert deleted.status_code == 204, deleted.text
    assert survivor.status_code == 200
    assert survivor.json()["parent_device_id"] is None
    assert links == 0, "a link to the deleted device is still on the map"


def test_a_device_panel_gets_that_devices_alerts_only():
    async def check():
        await _clean()
        async with async_session_maker() as db:
            a = await _device(db, "a", "10.0.0.1")
            b = await _device(db, "b", "10.0.0.2")
            db.add(Alert(device_id=a.id, alert_type="cpu_high", severity="warning",
                         message="a is busy", is_active=True))
            db.add(Alert(device_id=b.id, alert_type="device_offline", severity="critical",
                         message="b is gone", is_active=True))
            await db.commit()
            a_id = a.id
        async with await _client() as client:
            mine = (await client.get(
                "/api/v1/alerts?device_id=%d&is_active=true" % a_id)).json()
            everyone = (await client.get("/api/v1/alerts/active")).json()
        return mine, everyone

    mine, everyone = run(check())
    assert [a["message"] for a in mine["alerts"]] == ["a is busy"]
    assert mine["total"] == 1
    assert everyone["total"] == 2
    # What the acknowledge dialog shows: the device, not just its id
    assert mine["alerts"][0]["hostname"] == "a"
    assert mine["alerts"][0]["ip_address"] == "10.0.0.1"


def test_a_group_can_be_edited_and_cannot_contain_itself():
    async def check():
        await _clean()
        async with await _client() as client:
            parent = (await client.post("/api/v1/groups", json={"name": "Floor 1"})).json()
            child = (await client.post(
                "/api/v1/groups", json={"name": "IDF", "parent_id": parent["id"]})).json()
            renamed = await client.put(
                "/api/v1/groups/%d" % parent["id"], json={"name": "一樓"})
            loop = await client.put(
                "/api/v1/groups/%d" % parent["id"], json={"parent_id": child["id"]})
            gone = await client.delete("/api/v1/groups/%d" % parent["id"])
            left = (await client.get("/api/v1/groups")).json()
        return renamed, loop, gone, left, child

    renamed, loop, gone, left, child = run(check())
    assert renamed.status_code == 200 and renamed.json()["name"] == "一樓"
    assert loop.status_code == 400
    assert gone.status_code == 204, gone.text
    # The sub-group is not deleted with its parent, and is not orphaned
    assert [(g["id"], g["parent_id"]) for g in left] == [(child["id"], None)]


def test_devices_can_get_into_a_group_from_the_unassigned_list():
    """
    The only way into a group is from the list of devices that are in none,
    and that list used to be a sentence telling the operator to look
    elsewhere - so groups could be created and never filled.
    """
    async def check():
        await _clean()
        async with async_session_maker() as db:
            ids = [(await _device(db, "sw-%d" % n, "10.0.0.%d" % n)).id for n in range(4)]
            await db.commit()
        async with await _client() as client:
            group = (await client.post("/api/v1/groups", json={"name": "g"})).json()
            before = (await client.get("/api/v1/devices?ungrouped=true&limit=0")).json()
            assigned = await client.post(
                "/api/v1/groups/%d/devices" % group["id"],
                json={"device_ids": ids[:2] + [10 ** 8]})
            after = (await client.get("/api/v1/devices?ungrouped=true&limit=0")).json()
            members = (await client.get("/api/v1/groups/%d/devices" % group["id"])).json()
            listed = (await client.get("/api/v1/groups")).json()
        return before, assigned, after, members, listed

    before, assigned, after, members, listed = run(check())
    assert before["total"] == 4
    # The id that is not a device is skipped; it does not fail the other two
    assert assigned.status_code == 200 and assigned.json()["added"] == 2
    assert after["total"] == 2
    assert members["total"] == 2
    assert all(m["status"] == "managed" for m in members["devices"])
    assert listed[0]["device_count"] == 2


def test_profile_names_and_profiles_in_use():
    async def check():
        await _clean()
        async with await _client() as client:
            first = await client.post("/api/v1/profiles", json={"name": "strict", "thresholds": {}})
            again = await client.post("/api/v1/profiles", json={"name": "strict", "thresholds": {}})
            device = (await client.post("/api/v1/devices", json={
                "hostname": "d", "ip_address": "10.0.0.1", "snmp_community": "public",
                "alert_profile_id": first.json()["id"]})).json()
            unknown = await client.put(
                "/api/v1/devices/%d" % device["id"], json={"alert_profile_id": 10 ** 8})
            removed = await client.delete("/api/v1/profiles/%d" % first.json()["id"])
            after = (await client.get("/api/v1/devices/%d" % device["id"])).json()
        return again, unknown, removed, after

    again, unknown, removed, after = run(check())
    assert again.status_code == 400
    # Refused by the API itself: SQLite would have stored the dangling id
    assert unknown.status_code == 400
    assert removed.status_code == 204, removed.text
    assert after["alert_profile_id"] is None


def test_the_overview_is_the_whole_map_until_there_is_a_backbone():
    """One seed switch typed "core" and everything else discovered from it"""
    async def check():
        await _clean()
        async with async_session_maker() as db:
            await _device(db, "core-1", "10.0.0.1", device_type="core")
            for n in range(3):
                await _device(db, "floor-%d" % n, "10.0.1.%d" % n, device_type="access")
            await db.commit()
        async with await _client() as client:
            lonely = (await client.get("/api/v1/topology?view=overview")).json()
        async with async_session_maker() as db:
            await _device(db, "core-2", "10.0.0.2", device_type="core")
            await db.commit()
        async with await _client() as client:
            backbone = (await client.get("/api/v1/topology?view=overview")).json()
        return lonely, backbone

    lonely, backbone = run(check())
    assert len(lonely["nodes"]) == 4
    assert sorted(n["hostname"] for n in backbone["nodes"]) == ["core-1", "core-2"]


# -- settings ----------------------------------------------------------------


def test_settings_are_saved_on_the_server_and_validated(settings_file):
    async def check():
        async with await _client() as client:
            bad = await client.post("/api/v1/settings", json={
                "allowed_subnets": ["10.0.0.0/8", "not-a-network"]})
            good = await client.post("/api/v1/settings", json={
                "default_community": "  s3cret ", "enable_subnet_restriction": True,
                "allowed_subnets": ["10.1.2.3/8", "10.0.0.0/8", " "]})
            back = (await client.get("/api/v1/settings")).json()
        return bad, good, back

    bad, good, back = run(check())
    assert bad.status_code == 422
    assert good.status_code == 200
    assert back["default_community"] == "s3cret"
    # Normalised and de-duplicated: both spellings are the same network
    assert back["allowed_subnets"] == ["10.0.0.0/8"]
    assert json.loads(settings_file.read_text())["enable_subnet_restriction"] is True
    # The community is in that file
    assert settings_file.stat().st_mode & 0o077 == 0


def test_the_subnet_restriction_is_enforced_by_the_server(settings_file):
    """It used to be a check in the page's JavaScript, against a text box"""
    app_settings.save_settings(app_settings.SNMPSettings(
        allowed_subnets=["192.0.2.0/24"], enable_subnet_restriction=True))

    async def check():
        async with await _client() as client:
            test = await client.post("/api/v1/snmp/test", json={"ip": "198.51.100.7"})
            lldp = await client.post("/api/v1/snmp/lldp", json={"ip": "198.51.100.7"})
            manual = await client.post("/api/v1/discovery/manual", json={"ip": "198.51.100.7"})
        return test.json(), lldp.json(), manual.json()

    for answer in run(check()):
        assert answer["success"] is False
        assert "outside the allowed subnets" in answer["error"]


def test_what_the_restriction_allows(settings_file):
    allowed = app_settings.address_allowed
    # Off, or nothing listed: everything is allowed
    assert allowed("203.0.113.9")
    app_settings.save_settings(app_settings.SNMPSettings(
        allowed_subnets=["192.0.2.0/24"], enable_subnet_restriction=False))
    assert allowed("203.0.113.9")
    app_settings.save_settings(app_settings.SNMPSettings(
        allowed_subnets=[], enable_subnet_restriction=True))
    assert allowed("203.0.113.9"), "an empty list means not configured, not deny all"

    app_settings.save_settings(app_settings.SNMPSettings(
        allowed_subnets=["192.0.2.0/24"], enable_subnet_restriction=True))
    assert allowed("192.0.2.77")
    assert not allowed("192.0.3.1")
    assert not allowed("switch.example"), "a name is not inside any subnet"


def test_the_collector_does_not_probe_a_neighbour_outside_the_allowed_subnets(
    settings_file, monkeypatch
):
    """
    A neighbour is whatever is on the other end of a cable. Probing it means
    sending it this network's community string.
    """
    import app.collector as col

    app_settings.save_settings(app_settings.SNMPSettings(
        allowed_subnets=["10.0.0.0/8"], enable_subnet_restriction=True))

    probed = []

    class Collector:
        community = "s3cret"

        async def get_device_info(self, ip):
            probed.append(ip)
            return None

    class Neighbor:
        remote_hostname = "isp-router"
        remote_mgmt_ip = "203.0.113.1"

    monkeypatch.setattr(col, "discovery_cache", col.DiscoveryCache(negative_ttl=60))
    run(col.auto_discover_neighbor(1, "edge-sw", Neighbor(), Collector(), None))
    assert probed == []

    Neighbor.remote_hostname, Neighbor.remote_mgmt_ip = "floor-sw", "10.4.4.4"
    run(col.auto_discover_neighbor(1, "edge-sw", Neighbor(), Collector(), None))
    assert probed == ["10.4.4.4"]


# -- small things with one right answer --------------------------------------


@pytest.mark.parametrize("hostname, vendor, expected", [
    ("core-sw-02", "cisco_nxos", "core"),
    ("DIST2", "hp_aruba", "distribution"),
    ("bldg-a-rtr1", None, "router"),
    ("fw-01", "cisco_ios", "firewall"),
    ("FGT60F", "fortinet", "firewall"),
    ("AP-3F-12", "ruckus", "ap"),
    ("access-sw-03", "cisco_ios", "access"),
    ("cam-lobby-02", None, "camera"),
    ("NVR1", None, "nvr"),
    ("hq-nvr-02", "hikvision", "nvr"),
    ("wlc-01", "ruckus", "wlc"),
    ("SmartZone-HQ", None, "wlc"),
    ("dc-lb-01", None, "loadbalancer"),
    ("srv-pve-01", None, "server"),
    ("esxi3", None, "server"),
    ("vm-web-01", None, "vm"),
    # Whole words only: these contain "core" and "ap" and are neither
    ("scoreboard", None, "access"),
    ("snapshot-host", None, "access"),
    ("camelot", None, "access"),
    (None, None, "access"),
])
def test_the_first_guess_at_a_discovered_devices_type(hostname, vendor, expected):
    assert guess_device_type(hostname, vendor) == expected


def test_a_timestamp_written_now_is_now_in_the_database():
    """
    The suite runs in the deployment's time zone (tests/conftest.py), where a
    naive utcnow() written to PostgreSQL used to land eight hours ago. This
    asks the database's own clock, so it is the check on the leg where the
    bug lived; on SQLite it passes either way.
    """
    async def check():
        await _clean()
        async with async_session_maker() as db:
            device = await _device(db, "clock", "10.0.0.1", last_seen=utcnow())
            await db.commit()
            stored, created = (await db.execute(
                select(Device.last_seen, Device.created_at).where(Device.id == device.id)
            )).one()
        return stored, created

    stored, created = run(check())
    # created_at is the database's now(); last_seen is this process's
    assert abs((stored - created).total_seconds()) < 30
    assert stored.tzinfo is not None, "timestamps come back aware on every database"


def test_the_map_page_has_focus_and_rotation_wired_up():
    """The focus bar, auto focus and rotation controls are on the page, the
    script looks for offline devices through a route that filters on it,
    and every new string exists in all three languages."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    topo_js = (STATIC / "js" / "topology.js").read_text(encoding="utf-8")
    i18n_js = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    for element in ('id="focus-bar"', 'id="autofocus-btn"', 'id="rotate-btn"', 'id="rotate-interval"'):
        assert element in html
    assert "/devices?status=offline" in app_js
    for method in ("focusOn(", "clearFocus(", "zoomToFocus(", "computeFocus("):
        assert method in topo_js
    # interfaces: in the focused path, and on every link once zoomed in
    for needle in ("portLabel(", "shortPort(", "lbl-detail", "zoom-detail", "focus-label"):
        assert needle in topo_js, needle
    assert "focus-port" in app_js
    # the tree is the default layout now
    assert "saved === 'force' ? 'force' : 'tree'" in topo_js
    for key in ("autoFocus", "rotate", "focusLabel", "focusAutoLabel", "focusClear",
                "focusImpact", "focusNoTarget"):
        assert len(re.findall(rf"^\s+{key}: '", i18n_js, re.M)) == 3, key
