#!/bin/sh
# Fast, READ-ONLY status channel for the Web UI live bar.
#
# Sources ONLY /run, /proc, and existing state files (the day/night sidecar,
# the physical-privacy state file, prudynt.json via jct). It NEVER calls
# prudyntctl, never talks to the agent, and NEVER writes anything (no flash
# wear, no /tmp cache, no periodic state rewrite). It is polled by the active
# browser page only (~2s) and stops when the tab is hidden/closed, so it adds
# zero background load when nobody is viewing the Web UI.

# Check authentication
. /var/www/x/auth.sh
require_auth

printf 'Status: 200 OK\r\n'
printf 'Content-Type: application/json\r\n'
printf 'Cache-Control: no-store\r\n'
printf 'Connection: close\r\n'
printf '\r\n'

# --- Day/Night: TARGET (auto|day|night) from the sidecar helper; LIVE from the
#     /run file prudynt updates. Mirrors how ha-state/physical-privacy read it. ---
dn_target=$(/usr/sbin/daynight-state get 2>/dev/null)
case "$dn_target" in auto | day | night) ;; *) dn_target=day ;; esac
dn_live=$(awk 'NR==1{print $1}' /run/prudynt/daynight_mode 2>/dev/null)
case "$dn_live" in night) dn_live=night ;; *) dn_live=day ;; esac
if [ "$dn_target" = "auto" ]; then
	dn_enabled=true
	dn_mode="$dn_live"
else
	dn_enabled=false
	dn_mode="$dn_target"
fi

# --- Physical privacy: persisted "active" flag (read-only, we own the format) ---
case "$(grep -o '"active":[a-z]*' /etc/physical-privacy-state.json 2>/dev/null | head -1 | grep -o '[a-z]*$')" in
	true) pp=true ;;
	*) pp=false ;;
esac

# --- Shabbat Ready: motion detection OFF AND day/night FORCED (not auto). A
#     broken/missing sidecar already reads as forced 'day' above (not auto). ---
mo=$(jct /etc/prudynt.json get motion.enabled 2>/dev/null | tr -d '"')
if [ "$mo" = "false" ] && [ "$dn_target" != "auto" ]; then
	shabbat=true
else
	shabbat=false
fi

# --- CPU: 1-minute load average (pure read; on a single-core T23N the load is
#     ~= the CPU fraction, so ~0.25 ~= 25%). No delta/cache needed. ---
load1=$(awk '{print $1}' /proc/loadavg 2>/dev/null)
case "$load1" in '' | *[!0-9.]*) load1=0 ;; esac

# --- RAM: used% from /proc/meminfo (MemAvailable; fallback free+buffers+cached) ---
mem_total=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo 2>/dev/null)
mem_avail=$(awk '/^MemAvailable:/{print $2; exit}' /proc/meminfo 2>/dev/null)
if [ -z "$mem_avail" ]; then
	mem_avail=$(awk '/^MemFree:/{f=$2} /^Buffers:/{b=$2} /^Cached:/{c=$2} END{print f+b+c}' /proc/meminfo 2>/dev/null)
fi
mem_pct=0
case "$mem_total" in
	'' | 0 | *[!0-9]*) ;;
	*) [ -n "$mem_avail" ] && mem_pct=$(((mem_total - mem_avail) * 100 / mem_total)) ;;
esac
[ "$mem_pct" -lt 0 ] 2>/dev/null && mem_pct=0
[ "$mem_pct" -gt 100 ] 2>/dev/null && mem_pct=100

printf '{"daynight_mode":"%s","daynight_enabled":%s,"physical_privacy_active":%s,"shabbat_ready":%s,"cpu_load":"%s","mem_used_pct":%d}\n' \
	"$dn_mode" "$dn_enabled" "$pp" "$shabbat" "$load1" "$mem_pct"
