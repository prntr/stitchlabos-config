# StitchLabOS Config

Runtime configuration files for [StitchLabOS](https://github.com/prntr/StitchlabOS) — a Raspberry Pi OS image that turns a Klipper-based machine into a computerized embroidery system.

This repo is deployed to `~/stitchlabos-config` on the Pi and updated OTA via Moonraker's update manager.

## Contents

```
moonraker/components/
  wifi_manager.py        Moonraker component — WiFi API endpoints via NetworkManager

printer_data/config/
  embroidery_macros.cfg  Klipper macros: NEEDLE_TOGGLE, STITCH, LOCK_STITCH, EMBROIDERY_HOME, etc.

printer_data/scripts/
  wifi_status.sh         JSON output: current connection, signal, AP mode
  wifi_scan.sh           JSON output: visible networks with signal strength
  wifi_profiles.sh       JSON output: saved WiFi/AP profiles
```

### WiFi Manager

`wifi_manager.py` is a custom Moonraker component that exposes REST/WebSocket endpoints for WiFi management. It calls the shell scripts via Moonraker's `shell_command` facility, which in turn wrap `nmcli`. The Mainsail UI (StitchLab fork) uses these endpoints to provide a WiFi settings panel.

### Embroidery Macros

`embroidery_macros.cfg` provides Klipper G-code macros for needle control. The Z axis drives the needle via a handwheel — 1 full rotation (5mm) = 1 complete stitch cycle.

Key macros:
- `NEEDLE_TOGGLE` — move needle between UP (0deg) and DOWN (180deg) for maintenance
- `STITCH` — one full rotation without changing the logical Z position
- `LOCK_STITCH` — multiple rotations in place (default 3) to secure thread
- `EMBROIDERY_HOME` — home XY then Z, move to center
- `EMBROIDERY_STATUS` — print current needle state and stitch count

## Deployment

On a StitchLabOS image, files are symlinked to their expected locations:

```
~/stitchlabos-config/moonraker/components/wifi_manager.py
  -> ~/moonraker/moonraker/components/wifi_manager.py

~/stitchlabos-config/printer_data/config/embroidery_macros.cfg
  -> ~/printer_data/config/embroidery_macros.cfg

~/stitchlabos-config/printer_data/scripts/wifi_*.sh
  -> ~/printer_data/scripts/wifi_*.sh
```

The symlink into `~/moonraker/` is excluded from Moonraker's git tracking via `.git/info/exclude`.

## OTA Updates

Moonraker is configured to pull updates from this repo:

```ini
[update_manager stitchlabos]
type: git_repo
path: ~/stitchlabos-config
origin: https://github.com/prntr/stitchlabos-config.git
primary_branch: main
managed_services: moonraker
```

Updates appear in the Mainsail update panel. After update, Moonraker restarts to pick up any changes to `wifi_manager.py`.

## License

GPLv3 — see individual file headers.
