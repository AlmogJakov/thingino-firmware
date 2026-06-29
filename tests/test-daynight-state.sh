#!/bin/sh
# test-daynight-state.sh - offline unit tests for the day/night minimal-write set.
#
# Exercises the REAL /usr/sbin/daynight-state helper (paths redirected to a
# sandbox). prudynt reads the sidecar DIRECTLY at config-load (patch 0015 overlays
# CFG::load to open/read /etc/daynight.state); S31prudynt runs only `migrate` and
# NO LONGER syncs the sidecar into prudynt.json. (The 0015 startup crash that once
# forced the rootfs-sync workaround was a miscompiled libuclibcshim.so, since fixed
# by pinning the proven-good shim -- not 0015 itself.) These tests cover the HELPER:
#   1) missing-sidecar upgrade migration (seed from prudynt.json once)
#   2) corrupt/invalid sidecar => auto
#   3) set: write-on-change / validation / atomicity
#   4) sync-to-config mapping -- LEGACY/UNWIRED verb (off the boot path under 0015),
#      kept as a rollback aid; tested here so the escape hatch still works
#   5) pid-aware orphan reclaim
# Run:  sh tests/test-daynight-state.sh    (POSIX sh + coreutils only)
set -u

HELPER_SRC="${HELPER_SRC:-overlay/usr/sbin/daynight-state}"
[ -f "$HELPER_SRC" ] || HELPER_SRC="$(dirname "$0")/../overlay/usr/sbin/daynight-state"
[ -f "$HELPER_SRC" ] || { echo "cannot find daynight-state helper (set HELPER_SRC)"; exit 2; }

SB=$(mktemp -d); trap 'rm -rf "$SB"' EXIT
STATE="$SB/daynight.state"; PJSON="$SB/prudynt.json"
JCTLOG="$SB/jct.log"

# stub jct: emulates `jct <file> get daynight.{enabled,force_mode}` for reads and
# RECORDS `jct <file> set <key> <value>` calls (for the sync-to-config mapping
# checks). A file with no "daynight" object (our corrupt case) yields empty +
# exit 1 on get, like real jct.
cat > "$SB/jct" <<'JCT'
#!/bin/sh
JCTLOG="$(dirname "$0")/jct.log"   # set calls are recorded here for the tests
f="$1"; op="$2"; key="$3"; val="${4:-}"
case "$op" in
  get)
    grep -q '"daynight"' "$f" 2>/dev/null || exit 1
    case "$key" in
      daynight.enabled)    v=$(grep -o '"enabled":[a-z]*' "$f" | head -1 | cut -d: -f2) ;;
      daynight.force_mode) v=$(grep -o '"force_mode":"[a-z]*"' "$f" | head -1 | sed 's/.*:"//;s/"//') ;;
      *) v="" ;;
    esac
    [ -n "$v" ] && printf '%s\n' "$v" || exit 1
    ;;
  set)
    printf 'set %s=%s\n' "$key" "$val" >> "$JCTLOG"
    ;;
  *) exit 1 ;;
esac
JCT
chmod +x "$SB/jct"

HELPER="$SB/daynight-state"
sed -e "s#^STATE=.*#STATE=\"$STATE\"#" \
    -e "s#^PRUDYNT_CONFIG=.*#PRUDYNT_CONFIG=\"$PJSON\"#" \
    -e "s#^JCT=.*#JCT=\"$SB/jct\"#" \
    "$HELPER_SRC" > "$HELPER"
chmod +x "$HELPER"

# The helper's `get` parse is just the sidecar parser (NOT a mirror of any prudynt
# read path - prudynt does not read the sidecar). Kept here only as an oracle for
# the parse-tolerance cases below.
sidecar_parse() {
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
rmstate; printf '{"daynight":{"enabled":true,"force_mode":""}}\n' > "$PJSON"
"$HELPER" migrate; eq "migrate(auto) -> auto" "$("$HELPER" get)" "auto"
rmstate; printf '{"image":{"isp_bypass":true}}\n' > "$PJSON"
"$HELPER" migrate; eq "migrate(no daynight keys) -> auto" "$("$HELPER" get)" "auto"
# migrate seeds ONLY when the sidecar is missing (first boot after upgrade)
"$HELPER" set night >/dev/null
printf '{"daynight":{"enabled":true,"force_mode":""}}\n' > "$PJSON"
"$HELPER" migrate
eq "migrate is a no-op when sidecar already present" "$("$HELPER" get)" "night"

echo "== Scenario 2: corrupt/invalid sidecar => auto =="
wr 'this is garbage not our format'; eq "garbage -> get auto" "$("$HELPER" get)" "auto"
eq "garbage -> parse auto" "$(sidecar_parse "$STATE")" "auto"
wr ''; eq "empty -> get auto" "$("$HELPER" get)" "auto"
wr 'mode='; eq "mode= (no value) -> get auto" "$("$HELPER" get)" "auto"
wr 'mode=bogus'; eq "mode=bogus -> get auto" "$("$HELPER" get)" "auto"

echo "== Scenario 3: get parse tolerance (CRLF / quotes / missing) =="
printf 'mode=night\r\n' > "$STATE"
eq "CRLF night: get==night" "$("$HELPER" get)" "night"
printf 'mode="night"\n' > "$STATE"
eq "quoted night: get==night" "$("$HELPER" get)" "night"
rmstate
eq "missing: get==auto" "$("$HELPER" get)" "auto"

echo "== set semantics: write-on-change + validation + atomicity =="
rmstate
"$HELPER" set day; rc_first=$?
"$HELPER" set day; rc_same=$?
eq "set day (changed) rc==0"   "$rc_first" "0"
eq "set day (unchanged) rc==1" "$rc_same"  "1"
"$HELPER" set bogus 2>/dev/null; eq "set bogus rc==2" "$?" "2"
eq "file is exactly 'mode=day'" "$(cat "$STATE")" "mode=day"
eq "no leftover .tmp after set" "$(find "$SB" -name 'daynight.state.tmp.*' 2>/dev/null | wc -l | tr -d ' ')" "0"

echo "== sync-to-config mapping (sidecar mode -> prudynt.json keys) [LEGACY verb] =="
# NOTE: sync-to-config is OFF the boot path under patch 0015 (prudynt reads the
# sidecar directly; S31 runs only `migrate`). This scenario stays only to prove the
# rollback/escape-hatch verb still maps correctly if ever re-wired.
# sync-to-config mirrors prudynt's IMPSystem::init (force_mode applied REGARDLESS
# of enabled): auto -> force_mode=""+enabled=true; day -> force_mode=day+enabled=false;
# night -> force_mode=night+enabled=false. It is write-on-change: no `jct set` when
# prudynt.json already holds the target values. The stub jct records each `set` as
# a "set <key>=<value>" line in $JCTLOG.
sync_log() { : > "$JCTLOG"; "$HELPER" sync-to-config >/dev/null 2>&1; cat "$JCTLOG" 2>/dev/null; }
# count exact "set <key>=<value>" lines in the recorded log
nset() { grep -c -x "$1" "$JCTLOG" 2>/dev/null | tr -d ' '; }

# auto: from a forced-night config -> clears force_mode, enables auto
"$HELPER" set auto >/dev/null
printf '{"daynight":{"enabled":false,"force_mode":"night"}}\n' > "$PJSON"
sync_log >/dev/null
eq "auto: set force_mode=\"\""  "$(nset 'set daynight.force_mode=')"     "1"
eq "auto: set enabled=true"      "$(nset 'set daynight.enabled=true')"    "1"

# day: from an auto config -> force_mode=day, enabled=false
"$HELPER" set day >/dev/null
printf '{"daynight":{"enabled":true,"force_mode":""}}\n' > "$PJSON"
sync_log >/dev/null
eq "day: set force_mode=day"     "$(nset 'set daynight.force_mode=day')"  "1"
eq "day: set enabled=false"      "$(nset 'set daynight.enabled=false')"   "1"

# night: from an auto config -> force_mode=night, enabled=false
"$HELPER" set night >/dev/null
printf '{"daynight":{"enabled":true,"force_mode":""}}\n' > "$PJSON"
sync_log >/dev/null
eq "night: set force_mode=night" "$(nset 'set daynight.force_mode=night')" "1"
eq "night: set enabled=false"    "$(nset 'set daynight.enabled=false')"    "1"

# write-on-change: when prudynt.json already matches the sidecar, sync writes nothing.
"$HELPER" set night >/dev/null
printf '{"daynight":{"enabled":false,"force_mode":"night"}}\n' > "$PJSON"
log=$(sync_log)
eq "night already in sync -> no jct set" "$(printf '%s' "$log" | grep -c 'set ' | tr -d ' ')" "0"

echo "== migrate pid-aware orphan reclaim =="
"$HELPER" set night >/dev/null
: > "$STATE.tmp.999999"; : > "$STATE.tmp.$$"
"$HELPER" migrate
[ -e "$STATE.tmp.999999" ] && bad "dead-pid orphan removed" "present" "removed" || ok "dead-pid orphan removed"
[ -e "$STATE.tmp.$$" ] && ok "live-pid temp kept" || bad "live-pid temp kept" "removed" "kept"
rm -f "$STATE.tmp.$$"

echo; echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
