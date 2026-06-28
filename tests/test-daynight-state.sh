#!/bin/sh
# test-daynight-state.sh - offline unit tests for the day/night minimal-write set.
#
# Exercises the REAL /usr/sbin/daynight-state helper (paths redirected to a
# sandbox) plus a faithful shell replica of prudynt's CFG::load() sidecar parser
# (patch 0015), covering the four required scenarios:
#   1) missing sidecar upgrade migration
#   2) corrupt/invalid sidecar => auto
#   3) valid sidecar + corrupt prudynt.json => sidecar wins
#   4) S56ircut matching prudynt from boot (both readers agree for every input)
# plus set write-on-change/validation/atomicity and pid-aware orphan reclaim.
# Run:  sh tests/test-daynight-state.sh    (POSIX sh + coreutils only)
set -u

HELPER_SRC="${HELPER_SRC:-overlay/usr/sbin/daynight-state}"
[ -f "$HELPER_SRC" ] || HELPER_SRC="$(dirname "$0")/../overlay/usr/sbin/daynight-state"
[ -f "$HELPER_SRC" ] || { echo "cannot find daynight-state helper (set HELPER_SRC)"; exit 2; }

SB=$(mktemp -d); trap 'rm -rf "$SB"' EXIT
STATE="$SB/daynight.state"; PJSON="$SB/prudynt.json"

# stub jct: emulates `jct <file> get daynight.{enabled,force_mode}`; a file with
# no "daynight" object (our corrupt case) yields empty + exit 1, like real jct.
cat > "$SB/jct" <<'JCT'
#!/bin/sh
f="$1"; op="$2"; key="$3"
[ "$op" = get ] || exit 1
grep -q '"daynight"' "$f" 2>/dev/null || exit 1
case "$key" in
  daynight.enabled)    v=$(grep -o '"enabled":[a-z]*' "$f" | head -1 | cut -d: -f2) ;;
  daynight.force_mode) v=$(grep -o '"force_mode":"[a-z]*"' "$f" | head -1 | sed 's/.*:"//;s/"//') ;;
  *) v="" ;;
esac
[ -n "$v" ] && printf '%s\n' "$v" || exit 1
JCT
chmod +x "$SB/jct"

HELPER="$SB/daynight-state"
sed -e "s#^STATE=.*#STATE=\"$STATE\"#" \
    -e "s#^PRUDYNT_CONFIG=.*#PRUDYNT_CONFIG=\"$PJSON\"#" \
    -e "s#^JCT=.*#JCT=\"$SB/jct\"#" \
    "$HELPER_SRC" > "$HELPER"
chmod +x "$HELPER"

# Faithful shell replica of prudynt CFG::load() 0015 parse (src/Config.cpp):
# find "mode=", skip leading quotes/space, take the [a-z] run, match day/night
# else auto. MUST stay equivalent to the C++ so prudynt and the rootfs agree.
prudynt_parse() {
  m=""
  if [ -f "$1" ]; then
    line=$(grep -m1 'mode=' "$1" 2>/dev/null)
    if [ -n "$line" ]; then
      v=${line#*mode=}
      v=$(printf '%s' "$v" | sed 's/^["[:space:]]*//')
      m=$(printf '%s' "$v" | grep -o '^[a-z]*')
    fi
  fi
  case "$m" in day) echo day ;; night) echo night ;; *) echo auto ;; esac
}
s56_night() { [ "$("$HELPER" get)" = night ] && echo yes || echo no; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  PASS: %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL: %s (got=[%s] want=[%s])\n' "$1" "$2" "$3"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2" "$3"; }
wr()  { printf '%s' "$1" > "$STATE"; }
rmstate() { rm -f "$STATE"; }

echo "== Scenario 1: missing sidecar upgrade migration =="
rmstate; printf '{"daynight":{"enabled":false,"force_mode":"night"}}\n' > "$PJSON"
"$HELPER" migrate
eq "migrate(forced-night) -> sidecar=night" "$(cat "$STATE" 2>/dev/null)" "mode=night"
eq "  helper get == night" "$("$HELPER" get)" "night"
eq "  prudynt parse == night" "$(prudynt_parse "$STATE")" "night"
rmstate; printf '{"daynight":{"enabled":true,"force_mode":""}}\n' > "$PJSON"
"$HELPER" migrate; eq "migrate(auto) -> auto" "$("$HELPER" get)" "auto"
rmstate; printf '{"image":{"isp_bypass":true}}\n' > "$PJSON"
"$HELPER" migrate; eq "migrate(no daynight keys) -> auto" "$("$HELPER" get)" "auto"

echo "== Scenario 2: corrupt/invalid sidecar => auto =="
wr 'this is garbage not our format'; eq "garbage -> get auto" "$("$HELPER" get)" "auto"
eq "garbage -> prudynt auto" "$(prudynt_parse "$STATE")" "auto"
wr ''; eq "empty -> get auto" "$("$HELPER" get)" "auto"
wr 'mode='; eq "mode= (no value) -> get auto" "$("$HELPER" get)" "auto"
wr 'mode=bogus'; eq "mode=bogus -> get auto" "$("$HELPER" get)" "auto"

echo "== Scenario 3: valid sidecar + corrupt prudynt.json => sidecar wins =="
"$HELPER" set night >/dev/null; printf '{{{ truncated garbage' > "$PJSON"
"$HELPER" migrate
eq "sidecar stays night despite corrupt prudynt.json" "$("$HELPER" get)" "night"
eq "prudynt parse == night (sidecar only)" "$(prudynt_parse "$STATE")" "night"

echo "== Scenario 4: S56ircut matches prudynt for every input =="
for m in auto day night; do
  "$HELPER" set "$m" >/dev/null
  eq "$m: get==parse" "$("$HELPER" get)" "$(prudynt_parse "$STATE")"
done
"$HELPER" set night >/dev/null; eq "night: s56==yes" "$(s56_night)" "yes"
"$HELPER" set day  >/dev/null; eq "day: s56==no"  "$(s56_night)" "no"
printf 'mode=night\r\n' > "$STATE"
eq "CRLF night: get==parse" "$("$HELPER" get)" "$(prudynt_parse "$STATE")"
eq "CRLF night: parse==night" "$(prudynt_parse "$STATE")" "night"
printf 'mode="night"\n' > "$STATE"
eq "quoted night: get==parse" "$("$HELPER" get)" "$(prudynt_parse "$STATE")"
eq "quoted night: parse==night" "$(prudynt_parse "$STATE")" "night"
rmstate
eq "missing: get==parse" "$("$HELPER" get)" "$(prudynt_parse "$STATE")"
eq "missing: get==auto"  "$("$HELPER" get)" "auto"

echo "== set semantics: write-on-change + validation + atomicity =="
rmstate
"$HELPER" set day; rc_first=$?
"$HELPER" set day; rc_same=$?
eq "set day (changed) rc==0"   "$rc_first" "0"
eq "set day (unchanged) rc==1" "$rc_same"  "1"
"$HELPER" set bogus 2>/dev/null; eq "set bogus rc==2" "$?" "2"
eq "file is exactly 'mode=day'" "$(cat "$STATE")" "mode=day"
eq "no leftover .tmp after set" "$(find "$SB" -name 'daynight.state.tmp.*' 2>/dev/null | wc -l | tr -d ' ')" "0"

echo "== migrate pid-aware orphan reclaim =="
"$HELPER" set night >/dev/null
: > "$STATE.tmp.999999"; : > "$STATE.tmp.$$"
"$HELPER" migrate
[ -e "$STATE.tmp.999999" ] && bad "dead-pid orphan removed" "present" "removed" || ok "dead-pid orphan removed"
[ -e "$STATE.tmp.$$" ] && ok "live-pid temp kept" || bad "live-pid temp kept" "removed" "kept"
rm -f "$STATE.tmp.$$"

echo; echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
