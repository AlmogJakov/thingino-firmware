#!/bin/sh

# SSE heartbeat proxied from Thingino agent

. /var/www/x/auth.sh
require_auth

THINGINO_CONFIG="${THINGINO_CONFIG:-/etc/thingino.json}"

agent_port() {
	port=$(jct "$THINGINO_CONFIG" get agent.port 2>/dev/null | tr -d '\n"')
	case "$port" in
		'' | *[!0-9]*) port=1998 ;;
		0) port=1998 ;;
	esac
	printf '%s' "$port"
}

AGENT_URL="http://127.0.0.1:$(agent_port)"
HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-15}"
HEARTBEAT_RETRY_MS="${HEARTBEAT_RETRY_MS:-5000}"
# Emit a lightweight SSE keepalive comment every KEEPALIVE seconds so uhttpd's
# network timeout (-T) never trips on the idle gap and a client disconnect is
# detected within ~KEEPALIVE s (SIGPIPE on the ping), even though the expensive
# agent heartbeat only runs every HEARTBEAT_INTERVAL s.
HEARTBEAT_KEEPALIVE="${HEARTBEAT_KEEPALIVE:-5}"

http_200() { printf 'Status: 200 OK\r\n'; }

send_headers() {
	http_200
	printf 'Content-Type: text/event-stream\r\n'
	printf 'Cache-Control: no-cache\r\n\r\n'
}

trap 'exit 0' INT TERM PIPE HUP
send_headers

while true; do
	printf 'retry: %d\n' "$HEARTBEAT_RETRY_MS" || exit 0
	data=$(curl -sS --max-time 2 "$AGENT_URL/api/v1/runtime/heartbeat" 2>/dev/null)
	if [ -n "$data" ]; then
		printf 'data: %s\n\n' "$data" || exit 0
	else
		printf 'data: {"error":"Heartbeat daemon not running"}\n\n' || exit 0
	fi
	# Interruptible, keepalive-sliced wait: a trapped signal breaks the wait at
	# once; between slices a ":" SSE comment (ignored by EventSource) keeps the
	# connection alive and surfaces a client disconnect promptly via SIGPIPE.
	_waited=0
	while [ "$_waited" -lt "$HEARTBEAT_INTERVAL" ]; do
		sleep "$HEARTBEAT_KEEPALIVE" & wait $! || exit 0
		_waited=$((_waited + HEARTBEAT_KEEPALIVE))
		[ "$_waited" -lt "$HEARTBEAT_INTERVAL" ] && { echo ":" || exit 0; }
	done
done
