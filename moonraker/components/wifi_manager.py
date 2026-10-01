# WiFi Manager Component for Moonraker
# Provides API endpoints for WiFi management via AccessPopup/NetworkManager
#
# Copyright (C) 2024 StitchLabOS
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import asyncio
import logging
import json
import re
import shlex

from ..common import RequestType

from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
)

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest
    from .shell_command import ShellCommandFactory as SCMDComp


SCRIPTS_PATH = "/home/pi/printer_data/scripts"

# NetworkManager does not scan while wlan0 runs the access point, so
# `nmcli device wifi list` shows only the AP itself. AccessPopup scans with
# this exact command; the image's sudoers rule (020-stitchlab-wifi) allows
# it without a password, so it must stay byte-for-byte identical.
IW = "/usr/sbin/iw"
IW_AP_SCAN = f"sudo -n {IW} dev wlan0 scan ap-force"

WIFI_TYPE = "802-11-wireless"
# Tried in this order when a password is set; SAE for WPA3-only networks.
KEY_MGMT_CANDIDATES = ["wpa-psk", "sae"]


def parse_connection_list(output: str) -> list[tuple[str, str]]:
    """`nmcli -t -f UUID,TYPE connection show` into (uuid, type) pairs.

    Neither field can contain a colon, so terse escaping never applies.
    """
    pairs = []
    for line in output.splitlines():
        uuid, sep, conn_type = line.strip().partition(":")
        if sep and uuid:
            pairs.append((uuid, conn_type))
    return pairs


def profile_matches(wanted: str, name: str, ssid: str, mode: str) -> bool:
    """Whether a Wi-Fi client profile is the one meant by `wanted`.

    By SSID, because add_network names a profile after a sanitised SSID
    ("My Net" becomes "My_Net"); by name, because the page connects a saved
    profile by its name. Access-point profiles never match.
    """
    if mode == "ap":
        return False
    return wanted in (ssid, name)


def _dbm_to_quality(dbm: float) -> int:
    """Signal in dBm to 0-100, the same mapping NetworkManager uses."""
    dbm = min(max(dbm, -100.0), -40.0)
    return int(100 - (100 * abs(dbm + 40)) / 60)


def _iw_security(bss: Dict[str, Any]) -> str:
    labels = []
    if bss["wpa"]:
        labels.append("WPA1")
    if bss["rsn_psk"] or bss["rsn_8021x"]:
        labels.append("WPA2")
    if bss["rsn_sae"]:
        labels.append("WPA3")
    if bss["rsn_8021x"]:
        labels.append("802.1X")
    if not labels and bss["privacy"]:
        labels.append("WEP")
    return " ".join(labels) or "Open"


def parse_iw_scan(output: str, saved: set[str]) -> list[Dict[str, Any]]:
    """Turn `iw dev <if> scan` output into wifi_scan.sh's network entries.

    One entry per SSID (the strongest BSS wins); hidden networks are skipped.
    """
    bss_list: list[Dict[str, Any]] = []
    cur: Dict[str, Any] | None = None
    section = ""
    for raw in output.splitlines():
        line = raw.strip()
        if raw.startswith("BSS "):
            cur = {"ssid": "", "dbm": -100.0, "privacy": False, "wpa": False,
                   "rsn_psk": False, "rsn_sae": False, "rsn_8021x": False}
            bss_list.append(cur)
            section = ""
            continue
        if cur is None:
            continue
        if line.startswith("SSID:"):
            cur["ssid"] = line[5:].strip()
        elif line.startswith("signal:"):
            m = re.match(r"signal:\s*(-?[\d.]+)", line)
            if m:
                cur["dbm"] = float(m.group(1))
        elif line.startswith("capability:"):
            cur["privacy"] = "Privacy" in line
        elif line.startswith("RSN:"):
            section = "rsn"
        elif line.startswith("WPA:"):
            section = "wpa"
            cur["wpa"] = True
        elif section == "rsn" and "Authentication suites:" in line:
            suites = line.split(":", 1)[1]
            cur["rsn_psk"] = "PSK" in suites
            cur["rsn_sae"] = "SAE" in suites
            cur["rsn_8021x"] = "IEEE 802.1X" in suites
        elif not raw.startswith("\t\t") and section and ":" in line:
            section = ""

    best: Dict[str, Dict[str, Any]] = {}
    for bss in bss_list:
        ssid = bss["ssid"]
        if not ssid or ssid.replace("\\x00", "") == "":
            continue
        if ssid not in best or bss["dbm"] > best[ssid]["dbm"]:
            best[ssid] = bss
    networks = [
        {
            "ssid": ssid,
            "signal": _dbm_to_quality(bss["dbm"]),
            "security": _iw_security(bss),
            "in_use": False,
            "saved": ssid in saved,
        }
        for ssid, bss in best.items()
    ]
    networks.sort(key=lambda n: n["signal"], reverse=True)
    return networks


class WiFiManager:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.shell_cmd: SCMDComp = self.server.load_component(
            config, 'shell_command'
        )
        # add_network and connect both create profiles. Run them one at a
        # time, so the second sees the profile the first created: the page
        # sent both at once and NetworkManager ended up with two profiles of
        # one name (commissioning run of 2026-09-28).
        self._profile_lock = asyncio.Lock()

        # Register API endpoints
        self.server.register_endpoint(
            "/server/wifi/status",
            RequestType.GET,
            self._handle_status
        )
        self.server.register_endpoint(
            "/server/wifi/scan",
            RequestType.GET,
            self._handle_scan
        )
        self.server.register_endpoint(
            "/server/wifi/profiles",
            RequestType.GET,
            self._handle_profiles
        )
        self.server.register_endpoint(
            "/server/wifi/connect",
            RequestType.POST,
            self._handle_connect
        )
        self.server.register_endpoint(
            "/server/wifi/disconnect",
            RequestType.POST,
            self._handle_disconnect
        )
        self.server.register_endpoint(
            "/server/wifi/ap/enable",
            RequestType.POST,
            self._handle_ap_enable
        )
        self.server.register_endpoint(
            "/server/wifi/ap/disable",
            RequestType.POST,
            self._handle_ap_disable
        )
        self.server.register_endpoint(
            "/server/wifi/forget",
            RequestType.POST,
            self._handle_forget
        )
        # NEW: Add network endpoint
        self.server.register_endpoint(
            "/server/wifi/add",
            RequestType.POST,
            self._handle_add_network
        )
        # NEW: Update profile priority
        self.server.register_endpoint(
            "/server/wifi/priority",
            RequestType.POST,
            self._handle_set_priority
        )
        # NEW: Configure AP settings
        self.server.register_endpoint(
            "/server/wifi/ap/configure",
            RequestType.POST,
            self._handle_ap_configure
        )
        # NEW: Get AP configuration
        self.server.register_endpoint(
            "/server/wifi/ap/config",
            RequestType.GET,
            self._handle_ap_get_config
        )

        logging.info("WiFiManager: Component loaded (extended)")

    async def _run_script(self, script_name: str, timeout: float = 10.0) -> str:
        """Run a script from the scripts directory and return output."""
        script_path = f"{SCRIPTS_PATH}/{script_name}"
        try:
            result = await self.shell_cmd.exec_cmd(
                script_path,
                timeout=timeout,
                log_complete=False
            )
            return result
        except self.shell_cmd.error as e:
            logging.error(f"WiFiManager: Script {script_name} failed: {e}")
            raise self.server.error(f"Script execution failed: {e}", 500)

    async def _run_nmcli(self, cmd: str, timeout: float = 30.0) -> str:
        """Run an nmcli command and return output."""
        try:
            result = await self.shell_cmd.exec_cmd(
                cmd,
                timeout=timeout,
                log_complete=False
            )
            return result
        except self.shell_cmd.error as e:
            logging.error(f"WiFiManager: nmcli command failed: {e}")
            raise self.server.error(f"Command execution failed: {e}", 500)

    async def _run_nmcli_privileged(self, cmd: str, timeout: float = 30.0) -> str:
        """Run an nmcli command with sudo and return output."""
        return await self._run_nmcli(f"sudo -n {cmd}", timeout=timeout)

    async def _try_nmcli(self, cmd: str, timeout: float = 30.0) -> tuple[bool, str]:
        """Run an nmcli command and return success plus output/error."""
        try:
            result = await self.shell_cmd.exec_cmd(
                cmd,
                timeout=timeout,
                log_complete=False
            )
            return True, result
        except self.shell_cmd.error as e:
            return False, str(e)

    async def _try_nmcli_privileged(self, cmd: str, timeout: float = 30.0) -> tuple[bool, str]:
        """Run an nmcli command with sudo and return success plus output/error."""
        return await self._try_nmcli(f"sudo -n {cmd}", timeout=timeout)

    async def _connection_value(self, uuid: str, field: str) -> str:
        """One field of a connection, unescaped, or '' when unset."""
        ok, out = await self._try_nmcli(
            f"nmcli -e no -g {field} connection show uuid {shlex.quote(uuid)}",
            timeout=10.0
        )
        return out.strip() if ok else ""

    async def _find_wifi_profiles(self, wanted: str) -> list[str]:
        """UUIDs of the Wi-Fi client profiles for an SSID or profile name."""
        ok, out = await self._try_nmcli(
            "nmcli -t -f UUID,TYPE connection show", timeout=10.0
        )
        if not ok:
            raise self.server.error(f"Could not list connections: {out}", 500)
        matches = []
        for uuid, conn_type in parse_connection_list(out):
            if conn_type != WIFI_TYPE:
                continue
            name = await self._connection_value(uuid, "connection.id")
            ssid = await self._connection_value(uuid, "802-11-wireless.ssid")
            mode = await self._connection_value(uuid, "802-11-wireless.mode")
            if profile_matches(wanted, name, ssid, mode):
                matches.append(uuid)
        return matches

    async def _set_password(self, uuid: str, password: str) -> None:
        last_error = ""
        for key_mgmt in KEY_MGMT_CANDIDATES:
            ok, err = await self._try_nmcli_privileged(
                f"nmcli connection modify uuid {shlex.quote(uuid)} "
                f"wifi-sec.key-mgmt {key_mgmt} wifi-sec.psk {shlex.quote(password)}"
            )
            if ok:
                return
            last_error = err
        raise self.server.error(f"Failed to set the password: {last_error}", 500)

    async def _handle_status(self, web_request: WebRequest) -> Dict[str, Any]:
        """Get current WiFi connection status."""
        output = await self._run_script("wifi_status.sh")
        try:
            return json.loads(output)
        except json.JSONDecodeError as e:
            logging.error(f"WiFiManager: Failed to parse status JSON: {e}")
            raise self.server.error(f"Failed to parse status: {e}", 500)

    async def _wlan0_is_ap(self) -> bool:
        try:
            info = await self.shell_cmd.exec_cmd(
                f"{IW} dev wlan0 info", timeout=5.0, log_complete=False
            )
        except self.shell_cmd.error:
            return False
        return re.search(r"^\s*type AP\s*$", info, re.MULTILINE) is not None

    async def _scan_in_ap_mode(self) -> Dict[str, Any]:
        try:
            output = await self.shell_cmd.exec_cmd(
                IW_AP_SCAN, timeout=20.0, log_complete=False
            )
        except self.shell_cmd.error as e:
            logging.error(f"WiFiManager: AP-mode scan failed: {e}")
            raise self.server.error(
                "Scanning while the access point is active failed. The image "
                "must allow this via /etc/sudoers.d/020-stitchlab-wifi.", 500)
        try:
            names = await self._run_nmcli(
                "nmcli -t -f NAME connection show", timeout=10.0)
        except self.server.error:
            names = ""
        saved = {n for n in names.splitlines() if n}
        return {"networks": parse_iw_scan(output, saved)}

    async def _handle_scan(self, web_request: WebRequest) -> Dict[str, Any]:
        """Scan for available WiFi networks."""
        if await self._wlan0_is_ap():
            return await self._scan_in_ap_mode()

        # Force a rescan first
        try:
            await self.shell_cmd.exec_cmd(
                "nmcli device wifi rescan",
                timeout=10.0,
                log_complete=False,
                success_codes=[0, 1]  # May return 1 if scan already in progress
            )
        except self.shell_cmd.error:
            pass  # Ignore rescan errors

        output = await self._run_script("wifi_scan.sh")
        try:
            return json.loads(output)
        except json.JSONDecodeError as e:
            logging.error(f"WiFiManager: Failed to parse scan JSON: {e}")
            raise self.server.error(f"Failed to parse scan results: {e}", 500)

    async def _handle_profiles(self, web_request: WebRequest) -> Dict[str, Any]:
        """Get saved WiFi profiles."""
        output = await self._run_script("wifi_profiles.sh")
        try:
            return json.loads(output)
        except json.JSONDecodeError as e:
            logging.error(f"WiFiManager: Failed to parse profiles JSON: {e}")
            raise self.server.error(f"Failed to parse profiles: {e}", 500)

    async def _handle_connect(self, web_request: WebRequest) -> Dict[str, Any]:
        """Connect to a WiFi network."""
        ssid = web_request.get_str("ssid")
        password = web_request.get_str("password", None)

        if not ssid:
            raise self.server.error("SSID is required", 400)

        async with self._profile_lock:
            try:
                # A saved profile is activated, never created a second time.
                uuids = await self._find_wifi_profiles(ssid)
                if uuids:
                    if len(uuids) > 1:
                        logging.warning(
                            f"WiFiManager: {len(uuids)} profiles for {ssid}, using {uuids[0]}")
                    if password:
                        await self._set_password(uuids[0], password)
                    cmd = f"nmcli connection up uuid {shlex.quote(uuids[0])}"
                elif password:
                    cmd = (f"nmcli device wifi connect {shlex.quote(ssid)} "
                           f"password {shlex.quote(password)}")
                else:
                    cmd = f"nmcli device wifi connect {shlex.quote(ssid)}"
                result = await self._run_nmcli_privileged(cmd, timeout=60.0)
            except Exception as e:
                raise self.server.error(f"Failed to connect to {ssid}: {e}", 500)
        logging.info(f"WiFiManager: Connected to {ssid}")
        return {"status": "connected", "ssid": ssid, "message": result}

    async def _handle_disconnect(self, web_request: WebRequest) -> Dict[str, Any]:
        """Disconnect from current WiFi network."""
        try:
            await self._run_nmcli_privileged("nmcli device disconnect wlan0")
            logging.info("WiFiManager: Disconnected from WiFi")
            return {"status": "disconnected"}
        except Exception as e:
            raise self.server.error(f"Failed to disconnect: {e}", 500)

    async def _handle_ap_enable(self, web_request: WebRequest) -> Dict[str, Any]:
        """Enable Access Point mode."""
        ap_profile = web_request.get_str("profile", "AccessPopup")

        try:
            # Disconnect any existing connection
            try:
                await self._run_nmcli_privileged("nmcli device disconnect wlan0", timeout=10.0)
            except Exception:
                pass  # Ignore if already disconnected

            # Activate AP profile
            cmd = f'nmcli connection up "{ap_profile}"'
            result = await self._run_nmcli_privileged(cmd, timeout=30.0)
            logging.info(f"WiFiManager: AP mode enabled with profile {ap_profile}")
            return {"status": "ap_enabled", "profile": ap_profile, "message": result}
        except Exception as e:
            raise self.server.error(f"Failed to enable AP mode: {e}", 500)

    async def _handle_ap_disable(self, web_request: WebRequest) -> Dict[str, Any]:
        """Disable Access Point mode and attempt to reconnect to WiFi."""
        try:
            # Disconnect AP
            await self._run_nmcli_privileged("nmcli device disconnect wlan0", timeout=10.0)

            # Try to connect to the first available saved network
            profiles_output = await self._run_script("wifi_profiles.sh")
            try:
                profiles_data = json.loads(profiles_output)
                wifi_profiles = [
                    p["name"] for p in profiles_data.get("profiles", [])
                    if p.get("type") == "wifi"
                ]
            except (json.JSONDecodeError, KeyError):
                wifi_profiles = []

            if wifi_profiles:
                # Try to connect to the first WiFi profile
                try:
                    cmd = f'nmcli connection up "{wifi_profiles[0]}"'
                    await self._run_nmcli_privileged(cmd, timeout=60.0)
                    logging.info(f"WiFiManager: Reconnected to {wifi_profiles[0]}")
                    return {
                        "status": "reconnected",
                        "ssid": wifi_profiles[0]
                    }
                except Exception:
                    pass

            logging.info("WiFiManager: AP disabled, no WiFi reconnection")
            return {"status": "ap_disabled"}
        except Exception as e:
            raise self.server.error(f"Failed to disable AP mode: {e}", 500)

    async def _handle_forget(self, web_request: WebRequest) -> Dict[str, Any]:
        """Forget (delete) a saved WiFi profile."""
        profile = web_request.get_str("profile")

        if not profile:
            raise self.server.error("Profile name is required", 400)

        # Don't allow deleting the AP profile
        if profile.lower() == "accesspopup":
            raise self.server.error("Cannot delete the AccessPopup profile", 400)

        try:
            cmd = f'nmcli connection delete "{profile}"'
            await self._run_nmcli_privileged(cmd)
            logging.info(f"WiFiManager: Deleted profile {profile}")
            return {"status": "deleted", "profile": profile}
        except Exception as e:
            raise self.server.error(f"Failed to delete profile {profile}: {e}", 500)

    async def _handle_add_network(self, web_request: WebRequest) -> Dict[str, Any]:
        """Add a new WiFi network profile (without connecting)."""
        ssid = web_request.get_str("ssid")
        password = web_request.get_str("password", None)
        autoconnect = web_request.get_boolean("autoconnect", True)
        priority = web_request.get_int("priority", 0)

        if not ssid:
            raise self.server.error("SSID is required", 400)

        # Sanitize SSID for use as connection name
        conn_name = re.sub(r'[^a-zA-Z0-9_-]', '_', ssid)

        autoconnect_args = (
            f'connection.autoconnect {"yes" if autoconnect else "no"} '
            f'connection.autoconnect-priority {priority}'
        )
        async with self._profile_lock:
            try:
                # Look up by SSID first: a profile that connect created is
                # named after the SSID itself, not after conn_name.
                uuids = (await self._find_wifi_profiles(ssid)
                         or await self._find_wifi_profiles(conn_name))
                if uuids:
                    uuid = uuids[0]
                    ok, err = await self._try_nmcli_privileged(
                        f"nmcli connection modify uuid {shlex.quote(uuid)} "
                        f"wireless.ssid {shlex.quote(ssid)} {autoconnect_args}"
                    )
                    if not ok:
                        raise self.server.error(f"Failed to update profile for {ssid}: {err}", 500)
                    if password:
                        await self._set_password(uuid, password)
                    profile = await self._connection_value(uuid, "connection.id") or conn_name
                else:
                    await self._add_profile(conn_name, ssid, password, autoconnect_args)
                    profile = conn_name
            except Exception as e:
                raise self.server.error(f"Failed to add network {ssid}: {e}", 500)

        logging.info(f"WiFiManager: Saved network profile {profile}")
        return {
            "status": "added",
            "ssid": ssid,
            "profile": profile,
            "autoconnect": autoconnect,
            "priority": priority
        }

    async def _add_profile(self, conn_name: str, ssid: str,
                           password: str | None, autoconnect_args: str) -> None:
        base = (f"nmcli connection add type wifi con-name {shlex.quote(conn_name)} "
                f"ssid {shlex.quote(ssid)} {autoconnect_args}")
        if not password:
            ok, err = await self._try_nmcli_privileged(base)
            if not ok:
                raise self.server.error(err, 500)
            return
        last_error = ""
        for key_mgmt in KEY_MGMT_CANDIDATES:
            ok, err = await self._try_nmcli_privileged(
                f"{base} wifi-sec.key-mgmt {key_mgmt} wifi-sec.psk {shlex.quote(password)}"
            )
            if ok:
                return
            last_error = err
            # No profile of this name existed before, so this only removes a
            # partial one from the failed attempt.
            await self._try_nmcli_privileged(
                f"nmcli connection delete id {shlex.quote(conn_name)}", timeout=10.0)
        raise self.server.error(last_error, 500)

    async def _handle_set_priority(self, web_request: WebRequest) -> Dict[str, Any]:
        """Set the autoconnect priority for a profile."""
        profile = web_request.get_str("profile")
        priority = web_request.get_int("priority", 0)

        if not profile:
            raise self.server.error("Profile name is required", 400)

        try:
            cmd = f'nmcli connection modify "{profile}" connection.autoconnect-priority {priority}'
            await self._run_nmcli_privileged(cmd)
            logging.info(f"WiFiManager: Set priority {priority} for {profile}")
            return {"status": "updated", "profile": profile, "priority": priority}
        except Exception as e:
            raise self.server.error(f"Failed to set priority: {e}", 500)

    async def _handle_ap_configure(self, web_request: WebRequest) -> Dict[str, Any]:
        """Configure Access Point settings."""
        ap_profile = web_request.get_str("profile", "AccessPopup")
        ssid = web_request.get_str("ssid", None)
        password = web_request.get_str("password", None)
        ip_address = web_request.get_str("ip", None)

        changes = []

        try:
            if ssid:
                await self._run_nmcli_privileged(
                    f'nmcli connection modify "{ap_profile}" wireless.ssid "{ssid}"'
                )
                changes.append(f"ssid={ssid}")

            if password:
                if len(password) < 8:
                    raise self.server.error("Password must be at least 8 characters", 400)
                await self._run_nmcli_privileged(
                    f'nmcli connection modify "{ap_profile}" wifi-sec.psk "{password}"'
                )
                changes.append("password=***")

            if ip_address:
                # Validate IP format (basic check)
                if not re.match(r'^\d+\.\d+\.\d+\.\d+/\d+$', ip_address):
                    raise self.server.error("Invalid IP format. Use CIDR notation: 192.168.50.5/24", 400)
                await self._run_nmcli_privileged(
                    f'nmcli connection modify "{ap_profile}" ipv4.addresses "{ip_address}" ipv4.method shared'
                )
                changes.append(f"ip={ip_address}")

            if not changes:
                raise self.server.error("No changes specified", 400)

            logging.info(f"WiFiManager: Updated AP config: {', '.join(changes)}")
            return {
                "status": "configured",
                "profile": ap_profile,
                "changes": changes
            }
        except self.server.error:
            raise
        except Exception as e:
            raise self.server.error(f"Failed to configure AP: {e}", 500)

    async def _handle_ap_get_config(self, web_request: WebRequest) -> Dict[str, Any]:
        """Get current Access Point configuration."""
        ap_profile = web_request.get_str("profile", "AccessPopup")

        try:
            # Get AP settings
            ok, result = await self._try_nmcli(
                f'nmcli -t connection show "{ap_profile}"',
                timeout=10.0
            )
            if not ok:
                logging.warning(
                    f"WiFiManager: AP profile {ap_profile} not found, returning empty config"
                )
                return {
                    "profile": ap_profile,
                    "ssid": None,
                    "ip": None,
                    "security": None
                }

            config = {
                "profile": ap_profile,
                "ssid": None,
                "ip": None,
                "security": None
            }

            for line in result.split('\n'):
                if ':' in line:
                    key, _, value = line.partition(':')
                    if key == 'wireless.ssid':
                        config["ssid"] = value
                    elif key == 'ipv4.addresses':
                        config["ip"] = value
                    elif key == 'wifi-sec.key-mgmt':
                        config["security"] = value

            return config
        except Exception as e:
            raise self.server.error(f"Failed to get AP config: {e}", 500)


def load_component(config: ConfigHelper) -> WiFiManager:
    return WiFiManager(config)
