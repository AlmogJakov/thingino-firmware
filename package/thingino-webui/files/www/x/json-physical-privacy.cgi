#!/bin/sh
# Web UI physical-privacy control.
#
# GET ?state=on|off  -> triggers /sbin/physical-privacy on|off (the script arms
#                       the guard + detaches the lens move itself). Detached with
#                       setsid so it always completes even after this CGI closes.
# GET (no/other arg) -> returns the current persisted "active" flag (read-only).
#
# Query is matched with a plain case (NO eval of user input).

# Check authentication
. /var/www/x/auth.sh
require_auth

# Detach the lens move into its own session so it completes even after this CGI
# closes (setsid, with a nohup fallback if a future image lacks setsid).
run_privacy() {
	if command -v setsid >/dev/null 2>&1; then
		setsid /sbin/physical-privacy "$1" </dev/null >/dev/null 2>&1 &
	else
		nohup /sbin/physical-privacy "$1" >/dev/null 2>&1 &
	fi
}

printf 'Status: 200 OK\r\n'
printf 'Content-Type: application/json\r\n'
printf 'Cache-Control: no-store\r\n'
printf 'Connection: close\r\n'
printf '\r\n'

case "$QUERY_STRING" in
	*state=on*)
		run_privacy on
		printf '{"active":true}\n'
		;;
	*state=off*)
		run_privacy off
		printf '{"active":false}\n'
		;;
	*)
		case "$(grep -o '"active":[a-z]*' /etc/physical-privacy-state.json 2>/dev/null | head -1 | grep -o '[a-z]*$')" in
			true) printf '{"active":true}\n' ;;
			*) printf '{"active":false}\n' ;;
		esac
		;;
esac
