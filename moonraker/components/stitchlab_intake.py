# StitchLab Intake Component for Moonraker
# Phase 3 of the G-Code Intake plan: thin wrapper around the IntakeCore
# logic that lives in /home/pi/stitchlab_intake/component.py. Heavy work
# (parser, renderer, Pillow) runs in a subprocess via the
# stitchlab-gcode-intake CLI so Moonraker's process stays light.
#
# Copyright (C) 2026 StitchLabOS
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from ..common import RequestType

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest


# Allow `import stitchlab_intake.component` from the system venv. The
# package lives outside Moonraker's tree on the Pi.
_PKG_PARENT = "/home/pi"
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from stitchlab_intake.component import (  # noqa: E402
    Adapter,
    CliResult,
    IntakeConfig,
    IntakeCore,
)


log = logging.getLogger(__name__)


class StitchlabIntake:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()

        cfg = IntakeConfig(
            gcodes_root=Path(config.get(
                "gcodes_root", "/home/pi/printer_data/gcodes")),
            cli_path=config.get(
                "cli_path", "/usr/local/bin/stitchlab-gcode-intake"),
            hoops_config=config.get(
                "hoops_config", "/home/pi/stitchlab_intake/hoops.json"),
            default_hoop=config.get("default_hoop", "standard"),
            analyze_timeout=config.getfloat("analyze_timeout", 60.0),
            prepare_timeout=config.getfloat("prepare_timeout", 30.0),
            thumbnail_size=config.getint("thumbnail_size", 768),
            max_queue=config.getint("max_queue", 32),
        )
        self._worker_nice = config.getint("worker_nice", 10)
        self._worker_ionice = config.get("worker_ionice", "idle")

        adapter = Adapter(
            run_cli=self._run_cli,
            query_klippy_macros=self._query_macros,
            is_printing=lambda: self._printing,
            emit_event=self._emit_event,
        )
        self.core = IntakeCore(cfg, adapter)
        self._printing = False

        # JSON-RPC method names map automatically from the URI.
        self.server.register_endpoint(
            "/server/stitchlab_intake/analyze", RequestType.POST,
            self._ep_analyze,
        )
        self.server.register_endpoint(
            "/server/stitchlab_intake/prepare", RequestType.POST,
            self._ep_prepare,
        )
        self.server.register_endpoint(
            "/server/stitchlab_intake/status", RequestType.GET,
            self._ep_status,
        )
        self.server.register_endpoint(
            "/server/stitchlab_intake/recheck", RequestType.POST,
            self._ep_recheck,
        )
        self.server.register_endpoint(
            "/server/stitchlab_intake/cancel", RequestType.POST,
            self._ep_cancel,
        )
        self.server.register_endpoint(
            "/server/stitchlab_intake/metadata", RequestType.GET,
            self._ep_metadata,
        )
        self.server.register_notification("stitchlab_intake:status")

        self.server.register_event_handler(
            "server:klippy_ready", self._on_klippy_ready)
        self.server.register_event_handler(
            "server:klippy_disconnect", self._on_klippy_disconnect)
        self.server.register_event_handler(
            "file_manager:file_uploaded", self._on_file_uploaded)

    async def component_init(self) -> None:
        self.core.start()

    async def close(self) -> None:
        await self.core.stop()

    # --- endpoint handlers ----------------------------------------------

    async def _ep_analyze(self, web_request: WebRequest) -> Dict[str, Any]:
        filename = web_request.get_str("filename")
        hoop = web_request.get_str("hoop_id", None)
        return await self.core.enqueue_analyze(filename, hoop_id=hoop)

    async def _ep_prepare(self, web_request: WebRequest) -> Dict[str, Any]:
        filename = web_request.get_str("filename")
        placement = web_request.get("placement", None)
        hoop = None
        if isinstance(placement, dict):
            hoop = placement.get("hoop_id")
        return await self.core.prepare(filename, placement=placement, hoop_id=hoop)

    async def _ep_status(self, web_request: WebRequest) -> Dict[str, Any]:
        return await self.core.status(web_request.get_str("filename"))

    async def _ep_recheck(self, web_request: WebRequest) -> Dict[str, Any]:
        filename = web_request.get_str("filename")
        hoop = web_request.get_str("hoop_id", None)
        return await self.core.recheck(filename, hoop_id=hoop)

    async def _ep_cancel(self, web_request: WebRequest) -> Dict[str, Any]:
        return await self.core.cancel(web_request.get_str("filename"))

    async def _ep_metadata(self, web_request: WebRequest) -> Dict[str, Any]:
        # Lightweight read of the cached sidecar — used by Mainsail in
        # place of current_file.thumbnails since Moonraker's metadata
        # scanner does not pick up sidecar PNGs.
        return await self.core.metadata(web_request.get_str("filename"))

    # --- adapter implementations ----------------------------------------

    async def _run_cli(self, args: list, timeout: float) -> CliResult:
        # Wrap the CLI in nice/ionice so the worker can't starve Klippy.
        # Both are advisory — failure to set them must not abort the run.
        wrapped = ["nice", "-n", str(self._worker_nice),
                   "ionice", "-c", self._worker_ionice, "--"] + list(args)
        try:
            proc = await asyncio.create_subprocess_exec(
                *wrapped,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            # nice/ionice missing — fall back to plain exec.
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 2.0)
            except asyncio.TimeoutError:
                proc.kill()
            raise
        return CliResult(
            returncode=proc.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    async def _query_macros(self) -> Optional[set]:
        if not self.server.is_klippy_connected():
            return None
        try:
            kapi = self.server.lookup_component("klippy_apis")
            result = await kapi.query_objects({"configfile": None})
        except Exception:  # noqa: BLE001
            log.exception("intake: macro query failed")
            return None
        config = ((result or {}).get("configfile") or {}).get("config") or {}
        macros = set()
        for key in config.keys():
            if key.startswith("gcode_macro "):
                macros.add(key[len("gcode_macro "):].upper())
        return macros

    def _emit_event(self, name: str, payload: dict) -> None:
        self.server.send_event(name, payload)
        # Also push as a notification so Mainsail's WebSocket consumers
        # can subscribe without server-side state.
        self.server.send_event("server:status_update", {name: payload})

    # --- event handlers --------------------------------------------------

    async def _on_klippy_ready(self) -> None:
        try:
            kapi = self.server.lookup_component("klippy_apis")
            await kapi.subscribe_objects({"print_stats": ["state"]},
                                         self._on_print_stats)
        except Exception:  # noqa: BLE001
            log.exception("intake: print_stats subscription failed")

    async def _on_klippy_disconnect(self) -> None:
        # Klippy gone -> assume not printing; analyses can proceed.
        self.core.set_printing(False)

    def _on_print_stats(self, status: dict, eventtime: float) -> None:
        state = (status.get("print_stats") or {}).get("state")
        if state is None:
            return
        is_printing = state == "printing"
        self._printing = is_printing
        self.core.set_printing(is_printing)

    async def _on_file_uploaded(self, payload: dict) -> None:
        item = payload.get("item") or {}
        root = item.get("root")
        path = item.get("path") or ""
        if root != "gcodes" or not path.lower().endswith(".gcode"):
            return
        if path.startswith(".stitchlab_") or "/" in path.lstrip("./"):
            # Skip our own sidecars and anything inside subdirectories
            # other than the gcodes root — Mainsail's intake-relevant
            # uploads are always at the root.
            return
        await self.core.enqueue_analyze(path)


def load_component(config: ConfigHelper) -> StitchlabIntake:
    return StitchlabIntake(config)
