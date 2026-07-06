#!/bin/sh
# Cheap, READ-ONLY live day/night channel for the Web UI (gain + brightness only).
#
# The 2s fast channel (json-status-fast.cgi) is deliberately prudyntctl-free and
# cannot emit total_gain (no /proc/jz/isp field exposes it). The full agent SSE
# heartbeat DOES carry gain but is heavy (~650ms) and now streams on a slow ~15s
# cadence. This channel bridges the gap: it emits ONLY the two fast-changing
# light-sensor values via a single cheap prudyntctl query (~0-30ms), polled ~5s
# by the active page and stopped when the tab is hidden/closed. It NEVER writes
# anything (no flash wear, no /tmp cache) and talks only to the running prudynt.
#
# total_gain          <- prudyntctl json {"daynight":{"status":null}} .total_gain
# daynight_brightness <- same query .brightness_percent (the value prudynt also
#                        writes to /run/prudynt/daynight_brightness)

# Check authentication
. /var/www/x/auth.sh
require_auth

printf 'Status: 200 OK\r\n'
printf 'Content-Type: application/json\r\n'
printf 'Cache-Control: no-store\r\n'
printf 'Connection: close\r\n'
printf '\r\n'

# Single in-memory query; no temp file, no writes. If prudynt is down/restarting
# the output is empty -> both values fall back to null and the UI keeps its last
# value (updateHeartbeatUi guards on hasTotalGain / hasBrightness).
dn=$(prudyntctl json '{"daynight":{"status":null}}' 2>/dev/null)
gain=$(printf '%s' "$dn" | grep -o '"total_gain":[0-9-]*' | head -n 1 | cut -d: -f2)
bright=$(printf '%s' "$dn" | grep -o '"brightness_percent":[0-9-]*' | head -n 1 | cut -d: -f2)

printf '{"total_gain":%s,"daynight_brightness":%s}\n' "${gain:-null}" "${bright:-null}"
