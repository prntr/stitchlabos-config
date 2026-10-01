"""Tests for the Moonraker wifi_manager component and its scripts.

No NetworkManager needed: the component runs against a simulated nmcli, and
the scripts against a fake nmcli on PATH.
"""

import asyncio
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPTS = ROOT / "printer_data/scripts"


def load_component():
    """Import wifi_manager.py without Moonraker, which it imports relatively."""
    common = types.ModuleType("moonraker.common")
    common.RequestType = types.SimpleNamespace(GET="GET", POST="POST")
    for name, module in {
        "moonraker": types.ModuleType("moonraker"),
        "moonraker.components": types.ModuleType("moonraker.components"),
        "moonraker.common": common,
    }.items():
        sys.modules.setdefault(name, module)
    spec = importlib.util.spec_from_file_location(
        "moonraker.components.wifi_manager", ROOT / "moonraker/components/wifi_manager.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wm = load_component()


# --- pure helpers -----------------------------------------------------------

def test_parse_connection_list():
    out = "a1:802-11-wireless\nb2:802-3-ethernet\n\n"
    assert wm.parse_connection_list(out) == [("a1", "802-11-wireless"), ("b2", "802-3-ethernet")]


@pytest.mark.parametrize("wanted,name,ssid,mode,expected", [
    ("My Net", "My_Net", "My Net", "infrastructure", True),   # add_network's sanitised name
    ("My_Net", "My_Net", "My Net", "infrastructure", True),   # the page connects by name
    ("StitchlabSRV", "StitchlabSRV", "StitchlabSRV", "", True),
    ("Stitchlab", "AccessPopup", "Stitchlab", "ap", False),   # never the access point
    ("Other", "My_Net", "My Net", "infrastructure", False),
])
def test_profile_matches(wanted, name, ssid, mode, expected):
    assert wm.profile_matches(wanted, name, ssid, mode) is expected


# --- component against a simulated NetworkManager ----------------------------

class ShellError(Exception):
    pass


class FakeNetworkManager:
    """Answers the nmcli commands the component sends. Creating a profile
    yields to the event loop, like the real call, so an unserialised second
    request can look up profiles in between."""

    error = ShellError

    def __init__(self):
        self.profiles = {
            "ap-uuid": {"connection.id": "AccessPopup", "802-11-wireless.ssid": "Stitchlab",
                        "802-11-wireless.mode": "ap"},
        }
        self.commands = []

    def _create(self, name, ssid):
        uuid = f"uuid-{len(self.profiles)}"
        self.profiles[uuid] = {"connection.id": name, "802-11-wireless.ssid": ssid,
                               "802-11-wireless.mode": "infrastructure"}

    async def exec_cmd(self, cmd, timeout=None, log_complete=True):
        args = shlex.split(cmd)
        if args[:2] == ["sudo", "-n"]:
            args = args[2:]
        self.commands.append(args)
        if args == ["nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"]:
            return "\n".join(f"{u}:802-11-wireless" for u in self.profiles) + "\n"
        if args[:4] == ["nmcli", "-e", "no", "-g"] and args[5:8] == ["connection", "show", "uuid"]:
            return self.profiles[args[8]].get(args[4], "") + "\n"
        if args[:4] == ["nmcli", "device", "wifi", "connect"]:
            await asyncio.sleep(0.01)
            self._create(args[4], args[4])
            return "Device 'wlan0' successfully activated."
        if args[:4] == ["nmcli", "connection", "add", "type"]:
            await asyncio.sleep(0.01)
            self._create(args[args.index("con-name") + 1], args[args.index("ssid") + 1])
            return "Connection successfully added."
        if args[:3] == ["nmcli", "connection", "modify"] or args[:3] == ["nmcli", "connection", "up"]:
            if args[3] == "uuid" and args[4] not in self.profiles:
                raise ShellError(f"unknown connection {args[4]}")
            return ""
        raise ShellError(f"unexpected command: {args}")

    def wifi_clients(self):
        return [p for p in self.profiles.values() if p["802-11-wireless.mode"] != "ap"]


class FakeServer:
    error = RuntimeError

    def __init__(self, nm):
        self.nm = nm

    def load_component(self, config, name):
        return self.nm

    def register_endpoint(self, *args, **kwargs):
        pass


class FakeRequest:
    def __init__(self, **params):
        self.params = params

    def get_str(self, name, default=...):
        return self.params.get(name, default)

    def get_boolean(self, name, default=...):
        return self.params.get(name, default)

    def get_int(self, name, default=...):
        return self.params.get(name, default)


def manager():
    nm = FakeNetworkManager()
    server = FakeServer(nm)
    config = types.SimpleNamespace(get_server=lambda: server)
    return wm.WiFiManager(config), nm


def run(coro):
    return asyncio.run(coro)


def test_save_and_connect_at_once_make_one_profile():
    # The page sent server.wifi.add and server.wifi.connect together; each
    # created a profile and NetworkManager ended up with two of one name.
    async def both():
        mgr, nm = manager()
        params = {"ssid": "StitchlabSRV", "password": "secret"}
        await asyncio.gather(
            mgr._handle_add_network(FakeRequest(**params)),
            mgr._handle_connect(FakeRequest(**params)),
        )
        return nm

    nm = run(both())
    assert len(nm.wifi_clients()) == 1


def test_connect_uses_the_saved_profile_of_an_ssid_with_a_space():
    async def flow():
        mgr, nm = manager()
        await mgr._handle_add_network(FakeRequest(ssid="My Net", password="pw"))
        await mgr._handle_connect(FakeRequest(ssid="My Net", password="pw"))
        return nm

    nm = run(flow())
    assert [p["connection.id"] for p in nm.wifi_clients()] == ["My_Net"]
    assert nm.commands[-1] == ["nmcli", "connection", "up", "uuid", "uuid-1"]


def test_connect_by_profile_name():
    async def flow():
        mgr, nm = manager()
        await mgr._handle_add_network(FakeRequest(ssid="My Net"))
        await mgr._handle_connect(FakeRequest(ssid="My_Net"))
        return nm

    nm = run(flow())
    assert len(nm.wifi_clients()) == 1
    assert nm.commands[-1][:3] == ["nmcli", "connection", "up"]


def test_add_again_updates_instead_of_adding():
    async def flow():
        mgr, nm = manager()
        await mgr._handle_add_network(FakeRequest(ssid="StitchlabSRV", password="old"))
        result = await mgr._handle_add_network(FakeRequest(ssid="StitchlabSRV", password="new"))
        return nm, result

    nm, result = run(flow())
    assert len(nm.wifi_clients()) == 1
    assert result["profile"] == "StitchlabSRV"


def test_new_network_without_profile_connects_directly():
    async def flow():
        mgr, nm = manager()
        await mgr._handle_connect(FakeRequest(ssid='Cafe "Eck"', password='p"w $x'))
        return nm

    nm = run(flow())
    assert nm.commands[-1] == ["nmcli", "device", "wifi", "connect", 'Cafe "Eck"',
                               "password", 'p"w $x']


def test_connect_never_activates_the_access_point_profile():
    async def flow():
        mgr, nm = manager()
        await mgr._handle_connect(FakeRequest(ssid="Stitchlab"))
        return nm

    nm = run(flow())
    assert nm.commands[-1][:4] == ["nmcli", "device", "wifi", "connect"]


# --- scripts against a fake nmcli -------------------------------------------

FAKE_NMCLI = r'''#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["FAKE_NM_STATE"]))
args = sys.argv[1:]
if args == ["-t", "-f", "UUID,DEVICE", "connection", "show", "--active"]:
    for uuid, p in state["profiles"].items():
        if p.get("device"):
            print(f"{uuid}:{p['device']}")
elif args == ["-t", "-f", "AUTOCONNECT-PRIORITY,UUID,TYPE", "connection", "show"]:
    for uuid, p in state["profiles"].items():
        print(f"{p['priority']}:{uuid}:802-11-wireless")
elif args[:3] == ["-e", "no", "-g"] and args[4:6] == ["connection", "show"]:
    print(state["profiles"][args[7]].get(args[3], ""))
elif args[:3] == ["-f", "IN-USE,SIGNAL", "device"]:
    print("IN-USE  SIGNAL\n*       78")
elif args == ["radio", "wifi"]:
    print("enabled")
else:
    sys.exit(f"fake nmcli: unexpected {args}")
'''

STATE = {"profiles": {
    "u-ap": {"connection.id": "AccessPopup", "802-11-wireless.ssid": "Stitchlab",
             "802-11-wireless.mode": "ap", "connection.autoconnect": "no", "priority": 0},
    "u-1": {"connection.id": "StitchlabSRV", "802-11-wireless.ssid": "StitchlabSRV",
            "802-11-wireless.mode": "infrastructure", "connection.autoconnect": "yes",
            "priority": 5, "device": "wlan0", "IP4.ADDRESS": "10.1.2.3/24"},
    "u-2": {"connection.id": "StitchlabSRV", "802-11-wireless.ssid": "StitchlabSRV",
            "802-11-wireless.mode": "infrastructure", "connection.autoconnect": "yes",
            "priority": 0},
    "u-3": {"connection.id": 'Cafe "Eck"', "802-11-wireless.ssid": 'Cafe "Eck" 2\\G',
            "802-11-wireless.mode": "", "connection.autoconnect": "yes", "priority": 1},
}}


def run_script(name, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool, body in {"nmcli": FAKE_NMCLI,
                       "systemctl": "#!/bin/sh\necho active\n"}.items():
        (bin_dir / tool).write_text(body)
        (bin_dir / tool).chmod(0o755)
    state = tmp_path / "state.json"
    state.write_text(json.dumps(STATE))
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_NM_STATE": str(state)}
    out = subprocess.run(["bash", str(SCRIPTS / name)], capture_output=True, text=True,
                         env=env, check=True, timeout=20).stdout
    return json.loads(out)


def test_profiles_script_with_duplicate_names_and_quotes(tmp_path):
    profiles = run_script("wifi_profiles.sh", tmp_path)["profiles"]
    assert [p["priority"] for p in profiles] == [5, 1, 0, 0]
    by_uuid = {p["uuid"]: p for p in profiles}
    assert by_uuid["u-1"]["ssid"] == "StitchlabSRV"          # not "StitchlabSRV\nStitchlabSRV"
    assert by_uuid["u-3"]["ssid"] == 'Cafe "Eck" 2\\G'       # whole SSID, escaped
    assert by_uuid["u-3"]["type"] == "wifi"
    assert by_uuid["u-ap"]["type"] == "ap"
    assert by_uuid["u-1"]["autoconnect"] is True
    assert by_uuid["u-ap"]["autoconnect"] is False


def test_status_script_with_duplicate_names(tmp_path):
    status = run_script("wifi_status.sh", tmp_path)
    assert status["status"] == "connected"
    assert status["connection"]["ssid"] == "StitchlabSRV"
    assert status["connection"]["ip"] == "10.1.2.3"
    assert status["connection"]["signal"] == 78
    assert status["timer_active"] is True
