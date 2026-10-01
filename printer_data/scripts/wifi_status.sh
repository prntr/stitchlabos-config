#!/bin/bash
# WiFi Status JSON Output for Moonraker API
#
# The active connection is selected by UUID. By name, two profiles of one
# name printed both values into one JSON string, which Moonraker then
# rejected (commissioning run of 2026-09-28).

# A JSON string: backslash and quote escaped, control characters dropped.
json_str() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '"%s"' "$(printf '%s' "$s" | tr -d '\000-\037')"
}

# One field of the connection with UUID $1, unescaped.
field() {
    nmcli -e no -g "$2" connection show uuid "$1" 2>/dev/null
}

active_uuid=$(nmcli -t -f UUID,DEVICE connection show --active 2>/dev/null \
    | awk -F: '$2 == "wlan0" { print $1; exit }')

active_conn=""
ssid=""
ip_addr=""
is_ap="false"
if [ -n "$active_uuid" ]; then
    active_conn=$(field "$active_uuid" connection.id)
    ssid=$(field "$active_uuid" 802-11-wireless.ssid)
    [ "$(field "$active_uuid" 802-11-wireless.mode)" = "ap" ] && is_ap="true"
    ip_addr=$(field "$active_uuid" IP4.ADDRESS | head -n 1 | cut -d/ -f1)
fi

signal=0
if [ "$is_ap" = "false" ] && [ -n "$active_uuid" ]; then
    signal=$(nmcli -f IN-USE,SIGNAL device wifi 2>/dev/null | grep "^\*" | awk '{print $2}')
    signal=${signal:-0}
fi

wifi_enabled=$(nmcli radio wifi)
[ "$wifi_enabled" = "enabled" ] && wifi_enabled="true" || wifi_enabled="false"

timer_active=$(systemctl is-active AccessPopup.timer 2>/dev/null)
[ "$timer_active" = "active" ] && timer_active="true" || timer_active="false"

status="disconnected"
if [ -n "$active_uuid" ]; then
    [ "$is_ap" = "true" ] && status="ap_mode" || status="connected"
fi

cat <<JSON
{
  "status": "$status",
  "connection": {
    "name": $(json_str "${active_conn:-null}"),
    "ssid": $(json_str "${ssid:-null}"),
    "ip": $(json_str "${ip_addr:-null}"),
    "type": "wifi",
    "signal": ${signal},
    "is_ap": ${is_ap}
  },
  "wifi_enabled": ${wifi_enabled},
  "timer_active": ${timer_active}
}
JSON
