#!/bin/sh
# Health status channel for the Web UI badge: CPU / RAM / storage.
#
# Split off json-status-fast.cgi onto a slower ~7s cadence (these metrics don't
# need 2s freshness and the CPU sample costs a short in-request window). Pure
# READ-ONLY: reads /proc and df only, NEVER writes anything (no flash wear, no
# /tmp cache), NEVER calls prudyntctl or the agent, and runs no background daemon.
# Polled by the active browser page only and stops when the tab is hidden/closed.

# Check authentication
. /var/www/x/auth.sh
require_auth

printf 'Status: 200 OK\r\n'
printf 'Content-Type: application/json\r\n'
printf 'Cache-Control: no-store\r\n'
printf 'Connection: close\r\n'
printf '\r\n'

# --- CPU: REAL % from two /proc/stat samples over a short window. Pure reads +
#     a bounded usleep (no cache, no writes). idle = idle+iowait; busy = total-idle.
#     On the single-core T23N this is 0..100 (load average was misleading). ---
set -- $(awk '/^cpu /{print ($2+$3+$4+$5+$6+$7+$8+$9), ($5+$6); exit}' /proc/stat 2>/dev/null)
cpu_t1=${1:-0}
cpu_i1=${2:-0}
usleep 300000 2>/dev/null || sleep 1
set -- $(awk '/^cpu /{print ($2+$3+$4+$5+$6+$7+$8+$9), ($5+$6); exit}' /proc/stat 2>/dev/null)
cpu_t2=${1:-0}
cpu_i2=${2:-0}
cpu_pct=0
dt=$((cpu_t2 - cpu_t1))
di=$((cpu_i2 - cpu_i1))
[ "$dt" -gt 0 ] && cpu_pct=$(((dt - di) * 100 / dt))
[ "$cpu_pct" -lt 0 ] 2>/dev/null && cpu_pct=0
[ "$cpu_pct" -gt 100 ] 2>/dev/null && cpu_pct=100

# --- RAM: used% and used/total MB from /proc/meminfo. Prefer MemAvailable; this
#     Ingenic kernel lacks it, so fall back to MemFree+Buffers+Cached+SReclaimable
#     (matches busybox `free` used-excluding-cache). Pure reads. ---
mem_total=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo 2>/dev/null)
mem_avail=$(awk '/^MemAvailable:/{print $2; exit}' /proc/meminfo 2>/dev/null)
if [ -z "$mem_avail" ]; then
	mem_avail=$(awk '/^MemFree:/{f=$2} /^Buffers:/{b=$2} /^Cached:/{c=$2} /^SReclaimable:/{s=$2} END{print f+b+c+s}' /proc/meminfo 2>/dev/null)
fi
mem_pct=0
mem_total_mb=0
mem_used_mb=0
case "$mem_total" in
	'' | 0 | *[!0-9]*) ;;
	*)
		mem_total_mb=$((mem_total / 1024))
		if [ -n "$mem_avail" ]; then
			mem_pct=$(((mem_total - mem_avail) * 100 / mem_total))
			mem_used_mb=$(((mem_total - mem_avail) / 1024))
		fi
		;;
esac
[ "$mem_pct" -lt 0 ] 2>/dev/null && mem_pct=0
[ "$mem_pct" -gt 100 ] 2>/dev/null && mem_pct=100

# --- Storage: writable config overlay (df /, read-only). This small jffs2
#     overlay holds /etc config writes and is the partition that fills up and
#     breaks config on this device, so it is the most health-relevant storage.
#     Emit used% + free KB; the UI renders KB/MB/GB adaptively. ---
set -- $(df 2>/dev/null | awk '$NF == "/" {print $2, $3, $4; exit}')
st_total=${1:-0}
st_used=${2:-0}
st_free=${3:-0}
storage_pct=0
case "$st_total" in
	'' | 0 | *[!0-9]*) ;;
	*) storage_pct=$((st_used * 100 / st_total)) ;;
esac
[ "$storage_pct" -lt 0 ] 2>/dev/null && storage_pct=0
[ "$storage_pct" -gt 100 ] 2>/dev/null && storage_pct=100
case "$st_free" in '' | *[!0-9]*) st_free=0 ;; esac

# --- Clock: the CAMERA's own system time (epoch seconds), emitted so the Web UI
#     date/hour paints from this fast local channel instead of waiting on the agent
#     SSE. This is the device's authoritative time (NOT a browser clock); the UI
#     applies the configured timezone via resolveDeviceTimezone(). Emitted as a JSON
#     number, or null if unavailable (the reducer then keeps its last value). ---
time_now=$(date +%s 2>/dev/null)
case "$time_now" in '' | *[!0-9]*) time_now=null ;; esac

printf '{"time_now":%s,"cpu_pct":%d,"mem_used_pct":%d,"mem_used_mb":%d,"mem_total_mb":%d,"storage_used_pct":%d,"storage_free_kb":%d}\n' \
	"$time_now" "$cpu_pct" "$mem_pct" "$mem_used_mb" "$mem_total_mb" "$storage_pct" "$st_free"
