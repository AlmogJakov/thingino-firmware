#!/bin/sh
# Fast, READ-ONLY status channel for the Web UI live bar (UI state only).
#
# Emits the fast-changing UI state: day/night mode, physical-privacy, Shabbat
# readiness, and ISP gain. CPU / RAM / storage were split out to the slower
# json-status-health.cgi (~7s) so this 2s channel does pure instantaneous reads
# with NO in-request sleep. Sources ONLY /run, /proc, and existing state files
# (the day/night sidecar, the physical-privacy state file, prudynt.json via jct).
# It NEVER calls prudyntctl, never talks to the agent, and NEVER writes anything
# (no flash wear, no /tmp cache, no periodic state rewrite). Polled by the active
# browser page only (~2s) and stops when the tab is hidden/closed.

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

# --- Gain is NOT emitted here: /proc/jz/isp/isp-m0 has no "total_gain" field, so
#     the real value comes from the agent heartbeat (prudyntctl daynight.status). ---
printf '{"daynight_mode":"%s","daynight_enabled":%s,"physical_privacy_active":%s,"shabbat_ready":%s}\n' \
	"$dn_mode" "$dn_enabled" "$pp" "$shabbat"
