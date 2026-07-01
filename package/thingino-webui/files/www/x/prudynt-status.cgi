#!/bin/sh
# Lightweight prudynt liveness probe for the Web UI restart flow.
# Read-only: just pidof. Used by footer.js to detect when a restarted prudynt
# has actually come back (new pid) before reloading the page.

# Check authentication
. /var/www/x/auth.sh
require_auth

printf 'Status: 200 OK\r\n'
printf 'Content-Type: application/json\r\n'
printf 'Cache-Control: no-store\r\n'
printf 'Connection: close\r\n'
printf '\r\n'

pid=$(pidof prudynt 2>/dev/null | awk '{print $1}')
if [ -n "$pid" ]; then
	printf '{"alive":true,"pid":"%s"}\n' "$pid"
else
	printf '{"alive":false,"pid":""}\n'
fi
