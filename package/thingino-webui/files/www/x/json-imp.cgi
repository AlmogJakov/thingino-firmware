#!/bin/sh
# shellcheck disable=SC2039

# Check authentication
. /var/www/x/auth.sh
require_auth

http_200() {
  printf 'Status: 200 OK\r\n'
}

http_400() {
  printf 'Status: 400 Bad Request\r\n'
}

http_412() {
  printf 'Status: 412 Precondition Failed\r\n'
}

json_header() {
  printf 'Content-Type: application/json\r\n'
  printf 'Pragma: no-cache\r\n'
  printf 'Expires: %s\r\n' "$(TZ=GMT0 date +'%a, %d %b %Y %T %Z')"
  printf 'Etag: "%s"\r\n' "$(cat /proc/sys/kernel/random/uuid)"
  printf 'Connection: close\r\n'
  printf '\r\n'
}

json_error() {
  http_412
  json_header
  printf '{"error":{"code":412,"message":"%s"}}
' "$1"
  exit 0
}

json_ok() {
  http_200
  json_header
  if [ "{" = "${1:0:1}" ]; then
    printf '{"code":200,"result":"success","message":%s}
' "$1"
  else
    printf '{"code":200,"result":"success","message":"%s"}
' "$1"
  fi
  exit 0
}

bad_request() {
  http_400
  echo
  echo "$1"
  exit 1
}

# Read POST data
read -r POST_DATA

# Parse JSON (supports quoted or numeric val)
cmd=$(printf '%s' "$POST_DATA" | awk -F'"' '/"cmd"/{for(i=1;i<=NF;i++){if($i=="cmd"){print $(i+2); exit}}}')
val=$(printf '%s' "$POST_DATA" | sed -n 's/.*"val"[[:space:]]*:[[:space:]]*"\{0,1\}\([^",}]*\).*/\1/p')

[ -z "$cmd" ] && bad_request "missing required parameter cmd"
[ -z "$val" ] && bad_request "missing required parameter val"

# Physical-privacy interlock: a manual day/night-class change (auto / day-night /
# color / ircut) while physical privacy is on means "cancel privacy" — privacy is
# turned off and THIS command is dropped (re-issue it afterwards). We do NOT exit
# here: we record the drop, SKIP applying the command, then still read back and
# return the true (unchanged) state below so the UI corrects immediately instead
# of showing the optimistic value until the next heartbeat. Mirrors the MQTT path
# (ha-commands); ir850/ir940/white are NOT interlocked (they go through `light`,
# whose guard already refuses any IR/white turn-on while privacy is armed — a
# no-op the read-back below also captures).
privacy_msg=""
case "$cmd" in
  auto | daynight | color | ircut)
    if [ -x /sbin/physical-privacy ] && ! /sbin/physical-privacy guard; then
      privacy_msg="physical privacy was active and has been cancelled; re-issue your command"
    fi
    ;;
esac

if [ -z "$privacy_msg" ]; then
case "$cmd" in
  auto)
    # Delegate to the agent so runtime AND persisted config stay in sync.
    case "$val" in
      1 | true | on)
        thingino-agentctl daynight auto >/dev/null 2>&1 \
          || json_error "failed to enable auto daynight"
        ;;
      0 | false | off)
        # Turning auto off: lock to the mode the camera is currently showing.
        running=$(echo '{"image":{"running_mode":null}}' | prudyntctl json - 2>/dev/null \
          | grep -o '"running_mode":[^,}]*' | head -n 1 | cut -d: -f2 | tr -d ' "')
        case "$running" in
          1) mode=night ;;
          *) mode=day ;;
        esac
        thingino-agentctl daynight "$mode" >/dev/null 2>&1 \
          || json_error "failed to disable auto daynight"
        ;;
      *)
        json_error "invalid auto value"
        ;;
    esac
    ;;
  color)
    echo "{\"daynight\":{\"enabled\":false},\"image\":{\"running_mode\": $val}}" | prudyntctl json - >/dev/null 2>&1
    ;;
  daynight)
    # Delegate to the agent: persists config, syncs runtime, and physically
    # switches via the configured day/night executor (/sbin/daynight).
    case "$val" in
      day | night)
        thingino-agentctl daynight "$val" >/dev/null 2>&1 \
          || json_error "failed to switch daynight mode"
        ;;
      *)
        json_error "invalid daynight mode"
        ;;
    esac
    ;;
  ir850 | ir940)
    echo '{"daynight":{"enabled":false}}' | prudyntctl json - >/dev/null 2>&1
    light $cmd $val
    ;;
  white)
    # Plain visible floodlight — NOT day/night-managed (daynight.controls.white=
    # false), so prudynt never touches it. Do not disable auto; just toggle the
    # GPIO so turning the white LED on/off changes nothing else.
    light $cmd $val
    ;;
  ircut)
    echo '{"daynight":{"enabled":false}}' | prudyntctl json - >/dev/null 2>&1
    ircut $val >/dev/null
    ;;
esac
fi

# Read back the true post-command state for the GPIO/ISP toggles so the UI renders
# authoritative state (not just the optimistic value) — correcting a privacy-
# cancelled, light-guard-refused, failed, or externally-changed toggle immediately
# instead of waiting for the ~15s SSE heartbeat. Mapping mirrors the agent
# heartbeat: raw numeric `<cmd> read` (else null); color_mode = image.running_mode.
imp_num() { case "$1" in '' | *[!0-9]*) printf 'null' ;; *) printf '%s' "$1" ;; esac; }
state=""
case "$cmd" in
  white | ir850 | ir940)
    state="{\"${cmd}_state\":$(imp_num "$(light "$cmd" read 2>/dev/null)")}"
    ;;
  ircut)
    state="{\"ircut_state\":$(imp_num "$(ircut read 2>/dev/null)")}"
    ;;
  color)
    rm=$(echo '{"image":{"running_mode":null}}' | prudyntctl json - 2>/dev/null \
      | grep -o '"running_mode":[^,}]*' | head -n 1 | cut -d: -f2 | tr -d ' "')
    state="{\"color_mode\":$(imp_num "$rm")}"
    ;;
esac

# Other state (day/night, privacy, ...) is refreshed by the fast + SSE channels.
http_200
json_header
if [ -n "$state" ]; then
  printf '{"code":200,"result":"success","state":%s,"message":"%s"}\n' "$state" "$privacy_msg"
else
  printf '{"code":200,"result":"success","message":"%s"}\n' "$privacy_msg"
fi
exit 0
