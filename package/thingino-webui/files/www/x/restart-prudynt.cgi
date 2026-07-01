#!/bin/sh
# Restart prudynt service

# Check authentication
. /var/www/x/auth.sh
require_auth

printf 'Status: 200 OK\r\n'
printf 'Content-Type: application/json\r\n'
printf 'Connection: close\r\n'
printf '\r\n'
printf '{"status":"ok","message":"Prudynt restart initiated"}\n'

# Detach the restart into its own session so it always runs to completion, even
# after httpd tears down this CGI's process group when the connection closes.
# Without setsid the backgrounded restart can be killed mid-reclaim (the ~15s
# window between stop and start), leaving prudynt stopped -> "restart did nothing".
if command -v setsid >/dev/null 2>&1; then
	setsid sh -c 'service restart prudynt >/dev/null 2>&1' </dev/null >/dev/null 2>&1 &
else
	nohup service restart prudynt >/dev/null 2>&1 &
fi
