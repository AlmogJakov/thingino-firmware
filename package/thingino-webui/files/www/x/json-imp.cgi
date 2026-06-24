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
# turned off and THIS command is dropped (re-issue it afterwards). If privacy is
# mid transition, the change is dropped with no effect. json_ok exits the script.
# Mirrors the MQTT path (ha-commands): ircut joins the interlock; ir850/ir940/white
# are NOT interlocked here because they go through `light`, whose guard already
# refuses any IR/white turn-on while privacy is armed (so no extra cancel needed).
case "$cmd" in
  auto | daynight | color | ircut)
    if [ -x /sbin/physical-privacy ] && ! /sbin/physical-privacy guard; then
      json_ok "physical privacy was active and has been cancelled; re-issue your command"
    fi
    ;;
esac

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

# All state data is provided by heartbeat, no need to build payload here
json_ok
