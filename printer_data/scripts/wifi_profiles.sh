#!/bin/bash
# WiFi Saved Profiles JSON Output
#
# Profiles are read by UUID. By name, two profiles of one name printed both
# values into one JSON string (commissioning run of 2026-09-28), and an SSID
# with a space was cut at the space.

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

echo '{"profiles": ['
first=true

nmcli -t -f AUTOCONNECT-PRIORITY,UUID,TYPE connection show 2>/dev/null | sort -t: -k1,1nr | while IFS=: read -r priority uuid type; do
    [ -z "$uuid" ] && continue
    [ "$type" != "802-11-wireless" ] && continue

    name=$(field "$uuid" connection.id)
    mode=$(field "$uuid" 802-11-wireless.mode)
    mode=${mode:-infrastructure}

    profile_type="wifi"
    [ "$mode" = "ap" ] && profile_type="ap"

    ssid=$(field "$uuid" 802-11-wireless.ssid)

    autoconnect_bool="false"
    [ "$(field "$uuid" connection.autoconnect)" = "yes" ] && autoconnect_bool="true"

    if [ "$first" = true ]; then
        first=false
    else
        echo ","
    fi

    echo "  {\"name\": $(json_str "$name"), \"uuid\": $(json_str "$uuid"), \"type\": \"$profile_type\", \"ssid\": $(json_str "$ssid"), \"autoconnect\": ${autoconnect_bool}, \"priority\": ${priority:-0}}"
done

echo ']}'
