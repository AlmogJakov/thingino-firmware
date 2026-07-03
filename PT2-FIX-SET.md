# PT2 Fix Set - Sonoff CAM-PT2 (T23N / SC2336P / ATBM6012BX)

Branch: `pt2-firmware` (based on upstream `12445a6`).
This document records every change in this branch and the reasoning behind it, so a
future reviewer can understand *why* each fix exists. It is a repo document only - it
is **not** installed into the firmware image.

> Hardware/role: pan-tilt **life-safety** camera. Ingenic **T23N**, **SC2336P** sensor,
> **ir850-only** illuminator, stepper PTZ. Software path: Thingino + **prudynt**
> (`f4b3228`, live555 RTSP) → *(off-camera)* go2rtc → WebRTC / Home Assistant. go2rtc is
> **not** built into or run on this camera (absent from the PT2 defconfig; verified on-device:
> no binary, init script, process, or listening port). It runs on a **separate LAN host** that
> consumes the camera's RTSP. The RTSP stream **must** come up after every reboot and every
> unclean power loss, and stay up.

---

## 1. Executive Summary

### Problems found
- **Startup SIGSEGV crash-loop (every prudynt).** On the buildroot **2026.02.1** rootfs prudynt
  SIGSEGV'd at startup and crash-looped - never serving RTSP at all. Root-caused to a
  **miscompiled `libuclibcshim.so`** (the glibc→uClibc ABI shim; `-flto` miscompile, fault in
  its own `__fputc_unlocked`), **not** prudynt - though it was first misattributed to the
  day/night `0015` work, which cost a long detour. See §3-K.
- **Cold-boot streamless camera.** On a cold boot prudynt could finish "alive" but with
  **no RTSP channel registered** (SPS/PPS not ready inside the bootstrap deadline), or
  silently continue after an IMP-init failure → DESCRIBE 404 while the process is up.
- **Reboot loop / unrecoverable wedge.** The stock watchdog probed RTSP OPTIONS with no
  startup grace and no liveness check, restarting a still-bootstrapping prudynt into a
  colder, slower boot; a fast restart also raced the kernel's hardware reclaim → VPU wedge.
- **Anti-brick gap.** A corrupt `/etc/prudynt.json` could leave `isp_bypass=false` → a
  permanently stream-dead boot.
- **Day/night not surviving reboot / wrong optics / wrong colour.** A forced day/night
  mode was lost or mis-applied across reboot; the persisted `force_mode` was stored
  **double-quoted** and rejected by prudynt's strict validator; boot optics always ended
  in *day* regardless of mode; `imp-control`/`color` mapping was backwards.
- **Day/night executor race + white-LED drift.** Async fire-and-forget actuation and a
  racy singleton caused intermittent IR-cut / 850 non-switch; a white floodlight could
  light at boot.
- **No physical privacy.** No way to physically point the lens away, freeze the mode, and
  kill the emitters/mic that would otherwise warm the parked sensor - reboot-safe.
- **Timezone shows UTC.** uClibc-ng rejects the extended DST hour in the stock TZ rule →
  whole TZ discarded → clock in UTC.
- **A/V defects.** OPUS WebRTC stutter from jittery RTP timestamps; the mic mute restarted
  the audio worker and broke the stream; and a live audio-codec change (`mic_format` OPUS↔AAC)
  stuttered/killed audio until a full prudynt restart.
- **Flash wear / overlay exhaustion.** Config was rewritten on every action and large files
  were edited live, filling the tiny jffs2 config overlay.

### What was fixed
- **prudynt binary:** 16 source patches `0001`-`0016` (auto-applied by buildroot's global
  patch dir; the source is git-fetched at `f4b3228` and never hand-edited). For day/night
  persistence, patch `0015` makes `CFG::load()` read the `/etc/daynight.state` sidecar
  **directly** (a zero-allocation static-literal overlay) and set `daynight.enabled` /
  `force_mode_cfg` from it, so the sidecar is the single source of truth on every config load
  AND every ConfigWatcher reload. (An earlier attempt at `0015` was wrongly blamed for a
  startup crash and reverted in favor of a rootfs `sync-to-config` workaround; the real cause
  was a **miscompiled `libuclibcshim.so`** - fixed by pinning the proven-good shim - so `0015`
  was safe to re-introduce. See §3-K for the shim root cause and §7 for the sidecar design.)
- **Toolchain / libc shim:** the startup crash-loop's true cause was a **miscompiled
  `libuclibcshim.so`** (the glibc→uClibc ABI bridge prudynt force-links). Fixed by shipping the
  **pinned proven-good prebuilt** (sha `07710f80…`) and **dropping `-flto`** in
  `package/ingenic-uclibc/ingenic-uclibc.mk`; the build verifies the shim sha and refuses to
  proceed if it was rebuilt/altered. See §3-K.
- **jct (config tool):** stock jct - **the atomic-write fork was dropped**
  (`package/all-patches/thingino-jct/` no longer exists). `prudynt.json` writes use stock jct.
  The day/night sidecar's atomicity comes from the `/usr/sbin/daynight-state` helper's **own**
  `_write` (same-dir temp + `mv` + `sync`), not from any jct fork. A power cut during a (rare)
  `prudynt.json` jct write is healed by `S31prudynt`'s C1 `validate_and_restore_config`
  (restores the ROM copy); the day/night mode is unaffected - it lives in the sidecar, which
  prudynt reads directly.
- **Minimal-write day/night persistence:** the two MQTT-frequent modes (auto/day/night and
  physical-privacy's day/night freeze) no longer rewrite the whole ~8.5 KB `prudynt.json` on a
  runtime toggle. They persist in a tiny, atomically-written sidecar `/etc/daynight.state`
  (`mode=auto|day|night`) - the single source of truth read by **prudynt itself** (patch `0015`)
  and, on the shell side, by `S56ircut`, `physical-privacy`, the agent adapter and HA state via
  the shared `/usr/sbin/daynight-state` helper. At boot, `S31prudynt` runs only
  `daynight-state migrate` (seed the sidecar from the legacy `prudynt.json` keys on the first
  boot after an upgrade); there is **no** sidecar->`prudynt.json` sync, so the per-boot 8.5 KB
  rewrite is gone and prudynt boots straight into the persisted mode by reading the sidecar. See §7.
- **Rootfs:** ~20 script/asset changes (reboot hardening, day/night persistence + optics +
  colour, physical privacy, timezone, HA/WebUI integration) baked into the **read-only
  squashfs**, plus the `daynight-state` helper, the write-on-change guard in the agent
  adapter, and a one-rule `.mk` change for the motors interlock.
- Everything ships in the firmware image; **nothing depends on live overlay edits** anymore.

### What was intentionally deferred
- **Full agent-adapter writer redesign** beyond the write-on-change guard and the
  `force_mode` bare-write fix (both **done** - see §3-I and §3-C). The per-toggle flash churn for
  the frequent day/night path is now **done** via the `/etc/daynight.state` sidecar (§7), whose
  atomicity comes from the `daynight-state` helper's own write; other rarely-changed keys still
  ride **stock** jct (the atomic-write jct fork was dropped - see §5). Executor-invocation changes
  stay deferred.
- **No separate persistence for `color` / `ircut` / IR-LED — by design, not a gap.** Only the
  day/night mode and the physical-privacy state are persisted. `color`, `ircut` and the 850 nm
  LED are **derived runtime effects** of the selected auto/day/night mode (day ⇒ ir-cut on,
  color on, 850 off; night ⇒ ir-cut off, color off, 850 on), exactly as before this change.
  The HA `color`/`ircut` toggles stay runtime-only and are intentionally **not** wired to the
  sidecar (doing so would couple them to the day/night select). Behavior unchanged.
- **Default-setting changes** beyond the existing per-camera `prudynt.json` (see §6).
- **Deeper comment stripping** of the baked scripts (space-moot on the squashfs; risky on
  life-safety logic - only large pure-prose headers were trimmed).
- **JSON-aware `"active"` parse** in the privacy guard readers (compact-literal works today;
  see §5 note).
- **Sidecar/minimal-key storage, compact JSON, and the jct atomic same-dir temp patch** - not
  pursued: after write-on-change + ROM baking, flash wear is not a realistic concern (quantified
  in §5). The jct atomic-temp fix would address only the power-loss integrity window and is left
  optional for a possible future reliability patch.

### Flash-write behavior (summary)
- **Automatic / boot / steady-state / automatic dusk↔dawn switching: writes nothing to flash.**
  The day/night executor reads only; `color` uses a tmpfs file; HA state/discovery publish
  over MQTT.
- **Manual Auto/Day/Night, Physical Privacy, and Motion toggles: write-on-change only** -
  persistence happens *only when the value actually changes*.
- **Timezone:** write-on-change (~1 write/year at DST rollover).
- **Config restore:** recovery-only (fires only on a detected-corrupt config at boot).
- All changes live in the **read-only squashfs**; the writable ~224 KB jffs2 config overlay
  starts empty on a clean flash and stays near-empty in normal use. Full detail in §5.

---

## 2. File Change Summary

> **Batch 1 & 2** (HA entity config toggles, `S93ha` lifecycle, physical-privacy
> responsiveness, Web-UI status bar, read-only fast status channel, CPU/RAM/Storage
> health badge, Restart-Streamer fix) are documented in **§8**, with their own file list.

| File (repo path) | Action | Package owner | Installs to (device) | In ROM/rootfs? |
|---|---|---|---|---|
| `package/prudynt-t/files/daynight` | Replaced | prudynt-t | `/usr/sbin/daynight` | ✅ squashfs |
| `package/prudynt-t/files/color` | Replaced | prudynt-t | `/usr/sbin/color` | ✅ squashfs |
| `package/prudynt-t/files/imp-control` | Replaced | prudynt-t | `/usr/sbin/imp-control` | ✅ squashfs |
| `package/prudynt-t/files/S31prudynt` | Replaced | prudynt-t | `/etc/init.d/S31prudynt` | ✅ squashfs |
| `package/thingino-ha/files/ha-commands` | Replaced | thingino-ha | `/usr/sbin/ha-commands` | ✅ squashfs |
| `package/thingino-ha/files/ha-discovery` | Replaced | thingino-ha | `/usr/sbin/ha-discovery` | ✅ squashfs |
| `package/thingino-ha/files/ha-state` | Replaced | thingino-ha | `/usr/sbin/ha-state` | ✅ squashfs |
| `package/thingino-ha/files/json-config-ha.cgi` | Modified (+entity toggles; §8) | thingino-ha | `/var/www/x/json-config-ha.cgi` | ✅ squashfs |
| `package/thingino-webui/files/www/a/main.js` | Modified | thingino-webui | `/var/www/a/main.js` | ✅ squashfs |
| `package/thingino-webui/files/www/x/json-imp.cgi` | Replaced | thingino-webui | `/var/www/x/json-imp.cgi` | ✅ squashfs |
| `package/thingino-agent/files/thingino-agent-adapter-prudynt` | Modified (write-on-change guard + day/night→sidecar read/write) | thingino-agent | `/usr/libexec/thingino-agent/adapters/prudynt.sh` | ✅ squashfs |
| `package/thingino-motors/thingino-motors.mk` | Modified (1 rule) | thingino-motors | real CLI → `/usr/bin/motors-bin` | ✅ build rule |
| `overlay/etc/init.d/S56ircut` | Replaced | - (overlay) | `/etc/init.d/S56ircut` | ✅ squashfs |
| `overlay/etc/init.d/S01timezone` | Replaced | - (overlay) | `/etc/init.d/S01timezone` | ✅ squashfs |
| `overlay/etc/ntpd_callback` | Replaced | - (overlay) | `/etc/ntpd_callback` | ✅ squashfs |
| `overlay/usr/sbin/light` | Replaced | - (overlay) | `/usr/sbin/light` (& `/sbin/light`) | ✅ squashfs |
| `overlay/etc/init.d/S32prudyntwd` | **Added** | - (overlay)¹ | `/etc/init.d/S32prudyntwd` | ✅ squashfs |
| `overlay/etc/init.d/S60physical-privacy` | **Added** | - (overlay) | `/etc/init.d/S60physical-privacy` | ✅ squashfs |
| `overlay/usr/bin/motors` | **Added** | - (overlay) | `/usr/bin/motors` (wrapper) | ✅ squashfs |
| `overlay/usr/sbin/physical-privacy` | **Added** | - (overlay) | `/usr/sbin/physical-privacy` | ✅ squashfs |
| `overlay/usr/sbin/tz-update` | **Added** | - (overlay) | `/usr/sbin/tz-update` | ✅ squashfs |
| `overlay/usr/sbin/daynight-state` | **Added** | - (overlay) | `/usr/sbin/daynight-state` | ✅ squashfs |
| `package/all-patches/prudynt-t/0001…0016-*.patch` | Added (in branch) | prudynt-t (build patches) | compiled into `/usr/bin/prudynt` | ✅ binary |
| `package/ingenic-uclibc/ingenic-uclibc.mk` | Modified (pin prebuilt `.so`, verify sha, drop `-flto`) | ingenic-uclibc | `/usr/lib/libuclibcshim.so` | ✅ binary |
| `package/ingenic-uclibc/prebuilt/libuclibcshim.so` | **Added** (pinned proven-good prebuilt, sha `07710f80…`) | ingenic-uclibc | `/usr/lib/libuclibcshim.so` | ✅ binary |
| `tests/test-daynight-state.sh` | **Added** | - | not installed | ❌ repo test only |
| `PT2-FIX-SET.md` (this file) | **Added** | - | not installed | ❌ repo doc only |
| `.github/workflows/prudynt-binary.yml` | **Added** | - | not installed (CI) | ❌ builds prudynt binary only |
| `.github/workflows/pt2-build-artifact.yml` | **Added** | - | not installed (CI) | ❌ builds full PT2 image |

¹ The prudynt-t package ships `S32prudyntwd` but its install rule is commented out, so the
stock build has no watchdog. We add it via the overlay (no `.mk` change needed).

**Mechanism notes**
- **merged-usr** (`BR2_ROOTFS_MERGED_USR=y`): `/sbin`,`/bin`,`/lib` are symlinks into `/usr`.
  Overlay files go under `overlay/usr/...` only; `/sbin/X` resolves through the symlink.
- **Overlay applied after packages** (`BR2_ROOTFS_OVERLAY=overlay/`). Verified that **no
  enabled package** installs any path we ship via the overlay, so we never rely on
  overlay-beats-package ordering.
- **Contested files** (`daynight`/`privacy`/`imp-control`/`color`) have multiple providers,
  but on this board only **prudynt-t** is enabled (ircut/daynightd/dusk2dawn/raptor/
  libimp-control are disabled), so the package-source edits are unambiguous.
- **Line endings:** `core.autocrlf=true`; the overlay/package shell tree is committed CRLF and
  is the proven-working state (the GitHub-built firmware runs it fine), so do **not** mass-
  normalize to LF (huge churn, diverges from the proven format). Edit content normally;
  `git add` keeps the existing CRLF, so diffs stay content-only.
- **Pinned prebuilt shim:** `libuclibcshim.so` (the glibc→uClibc ABI bridge prudynt force-links)
  is the one component shipped as a **committed prebuilt binary**, not source-built - the
  2026.02.1 toolchain miscompiled it from source (§3-K). `ingenic-uclibc.mk` installs
  `prebuilt/libuclibcshim.so` to `/usr/lib` and `sha256`-gates it; do **not** revert to a source
  build without re-verifying prudynt boots and re-pinning the sha.

---

## 3. Detailed Fixes

### A. Cold-boot RTSP stream reliability
- **Problem:** camera boots, prudynt is running, but RTSP serves no channel (DESCRIBE 404 /
  OPTIONS 200) - "alive but streamless."
- **Root cause:** `RTSP::addSubsession` waits a fixed deadline for SPS/PPS and *skips* the
  channel on timeout; on a cold boot (slow AE in low light) ch0 misses the 10 s deadline.
  Separately, an IMP-init failure or a thrown init exception could be swallowed and prudynt
  would continue without a pipeline.
- **Solution:**
  - `0009` - raise the cold-boot SPS/PPS bootstrap deadline **10 s → 45 s** per channel.
  - `0010` - **fail-fast**: if `IMPSystem::createNew()` returns null, log and `exit(1)` so the
    supervisor restarts instead of running headless.
  - `0011` - **fail-fast**: if `stream0/1` are enabled but **zero** channels register, raise
    `SIGTERM` to itself (clean shutdown) so a streamless prudynt becomes a *down socket* the
    watchdog can actually recover.
  - `0012` - **anti-brick**: apply factory config defaults when `prudynt.json` fails to parse,
    so a corrupt config can't strand `isp_bypass=false`.
  - `S31prudynt` **C1** - at boot, trust the live config only if `jct` can read
    `image.isp_bypass` as a literal `true|false`; otherwise atomically restore the clean
    `/rom` copy (temp + single `sync` + rename).
- **Files:** patches `0009`-`0012`; `package/prudynt-t/files/S31prudynt`.
- **Notes:** "alive-but-streamless" is converted to "down" so the watchdog's recovery path
  (§B) can act. C1 restore is **recovery-only** (fires only on a detected-corrupt config).

### B. Restart hardening, VPU reclaim, and the watchdog
- **Problem:** restarting prudynt could wedge the VPU; the stock watchdog caused reboot loops.
- **Root cause:** T23 prudynt skips hardware teardown on SIGTERM and exits fast, so the kernel
  reclaims VPU/ISP/VB/audio-DMA/`:554` over several seconds - a fast restart races that and
  wedges. The stock watchdog had **no startup grace** and **no liveness check**: it probed
  RTSP OPTIONS and restarted a still-bootstrapping prudynt, escalating to reboot → colder,
  slower boot → loop.
- **Solution:**
  - `S31prudynt` **C3** - deterministic stop (`SIGTERM`, 2 s → `SIGKILL`+`killall`, 2 s →
    confirm-gone, zombie-aware), and a `restart()` that stops the heartbeat, stops the daemon,
    waits **`RECLAIM_WAIT=15 s`** (field-proven floor) for hardware reclaim, then starts -
    aborting *without* relaunch if death can't be confirmed (returns non-zero).
  - `S32prudyntwd` - rewritten: `pidof` **liveness gate** (zombies excluded) + **monotonic
    `/proc/uptime` startup grace**. The RTSP OPTIONS probe only *classifies* serving-vs-not
    and is **never itself a trigger**. Only an alive-but-not-serving prudynt **past grace** for
    consecutive cycles is treated as wedged. Backstop: after `RESTART_LIMIT` *warranted*
    restarts with no sustained recovery → reboot (a true VPU wedge is reboot-only); the
    counter resets only after a sustained-healthy dwell.
  - `0002` (videoworker teardown order) and `0003` (quiesce encoders before rebuild) reduce
    teardown-order hazards during live reconfig/rebuild.
- **Files:** `S31prudynt`, `S32prudyntwd`; patches `0002`, `0003`.
- **Notes:** S31 and S32 are **co-designed** - the watchdog branches on S31's restart exit
  code and re-arms grace only on a confirmed relaunch. Bake both or neither. The hardware
  watchdog (K99) is kicked by an independent daemon, so the 15 s reclaim does not starve it;
  validate total rcK stop time stays < 60 s on real hardware.

### C. Day/Night - forced-mode persistence, boot optics, and colour
- **Problem:** a forced day/night mode didn't survive reboot (or booted day + colour + IR
  filter wrong); colour control was backwards.
- **Root cause:** (1) prudynt didn't apply the persisted forced mode at startup; (2) the agent
  adapter stored `force_mode` **double-quoted** (`"\"night\""`), which prudynt's strict
  validator (`=="day"||=="night"`) rejected → defaulted to day; (3) the boot IR-cut init always
  finished in *day* (filter engaged, IR off) regardless of mode; (4) `imp-control`/`color`
  mapped colour↔mode backwards.
- **Solution:**
  - `0001` - apply the persisted forced mode (and report real day/night stats) at startup.
  - **Agent adapter writer** - the manual `daynight day|night` command now persists
    `force_mode` **bare** (`night`), so `jct` stores `"force_mode":"night"` correctly instead
    of the old `"\"night\""`. (`apply_payload` still quotes it for the `prudyntctl` runtime
    call.) This fixes the bug at the source.
  - `0013` - the config reader **strips surrounding quotes** from `daynight.force_mode`, so a
    double-quoted value is still honored at boot (defense-in-depth for legacy/existing configs
    that were written before this fix).
  - `S56ircut` - keeps the exact stock filter calibration, then for a **forced-night** camera
    (`daynight.enabled=false` AND `force_mode=night`) removes the IR-cut filter and turns the
    IR LED on, so it boots straight into night optics with no day flash. Pure GPIO; never
    touches the pipeline.
  - `imp-control` - split the combined handler into strict `ispmode` (0/1) and a `color` verb
    with the corrected mapping (+ day/night word aliases), fixing the backwards behavior.
  - `color` - switch from the HTTP `imp-control` backend to direct `prudyntctl` JSON IPC,
    idempotent (early-return if already at target), cache only on success.
- **Files:** patches `0001`, `0013`; `S56ircut`, `imp-control`, `color`;
  `thingino-agent-adapter-prudynt` (writer bare-persist).
- **Notes:** ISP colour/`running_mode` is owned by prudynt's persisted config (applied
  natively at start); `S56ircut` only settles the **optics**. Forced-night boot is unexercised
  on this unit (currently auto) - validate on a forced-night unit.

### D. Day/Night executor race + white-LED drift
- **Problem:** intermittent IR-cut / 850 non-switch in auto mode; a white floodlight could
  light at boot.
- **Root cause:** the `/usr/sbin/daynight` executor fired actuators asynchronously (`&`) with a
  racy `pidof` singleton; white-LED branches drove a floodlight on day/night transitions.
- **Solution:** `daynight` runs actuators **synchronously and in order** with read-back
  verification (`set_ircut_verified` / `set_light_verified`), propagates the real exit code,
  removes the white-LED day/night branches (white is manual-only), and early-returns on a
  same-mode request. A privacy-active gate keeps IR off in night while privacy is armed.
- **Read-back scope (this board is dual-pin, `gpio.ircut="17 16"`):** the IR LEDs and any
  single-pin IR-cut expose a real GPIO level, so verification is physical there. The **dual-pin
  IR-cut latch has no GPIO read-back**, so `set_ircut_verified` can only confirm the `ircut`
  **invocation** ran (it catches a crashed/missing call), not that the mechanical latch physically
  flipped; `ircut read` reflects the last commanded state (`/tmp/ircutmode.txt`). A dropped pulse on
  a healthy latch is therefore not software-detectable — an accepted HW limitation, behavior
  unchanged (see memory `ircut-dualpin-verify-lies`).
- **Files:** `package/prudynt-t/files/daynight`.
- **Notes:** the executor **does not write flash** (it reads config and drives GPIO/runtime),
  so automatic dusk↔dawn switching is flash-free.

### E. Physical Privacy (pan/tilt lens park)
- **Problem:** need a true physical-privacy mode for a PTZ camera, reboot/power-safe, that
  can't be defeated by a stray emitter or a motor move.
- **Root cause / design:** unlike the prudynt video-overlay "privacy screen", physical privacy
  **points the lens away** (tilts to a privacy pose), **freezes** the current day/night mode
  (disables auto, pins `force_mode` to the live mode), kills the emitters that would warm the
  parked sensor (white always; IR only in night), and **soft-mutes** the mic.
- **Solution:**
  - `physical-privacy` (engine) - tilt to pose, freeze mode (persist *only on change*), kill
    emitters with `light` read-back retries, soft-mute mic (`0007`), atomic state →
    `/etc/physical-privacy-state.json` + `sync`, `/run` armed marker + lock; subcommands
    `on/off/reenter/guard/status/set-pose`. Fail-closed on enter, verified-return on exit.
  - `0007` - mic mute via prudynt's runtime `mic_muted` flag, which zeros the captured PCM with
    **no audio-worker restart**, so the RTSP/go2rtc stream is undisturbed (an earlier
    `mic_enabled` approach restarted audio and broke the stream - reverted).
  - `light` guard - for emitter types on **turn-ON only**, refuse (exit 0) while privacy is
    active by either the `/run` armed marker **or** the persisted `"active":true` (so the guard
    holds at boot before `/run` exists).
  - `S60physical-privacy` - after boot homing settles (bounded waits, 3 consecutive identical
    lens reads), re-park into the saved pose. Does not modify the snapshot.
  - **Interlock choke points** - `/usr/bin/motors` wrapper (§4) and the day/night handlers in
    `ha-commands` / `json-imp.cgi` call `physical-privacy guard`: while armed, a user move or a
    day/night change is treated as "cancel privacy" and the original command is dropped.
- **Files:** `physical-privacy`, `S60physical-privacy`, `motors` (wrapper), `light`; patch
  `0007`; interlock hooks in `ha-commands`, `json-imp.cgi`.
- **Notes:** multi-file feature - ship together. The frozen mode is the persisted prudynt
  config, applied natively at start + by `S56ircut` (optics), so no script runs it at boot.

### F. Timezone (uClibc-ng) - UTC instead of local
- **Problem:** date shows UTC despite a configured zone (e.g. Asia/Jerusalem).
- **Root cause:** uClibc-ng's POSIX-TZ parser rejects a DST transition hour > 24 (the stock
  rule used an extended hour like `.../26`) → the whole TZ is discarded → UTC.
- **Solution:** `tz-update` emits a **uClibc-safe** POSIX TZ string (exact Israel spring rule
  for the current year, computed at noon UTC to dodge edge cases; generic `sanitize_hours` for
  other unsafe zones). `S01timezone` runs `tz-update boot` (conservative); `ntpd_callback` runs
  `tz-update sync` once the clock is trusted. Both write `/etc/TZ` **only on change**.
- **Files:** `tz-update` (new), `S01timezone`, `ntpd_callback`.
- **Notes:** hard companions - inert unless all three ship. Validate busybox `date -d @epoch`
  / `-u` on the real unit; otherwise it falls through to the approximate path.

### G. A/V quality + live audio codec switching
- **Problem:** OPUS WebRTC stutter; mic mute broke the stream; and a live audio-codec change
  (`mic_format` OPUS↔AAC in the web UI) stuttered / killed go2rtc audio until `S31prudynt restart`.
- **Root cause:** (1) RTP timestamps from jittery `gettimeofday` deltas; (2) the old mute restarted
  the audio worker; (3) a live `mic_format` change raised `global_restart_audio` (rebuilds the encoder
  → new-codec bytes on the wire) but **not** `global_restart_rtsp`, so the cached RTSP audio **SDP**
  stayed stale (live555 freezes `fSDPLines` at the first DESCRIBE) → the client kept depacketizing the
  old codec → stutter / AAC→OPUS silence. A full restart rebuilt the SDP, which is why it worked.
- **Solution:**
  - `0008` - sample-clock RTP timestamps, forward-only drift-gated re-anchoring (flat 1920 deltas),
    **all codecs**, plus the AAC+`force_stereo` reframer-buffer fix. (An OPUS-only revision of `0008`
    was explored and **abandoned** - the AAC defect was the codec-switch bug below, not `0008`.)
  - `0007` - software PCM mute (see §E).
  - `0014` - on a **real** `mic_format` change, `handle_audio` also raises `global_restart_rtsp`
    (read-back diff-gated so HA/agent re-sends don't churn RTSP), mirroring the video `format` path, so
    the RTSP audio subsession + SDP are rebuilt and clients re-DESCRIBE the new codec. No `S31prudynt
    restart`, no stutter; a fresh RTSP client gets the new codec immediately. (`mic_sample_rate`, the
    RTP clock, is also SDP-affecting but rarer - left on the stock path; easy follow-up.)
  - `main.js` rewires the WebUI mic button from `mic_enabled` (restart) to `mic_muted` (soft-mute).
- **Files:** patches `0007`, `0008`, `0014`; `main.js`.
- **Known limitation (accepted; downstream of prudynt, no change made):** a **live** HA WebRTC card
  stays muted after a codec change until it is re-opened. go2rtc locks the WebRTC consumer's audio codec
  at the initial SDP and does **not** renegotiate a surviving session when the source codec flips (video
  recovers; audio does not) - confirmed in go2rtc source (`add_consumer.go` one-shot match; `producer.go`
  reconnect `MatchCodec`). Accepted because a codec change is deliberate and rare. Optional seamless fix
  (not applied): a codec-stable OPUS track in go2rtc via `ffmpeg:<src>#video=copy#audio=copy#audio=opus`.
- **Recovery model (life-safety):** the codec change is the **only** non-recovering case, because it is
  the only event that changes the negotiated codec. Every **involuntary** disruption (reboot, power-cut,
  network drop, prudynt crash/wedge/restart) keeps the same codec → go2rtc reconnects and re-binds both
  tracks → audio+video recover. Prerequisite: the **off-camera** go2rtc must be supervised
  (auto-restart) **on its own host** — go2rtc is not installed or run on this camera, so on-camera
  supervision is out of scope. The camera's responsibility is to keep prudynt's RTSP (`:554`) up,
  which the watchdog (§B) probes directly, complementing prudynt fail-fast (§A). (Confirmed in the
  pre-release audit — see §8.7 — that go2rtc is not in the PT2 image, so the earlier "unsupervised
  go2rtc" finding is not applicable to the firmware.)

### H. Live-reconfig / stream seamlessness (T23)
- **Problem:** stream-config changes could freeze the VPU or drop the stream.
- **Solution:** `0004` (live reconfig + day/night IDR), `0005` (lifeguard fault tolerance),
  `0006` (high-frequency stream seamlessness). These harden the live reconfiguration path so
  config changes apply without tearing down the pipeline.
- **Files:** patches `0004`, `0005`, `0006`.

### I. Flash wear / write-on-change
- **Problem:** config was rewritten on every action (a `jct set` rewrites the whole ~8.5 KB
  `prudynt.json` - see the verified jct write path in §5), churning the tiny jffs2 overlay;
  large files edited live filled it.
- **Solution:**
  - **Adapter `persist_value` write-on-change** - the central persist function now compares the
    stored value (quote-normalized) to the requested value and **skips the `jct set` when
    unchanged**. Because every setting (`daynight.force_mode`, `daynight.enabled`,
    `motion.enabled`, image/stream settings, …) persists through this one function, **all**
    manual persistence - Auto/Day/Night, Physical Privacy freeze, and Motion - is now
    write-on-change. The runtime apply (`prudyntctl`) is separate, so skipping a redundant
    persist is behavior-safe.
  - `color` uses a **tmpfs** mode file (`/tmp/colormode.txt`) - no flash on day/night switches.
  - `physical-privacy` persists state and prudynt keys **only on change** and `sync`s **only if
    it actually wrote**.
  - Baking everything into the **read-only squashfs** removes the live-edit copy-ups that filled
    the overlay.
- **Files:** `thingino-agent-adapter-prudynt` (1-line guard), `color`, `physical-privacy`.
- **Notes:** see the full audit in §5.

### J. Home Assistant / WebUI integration
- **Solution:** `ha-commands`/`ha-discovery`/`ha-state` add (feature-gated) entities - Physical
  Privacy switch, Day/Night target + status, Microphone soft-mute, PTZ buttons, Config-Storage
  and Shabbat sensors - route day/night through `thingino-agentctl`, and intercept actuation
  while privacy is armed. `json-imp.cgi` adds the same privacy interlock and routes day/night
  through the agent; `json-config-ha.cgi` adds an `enable_live_view` field (3-line additive
  delta); `main.js` rewires the mic button (see §G). HA state/discovery publish over **MQTT**
  (network), not flash.
- **Files:** `ha-commands`, `ha-discovery`, `ha-state`, `json-imp.cgi`, `json-config-ha.cgi`,
  `main.js`.

### K. Startup SIGSEGV crash-loop - miscompiled `libuclibcshim.so` (true root cause)
- **Problem:** on the buildroot **2026.02.1** rootfs, EVERY prudynt SIGSEGV'd at startup in a
  tight crash-loop - never serving RTSP. This was first misattributed to prudynt (specifically
  the day/night `0015` work), which sent the investigation down a dead end.
- **Root cause:** `libuclibcshim.so` is a hand-written glibc/musl→uClibc ABI bridge
  (`uclibc_shim.c`) that pokes raw `FILE`-struct offsets and interposes libc symbols. The
  2026.02.1 gcc - **especially with `-flto`** - **miscompiled** it; prudynt faulted inside the
  shim's own `__fputc_unlocked` (epc `libuclibcshim+0xbc8`). The bad rebuild's sha was
  `b3ead471…`; the proven-good binary built by the older toolchain (sha `07710f80…`) boots
  prudynt fine on the same 2026.02.1 rootfs - confirmed by swapping the shim on the cameras
  (cam4's bad shim crashed; cam3's good shim ran the **same** prudynt).
- **Solution:** `package/ingenic-uclibc/ingenic-uclibc.mk` now **ships the pinned, proven-good
  prebuilt** `prebuilt/libuclibcshim.so` (sha `07710f80…`) to `/usr/lib/libuclibcshim.so`
  instead of compiling the `.so` from source, and **drops `-flto`** (the flag that miscompiled
  it; kept off the `.a` too). `INGENIC_UCLIBC_BUILD_CMDS` runs `sha256sum -c` against
  `INGENIC_UCLIBC_GOOD_SHA` and **fails the build** if the pinned file was altered. The static
  `.a` (used only when raptor links the shim statically) is still built from source; PT2/prudynt
  use the dynamic `.so`. Both CI workflows additionally fail if the **shipped** shim sha is
  anything other than `07710f80…`. To bump the toolchain or shim source: rebuild the `.so`,
  verify prudynt boots, then re-pin (update both the file and the sha).
- **Files:** `package/ingenic-uclibc/ingenic-uclibc.mk`, `package/ingenic-uclibc/prebuilt/libuclibcshim.so`.
- **Notes:** this - **not** `0015` - is why the earlier `0015` revert + rootfs `sync-to-config`
  workaround happened; with the shim fixed, `0015` was safe to re-introduce (§7). When prudynt
  SIGSEGVs in `libuclibcshim` at startup, first `sha256sum /lib/libuclibcshim.so` on a working
  vs broken unit and swap it - do not touch prudynt.

---

## 4. Motors Special Case - wrapper → `motors-bin`

**Why a wrapper exists.** Physical privacy must be cancelled the instant a user moves the
motors from *any* path (web, MQTT/HA, ONVIF, CLI). The clean choke point is a thin shell
wrapper at `/usr/bin/motors` that classifies the command: status/speed reads pass straight
through; an **actuation** while privacy is armed cancels privacy and drops the move. Because
`/bin → /usr/bin` (merged-usr), the wrapper also catches `/bin/motors` and bare `motors`.

**The self-exec trap.** On the live device the wrapper was an *overlay* file sitting on top of
the real client that remained at `/rom/usr/bin/motors`, so the wrapper could `exec
/rom/usr/bin/motors`. **In a fresh firmware bake this breaks:** the build copies the overlay
over `TARGET_DIR` *before* `mksquashfs`, so `/usr/bin/motors` (and therefore `/rom/usr/bin/motors`)
**is** the wrapper → the wrapper would `exec` itself → infinite loop → dead PTZ.

**The fix (loop-proof by construction):**
1. `thingino-motors.mk` installs the **real** motor client to **`/usr/bin/motors-bin`**
   (the daemon stays at `/usr/bin/motors-daemon`).
2. The overlay ships the wrapper at `/usr/bin/motors`, which `exec`s **`/usr/bin/motors-bin`**.
   The exec target (`motors-bin`) ≠ the wrapper path (`motors`), so it can never self-exec -
   in a fresh squashfs *and* independent of any `/rom` semantics.
3. `physical-privacy` and `S60physical-privacy` (the streamer's own internal moves) talk to
   `/usr/bin/motors-bin` **directly**, bypassing the wrapper.
4. **`motors-bin` is not placed in the overlay** (only the `.mk` installs it), so nothing
   re-shadows it.
5. The wrapper has a **fail-open** guard: if `motors-bin` is missing it logs and exits 0 (never
   loops, never hard-fails every motor caller).

**Verified:** no caller hardcodes an absolute `/usr/bin/motors` as the real binary (all use the
PATH name `motors`), so the rename is transparent. **Post-bake check:** in the staged
`TARGET_DIR`, `/usr/bin/motors` is the ~1.7 KB `#!/bin/sh` wrapper and `/usr/bin/motors-bin` is
the ~14 KB ELF (`7f454c46`).

---

## 5. Flash Write Audit

### How jct persists a value (verified against `themactep/jct` @ `46e15ef`)

`/etc` lives on the writable jffs2 config overlay; `/tmp`, `/run`, `/dev/shm` are **tmpfs
(RAM)**. Every `jct set` goes through the path below - read from jct's source, not assumed:

- **Full-file rewrite.** `handle_set_command` calls `load_config` (parses the whole file),
  `set_nested_item` (changes one key in memory), then `save_config` -> `write_json_to_file`,
  which **re-serializes the entire JSON tree** (all keys, sorted, re-indented). There is no
  partial / in-place single-key write.
- **~8.5 KB per write.** The device `/etc/prudynt.json` is ~8.5 KB (8585 B), so changing one
  key (`motion.enabled`, `daynight.force_mode`, ...) rewrites the **whole ~8.5 KB** file. (jct
  re-serializes with its own indentation/sorted keys, so the size is approximate, not byte-identical.)
- **jct fork dropped - `prudynt.json` writes use *stock* jct.** The atomic-write jct fork was
  **not pursued** (`package/all-patches/thingino-jct/` no longer exists). Stock jct writes a
  temp to `/tmp/prudynt_config_temp_<pid>.json` (tmpfs) then `rename()`s onto `/etc` (jffs2); the
  cross-filesystem rename returns `EXDEV` and falls back to `fopen("…","w")` (**truncate**) + byte
  copy - a power-loss window that could leave the file 0-byte/partial. This pre-existing window is
  retained for `prudynt.json` writes, which after baking are rare (occasional non-daynight settings
  changes; the day/night keys are dead under `0015`). The **day/night sidecar**
  (`/etc/daynight.state`) is the atomic path - its `daynight-state` helper does the same-dir temp +
  `mv` + `sync` itself (pid-aware orphan reclaim), independent of jct. A `prudynt.json` corrupted in
  jct's window is **C1-healed** (ROM restore); the day/night mode is unaffected - it lives in the
  atomic sidecar, which prudynt reads directly.
- **JFFS2 churn.** jffs2 is log-structured: each ~8.5 KB rewrite appends ~8.5 KB of new nodes
  and marks the old ones obsolete; GC reclaims them **lazily** (on pressure / reboot). So a
  burst of redundant writes can quickly pressure the small (~224 KB) overlay before GC runs -
  this is what filled it originally.
- **So write-on-change is a requirement, not an optimization** (§3-I): skipping a redundant
  `jct set` avoids both the ~8.5 KB churn and the corruption window entirely.
- **Backstops when a write does happen:** patch **0012** (apply factory defaults on a parse
  failure) and **S31prudynt C1** (restore the clean `/rom` copy when `isp_bypass` is unreadable)
  recover a config corrupted in that window (§3-A).

The table below is every remaining write path in the changed files.

| Path / site | Trigger | Class | Writes flash? |
|---|---|---|---|
| `daynight` executor | every day/night switch incl. **automatic** dusk↔dawn | automatic | **No** - reads config, drives GPIO + runtime `prudyntctl` |
| `color` → `/tmp/colormode.txt` | every day/night switch | automatic | **No** - tmpfs (RAM), idempotent |
| `ha-state`, `ha-discovery` | periodic / on change | automatic | **No** - publishes to **MQTT** (network) |
| `daynight-state set` (sidecar `/etc/daynight.state`) | **manual/MQTT** Auto/Day/Night (web/HA/agent) | **write-on-change, atomic** | a single ~11 B atomic write (same-dir temp+`mv`+`sync`), only on change - **no** 8.5 KB `prudynt.json` rewrite |
| `physical-privacy` state + day/night freeze | **manual** Physical Privacy button | **write-on-change, atomic** | ~100 B state JSON + the ~11 B sidecar, `sync` only if it wrote |
| adapter `persist_value` (`motion.enabled`) | **manual** HA Motion switch | **write-on-change** | only when the value actually changes |
| `tz-update` → `/etc/TZ` | boot + NTP callback | **write-on-change** | ~1 write/year at DST rollover |
| `S31prudynt` C1 restore | boot, **only if** config detected corrupt | **recovery-only** | rare (anti-brick) |
| `S32prudyntwd` `sync` | inside `do_reboot()` only, before `reboot` | recovery-only | flush before reboot (rare backstop) |
| `json-config-ha.cgi` | **manual** "save HA settings" page | manual (on save) | yes, on explicit save |
| `light` `set_config_pin` | only `light <type> gpio <pin>` (pin config) | manual (setup) | yes, only when configuring a pin |

**Bottom line:** background/automatic operation - including automatic dusk↔dawn switching -
writes **nothing** to flash. The three user toggles (Auto/Day/Night, Physical Privacy, Motion)
are all **write-on-change**. Timezone is write-on-change (~1/yr); config restore is
recovery-only. The HA-settings save and explicit `light … gpio` pin config are deliberate
user config actions.

**Note (low-urgency):** the `light`/`daynight` privacy-guard readers match the **compact**
`"active":true` literal that `physical-privacy` writes today; a future pretty-printing writer
would need a JSON-aware read. The `/run` armed marker OR-gate covers the transition window.

### Flash-wear & overlay-space assessment (post write-on-change)

Quantified, with stated assumptions (~224 KB jffs2 overlay, SPI-NOR ~100,000 erase/sector,
jffs2 wear-levels across the partition; confirm on-device via `cat /proc/mtd` + the flash
datasheet):

- **Per-event cost:** a real change writes ~8.5 KB (one key) to ~17 KB (two keys, e.g. the
  day/night `enabled` + `force_mode`).
- **Realistic volume** (write-on-change means only actual changes write; automatic dusk/dawn
  switching writes nothing to flash): ~50 KB/day light use, ~175 KB/day active, ~640 KB/day
  heavy automation.
- **Endurance:** lifetime write budget ~4.6 GB (conservative, ~5x derated) to ~22 GB (ideal)
  = ~19 years at the heavy rate and centuries at light/active rates - comfortably beyond the
  camera's service life, even if real usage is 10x the estimate.
- **Overlay space** (the actual historical `No space left on device` failure): that was the
  224 KB overlay *filling* under lazy GC, driven by live-editing an 85 KB file into a copy-up
  and by redundant ~8.5 KB config rewrites. **Both dominant sources are removed** - baking
  moved the large files into read-only `/rom`, and write-on-change eliminates the redundant
  rewrites. Steady-state usage is now a few tens of KB of live config plus the obsolete nodes
  from ~5-30 rewrites/day, far below 224 KB, so GC keeps pace.

**Conclusion: after write-on-change + ROM baking, flash wear is not a realistic concern on this
device, and the dominant source of unnecessary writes has been removed.**

Decisions (deliberately not pursued):
- **Sidecar / minimal-key storage** and **compact JSON serialization** - NOT implemented: wear
  is no longer a concern, so the added complexity is unjustified.
- **jct atomic same-dir temp patch** - kept OUT for now. It addresses the power-loss *integrity*
  window (see "How jct persists" above), not wear, so it is optional / nice-to-have, to be
  revisited only as part of a separate reliability patch if desired.

---

## 6. Default Configuration

The device's `/etc/prudynt.json` is **assembled at build time** by `prudynt-t.mk`, in order:

1. **`package/prudynt-t/files/prudynt.json`** - base defaults (e.g. `image.hflip:false`,
   `image.vflip:false`, `image.running_mode:0`).
2. **`configs/cameras/sonoff_pt2_t23n_sc2336p_atbm6012bx/prudynt.json`** - the **per-camera
   override**, applied via `jct import` (`prudynt-t.mk` `PRUDYNT_T_OVERRIDE_FILE`).
3. Optional websockets merge + user overrides.

**To change a default:** edit the small per-camera override (board-scoped, recommended) or the
package base (all cameras), then rebuild. It is just a JSON edit. The override already
demonstrates it - it currently contains:

```json
{ "image": { "hflip": true, "vflip": true } }
```

so the **orientation default is already corrected** (the upside-down-stream issue) for this
board.

**Related per-camera/board defaults** live alongside it:
- `configs/cameras/sonoff_pt2…/thingino.json` - GPIO pin map (IR/white/etc.).
- `configs/cameras/sonoff_pt2…/motors.json` - PTZ limits / home positions.
- `configs/cameras/sonoff_pt2…/<board>_defconfig` - enabled packages / SoC / sensor.

**Caveat:** editing these changes the **fresh-flash** default; it does **not** rewrite an
already-running device's persisted `/etc/prudynt.json` (use `jct set` at runtime there, which
is a flash write).

---

## 7. Minimal-write day/night persistence (sidecar)

> The startup crash that earlier got `0015` reverted was the **miscompiled shim** (§3-K), not
> this design; with the shim pinned, `0015` (prudynt reading the sidecar directly) is safe.

**Why.** Auto/Day/Night and physical-privacy are the **MQTT-frequent** modes and have
reboot/power-outage recovery, so persisting them on a runtime toggle must be tiny and
power-safe - not the full ~8.5 KB non-atomic `prudynt.json` rewrite the other (rare) settings use.

**Design (single source of truth).** `/etc/daynight.state` holds one line
`mode=auto|day|night`. The shared helper `/usr/sbin/daynight-state` owns the format, the
**atomic write-on-change** writer (same-dir temp + `mv` + `sync`, fail-closed, pid-aware
orphan reclaim) and a one-time `migrate`. prudynt reads the sidecar **directly** (patch `0015`):

| role | who | how |
|---|---|---|
| read at boot + every reload (color/mono + worker gate) | prudynt `CFG::load()` (**patch 0015**) | a zero-allocation overlay POSIX-reads `/etc/daynight.state` and sets `daynight.force_mode_cfg` / `daynight.enabled` from it (static literals; `auto` preserved; missing/corrupt → forced `day`). The sidecar is the **final word** on every config load AND every ConfigWatcher reload - read **directly**, no `prudynt.json` round-trip |
| read at boot (IR-cut/IR-LED optics) | `S56ircut` | `daynight-state get` (same sidecar prudynt reads → optics & ISP can't disagree) |
| write (manual/MQTT runtime toggle) | agent adapter, `physical-privacy` freeze/restore | `daynight-state set` only (live apply via `prudyntctl json -`; **no** `prudynt.json` rewrite - this is the flash-wear win) |
| read (HA state / API) | `ha-state`, adapter status + per-setting GET | `daynight-state get` / sidecar-derived helpers |
| ensure a valid sidecar at boot | `S31prudynt start()` → `daynight-state migrate` | leaves a valid sidecar untouched; seeds forced **`day`** when it is **missing or corrupt** (self-heal); reclaims an orphaned temp. Does **not** read `prudynt.json` — the legacy daynight keys are **dead** |

The `0015` overlay maps the sidecar to prudynt's members the same way `IMPSystem::init` consumes
them (`force_mode` applied **regardless of** `enabled`): `auto` → `force_mode_cfg=""` +
`enabled=true`; `day` → `force_mode_cfg="day"` + `enabled=false`; `night` → `force_mode_cfg="night"`
+ `enabled=false`. It allocates nothing (static string literals) and frees nothing, so it adds no
memory leak and cannot use-after-free the lock-free readers. A missing / unreadable / malformed
sidecar falls back to **forced `day`** (`force_mode_cfg="day"` + `enabled=false`) and never crashes
— the chosen safe default for broken persistence, applied identically by the overlay, the
`daynight-state get` helper, and `migrate` (which self-heals a missing/corrupt sidecar to `day` at
boot). A legitimate `mode=auto` is preserved as auto.

**Leak-free `force_mode_cfg` (patch `0016`).** `force_mode_cfg` is a `const char*` that the config
layer never frees (the lock-free readers `IMPSystem`/`DayNightWorker`/`JsonAPI` would use-after-free
on a `free()`). Previously it was `strdup`'d on every `CFG::load` (the char-item loop), on the
`0013` quote-strip, and on every runtime force (`JsonAPI`) - a recurring ~16 B leak that `0015`'s
overlay then orphaned. Patch `0016` makes it **literal-only**: the `daynight.force_mode` char-item
entry is removed, and `0013` + `JsonAPI` assign `"day"`/`"night"` string literals instead of
`strdup`. So `force_mode_cfg` is now *never* on the heap - nothing to leak, nothing to free, no
UAF, and a racing reader only ever sees a complete, immortal literal (atomic pointer swap). The
only side effect (`updateConfig` no longer auto-mirrors `daynight.force_mode` into `prudynt.json`)
is a no-op: the sidecar is the source of truth and `0015` discarded that JSON value anyway. CI
(both workflows) fails the build if any `force_mode_cfg = strdup` reappears.

**Net per runtime toggle:** one ~11 B atomic sidecar write - **no `prudynt.json` write at all**, at
runtime or boot. The legacy `daynight.enabled`/`daynight.force_mode` keys are **dead** after the
one-time `migrate`; the helper's `sync-to-config` verb still exists but is a **legacy/unwired**
rollback aid, off the boot path (CI fails the build if `S31prudynt` calls it). Tests:
`tests/test-daynight-state.sh` (helper get/set write-on-change/validation/atomicity, upgrade
migration, corrupt→auto, pid-aware reclaim, and the legacy `sync-to-config` mapping). Reviewed by
an adversarial QA pass; the confirmed findings were fixed.

**Scope (by design):** only the day/night mode and physical-privacy are persisted. `color`,
`ircut` and the 850 nm LED stay **derived runtime effects** of the day/night mode (unchanged
from before); the HA `color`/`ircut` toggles remain runtime-only and are intentionally not
persisted - see §1.

---

## 8. HA entity config toggles + Web UI status bar (Batch 1 & 2)

Two rootfs/web-only batches (no prudynt rebuild) that (a) make the MQTT/HA entities
manageable in the web HA-config page and fix the HA-daemon restart lifecycle, and
(b) add a Web-UI status bar with health-oriented CPU/RAM/Storage indicators plus a
PTZ Home button, a Physical Privacy toggle, and a read-only Shabbat indicator. All
status reads are read-only (no flash writes); the fast status channel polls only
while a page is open, so it adds zero device load when nobody is viewing the UI.

### 8.1 HA entity config toggles + `S93ha` daemon lifecycle (Batch 1 - `c5a43f4`)
- **Problem:** the Mic / Physical-Privacy / PTZ / Day-Night-Status / Shabbat MQTT
  entities already existed and published (default-on via `ha_entity_enabled`), but
  were not toggleable in the web HA-config page. `enable_ptz` was a **dead flag** -
  `ha-discovery` gated PTZ only on `command -v motors` and never read it. And `S93ha`
  called an **undefined** `wait_for_ha_shutdown` (it never sourced `ha-common`, so
  `HA_CAMERA_ID` was empty and the mosquitto reaping was skipped) → a non-graceful
  stop and a retained-`offline`/`online` **restart race** that could leave every HA
  entity stuck "unavailable" after a `killall` (it recovered only via a clean web
  "Save changes" → `S93ha restart`).
- **Solution:**
  - `config-ha.js` + `config-ha.html`: add enable toggles for `daynight_status`,
    `shabbat` (Sensors) and `mic`, `physical_privacy`, `ptz` (Switches).
  - `json-config-ha.cgi`: read / normalize / persist the 5 new `enable_` keys (it had
    a hardcoded 15-key whitelist; `jct import` deep-merges, so all keys survive).
  - `ha-discovery`: gate the PTZ buttons on `ha_entity_enabled ptz && command -v motors`
    (the existing `else` already `ha_clear_disc`'s them) so the toggle actually works.
  - `thingino-ha.json`: default `enable_ptz` **true** (so wiring the gate doesn't hide
    working PTZ) + explicit `true` defaults for the 4 opt-out entities.
  - `S93ha`: source `/usr/share/ha-common` (sets `HA_CAMERA_ID`) and **define** the
    bounded `wait_for_ha_shutdown` (`ps`-based poll ≤5 s on `ha-daemon|ha-commands`,
    then the existing SIGKILL escalation), making stop→start deterministic so the
    dying daemon's `offline` lands before the fresh daemon's `online`.
  - **Availability scheme is unchanged** (retained `online`, non-retained LWT
    `offline`, retained cleanup `offline`) - only the ordering was fixed.

### 8.2 Physical-privacy HA responsiveness (`30282b7`)
- **Problem:** `physical_privacy` was the only `ha-commands` handler that published the
  optimistic HA state **before** running the action, adding an MQTT round-trip in front
  of the lens command.
- **Solution:** reorder to **action-first** and background the trigger
  (`/sbin/physical-privacy on|off … &`) so its ~0.5 s guard/LED/mic setup never delays
  the publish; publish the optimistic state immediately; keep the delayed
  `{ sleep 3; ha-state; }` re-sync. Now matches every other handler. Files: `ha-commands`.

### 8.3 Web UI status bar (Batch 2 - `a335b044d`)
- **PTZ Home** button (`#ptz-home` → `GET /x/json-motor.cgi?d=r`), **Physical Privacy**
  toggle (`#physical-privacy` → new `/x/json-physical-privacy.cgi` → setsid-detached
  `/sbin/physical-privacy`), a read-only **Shabbat** indicator (non-clickable), and a
  minimal **CPU · RAM · Storage** badge - in the shared control-bar status row.
- Files: `control-bar.js`, `main.js`.

### 8.4 Fast status channel - READ-ONLY, page-open-only (`a335b044d`)
- New `/x/json-status-fast.cgi` sources **only** `/run`, `/proc`, and existing state
  files (the day/night sidecar, `physical-privacy-state.json`, `prudynt.json` via
  `jct get`; `/proc/jz/isp/isp-m0`). It **never** calls `prudyntctl`, never talks to the
  agent, and **writes nothing** (no flash wear, no `/tmp` cache, no state rewrite).
  `main.js` polls it ~2 s **only while the page is visible** (gated on `document.hidden`;
  torn down on `pagehide`/`beforeunload`), so a closed/hidden tab produces zero device load.
- Carries the fast-changing **UI state**: day/night (fixes the ~5-10 s lag),
  physical-privacy, shabbat, gain (`total_gain` - also fixing a reducer flicker where a
  mode-only update blanked it to "---"). It does **no** in-request sleep. CPU / RAM /
  Storage were later split onto the slower health channel (§8.7).
- **Speaker + Mic** stay on the pre-existing `prudyntctl` mic-poll (both runtime-only;
  `spk_enabled` is **not** persisted to `prudynt.json`, confirmed on device), with
  `spk_enabled` folded into that **same** existing 15 s query - no new fork.
- Files: `json-status-fast.cgi` (new), `json-physical-privacy.cgi` (new),
  `prudynt-status.cgi` (new), `thingino-webui.mk` (installs the 3, `-m 0755`).

### 8.5 Health metrics: CPU / RAM / Storage
- **CPU** (`2e6a51cf`, `61be971a`): a 1-min **load average was misleading** (it showed
  ~332 % while real CPU was ~25 %). Now a real `/proc/stat` delta (idle = idle+iowait,
  busy = total-idle, clamped 0-100), then a **browser-side rolling average** of the last
  ~12 samples (~84 s at the ~7 s health cadence, see §8.7) shown as `CPU avg NN%` so single
  per-frame bursts don't register and only **sustained** load moves it; it turns amber at a
  sustained avg ≥ 90 %. Still the real total system CPU; averaging is JS-only (no writes, no
  daemon, page-open-only).
- **RAM** (`2e6a51cf`, `3b000cd3`): used% + used/total MB from `/proc/meminfo`. This
  Ingenic kernel has **no `MemAvailable`**, so the fallback is
  `MemFree+Buffers+Cached+SReclaimable` (matches busybox `free`'s used-excluding-cache;
  omitting `SReclaimable` read ~5 % high).
- **Storage** (`f0fcbca7`): the writable config overlay (`df /`) used% + free (adaptive
  KB/MB/GB) - the small jffs2 partition that fills up and breaks config, so the most
  health-relevant "storage" to watch.
- **Observer note:** the preview page runs an MJPEG stream (`/x/ch0.mjpg` → `exec
  prudyntctl mjpeg -f 5`), so prudynt JPEG-encodes and `uhttpd` serves it → total CPU
  legitimately climbs *while viewing* (per-thread debug: `uhttpd` ~8 %, network softirq
  ~9 %, `prudyntctl mjpeg` ~2 %, prudynt threads up). The rolling average keeps the
  badge readable through that; it is not a bug. `/tmp` is `tmpfs` (RAM) - none of its
  runtime files (`ha_state_cache`, `colormode.txt`, `ircutmode.txt`, IMP/ISP info,
  `resolv.conf`, …) touch flash.

### 8.6 Restart-Streamer fix (`a335b044d`)
- **Problem:** the footer "Restart streamer" fired `service restart prudynt &` with no
  `setsid` (killable mid-reclaim), and `footer.js` reloaded after a fixed 3 s into a
  ~17 s restart → onto a dead stream.
- **Solution:** `restart-prudynt.cgi` **setsid-detaches** the restart (survives the CGI
  close; `nohup` fallback) + CRLF headers; `footer.js` records prudynt's pid then polls
  new `/x/prudynt-status.cgi` until a **new** pid appears (restart truly complete, ~17 s,
  30 s cap) before reloading. Files: `restart-prudynt.cgi`, `footer.js`, `prudynt-status.cgi`.

### 8.7 Pre-release audit fixes (verified review)
A 7-dimension adversarial audit + verification pass before public release (see memory
`pre-release-audit-verified`). Confirmed items are fixed here; false positives and
pre-existing/accepted items are recorded so a reviewer does not "re-fix" them.

- **go2rtc supervision - NOT APPLICABLE (verified false positive).** go2rtc is **not** in the
  PT2 build: absent from the defconfig, `package/go2rtc/Config.in` has no `default y`, and
  on-device there is no binary, init script, process, or listening port. `thingino-ha` never
  references it; the only Web UI WebRTC path (`preview-raptor.html` + `webrtc-whip.cgi`) belongs
  to the `thingino-raptor` streamer variant, which PT2 does **not** select (it ships plain
  `preview.html` = MJPEG/RTSP). go2rtc runs on a **separate LAN host**; supervising it is that
  host's concern. Action: **none on-camera** (already uninstalled); doc corrected (header + §G).

- **[BLOCKER - fixed] `json-motor.cgi` command injection → root RCE.** The endpoint parsed the
  query with `eval $(echo "$QUERY_STRING" | sed "s/&/;/g")` = arbitrary shell as root. Pre-existing
  upstream, but this branch newly wires an authenticated caller to it (PTZ Home button → `?d=r`),
  so it is a release blocker for PT2. **Fix:** replaced the `eval` with a safe `IFS='&'` field-parse
  loop (`set -f` around the split to disable glob expansion of `$QUERY_STRING`; only `d`/`x`/`y`
  recognized), kept the existing `case "$d"` verb allow-list as the trust boundary, and constrained
  `x`/`y` to numeric chars (`'' | *[!0-9.-]*` → `0`). PTZ behavior is
  unchanged: verb routing identical, fractional/negative `d=g` moves preserved, `d=x` still returns
  "unsupported" exactly as before (no scope creep). Verified: `sh -n` clean; injection payloads
  (`;touch`, `$(...)`, backticks, `|rm -rf /`) execute nothing and are rejected/zeroed. No flash
  writes. File: `www/x/json-motor.cgi`.

- **[minor - fixed] SSE reconnect could resurrect on a hidden tab.** The heartbeat SSE `onerror`
  scheduled `setTimeout(heartbeat, …)` into an **untracked** timer, so `cleanupHeartbeatResources()`
  (run on `visibilitychange`→hidden) could not cancel it and a late-firing timer reopened the
  EventSource (one 5 s server-side curl loop) on a hidden tab - breaking the "zero load when hidden"
  contract. (The 2 s/15 s pollers did **not** resume - they self-gate on `document.hidden`.) **Fix:**
  track the timer in `heartbeatReconnectTimer`, clear it in `cleanupHeartbeatResources()`, and
  early-return `heartbeat()` when `document.hidden`. No polling or reconnect while hidden/closed.
  File: `main.js`.

- **[overhead - fixed] Split CPU/RAM/Storage onto a slower cadence.** The 2 s fast channel computed
  CPU via a 300 ms in-request `usleep` and parsed `/proc/meminfo` + `df` every poll; these health
  metrics do not need 2 s freshness. **Fix:** the fast channel (`json-status-fast.cgi`, ~2 s) now
  carries **UI state only** (day/night, physical-privacy, shabbat, gain) with **no in-request
  sleep**; CPU/RAM/Storage moved to a new **`json-status-health.cgi`** polled at **~7 s**. Both
  channels are read-only, single-in-flight, and page-visible-only (`document.hidden` gated, torn down
  on hide/unload) - no writes, no daemon, no background load when not viewing. CPU rolling-average
  window ≈ 12 samples (~84 s at 7 s). Files: `json-status-fast.cgi` (trimmed), `json-status-health.cgi`
  (new), `main.js`, `thingino-webui.mk` (installs the new CGI, `-m 0755`).
- **[first-paint - fixed] Date/hour on the health channel + immediate first fetch.** After reboot the
  Web UI clock (`#time-now`) lagged ~7-12 s because `time_now` came **only** from the agent SSE, whose
  first streamed event is late even when the agent itself answers a one-shot `curl` in <2 s (the file
  CGIs and the `window.load` gate were measured fast - 262 ms load, <0.3 s CGIs - so neither was the
  cause). **Fix:** `json-status-health.cgi` now also emits `time_now` = the **camera's own** `date +%s`
  (JSON number, or `null` if unavailable → reducer keeps its last value); the reducer already applies
  the configured timezone via `resolveDeviceTimezone()`, so this is device-authoritative time, **not** a
  browser-local clock. It rides the health channel (not the 2 s fast path) by request. The health
  channel's first fetch runs **immediately** on page open/visible (`heartbeat()` → `startHealthStatus()`
  → `fetchHealthStatus()`, no pre-wait), then repeats every ~7 s, and stops completely when hidden/closed
  (`cleanupHeartbeatResources()` on `visibilitychange`/`pagehide`/`beforeunload`; `heartbeat()` self-guards
  on `document.hidden`). So Date/CPU/RAM/Storage all paint on the first health fetch (<1 s) after open.
  Note: **Gain** was independently made fast by the split above - `json-status-fast.cgi` emits `total_gain`
  from `/proc/jz/isp/isp-m0`, so on the current (pre-flash) build gain rides the slow agent SSE, but the
  pending build paints it from the fast file channel. File: `json-status-health.cgi`.

- **[pre-existing - documented, no code change] Dual-pin IR-cut has no HW read-back** - see §E's
  read-back-scope note and memory `ircut-dualpin-verify-lies`. Behavior unchanged.

- **[pre-existing - accepted, no code change] Boot privacy-semantics window (~100 s)** after power
  loss during privacy - already documented (§E; `physical-privacy` header). Behavior unchanged.

- **Not done (by request): cross-boot reboot circuit-breaker.** The audit flagged that
  `S32prudyntwd`'s restart budget is process-local and resets each boot (a deterministic,
  reboot-surviving bringup fault could slow-loop). **Deferred** - it would need a new persistent
  counter (U-Boot env), and no new persistent writes are being added without approval. Allowed
  persistent writes remain `/etc/daynight.state`, `/etc/physical-privacy-state.json`, and normal
  explicit config saves only.

### Files (Batch 1 & 2)
| File (repo path) | Action | Installs to (device) |
|---|---|---|
| `package/thingino-ha/files/config-ha.js` | Modified (+5 toggles) | `/var/www/a/config-ha.js` |
| `package/thingino-ha/files/config-ha.html` | Modified (+5 checkboxes) | `/var/www/config-ha.html` |
| `package/thingino-ha/files/json-config-ha.cgi` | Modified (+5 `enable_` keys) | `/var/www/x/json-config-ha.cgi` |
| `package/thingino-ha/files/ha-discovery` | Modified (PTZ gate) | `/usr/sbin/ha-discovery` |
| `package/thingino-ha/files/ha-commands` | Modified (physical-privacy action-first) | `/usr/sbin/ha-commands` |
| `package/thingino-ha/files/thingino-ha.json` | Modified (`enable_` defaults) | seed → `/etc/thingino.json` |
| `package/thingino-ha/files/S93ha` | Modified (`wait_for_ha_shutdown` + source `ha-common`) | `/etc/init.d/S93ha` |
| `package/thingino-webui/files/www/a/control-bar.js` | Modified (bar buttons + status column) | `/var/www/a/control-bar.js` |
| `package/thingino-webui/files/www/a/main.js` | Modified (toggles, reducer, fast poll, CPU avg) | `/var/www/a/main.js` |
| `package/thingino-webui/files/www/a/footer.js` | Modified (restart liveness poll) | `/var/www/a/footer.js` |
| `package/thingino-webui/files/www/x/restart-prudynt.cgi` | Modified (setsid + CRLF) | `/var/www/x/restart-prudynt.cgi` |
| `package/thingino-webui/files/www/x/json-status-fast.cgi` | **Added** | `/var/www/x/json-status-fast.cgi` |
| `package/thingino-webui/files/www/x/json-physical-privacy.cgi` | **Added** | `/var/www/x/json-physical-privacy.cgi` |
| `package/thingino-webui/files/www/x/prudynt-status.cgi` | **Added** | `/var/www/x/prudynt-status.cgi` |
| `package/thingino-webui/thingino-webui.mk` | Modified (install 3 new cgis, `-m 0755`) | build rule |

All are shell / JS / JSON / Makefile - **no prudynt rebuild**. New cgis are git mode
`100755` and explicitly listed in `thingino-webui.mk` (which installs every `www` file).

### Commits (branch `pt2-firmware`)
- `c5a43f4` feat(thingino-ha): configurable MQTT entity toggles + deterministic S93ha restart
- `30282b7` fix(thingino-ha): physical_privacy triggers the lens move before the optimistic publish
- `a335b044d` feat(webui): Batch 2 - PTZ Home + Physical Privacy + Shabbat/CPU-RAM, fast status channel, restart fix
- `2e6a51cf` fix(webui): accurate CPU%, memory MB, gain flicker, Shabbat "Not Ready" label
- `036bd37e` fix(webui): show live speaker state faster via the existing mic poll
- `f0fcbca7` feat(webui): add storage usage to the status badge
- `3b000cd3` fix(webui): RAM% matches busybox free on kernels without MemAvailable
- `61be971a` feat(webui): CPU badge shows a rolling average (health monitoring)
- `07791b143` fix(webui): pre-release audit - json-motor RCE, SSE hidden-tab, fast/health split + clock

## 9. Default settings (PT2 device profile)

These change the **factory defaults** baked into a fresh image. They take effect on a full flash /
factory reset; a config-preserving upgrade keeps the existing `/etc` values. All live in the PT2
**device override** files, deep-merged via `jct import` at build time (verified recursive merge in
jct `merge_object_into`: only the listed leaf keys change, every base sibling is preserved). No
prudynt rebuild.

### 9.1 Streamer / OSD / photosensing / audio -> `configs/cameras/sonoff_pt2_t23n_sc2336p_atbm6012bx/prudynt.json`
| Setting (Web UI) | json path | Base default | New default |
|---|---|---|---|
| Main RTSP Bitrate | `stream0.bitrate` | 3000 | **2048** |
| Sub RTSP Bitrate | `stream1.bitrate` | 1000 | **1024** |
| Main OSD Logo | `stream0.osd.logo.enabled` | true | **false** |
| Sub OSD Logo | `stream1.osd.logo.enabled` | true | **false** |
| Main OSD time Format | `stream0.osd.time.format` | `%F %T` | **`%d-%m-%Y %T`** |
| Sub OSD time Format | `stream1.osd.time.format` | `%F %T` | **`%d-%m-%Y %T`** |
| Photosensing "switch to night above" | `daynight.total_gain_night_threshold` | 3000 | **20000** |
| Photosensing "switch to day below" | `daynight.total_gain_day_threshold` | 300 | **260** |
| Speaker volume | `audio.spk_vol` | 80 | **65** |
| Speaker gain | `audio.spk_gain` | 20 | **25** |

- **RC Mode (Main+Sub) left at CBR.** The user requested SMART, but on the T23N prudynt force-overrides
  H264 SMART->CBR at encoder init (`IMPEncoder.cpp:369` `LOG_WARN("T23: forcing H264 RC mode SMART -> CBR
  for encoder stability")`), so SMART is a no-op on this chip. Per the user's decision, mode stays **CBR**
  (the base default) - no override written.
- **Photosensing hysteresis** stays valid: day(260) < night(20000); the 260..20000 gap is the intended
  dead-zone. `night_count_threshold`=6 / `day_count_threshold`=4 still gate a switch. These thresholds are
  read from prudynt.json and are NOT touched by the 0015 sidecar (which only overrides `daynight.enabled`/
  `force_mode`).
- **OSD-format note:** a default is applied at prudynt **startup** (fresh bring-up), so it does NOT hit the
  live-reconfig VPU freeze. That freeze is triggered only by a *live* UI OSD edit, because the web UI
  gratuitously attaches a `ThreadVideo` rebuild to every OSD change (`preview.js` `sendOsdUpdate`) -> prudynt
  `global_restart_video` -> the issue2 T23 teardown/rebuild hazard. A UI-side fix (don't request a video
  rebuild for pure text/format edits) is a possible follow-up.

### 9.2 Blue LED -> `configs/cameras/sonoff_pt2_t23n_sc2336p_atbm6012bx/thingino.json`
| Setting | json path | Base | New |
|---|---|---|---|
| Blue LED active on boot | `gpio.led_b.active_on_boot` | true | **false** |

- Makes the config match observed reality: the blue LED is already **off** at boot due to a boot-order
  race - `F00ledd` starts the `ledd` daemon and hands it pin 57 to blink (snapshotting the cold-boot OFF
  state); `S05led`'s `gpio set 57 1` (which DOES honor `active_on_boot`) is blinked over, then `rm
  /run/ledd/*` makes `ledd` restore pin 57 to the OFF snapshot. So `active_on_boot=false` = LED off at boot
  (the desired state). **Update:** the `ledd`-race fix has since been implemented (§10.3 - `F00ledd` skips
  handing active-on-boot LEDs to the blink daemon), so setting `active_on_boot=true` now keeps the blue LED ON
  at boot; for the shipped default (`false`) the pin is simply left off.

### Investigation answers (recorded)
- **`buffers=-1` is intentional** (auto: `max(2,(fps+9)/10)` ring depth, RAM-clamped, floor 2 on T23).
  NOT changed - forcing `1` would drop below the intended minimum and risk frame stalls / encoder starvation.
- **"Enable photosensing on boot"** = `daynight.enabled` (auto light-based day/night switching). On this
  build the persistent boot authority is the `/etc/daynight.state` sidecar (patch 0015 overwrites
  `daynight.enabled`/`force_mode` from it at load); missing/corrupt sidecar -> forced day (safe). Not changed.

### Files (§9)
| File (repo path) | Action |
|---|---|
| `configs/cameras/sonoff_pt2_t23n_sc2336p_atbm6012bx/prudynt.json` | Modified (+audio/daynight/stream0/stream1 overrides) |
| `configs/cameras/sonoff_pt2_t23n_sc2336p_atbm6012bx/thingino.json` | Modified (`gpio.led_b.active_on_boot` -> false) |

## 10. Reference-parity fixes (from the 7c43396 comparison)

Compared our tree to the user's older reference firmware (`thingino-firmware-7c43396`, prudynt `3c8e8350`,
Web UI fast + OSD/LED flawless). Findings adversarially verified; the fixes below are PORTABLE (keep our
prudynt `f4b32289` + all our features).

### 10.1 OSD live-edit no longer freezes the stream (DONE)
- **Problem:** editing an OSD field live (e.g. time Format) wedged the stream on a static frame until
  `S31prudynt restart` (the issue2 T23 VPU freeze). The reference does NOT freeze, but its Web UI is
  byte-identical (both send `restart_thread: ThreadVideo | ThreadOSD`) - the only difference is the prudynt
  version (reference `3c8e8350` predates the f4b3228 `global_restart_video` teardown regression).
- **Fix (webui-only):** `preview.js` `sendOsdUpdate` + `setFont` now send `restart_thread: ThreadOSD` (drop
  `ThreadVideo`). On f4b3228 the action handler decodes only rtsp/video/audio (`JsonAPI.cpp:1544-1552`);
  `ThreadOSD` (bit 8) raises NO restart flag, so `global_restart_video`/the teardown never runs -> no freeze.
  The OSD config write still lands via `handle_osd`, and the OSD worker re-reads text fields live each second
  (`OSD.cpp:1184`), so time/usertext/uptime + their format/position/color apply within ~1s.
- **Tradeoff (accepted):** structural OSD changes (enable/disable an item, logo, font/stroke) are saved but
  apply on the next prudynt restart rather than live - a clear net win over wedging the stream. A future
  prudynt patch could decode `ThreadOSD` as a real OSD-only re-render (no video teardown) to make those live.
- **Files:** `package/thingino-webui/files/www/a/preview.js`.

### 10.2 Photosensing "Enable photosensing on boot" checkbox removed (DONE)
- Under our sidecar model (patch 0015), `/etc/daynight.state` mode is the single source of truth (`auto` =>
  photosensing active; `day`/`night` => off), enforced at every prudynt load and mirrored by every runtime
  reader (json-status-fast.cgi, agent adapter, ha-state). The separate `daynight.enabled` checkbox was a
  pre-sidecar vestige: its saved value is overwritten by 0015 on next load, and it could push a transient
  live `daynight.enabled` contradicting the sidecar (mode=auto but photosensing paused). **Fix:** removed the
  checkbox (config-photosensing.html) + dropped `"enabled"` from `dayNightParams` (config-photosensing.js);
  the dashboard Auto/day/night control (which writes the sidecar) is the single enable/disable path.
  Thresholds unchanged. **Files:** `config-photosensing.html`, `a/config-photosensing.js`.

### 10.3 Findings recorded (not changed / pending decision)
- **`buffers` stays `-1` (NOT 1).** Reference used `1` only because its older prudynt (`3c8e8350`) had no auto
  logic and its validator rejected `-1`. Our f4b3228 `-1` = auto (fps/RAM-scaled, floor 2 on T23) is strictly
  better; a literal `1` would force one VB below the T23 floor and risk stalls. Unrelated to the reference's
  UI speed. No change.
- **Blue-LED boot race (DONE - `F00ledd`).** Reference blinks a sentinel file (`/run/boot`) and stops `ledd`
  at end of boot, so `S05led` is the last writer. Ours blinks the real per-pin `/run/ledd/57` and never stops
  `ledd` at boot, so `ledd` clobbers `S05led`'s `gpio set`. **Fix:** in `overlay/etc/init.d/F00ledd`, skip
  handing an `active_on_boot` LED to the blink daemon (don't write its `/run/ledd/$pin` file), so `ledd` never
  touches that pin and `S05led` sets it uncontested. Chosen over the `S05led rm-before-set` option because
  deleting a `/run/ledd/$pin` makes `ledd` RESTORE the pin to its cold-boot OFF snapshot (a delete-then-set
  race that could re-clobber the ON); never blinking the pin is race-free. Truthiness matches S05led's
  `bool_flag`; non-active LEDs still blink for boot progress. (Our default is LED-off, so this benefits users
  who set active-on-boot=true.) File: `overlay/etc/init.d/F00ledd`.
- **UI-vars display speed (RESOLVED: SSE kept on-demand at 5s + daynight/gain de-duped; daemon NOT re-enabled).** Reference paints every var
  from ONE 1s SSE that `cat`s a local cache (`/tmp/heartbeat_cache.json`) maintained by `S99heartbeat`.
  CRITICAL: our `S99heartbeat` is DELIBERATELY DISABLED (commented out in `thingino-webui.mk:62-63`) - it is an
  ALWAYS-ON 1 Hz daemon that calls `prudyntctl` every second regardless of viewing (continuous idle load); our
  branch replaced it with the on-demand agent SSE (zero load when not viewing) per the zero-idle-load design.
  Re-pointing the SSE to the cache would require re-enabling that always-on daemon = 24/7 background load,
  contradicting that design. Also, the vars actually reported slow (date/gain/CPU/RAM/day-night/mic-speaker)
  are ALREADY fast via the on-demand file channels (§8.4-8.7); the remaining agent-SSE vars (uptime/rec/motion/
  wg/ir states) change slowly and first-paint in ~1-2 s once the agent is up (the 7-12 s was agent warmup after
  reboot, which an always-on daemon would not fix either). DECISION: keep the on-demand agent SSE (zero idle
  load) at **5 s**. A brief experiment dropped the SSE poll to 2 s, but the expanded review found the agent
  heartbeat is the HEAVIEST channel (a `prudyntctl` IPC + ~a dozen forks + four `light/ircut read` execs per
  beat), so 2 s tripled that while-viewing load for slow-changing vars that don't need it -> reverted to 5 s.
  Additionally, `daynight_mode`/`daynight_enabled`/`total_gain` were removed from the agent heartbeat payload
  (they are served by the fast file channel `json-status-fast.cgi`) to stop double reducer updates + badge
  flicker and shrink the heartbeat. Files: `json-heartbeat.cgi` (5 s), `thingino-agent-adapter-prudynt` (de-dup).

### 10.4 Live-apply for structural OSD changes (patch 0017 - enable/disable + logo)
- **Problem:** after §10.1 dropped `ThreadVideo` from OSD edits (to kill the freeze), structural OSD changes
  (item enable/disable, logo on/off) were saved but only applied on the next prudynt restart. The reference
  applied them live via the video rebuild - which freezes on f4b3228. A design workflow's adversarial verify
  confirmed the naive "OSD re-init" (`osd->exit()+init()`) would `IMP_OSD_DestroyGroup`/`CreateGroup` a group
  still `IMP_System_Bind`-bound to the encoder = the issue2 freeze trigger (per 0002). Rejected.
- **Fix (prudynt patch 0017, phased scope):** apply changes at the OSD-region layer ONLY, never touching the
  group/encoder bind. `OSD::init()` now creates the time/usertext/uptime/logo regions UNCONDITIONALLY with
  `grpRgnAttr.show = <item>_enabled ? 1 : 0` (disabled = created-but-hidden; brightness unchanged). New
  `OSD::applyStructural()` toggles visibility via `IMP_OSD_ShowRgn(rgn, osdGrp, enabled?1:0)` - NO runtime
  Create/Destroy/DestroyGroup. Trigger: `handle_action` decodes the `ThreadOSD` bit (`mask & 8`, already sent
  by `preview.js`) -> sets new `std::atomic<bool> video_stream::osd_reinit` (mirrors 0004's flag/exchange
  idiom, NOT `global_restart_video`) -> `OSD::thread_entry` consumes it (`exchange(false)`) on the OSD worker
  thread and runs `applyStructural()` -> live within ~100 ms. No webui change needed.
- **Scope / NOT covered (still need a restart):** font_size / stroke_size (needs `libschrift` re-init +
  glyph-cache clear), and turning a stream's WHOLE OSD on from fully off (no OSD object/worker exists when
  `stream.osd.enabled=false` at encoder create).
- **STATUS: UNTESTED** - prudynt binary patch; needs the prudynt-binary CI build + on-device verification
  (toggle each OSD item live; confirm ~1 s apply AND that the stream never freezes on toggle). Static review
  passed (`global_video` visible in JsonAPI.cpp; `IMP_OSD_ShowRgn` signature matches `OSD.cpp:1119`; config
  bools exist); applies cleanly (`patch -p1`, verified). Rollback = delete the patch file (reverts to the
  safe apply-on-restart behavior).
- **Files:** `package/all-patches/prudynt-t/0017-osd-live-structural-apply.patch` (patches prudynt
  `src/OSD.cpp`, `src/OSD.hpp`, `src/JsonAPI.cpp`, `src/globals.hpp`).

### 10.5 On-device diagnostics + gain-regression fix (live camera)
Connected to cam4 via SSH key auth (read-only) to investigate two reported issues.
- **Gain sensor stuck at 8 (REGRESSION from §10.3 de-dup) - FIXED.** The gain badge is fed by the agent
  heartbeat's `total_gain` (real value, e.g. 380, from `prudyntctl daynight.status.total_gain`).
  `json-status-fast.cgi` ALSO tried to read `total_gain` from `/proc/jz/isp/isp-m0` - but that node has
  **no `total_gain` field** (only "ISP Tgain DB" in dB + raw sensor gains), so it always emitted `-1`,
  which the reducer treats as "no gain" and falls back to `daynight_brightness` (= `brightness_percent`
  = **8**). The §10.3 de-dup removed `total_gain` from the agent heartbeat, leaving only the broken file
  read -> badge showed 8. **Fix:** restore `total_gain` to the agent heartbeat (daynight_mode/enabled stay
  de-duped - those DO come correctly from the fast channel), and remove the dead `-1` read from
  `json-status-fast.cgi`. Files: `thingino-agent-adapter-prudynt`, `json-status-fast.cgi`.
- **CPU badge "100%" - NOT a runaway; measurement/observer artifact.** On-device: prudynt ~31% of the
  single T23N core (expected dual-stream H.264 HW-encode + ISP + audio + OSD + RTSP baseline; `loglevel`
  INFO, zero log spew, no debug build), idle total ~35-45%. The health CGI's own 300 ms CPU calc, run
  standalone, gives 32-47% (NOT 100%). The badge reads ~100% because it samples over a short 300 ms window
  WHILE the Web UI generates concurrent load (2 s fast poll ~10 forks + SSE + especially the MJPEG
  preview's `prudyntctl mjpeg` + `uhttpd`) -> the single core saturates during the sample window. The idle
  ~31% is normal. **FIXED (raw-jiffy browser-diff):** `json-status-health.cgi` now emits raw `cpu_total`
  + `cpu_idle` /proc/stat counters with NO in-request `usleep`; `main.js` computes %CPU as the delta
  across consecutive ~7 s health polls (rolling mean of 4 ≈ 28 s). Result: a smooth, true system-CPU
  average (~30%) instead of the spiky 300 ms-window artifact, and the 300 ms CGI sleep is removed
  (lighter). Files: `json-status-health.cgi`, `a/main.js`.

### On-device validation
- **HA:** the 5 toggles appear in the HA-config page and persist; entities publish; a
  `S93ha restart` (or web "Save changes") is clean - no `wait_for_ha_shutdown: not found`,
  no entities stuck unavailable.
- **Bar:** PTZ Home recenters; Physical Privacy toggles cleanly (no flicker); Shabbat reads
  "Ready" / "Not Ready"; CPU shows `avg NN%` (≈25 % idle, higher while previewing), RAM
  matches `free`, Storage matches `df -h /`.
- **Fast channel:** day/night + gain update in ~2 s; polling stops when the tab is hidden.
- **Flash:** status is read-only; the only persistent flash writes remain the day/night
  sidecar (`/etc/daynight.state`) and physical-privacy state (§7, §5).

---

## Build & validate (reminder)
- prudynt C++ ships **only** via patches `0001`-`0016` in `package/all-patches/prudynt-t/`
  (auto-applied; source git-fetched at `f4b3228` - never hand-edit source). There is **no**
  `package/all-patches/thingino-jct/` (the jct atomic-write fork was dropped; `prudynt.json` uses
  stock jct).
- **Shim sha gate (critical):** the shipped `/usr/lib/libuclibcshim.so` MUST be the pinned
  proven-good build - sha256 `07710f80ebc4683c9a9c658142a3cdce018db2949b844647e1709446e21c369b`
  (§3-K). `ingenic-uclibc.mk` verifies the pinned prebuilt before install, and **both** CI
  workflows fail the run if the shim in the rootfs/staging is anything else (a rebuilt shim is
  unverified and was what crashed every prudynt). Re-pin only after verifying prudynt boots.
- After build, confirm `package/all-patches/prudynt-t/` holds **exactly** `0001`-`0016`. In the
  staged image, verify the motors split (§4); that `0007`/`0013`/`0014` are in the binary (grep
  `add_strk_a_rtsp` for `0014`); that the prudynt source/binary **does** reference the literal
  `/etc/daynight.state` (patch `0015`'s direct-read overlay - distinct from the
  `prudynt_daynight_state` Prometheus metric); that **no `force_mode_cfg = strdup` remains** in
  the source (patch `0016` makes it literal-only - leak-free); that `/usr/sbin/daynight-state` is
  present + executable; and that `S31prudynt` calls `migrate` and does **not** call
  `sync-to-config` before `start_daemon`. (The `pt2-build-artifact` / `prudynt-binary` workflows
  enforce all of this.)
- Offline: run `sh tests/test-daynight-state.sh`.
- On device after flash: forced-night boot (optics == colour mode), Day↔Night↔Auto via MQTT
  with **no** `prudynt.json` size change, physical-privacy enter/exit keeps the mode, a
  power-cut mid-toggle leaves a valid mode, `tz-update` date math, rcK stop < 60 s, motors interlock.
