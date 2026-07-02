#!/bin/sh

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

# Parse QUERY_STRING safely (NO eval). Only d/x/y are recognized; d is
# allow-listed by the case below, x/y are constrained to numeric characters.
# This prevents shell/command injection via the query string.
OLD_IFS=$IFS
IFS='&'
set -f  # disable glob expansion of $QUERY_STRING during the split
for KV in $QUERY_STRING; do
  case "$KV" in
    d=*) d=${KV#d=} ;;
    x=*) x=${KV#x=} ;;
    y=*) y=${KV#y=} ;;
  esac
done
set +f
IFS=$OLD_IFS

[ -z "$x" ] && x=0
[ -z "$y" ] && y=0
[ -z "$d" ] && d="g"

# Coordinates must be numeric only (digits, optional sign/decimal); reject any
# value containing shell metacharacters. d is validated by the case below.
case "$x" in '' | *[!0-9.-]*) x=0 ;; esac
case "$y" in '' | *[!0-9.-]*) y=0 ;; esac

emit_status() {
  local payload
  if ! payload=$(motors -j 2>/dev/null); then
    json_error "motors-status-failed"
  fi
  json_ok "$payload"
}

case "$d" in
  g) motors -d g -x "$x" -y "$y" >/dev/null ;;
  r) motors -r >/dev/null ;;
  h) motors -d h -x "$x" -y "$y" >/dev/null ;;
  s) motors -d s >/dev/null ;;
  b) motors -d b >/dev/null ;;
  i)
    payload=$(motors -i 2>/dev/null) || json_error "motors-initial-failed"
    json_ok "$payload"
    ;;
  j)
    payload=$(motors -j 2>/dev/null) || json_error "motors-status-failed"
    json_ok "$payload"
    ;;
  *)
    json_error "motors-command-unsupported"
    ;;
esac

emit_status
