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

# --- Gain (ISP total gain): file read of /proc/jz/isp/isp-m0 (same source as
#     ha-state). Emit -1 when unreadable so the UI keeps its last value instead
#     of flashing "---". Providing it here also updates gain at the fast cadence. ---
gain=$(awk '/total_gain/{print $NF; exit}' /proc/jz/isp/isp-m0 2>/dev/null)
case "$gain" in '' | *[!0-9-]*) gain=-1 ;; esac

# --- CPU: REAL % from two /proc/stat samples over a short window. Pure reads +
#     a bounded usleep (no cache, no writes). Matches top-style CPU%; on the
#     single-core T23N this is 0..100 (load average was misleading -> could show
#     >100% while actual CPU was ~25%). idle = idle+iowait; busy = total-idle. ---
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

# --- RAM: used% and used/total MB from /proc/meminfo (MemAvailable; fallback
#     free+buffers+cached). Pure reads. ---
mem_total=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo 2>/dev/null)
mem_avail=$(awk '/^MemAvailable:/{print $2; exit}' /proc/meminfo 2>/dev/null)
if [ -z "$mem_avail" ]; then
	mem_avail=$(awk '/^MemFree:/{f=$2} /^Buffers:/{b=$2} /^Cached:/{c=$2} END{print f+b+c}' /proc/meminfo 2>/dev/null)
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

printf '{"daynight_mode":"%s","daynight_enabled":%s,"physical_privacy_active":%s,"shabbat_ready":%s,"total_gain":%d,"cpu_pct":%d,"mem_used_pct":%d,"mem_used_mb":%d,"mem_total_mb":%d,"storage_used_pct":%d,"storage_free_kb":%d}\n' \
	"$dn_mode" "$dn_enabled" "$pp" "$shabbat" "$gain" "$cpu_pct" "$mem_pct" "$mem_used_mb" "$mem_total_mb" "$storage_pct" "$st_free"
