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

> **Current complete record:** **section 13** (bottom of this file) is the consolidated,
> standalone engineering record as of branch tip `f256057a9` (build
> `pt2-firmware+f256057`, validated live on cam4). Start there for the full picture -
> every problem, fix, deployment status, validation, remaining risk, and open follow-up.
> Sections 1-12 below hold the deeper historical per-fix detail.

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
- **prudynt binary:** 17 source patches `0001`-`0017` (auto-applied by buildroot's global
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
- **No separate persistence for `color` / `ircut` / IR-LED - by design, not a gap.** Only the
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
| `package/all-patches/prudynt-t/0001…0017-*.patch` | Added (in branch) | prudynt-t (build patches) | compiled into `/usr/bin/prudynt` | ✅ binary |
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
  a healthy latch is therefore not software-detectable - an accepted HW limitation, behavior
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
  (auto-restart) **on its own host** - go2rtc is not installed or run on this camera, so on-camera
  supervision is out of scope. The camera's responsibility is to keep prudynt's RTSP (`:554`) up,
  which the watchdog (§B) probes directly, complementing prudynt fail-fast (§A). (Confirmed in the
  pre-release audit - see §8.7 - that go2rtc is not in the PT2 image, so the earlier "unsupervised
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
| ensure a valid sidecar at boot | `S31prudynt start()` → `daynight-state migrate` | leaves a valid sidecar untouched; seeds forced **`day`** when it is **missing or corrupt** (self-heal); reclaims an orphaned temp. Does **not** read `prudynt.json` - the legacy daynight keys are **dead** |

The `0015` overlay maps the sidecar to prudynt's members the same way `IMPSystem::init` consumes
them (`force_mode` applied **regardless of** `enabled`): `auto` → `force_mode_cfg=""` +
`enabled=true`; `day` → `force_mode_cfg="day"` + `enabled=false`; `night` → `force_mode_cfg="night"`
+ `enabled=false`. It allocates nothing (static string literals) and frees nothing, so it adds no
memory leak and cannot use-after-free the lock-free readers. A missing / unreadable / malformed
sidecar falls back to **forced `day`** (`force_mode_cfg="day"` + `enabled=false`) and never crashes
- the chosen safe default for broken persistence, applied identically by the overlay, the
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

## 11. Day/night wedge fix, OSD live-apply verification, heartbeat perf, flash-overlay audit
Session 2026-07-03 (branch `pt2-firmware`). Commits: `79933cac0` (ircut fast-path),
`9da70dad0` (day/night wedge fix), `003a5ef67` (OSD comment correction). All verified on cam4.

### 11.1 Extended production-readiness tests (on-device)
- **RTSP:** 5/5 `OPTIONS -> 200`. **Leak:** prudynt RSS 8 MB / mem 17 MB / 35 threads flat over 45 s.
- **Load times (server-side):** jct parse ~20 ms, health CGI ~20 ms, fast CGI ~66 ms, agent heartbeat ~650 ms (see 11.2).
- **Live config-apply:** `image.brightness` 128->160 instant, RTSP never dropped, reverted clean.
- **Reboot recovery:** SSH back ~32 s, RTSP `200` ~2 s later; all config persisted (bitrate/thresholds/led_b); blue LED off at boot (F00ledd fix confirmed, gpio 57 = 0); idle load 0.37.
- **Gain sensor:** live `total_gain` 330-429 (the §10.5 fix confirmed working).

### 11.2 Heartbeat perf - `ircut read` fast-path (`79933cac0`)
Agent heartbeat ~650 ms/call; breakdown: `prudyntctl`(total_gain)+`wg` ~0 ms, the 4 GPIO reads ~266 ms (`ircut read` ~100-133 ms alone). `ircut read` re-ran full `load_ircut_config` (~4 jct parses) before just returning `/tmp/ircutmode.txt`. Added a `read`/`status` fast-path BEFORE `load_ircut_config` (apply/switch paths untouched). Validated: `ircut read` ~100 ms -> ~0 ms, identical output. The remaining ~200 ms (3 `light read`) DEFERRED (a cache would regress the live IR toggle buttons; needs set-path invalidation).

### 11.3 Day/night night->day wedge - FIXED (`9da70dad0`)
`daynight day` could silently no-op and strand the camera in night. Root cause: `switch_to_day`/`switch_to_night` (deployed source = `package/prudynt-t/files/daynight`, which overrides the stock thingino-ircut copy) deduped on `/run/prudynt/daynight_mode` (MODE_FILE) - a file the executor NEVER writes (prudynt owns it) and which lags/desyncs (stays "day" after a night switch). Fix: dedup + toggle now key on a script-owned `/run/daynight.applied` marker, written only on a fully-verified switch (`_rc=0`); MODE_FILE/$state kept only for read/status. Cannot wedge (night->day always actuates unless WE last applied day). The tmpfs marker is absent at boot -> the first boot switch actuates -> ALSO fixes the boot IR-cut settle window (the boot day-apply previously deduped against prudynt's early "day"). Night switch is freeze-clean (fps held through). Validated on cam4 (exact wedge sequence now restores day; same-mode re-send still deduples - no IR-cut re-pulse). Adversarial review (correctness/regression/failure-contract) = 3/3 pass, no blockers. Pre-existing (not introduced) minor: chronic ircut-verify-failure re-pulses the latch (§ ircut-dualpin-verify-lies).

### 11.4 OSD structural live-apply - VERIFIED, comments corrected (`003a5ef67`)
Patch 0017 (`OSD::applyStructural`) IS in the deployed binary (`strings` confirms `_ZN3OSD15applyStructuralEv`). Verified on-device by snapshot: **enabling the logo applied LIVE within ~1 s with NO stream freeze** (fps held 16); **font_size 64 did NOT enlarge the live text** (font/stroke still need a restart - 0017 doesn't re-init libschrift). So OSD item enable/disable + logo apply live; font/stroke need restart. `prudyntctl json` OSD writes are IN-MEMORY (flash byte-identical after the test). Corrected the stale `preview.js` comments that claimed enable/logo need a restart and that ThreadOSD is a no-op. (0017 is now verified-working, closing its "untested" status.)

### 11.5 SSE cadence - verified already 5 s (no change)
`json-heartbeat.cgi` `HEARTBEAT_INTERVAL:-5`, `main.js` `HeartBeatReconnectDelay = 5000`; deployed matches. (`6ee073d55` handled it earlier.)

### 11.6 Flash-overlay write audit (report-only - NO changes made)
Question: does anything write persistent flash (jffs2 `/overlay`, 224 KB) except day/night mode + physical-privacy state, and can normal/UI use fill it? Method: a 120 s on-device steady-state monitor + a fanned-out code trace of every subsystem (logs, heartbeat/cache, OSD, HA/MQTT/WG, LED/IR, privacy/config/markers) + an adversarial repo-wide sweep for flash-write patterns; cross-checked.

**IDLE / steady-state (streaming + MQTT + heartbeat, no user action): NO overlay growth.**
- Empirical: **0 overlay files written in 120 s; df delta 0 KB.**
- Code trace: 0 idle flash writers in every subsystem. Logs -> tmpfs (syslogd `-C64` 64 KB RAM ring; `/var/log -> /tmp`); heartbeat/status/health/fast CGIs read-only (mktemp in /tmp, rm'd same call); OSD live edits -> `prudyntctl json` = IN-MEMORY; HA/MQTT publish-only (state cache in /tmp); LED/IR = GPIO (no file); ircut marker `/tmp/ircutmode.txt` + daynight marker `/run/daynight.applied` = tmpfs; `/etc/resolv.conf -> /tmp`; `/etc/onvif.json` = boot-only (S96/S97); no active cron.
- The ONE idle-reachable flash path: `tz-update -> /etc/TZ` via the NTP poll callback - but strictly **write-on-change** (skips if the TZ string is unchanged), so ~1 write/year (DST rollover) + first-sync-after-boot. `/etc/TZ` mtime is stable (first-boot), confirming it does not churn.

**User-action writers (bounded, expected):** config Saves (`json-config-*.cgi` -> `/etc/*.json`, per POST), the agent `persist_value` (write-on-change), day/night sidecar `/etc/daynight.state` (~11 B, atomic, write-on-change), `/etc/physical-privacy-state.json` (atomic), `light <type> gpio <pin>` pin reassignment, and two HA paths: HA config Save; and **HA Motion Guard toggle -> full `jct set motion.enabled` rewrite of prudynt.json** (`ha-commands:75,80`, NOT write-on-change).

**Verdict on the intended rule ("only day/night + privacy write flash"):**
- **Autonomous / idle writes: essentially TRUE** - the sole exception is the ~yearly write-on-change `/etc/TZ` (system-time category). The overlay does not grow on its own.
- **Literally, incl. user actions: NOT exactly** - config Saves + the two HA paths also write flash. All deliberate, bounded, user-triggered - not idle churn.

**Can normal long-term use or repeated UI use grow/fill the overlay? Answer: NO.**
- Idle/long-term: no autonomous growth (0 writers except ~1/yr TZ).
- Repeated UI: live tuning/viewing = 0 flash; config Saves = fixed-size rewrites -> jffs2 obsolete-node churn, GC-reclaimed. Overlay currently 54 % (29 KB real data + jffs2 churn). No append/log/per-event-file growth vector exists.
- Realistic fill risks (NOT normal use): (a) an HA automation toggling Motion Guard frequently (repeated 9 KB prudynt.json rewrites), (b) a pathological manual save rate hitting jffs2's ~5-erase-block GC reserve (transient ENOSPC), (c) a large new file copied-up (the historical `prudynt.sh` 85 KB - not present now).

**Mitigations:**
1. **IMPLEMENTED (`8fd3fe50f`):** HA Motion Guard command is now **write-on-change** - the `jct set` is guarded by a get-compare (skip if `motion.enabled` already matches); the live `prudyntctl` apply is unchanged. Removes the main repeated-UI churn vector. Validated on cam4 against a copy of the real config: redundant on/off = no flash write, genuine change = writes once. [`ha-commands` motion_guard branch]
2. (Optional, NOT done - general config-save flow intentionally left unchanged per request) write-on-change in the config-save CGIs' `write_config`.
3. Keep the health-channel storage monitor (~80 % alert) + the tmpfs-for-runtime-state pattern; land the Phase 3 firmware rebuild (bake fixes into `/rom` -> overlay clears to a few KB).
The `tz-update` path needs no change (bounded + correct).

### 11.7 Web UI live-status latency - Option B split (`bd2dde208`, BUILD-ONLY, not deployed)
Session 2026-07-06. Splits the fast-changing gain/brightness out of the heavy ~650 ms agent SSE so it no longer runs every 5 s, and makes LED/colour/ircut toggles show authoritative state at once. **4 files** (supersedes 11.5's "no change"; addresses 11.2's deferred light-read cost by polling *less often*, not by caching):
- **NEW `www/x/json-daynight.cgi`** - cheap gain/brightness endpoint: one `prudyntctl json '{"daynight":{"status":null}}'` (~0 ms), inline `grep` parse (`total_gain` + `brightness_percent`), `require_auth`, **no temp file / no writes**. Polled by a **new `main.js` LiveGain 5 s channel** (fetch/schedule/start, gated on password + `document.hidden`, cleared in `cleanupHeartbeatResources()` = zero idle, immediate first fetch).
- **`main.js`** - the LiveGain channel + a **null-guard fix**: `hasTotalGain`/`hasBrightness` now reject JSON `null` (JS quirk: `null >= 0` is `true`), so a null read-back (prudynt down/restarting) keeps the last value instead of blanking the badge.
- **`www/x/json-heartbeat.cgi`** - full-heartbeat cadence **5 s -> 15 s**; a 5 s `:` SSE keepalive comment keeps output flowing (avoids uhttpd's `-T 15` network timeout dropping a silent stream) + a fixed 5 s reconnect retry; interruptible `sleep & wait $!` for prompt disconnect cleanup (busybox ash defers a trap until an external `sleep` returns).
- **`www/x/json-imp.cgi`** - authoritative read-back of the true post-command state for `white`/`ir850`/`ir940`/`ircut`/`color` (returned as `data.state`; mapping mirrors the agent heartbeat exactly). The privacy interlock now records the drop + SKIPS applying but STILL returns the true (unchanged) state, so a privacy-cancelled or light-guard-refused **no-op toggle shows reality at once** instead of the optimistic value.

**Build-only reason:** `main.js` (~104 KB) exceeds the ~92 KB free jffs2 overlay, so it CANNOT be live-copied (§5 / the flash-overlay-space failure). The whole set ships via the **next firmware build** (baked into `/rom`, zero overlay cost). **Not deployed live** - validated only via `/tmp` harnesses + read-only device reads; the camera is untouched. Adversarial review (5 lenses / 10 agents): 1 low bug FIXED (the null-guard), rest nit/accepted/false-positive.

**Expected after next build:** gain first value ~66 ms (was ~650 ms), refresh ~5 s; full heartbeat (LED/colour/ircut/motion/privacy/WG/rec/uptime) every 15 s; server CPU while viewing **~18 % -> ~10.6 %** (the heavy agent call runs 3x less); SSE still emits every 5 s (keepalive) so uhttpd never drops it; IR/white/colour/ircut toggles reflect true state instantly incl. interlock/no-op; external/auto LED changes reflect <=15 s (day/night *mode* still <=2 s via the fast channel); hidden/closed tab = zero idle; prudynt-down keeps the last gain value.

**Rollback:** `git revert bd2dde208` (or drop the commit) before the build.

---

## 12. Reliability safety batch - Steps 1-5 (2026-07-07)
Staged from the 2026-07-07 production-readiness audit (46 review agents, 27 confirmed findings).
Each fix is its own commit with a `/opt/wd-bak/<name>.orig` backup, validated before deploy and
verified after. **Branch `pt2-firmware` - always build from the current branch TIP (do NOT pin an older per-step SHA such as `af35c0ad6`).** Batch commit range
`5a7a58477..HEAD` (build the branch tip; pinning the stale `af35c0ad6` would omit the `2dfd2c2bd` packaging fixes + later watchdog hardening), plus the earlier Option B Web-UI commit `bd2dde208`.
System invariants held throughout: prudynt PID unchanged, RTSP 200, SSH connected, no unexpected
restart/reboot, no new errors.

### 12.1 Deployed LIVE to cam4 (active now - rootfs; took effect without a build)
- **Step 1 - watchdog frame-liveness probe** (`5a7a58477`, `overlay/etc/init.d/S32prudyntwd`):
  detects a socket-up-but-frozen/black stream that the OPTIONS-only probe was blind to, via a
  local `prudyntctl` `stream0.stats.fps` check, and arms the reboot ladder. Fail-safe: only a
  CONFIRMED `fps==0` downgrades "serving"; any query hiccup is INCONCLUSIVE (treated as serving),
  so it can never cause a self-inflicted restart.
- **Step 2a - prudynt OOM protection `oom_score_adj=-800`** (`083677018`, `package/prudynt-t/files/S31prudynt`):
  makes prudynt a near-last-resort OOM victim (was `oom_score` 197 = first victim on this 36 MB
  no-swap box); re-applied on every start/restart; `start-stop-daemon` return code preserved.
- **Step 2b - dropbear SSH-listener OOM protection `-500`** (`2fbba09ef`, renamed to `S51oomprotect` in `2dfd2c2bd`; ships as `overlay/etc/init.d/S51oomprotect`, executable + ordered after `S50dropbear`):
  keeps remote-recovery SSH out of the OOM killer's first picks; children inherit the score.
- **Step 2b-ii - watchdog auto-re-asserts dropbear `-500` ≤60 s** (`b6776c415`, `S32prudyntwd`):
  a check-then-set line in the existing loop re-applies the shield after a mid-run dropbear restart.
- **Step 3b - `agent.cgi` non-streaming curl bounded** `--connect-timeout 2 --max-time 8`
  (`79d0734bb`): a stalled/deadlocked agent can no longer hang the Web UI. The SSE `curl -N`
  streaming path is deliberately left unbounded.
- **Step 3c - all 8 HA direct publishes wrapped in `timeout ${HA_PUB_TIMEOUT:-4}`** (`bc42afe36`,
  `ha-common`/`ha-state`/`ha-daemon`/`ha-discovery`): a broker/Wi-Fi outage can no longer stall the
  synchronous ha-daemon loop for minutes. `_ha_pub_confirm` was already wrapped.
- **Step 5 - `physical-privacy off --force` explicit fail-open escape** (`af35c0ad6`,
  `overlay/usr/sbin/physical-privacy`): a motor-readback fault can no longer pin the camera blind
  with no software override. Default `off` stays fail-CLOSED; `do_guard` never force-opens; no
  automatic fail-open, no persistent state.

### 12.2 BUILD-ONLY - active only AFTER the firmware build + flash
- **Option B Web-UI live-status split** (`bd2dde208`): LiveGain 5 s channel + `json-daynight.cgi`
  + full SSE heartbeat slowed to 15 s w/ 5 s keepalive + null-guard. Build-only because `main.js`
  (~104 KB) exceeds the ~92 KB overlay free and cannot be live-copied.
- **Step 3a - `json-daynight.cgi` prudyntctl `timeout 2`** (`08a7eee2a`): ships with Option B
  (the CGI is not on the live device).
- **Step 4 - prudynt patch `0005` bounded VPU-stall exit** (`777c6427a`): after 3 in-process
  rebuilds that don't restore a frame, raise `kill(getpid(), SIGTERM)` (the `0011` clean-exit
  idiom) so a hard VPU wedge recovers via S31/S32 instead of looping alive-but-frameless. Baked
  into the prudynt binary. Regenerated from `themactep/prudynt-t@f4b3228` + `0001-0004`,
  `git apply --check` clean. NOTE: `0005` is the broader **T23 life-guard fault-tolerance** patch (it also carries the bounded prudyntctl IPC socket timeout, honour-actuation-result day/night switch, and persist-forced-mode - see section 3-G); this batch ADDED its bounded VPU-stall exit.

### 12.3 Deferred (agreed - not in this batch)
- **2c `vm.min_free_kbytes`** - delicate reclaim knob on a 36 MB no-swap box; the real OOM safety
  (victim ordering) is already done.
- Watchdog **over-count** (counts legitimate external restarts) + **cross-boot circuit-breaker**
  (needs a new persistent write).
- Low-priority lows: RTSP `max_clients` cap, Wi-Fi power-save/`bgscan`, DHCP-renew address flush,
  `S00blink` (dead code), WebUI session expiry.

### 12.4 Post-build / post-flash validation checklist
1. SSH reconnects (~2-5 min); Wi-Fi `Zoe2.4` up; `/root/.ssh/authorized_keys` intact; day/night +
   physical-privacy state preserved.
2. Build-only fixes ACTIVE: `json-daynight.cgi` has `timeout 2`; `main.js` LiveGain channel
   present; prudynt binary shows the `0005` stall-exit log string; Option B channels work.
3. Rootfs fixes baked in `/rom`: `S32prudyntwd` (`check_frames` + dropbear re-assert), `S31prudynt`
   (`-800`), `S51oomprotect`, `physical-privacy --force`, `ha-*`/`agent.cgi` timeouts.
4. Runtime: prudynt `oom_score_adj=-800`, dropbear `-500`, watchdog running (frame-probe), RTSP
   `200`, streaming healthy.
5. Overlay reclaimed (optional: `rm` each `/overlay` copy whose md5 matches `/rom`).
6. Clean cold-boot (the flash-reboot is itself the test) + short soak; no new errors.

### 12.5 Operational notes
- **Overlay usage rose on the live unit** to ~75 % (168 KB used / 56 KB free) from the live rootfs
  deploys (copy-ups of the modified scripts). Still safe, but monitor.
- **The firmware build reclaims overlay space** - once the fixes bake into `/rom`, the overlay
  copies are redundant and can be removed, dropping the overlay back to a few KB.
- **DO NOT FLASH without:** (a) off-device backups of mtd2 `config` + mtd3 `kernel` + mtd4 `rootfs`;
  (b) a **verified config-preserving** `sysupgrade` plan - mtd2 holds Wi-Fi `Zoe2.4` + the SSH
  authorized_keys, so losing it means losing the remote camera; and (c) confirmed physical
  UART/U-Boot recovery access. `sysupgrade` also requires `fw_setenv enable_updates true` + a reboot
  first.

---

## Build & validate (reminder)
- prudynt C++ ships **only** via patches `0001`-`0017` in `package/all-patches/prudynt-t/`
  (auto-applied; source git-fetched at `f4b3228` - never hand-edit source). There is **no**
  `package/all-patches/thingino-jct/` (the jct atomic-write fork was dropped; `prudynt.json` uses
  stock jct).
- **Shim sha gate (critical):** the shipped `/usr/lib/libuclibcshim.so` MUST be the pinned
  proven-good build - sha256 `07710f80ebc4683c9a9c658142a3cdce018db2949b844647e1709446e21c369b`
  (§3-K). `ingenic-uclibc.mk` verifies the pinned prebuilt before install, and **both** CI
  workflows fail the run if the shim in the rootfs/staging is anything else (a rebuilt shim is
  unverified and was what crashed every prudynt). Re-pin only after verifying prudynt boots.
- After build, confirm `package/all-patches/prudynt-t/` holds **exactly** `0001`-`0017`. In the
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

## 13. Engineering Record - complete reference (branch tip `f256057a9`)

*Consolidated 2026-07-08. Standalone current record for the `pt2-firmware` branch: every problem
fixed, why it mattered, the fix, its deployment status, how it was validated, remaining risk, and
open follow-ups. §1-§12 hold the deeper historical detail and are cross-referenced where useful.*

### 13.0 Overview
- **Branch / base:** `pt2-firmware`, based on upstream `12445a6`. This file is a repo document only - **not** installed into the image.
- **Hardware / role:** Sonoff CAM-PT2 - Ingenic **T23N**, **SC2336P** sensor, **ATBM6012BX** Wi-Fi, **ir850-only** illuminator, stepper PTZ. **Life-safety** camera. Path: Thingino + prudynt (`f4b3228`, live555 RTSP) → *(off-camera)* go2rtc → WebRTC / Home Assistant. go2rtc is **not** on the camera (absent from the PT2 defconfig).
- **Current branch tip:** `f256057a9`.
- **Currently-flashed build (validated live, cam4):** `BUILD_ID="pt2-firmware+f256057, 2026-07-07 21:14:20 +0000"`. Build via GitHub Actions `pt2-build-artifact` (workflow_dispatch → branch `pt2-firmware`, ~1-2 h, artifact `pt2-firmware-images`). Flashing is performed by the operator (the assistant does prep/validation only).
- **Status legend:** **BAKED** = in the flashed image (rootfs / overlay / prudynt patch); **BUILD-ONLY** = ships only via a firmware build, never live-copied (e.g. `main.js` is 107 KB > overlay free, and prudynt patches are compiled in); **ROOTFS/LIVE-CAPABLE** = a small overlay/CGI file that *can* be live-copied but for `f256057a9` is baked.

### 13.1 Safety-batch summary
| # | Subsystem | Fix (one-line) | Key commit(s) | Status |
|---|---|---|---|---|
| 1 | Watchdog / stream-liveness | frame-liveness probe (prudynt `stream0.stats.fps`), fail-safe | `5a7a58477` | BAKED |
| 2 | Watchdog SSH/SIGPIPE safety | boot watchdog stdout/stderr on `/dev/console`, never an SSH pipe | (boot path) | BAKED |
| 3 | OOM protection | prudynt `-800`, dropbear `-500`, watchdog subshell `-300` | `083677018`,`2fbba09ef`→`2dfd2c2bd`,`b6776c415`,`1985c7d84` | BAKED |
| 4 | `confirm_gone` / restart timing | bounded poll `DELAY=5`/`BUDGET=25`; arm grace + reset `DOWN_STREAK` on reappear | `b03793670`,`1985c7d84` | BAKED |
| 5 | Web UI / LiveGain / first-load | Option B split; `json-daynight.cgi` (`timeout 2`) + install rule; `agent.cgi` non-stream curl bounded; SSE unbounded; `heartbeat()` reorder | `bd2dde208`,`08a7eee2a`,`2dfd2c2bd`,`79d0734bb`,`f256057a9` | BUILD-ONLY |
| 6 | HA / MQTT timeout hardening | all 9 direct publishes wrapped in `timeout "${HA_PUB_TIMEOUT:-4}"`; `_ha_pub_confirm` not double-wrapped | `bc42afe36` | BAKED |
| 7 | prudynt VPU stall recovery | patch `0005`: bounded stall restarts → clean `SIGTERM` self-exit for VPU reclaim | `777c6427a` | BAKED (patch) |
| 8 | Physical privacy | explicit manual-only `off --force`; default `off` stays fail-closed; no auto fail-open | `af35c0ad6` | BAKED |
| 9 | Storage / flash / overlay | write-on-change discipline; status-channel UI is read-only; no cleanup performed | (see §5, §11.6) | BAKED |
| - | Docs / CI + dropbear naming | build-from-tip, patch-gate `0001-0017`, S51oomprotect comment `S30dropbear` | `a71f7e2a9`,`5c1ff8336` | doc/CI only |

### 13.2 Fixes by subsystem

**1. Watchdog / stream-liveness** (`overlay/etc/init.d/S32prudyntwd`, `5a7a58477`).
- *Problem / why it mattered:* the old watchdog probed only RTSP `OPTIONS`. A socket-alive but **black/frozen** stream (VPU stall, stopped encoder channel) still answered `OPTIONS 200`, so a life-safety camera could show no usable video while the watchdog believed it healthy.
- *Fix:* `check_frames()` queries the local `prudyntctl` `stream0.stats.fps`; `probe_serving()` now requires both a serving socket **and** live frames.
- *Fail-safe:* only a **confirmed numeric `fps==0`** downgrades "serving." Any hiccup - empty/non-numeric output, timeout, `prudyntctl` missing - returns **INCONCLUSIVE** and is treated as *serving*, so a probe glitch can never cause a self-inflicted restart.
- *Validation:* live cam4 - RTSP `200`, both streams 16-18 FPS, `0` watchdog restarts over baseline + soak (§13.3).

**2. Watchdog SSH / SIGPIPE safety** (boot path).
- *Problem:* if the watchdog is (re)started from an **interactive SSH** session, its backgrounded `watch()` subshell inherits the SSH pty as stdout/stderr; when the session closes, the next `echo` writes to a dead pipe → **SIGPIPE** → the watchdog dies silently.
- *Fix:* the boot-started watchdog inherits init's descriptors, so `watch()`'s fd1/fd2 are **`/dev/console`** (fd0 `/dev/null`) - never an SSH pipe. Manual restarts during testing are done detached to `/dev/console` (never left attached to an SSH pty).
- *Validation:* `ls -l /proc/<wpid>/fd/{1,2}` → `/dev/console` at first boot **and** after a controlled reboot (§13.3).

**3. OOM protection** (36 MB box, no swap).
- *Problem / why it mattered:* under memory pressure the kernel OOM killer picks the largest-RSS process; killing prudynt loses video, and killing dropbear loses the **remote-recovery path**. On this box that made the two most critical processes the *first* victims.
- *Fix + ordering:* `prudynt oom_score_adj=-800` (`package/prudynt-t/files/S31prudynt`, re-applied on every start/restart, `start-stop-daemon` rc preserved) < `dropbear -500` < **watchdog subshell `-300`** (`WATCHDOG_OOM_ADJ`, set in `S32prudyntwd` `start()` on `PID=$!` - the backgrounded loop, not the parent init; `[ -w ]`-guarded, `start()` ends `return 0` so it can never fail service startup) < everything else `0`. Rationale: keep the video pipeline last to die, the SSH listener second-last, and the recovery watchdog third - each strictly less protected than the thing it guards.
- *`S51oomprotect`:* boot one-shot + manually re-runnable; reads `/run/dropbear.pid`, pins the dropbear **listener** to `-500` (session children inherit at fork). Best-effort, never blocks boot. The watchdog *also* re-asserts dropbear `-500` each loop (`b6776c415`) in case a mid-run dropbear restart resets it to 0.
- *Why `S50dropbear` stays in the repo but the device shows `S30dropbear`:* `overlay/etc/init.d/S50dropbear` is thingino's **customized** dropbear init (port 22, no-blank-password login, display-default-creds) that intentionally overrides the buildroot package default; `scripts/rootfs_script.sh` then **renames `S50dropbear` → `S30dropbear`** at build finalize (to start dropbear earlier). So the file must be kept - removing it reverts dropbear to the buildroot default and loses the security customizations (`docs/overlayfs.md` documents this pattern). Ordering therefore holds: dropbear at order **30**, `S51oomprotect` at order **51** (shield applied after dropbear is up). *(The `S51oomprotect` header comment was corrected to reference `S30dropbear` in `5c1ff8336`. Earlier, the `S33oomprotect`→`S51oomprotect` rename + `chmod +x` in `2dfd2c2bd` fixed an inert shield - as `S33`, mode 100644 + ordered before dropbear, rcS skipped it and it no-op'd.)*
- *Validation:* live - `-800`/`-500`/`-300` all correct at first boot **and** re-applied after a controlled reboot (§13.3).

**4. `confirm_gone` / restart timing** (`S32prudyntwd`, `b03793670` + `1985c7d84`).
- *Problem / why it mattered:* the watchdog's GONE-confirm was a single 5 s re-check. But an external `service restart prudynt` (day/night, MQTT, Web UI) holds prudynt genuinely down for `S31prudynt`'s `TERM_GRACE 2 + KILL_GRACE 2 + RECLAIM_WAIT 15 ≈ 19 s`. A 5 s window expired mid-restart, so the watchdog could **double-drive** its own restart on a perfectly normal event, thrashing the VPU and eroding the reboot backstop.
- *Fix:* `confirm_gone()` now **bounded-polls** `prudynt_live` every `GONE_CONFIRM_DELAY=5 s` up to `GONE_CONFIRM_BUDGET=25 s`, returning "alive" the instant prudynt reappears and "gone" only after the full bounded window (never unbounded). 25 s > S31's ~19 s down-window. Complementarily (`b03793670`), when prudynt reappears during the confirm window the code treats it as a fresh incarnation: `arm_grace` + reset `DOWN_STREAK` (mirrors the new-incarnation handler), so the next slow cold-start interval isn't counted as a serving failure. Trade-off: a genuine crash's restart latency rises ~+20 s (bounded) - acceptable, since the watchdog is prudynt's only respawner.
- *Validation:* live controlled `prudynt` restart (down+up 17 s) **absorbed with 0 spurious watchdog restarts**; prudynt PID stable 90 s, RTSP recovered, `-800` re-applied (§13.3). Bench harness pre-flash: true-gone → bounded gone; quick/mid reappear → early alive; no infinite wait.

**5. Web UI / LiveGain / first-load performance** (`main.js`, `json-daynight.cgi`, `agent.cgi`).
- *Option B split (`bd2dde208`, BUILD-ONLY):* the heavy ~650 ms agent SSE no longer runs every 5 s. Fast-changing **gain/brightness** moved to a cheap dedicated `json-daynight.cgi` (single `prudyntctl` query, `timeout 2`, read-only, no writes) polled by a 5 s **LiveGain** channel; the full SSE heartbeat slowed to 15 s with 5 s keepalive slices.
- *`json-daynight.cgi` install rule (`2dfd2c2bd`, Fix C):* the CGI had **no `$(INSTALL)` line** in `thingino-webui.mk` → it would have been missing from `/var/www/x` (LiveGain 404). Added. (`timeout 2` was `08a7eee2a`.)
- *`agent.cgi` timeout (`79d0734bb`):* the **non-streaming** curl carries `--connect-timeout 2 --max-time 8` so a stalled agent can't hang the Web UI. The **SSE `exec curl -N` streaming path is intentionally left unbounded** (a `--max-time` would kill a healthy long-lived stream).
- *First-load issue + fix (`f256057a9`, BUILD-ONLY):* CPU/RAM/storage/date-time are **single-sourced** from the 7 s HealthStatus channel (`json-status-health.cgi`), whereas gain is triple-sourced (fast 2 s + LiveGain 5 s + SSE). On a **cold** page load (uncached 107 KB `main.js` + assets consuming the browser's ~6 connections), `heartbeat()` opened the **persistent SSE first** and the ~0.7 s agent-backed slow-heartbeat second, so the cheap health fetch (dispatched 4th) queued behind them → the four badges lagged ~1.5 s+. On **refresh** (warm cache, free connections) they painted in ~0.15 s - hence "slow first load, fast refresh." **Fix:** reorder `heartbeat()` to start the cheap fetches (`startFastStatus`, `startHealthStatus`, `startLiveGainStatus`) **before** the SSE + slow-heartbeat, so the badges grab a connection first. Minimal: only the 5 startup calls reordered - no logic/guard/interval/endpoint change; `document.hidden` gating, `passwordCheckComplete` gating, single-in-flight guards, and SSE `onerror` reconnect all unchanged.
- *Validation:* live authenticated CGI timing (session 0.09 s, fast 0.17 s, health 0.15 s, daynight 0.16 s, slow-heartbeat 0.70 s, SSE first-data +0-1 s); contended cold-load **model** time-to-health **1.66 s (old order) → 0.35 s (new order)**; warm/uncontended ~0.14 s either order. Deployed `main.js` md5 == committed `f256057a9` blob (reorder confirmed live). *Note:* the real browser first-paint improvement is verified after the next build; the reorder does not remove the inherent cold-asset download cost.

**6. HA / MQTT timeout hardening** (`package/thingino-ha/files/*`, `bc42afe36`).
- *Problem / why it mattered:* a broker/Wi-Fi outage could block a **synchronous** direct `mosquitto_pub` for minutes, stalling the `ha-daemon` loop (and any HA-triggered action) indefinitely.
- *Fix:* all **9** direct publish sites (in `ha-common` helpers, sourced by `ha-state`/`ha-daemon`/`ha-discovery`) are wrapped with `timeout "${HA_PUB_TIMEOUT:-4}"`; on timeout the entity keeps its last value. `_ha_pub_confirm` is **not double-wrapped**. Deployed HA scripts are byte-identical to the committed blobs (md5 verified).
- *Validation:* live - daemon up, broker `ESTABLISHED`, `ha-state force` completes in ~5 s (rc 0, no hang), **0 publish-storm / 0 reconnect-loop**; one benign startup `TIME_WAIT` → `ESTABLISHED` (§13.3).

**7. prudynt VPU stall recovery** (patch `0005`, `777c6427a`).
- *Problem / why it mattered:* on a hard T23 VPU wedge, prudynt's in-process pipeline-rebuild loop could retry **forever**, leaving the daemon alive-but-frameless with no external recovery.
- *Fix:* `0005` bounds in-process stall restarts - after **3** rebuilds that don't restore a frame it logs `... pipeline rebuilds did not restore frames; raising SIGTERM ...` and calls `kill(getpid(), SIGTERM)`, handing recovery to `S31prudynt`/`S32prudyntwd` (which do a proper VPU reclaim). The stall counter resets on a delivered frame and on subscriber-connect (bounds only true stalls, not idle).
- *Why `kill(getpid(), SIGTERM)` and not `_exit`/`abort`:* `SIGTERM` runs prudynt's **normal signal handler / clean shutdown** (release IMP/VPU/encoder groups, RTSP teardown) so the kernel-held VPU is properly relinquished before S31's reclaim + relaunch. `_exit()` skips cleanup - risking a VPU still held by the dead PID, the exact wedge we're recovering from; `abort()` would SIGABRT/coredump (unclean, no graceful release). Mirrors the `0011` clean-exit idiom.
- *Validation:* patch applies cleanly on `f4b3228 + 0001-0004` (`git apply --check`); the deployed binary contains the escalation string `raising SIGTERM for VPU reclaim` (grep on `/usr/bin/prudynt`); the SIGTERM→S31/S32 recovery path is exercised indirectly by the controlled-restart test (§13.3). The pathological "prudynt never recovers" path remains untested by design (see gap **A**).

**8. Physical privacy** (`overlay/usr/sbin/physical-privacy`, `af35c0ad6`).
- *Problem / why it mattered:* a motor/tilt **readback fault** could leave the lens parked (camera blind) with no software escape.
- *Fix:* an **explicit, manual-only** `physical-privacy off --force` that lets `do_off` proceed even when the tilt can't be verified. `force=1` is set **only** by the `--force` flag (line 288); nothing sets it automatically.
- *Unchanged:* the default `off` (no flag) stays **fail-closed** (refuses to open on an unreadable tilt); `do_guard` never force-opens; no automatic fail-open was added; no new persistent state.
- *Validation:* argument-parse + guard inspection confirmed; current state `active:false` preserved across reboot. The **motor actuation path was deliberately not exercised** (no motor movement during validation) - a coverage note, not a defect (degraded branches are fail-safe).

**9. Storage / flash / overlay behavior.**
- *Overlay usage:* pre-flash the 224 KB jffs2 config overlay had been near-full (75 %, the flash-overlay-space wedge). After the fresh flash it sits at **50 %** (112 K used / 112 K free, 27 files); jffs2 GC reclaims lazily on reboot.
- *Write discipline:* all persistent writes are **write-on-change / recovery-only**; runtime state lives on tmpfs (`/tmp`, `/run`). Status-channel Web-UI activity produced **zero** overlay writes (verified by md5-snapshot diff around a UI burst).
- *Overlay cleanup was NOT performed* (operator has not approved it; jffs2 GC handles reclaim). Only `/etc/passwd` (390 B) is a redundant copy identical to `/rom` - negligible; cleanup not recommended.
- *Recommendation:* avoid unnecessary flash writes - `main.js`/large assets ship via **build only** (never live-copied into the overlay); config saves go through `jct` (see write-path coverage gap **D**).

### 13.3 Validation evidence - post-install on `f256057a9` (cam4, 2026-07-08)
Method: direct SSH (read-only + serialized controlled tests) + an 11-agent adversarial review of the captured evidence and code paths. Only two live actions were taken - one `prudynt` service restart and one controlled reboot (both self-restoring); no config values were changed.

- **Build identity:** `pt2-firmware+f256057` (matches tip `f256057a9`).
- **Baseline:** Wi-Fi `192.168.1.137/24`, single boot banner, dmesg/logread clean, `0` watchdog restarts.
- **RTSP / FPS:** OPTIONS `200`; stream0 & stream1 both 16-18 FPS.
- **OOM (first boot AND after controlled reboot):** prudynt `-800`, dropbear `-500`, watchdog `-300`; watchdog fd1/2 → `/dev/console`.
- **Web UI:** deployed `main.js` md5 == committed `f256057a9` blob (reorder live); CGI timings as in §13.2 #5.
- **CPU / memory:** prudynt ~29 % of one core quiescent (matches the ~25 % HW-encoder baseline); system ~40 % quiescent / ~50 % under an aggressive UI burst; **no leak** (prudynt RSS flat 8040 KB, sysmem steady over ~75 s).
- **Concurrency:** 24/24 concurrent status-CGI requests → `200`, no hang/stale.
- **HA / MQTT:** daemon up; broker `192.168.1.133:1883` `ESTABLISHED`; `ha-state force` rc 0 in 5 s (no hang); 0 storm / 0 reconnect-loop.
- **Reliability - controlled `prudynt` restart:** down+up 17 s → **absorbed, 0 spurious watchdog restarts**; PID stable 90 s, RTSP 200 throughout, `-800` re-applied.
- **Reliability - controlled reboot:** Wi-Fi/SSH/RTSP/uhttpd/HA all returned; all 3 OOM shields re-applied; state preserved (privacy `off`, daynight `day`); single boot, no reboot loop.
- **Storage:** UI status activity → 0 overlay writes; overlay steady 50 % before/after all tests incl. reboot; test artifacts were `/tmp`-only (cleared by reboot).
- **Benign false-positives characterized:** a transient `ps` "1 zombie" (targeted `/proc` scan = 0 persistent - a short-lived reaped child); two "wlan/wpa" log lines (a boot button-config load + normal dropbear session disconnects); a "1 error" log hit that is `loops_per_jiffy` matching `/oops/i`; and the HA startup `TIME_WAIT`→`ESTABLISHED`.
- **Verdict:** **0 blockers, 0 confirmed majors** (both candidate majors downgraded on verification), **4 minors** (all non-blocking; §13.4 A-D).

### 13.4 Known gaps / deferred work
The four minors from the final reliability review (all **build-only** fixes, none block normal use):

**A. Cross-boot reboot circuit-breaker** *(the long-deferred item; most worthwhile).*
- *Problem:* `RESTART_COUNT`/`RESTART_LIMIT=3` are process-local to the `watch()` subshell and reset to 0 every boot. A **deterministically un-startable** prudynt (a bringup-crash regression, a corrupt config on a full overlay that can't self-heal, a reboot-surviving VPU wedge) climbs to 3 restarts (~3 min), reboots, and repeats **indefinitely** - a ~3-4 min reboot loop with no video and no operator-visible "give up and stay reachable" state.
- *Suggested direction:* a **persistent, self-decaying reboot counter** (small file, e.g. `/overlay/etc/wd_reboot_count`, or U-Boot env) + timestamp; if N reboots occur within a window (e.g. 3 in 30 min) **stop escalating to reboot** - keep slow-restarting prudynt but leave SSH/HA/uhttpd reachable for diagnosis; decay/reset after a sustained-healthy interval. For a life-safety unit, prefer "stuck alive + reachable" over "perpetual reboot loop."
- *Note:* requires a **new persistent write** - design carefully (atomicity, overlay-space, no steady-state churn). This is why it was deferred.

**B. Warm-restart grace duration.**
- *Problem:* `do_restart` re-arms the full **200 s cold-boot** `GRACE` after every *warm* restart. An alive-but-streamless prudynt (fps==0 after a restart) then takes ~10-14 min (≈3× the 200 s grace) before the reboot backstop clears it.
- *Suggested direction:* keep `GRACE=200` as the cold-boot seed, but arm a shorter `RESTART_GRACE` (~90-120 s) on warm restarts (the VPU/ISP were just reclaimed and bring up faster). Measure warm-restart bringup on-device before pinning the value.

**C. `S31prudynt` concurrency lock.**
- *Problem:* `service restart prudynt` execs `S31prudynt restart` with **no lock**. Two concurrent external callers (e.g. two Web-UI actions / `preview.cgi` uploads) can interleave: the second SIGTERMs the first's freshly-launched prudynt → doubled downtime + 2× VPU teardown churn. (The watchdog's own path is already defended by the `confirm_gone` bounded poll.)
- *Suggested direction:* a single `flock` choke point in `S31prudynt` around `restart()/stop()/start()` (e.g. `exec 9>/run/prudynt.svc.lock; flock 9`) so concurrent invocations queue instead of interleaving.

**D. Write-path validation gap** *(coverage, not a confirmed defect).*
- *Problem:* the "UI activity produced no persistent writes" evidence covers only the **read-only status channels**. The ~17 **writer** CGIs (e.g. `json-prudynt-save.cgi`, which does a full-file `jct import` rewrite of `/etc/prudynt.json`) were **not** exercised, so their overlay-write cost/atomicity is unverified on this build.
- *Suggested direction:* re-run overlay write-monitoring while performing representative **safe** config saves (photosensing/RTSP/network/motion), snapshotting overlay used/free + file count before/after and confirming atomicity; then restore original values. Context: overlay-exhaustion is a *pre-existing* constraint (separately audited 07-03 as "won't fill under normal use"), and the C1 config-heal in `S31prudynt` covers a torn config at the next restart - so this is a verification gap, not a known break.

**Refuted during review (investigated, not real defects):** the physical-privacy motor path (no code defect; degraded branches fail-safe; the `--force`-manual-only invariant *was* verified - live actuation is a coverage note only); the `confirm_gone` 25 s margin vs a corrupt-config heal (`jct` returns immediately on a corrupt file, real down-window ~16-18 s < 25 s - only a doc nit: the `S32prudyntwd:283` comment "~19 s" omits validate+migrate); and an alleged `ha-daemon` `/tmp` temp leak (every temp is `rm`'d unconditionally).

**Other deferred items (agreed, pre-existing):**
- **RTSP `max_clients` cap** - bound concurrent RTSP clients (DoS / resource hygiene).
- **Watchdog cross-boot breaker** - same item as **A**.
- **Watchdog restart over-counting** - `do_restart` counts *legitimate* external restarts toward the reboot ladder; largely mitigated by the `confirm_gone` bounded poll + grace-reset (fix #4), but a dedicated "don't count an operator-initiated restart" refinement is still open.
- **Wi-Fi power-save / `bgscan` review** - verify power-save / background-scan settings don't cause latency or drops on the ATBM6012BX.
- **DHCP-renew stale address** - confirm address flush / re-acquire behavior on lease renewal.
- **`S00blink`** - cosmetic boot-LED init (dead / no-op code); tidy or remove.
- **Web UI session expiry / cleanup** - confirm session-store expiry + cleanup (no unbounded session accumulation).
- **`2c vm.min_free_kbytes`** - a delicate reclaim knob on 36 MB no-swap; deferred because the real OOM safety (victim ordering, fix #3) is already in place.

### 13.5 Post-flash checklist
After flashing and cold boot:
1. **SSH** reachable (`ssh root@<cam>`) - the whole recovery guarantee hinges on it.
2. **OOM:** `cat /proc/$(pidof prudynt)/oom_score_adj` = `-800`; `cat /proc/$(cat /run/dropbear.pid)/oom_score_adj` = `-500`; `cat /proc/$(cat /run/rtsp_watchdog.pid)/oom_score_adj` = `-300`.
3. **Init:** `/etc/init.d/` shows `S30dropbear`, `S31prudynt`, `S32prudyntwd`, and **`S51oomprotect`** (executable, order 51 > 30). *(Verify `S51oomprotect`, not the old `S33oomprotect`.)*
4. **Watchdog:** running with the frame probe; fd1/2 → `/dev/console` (not an SSH pty); `S32prudyntwd` carries `GONE_CONFIRM_DELAY=5` + `GONE_CONFIRM_BUDGET=25` + `WATCHDOG_OOM_ADJ="-300"`.
5. **RTSP:** OPTIONS `200`; both streams non-zero FPS.
6. **Web UI:** `/var/www/x/json-daynight.cgi` present (not 404) with `timeout 2`; `main.js` `heartbeat()` order = fast, health, LiveGain, SSE, slow.
7. **prudynt patches `0001`-`0017`** applied (build gate `.applied_patches_list`); binary contains `raising SIGTERM for VPU reclaim`.
8. **HA:** entities publish; broker connection `ESTABLISHED`; no publish hang.
9. **Privacy:** default `off` fail-closed; `off --force` exists and is manual-only.
10. **Soak:** short run - no crash-loop, no spurious watchdog restart, overlay not growing.

### 13.6 Build notes / branch-tip notes
- **Build the branch TIP**, not an older per-step SHA: GitHub Actions `pt2-build-artifact` (workflow_dispatch → branch `pt2-firmware`) resolves to the tip. Current tip: **`f256057a9`**. (An older pin such as `af35c0ad6` omits the `2dfd2c2bd` packaging fixes and later hardening.)
- **Commit lineage since the previously-flashed `1985c7d84`:** `5c1ff8336` (S51oomprotect dropbear comment `S30dropbear`, comment-only) → `f256057a9` (`heartbeat()` reorder, `main.js`, build-only). Both are documentation / frontend-only relative to the validated `1985c7d84` runtime.
- **prudynt patches:** `package/all-patches/prudynt-t/0001-*..0017-*.patch`, auto-applied by buildroot (`BR2_GLOBAL_PATCH_DIR`) over the git-fetched source `themactep/prudynt-t@f4b3228`. **Never hand-edit the prudynt source** - regenerate patches. The CI patch-gate enumerates `0001-0017`.
- **`main.js` is BUILD-ONLY:** 107 KB > the config-overlay free space, so a modified `main.js` **cannot** be live-copied into the overlay - it ships only via a firmware build baked into `/rom`. (CGI / rootfs-script changes are small and *can* live-deploy, but for `f256057a9` everything is baked.)
- **Line endings:** the repo is Windows `core.autocrlf=true`; working copies are CRLF but committed **blobs are LF** (`git ls-files --eol` → `i/lf`), which is what the Linux build needs. Overlay shell scripts **must** be LF (a CRLF shebang = "bad interpreter"). Verify blob EOL with `git ls-files --eol` or `git cat-file blob <oid> | tr -cd CR | wc -c` (want 0) - not `git show | grep` (unreliable under autocrlf).

---

## 14. Kernel D-state wedge - incident, investigation, and fixes R1/R2/R3 (2026-09)

Investigated on **cam5** (`cam5-ing-sonoff-pt2-e214`, `192.168.1.139`, running build `pt2-firmware+f256057`). All investigation was read-only or reversible/RAM-only (SysRq dumps, raw MTD reads, `/proc` inspection); nothing was written to flash and no process was killed. Commit hashes: **R1 `e24ecd88f`**, **R2 `cf3034e0c`**, **R3 `2d795da45`**.

### 14.1 Observed failure & symptoms
- Day/night switching from the Web UI (or HA) stopped taking effect: after a Night→Day switch the camera stayed in night (IR-cut not restored), while HA/MQTT still reported `daynight=day`, `ircut=ON` - a **logical-vs-physical desync**.
- Both mains-powered, always-streaming cameras (cam4 + cam5) were found wedged together; on-demand/battery cameras were unaffected.
- The **video stream kept running** throughout; the fault was confined to the day/night + config-report path, load climbed over time, and it cleared **only on reboot**.

### 14.2 The cascade (mechanism)
1. A task (`jct`, spawned by `ha-state`) stuck in uninterruptible sleep (`D`, "disk sleep") **holding its `mmap_sem` in write mode**.
2. `pidof`/`ps` read `/proc/<pid>/cmdline` of *every* process; reading the stuck task's cmdline needs that `mmap_sem`, so those readers wedged in `D` too.
3. `/sbin/daynight`'s `singleton()` guard used `pidof`, so **every** day/night switch then wedged *before* touching the IR-cut. `ha-state` and the stream-watchdog `S32prudyntwd` wedged the same way. `D`-state tasks are unkillable → the pile grew and load climbed (6.8→10.8, pure `D`-state inflation, ≈0 real CPU) until reboot.

### 14.3 Forensic evidence collected
- **SysRq-w** (RAM-only) blocked-task dump: `jct`, several `pidof`, `ps`, `tr` all `D`; `isp_fw_process` `D` (its normal state).
- For the stuck `jct`, `/proc/<pid>/{stat,status}` read fine but **`/cmdline` and `/maps` HANG** → it holds `mmap_sem` in write mode (exactly why `pidof`/`ps` wedge). Verified live that `cat /proc/<pid>/cmdline` hangs.
- Raw `dd if=/dev/mtd2|4|5` reads return instantly → **flash hardware is fine** (not an SPI-NOR/jz_sfc hang).
- `/proc/meminfo` `Dirty=0 Writeback=0`; `jffs2_gcd_*`, `kswapd`, `writeback` threads idle → **not** writeback/GC/reclaim.
- Fresh `jct get`, `cat /etc/prudynt.json`, `ls` all succeed instantly → the resource is free; the task simply never woke.
- go2rtc logs: cam4 (137) + cam5 (139) producers aborted within **20 ms** of each other → the "both at once" is a shared-cause event.
- `uname` `3.10.14__isvp_pike_1.0__ #2 PREEMPT`; full `dmesg` since boot shows **no** oops/BUG/hung-task/jffs2/mtd error before the SysRq dump (only WiFi `AP lost`) → the wedge is silent.
- Kernel config: `CONFIG_KALLSYMS`, `DEBUG_INFO`, `FRAME_POINTER`, `STACKTRACE`, `DETECT_HUNG_TASK` all **off**; `MAGIC_SYSRQ=y`; `LOCKUP_DETECTOR=y`+`BOOTPARAM_SOFTLOCKUP_PANIC=y` (soft-lockup only - does **not** catch a `D`-state sleep). No `/proc/kallsyms`, no `/proc/<pid>/stack`, **no RTC, no u-boot `bootcount`**.

### 14.4 Root cause - proven vs inferred
**Proven (evidence-backed, symbol-independent):**
- A task is stuck in `D` **holding `mmap_sem`** (cmdline/maps hang; stat/status don't).
- **Not** hardware (raw flash reads OK), **not** GC/writeback (Dirty=0, threads idle), **not** an ongoing lock or memory exhaustion (fresh identical ops succeed).
- The cascade is real: reading the stuck task's `/proc/cmdline` hangs - exactly what `pidof` does - so the `pidof`-based `singleton` wedges. (This alone justifies R1, independent of the exact kernel bug.)
- Recovery is **reboot-only** (D-state unkillable; the reboot confirmably cleared it).

**Inferred (strongly implied, not symbolized):**
- The exact kernel function/line, and that it is specifically a **lost-wakeup** in a process-startup mm path on the old Ingenic 3.10 vendor kernel. The signature (resource free + still stuck + holds `mmap_sem` write + wedged at fork/exec before opening any file) points to a lost wakeup, but *naming the site needs symbols* (R5/R6). Raw SysRq backtrace addresses were captured for later resolution.

### 14.5 Why MQTT reboot failed but Web reboot worked
`cameras/<id>/reboot/set` is handled by **`ha-commands`**, part of the same HA subsystem that forks `jct` heavily and was caught in the cascade - so the command reached the broker but cam5 never acted on it. The **Web UI** reboot runs under **uhttpd**, an independent process *outside* the wedged chain, so its CGI called `reboot` and succeeded. This is consistent with the root cause and directly motivates R3's subsystem-independent recovery.

### 14.6 R1 - flock singleton *(the resilience fix)* - `e24ecd88f`
- **File:** `package/prudynt-t/files/prudynt-helpers`, `singleton()`.
- **Was:** `pids=$(pidof -o %PPID "$appname")` - `pidof` reads `/proc/<pid>/cmdline` of every process, so it blocks behind any task holding `mmap_sem`, wedging the guard and every caller (`daynight` ×4 variants, `formatsd`). **This is the exact failure mode above.**
- **Now:** a non-blocking advisory `flock` on a tmpfs lock file (`exec 9>/run/lock/singleton.<name>; flock -n 9`). Touches **no `/proc`**; the kernel releases the lock on process exit (incl. SIGKILL/crash) - no stale lock, no reclaim, no trap; cheaper than the `/proc`-wide scan; **no flash write** (tmpfs).
- **Why safer:** the guard can no longer wedge behind an unrelated stuck task. Verified `flock -n` works on this busybox; all current callers are short-lived with synchronous children (audited), so the lock releases promptly; fd-9 inheritance by a *future* long-lived child is documented (`9>&-` convention), since ash cannot mark a shell fd close-on-exec.

### 14.7 R2 - jct fork reduction *(exposure reduction, not the fix)* - `cf3034e0c`
- **File:** `package/thingino-ha/files/ha-common`, `ha_entity_enabled()`.
- **Was:** one `jct` fork per entity - `ha-state` ~12 `jct`/poll, `ha-discovery` ~20/run.
- **Now:** read the `ha` object **once** and parse the boolean `enable_*` flags in-shell (line-anchored `sed`), with a per-key fallback to the exact old `jct` read for anything not captured. Measured on cam5: **12 → 1 `jct`** for the 12 `ha-state` checks, with **identical** enabled/disabled results (0 mismatches across all 20 entities).
- **Behaviour / staleness:** the cache is a per-process shell variable (no file, no persistence, reset on every `ha-common` source) → **no cross-invocation staleness**; `ha-daemon` reads `live_view` once at startup (unchanged); the only difference is a consistent intra-run snapshot bounded to a single poll (adversarially reviewed; benign). MQTT/password parsing was **deliberately left** on per-key `jct` (shell-parsing a password is unsafe).
- **Role:** lowers the *probability* of hitting the kernel lost-wakeup (the `jct` that wedged was an `ha-state` `jct`); it does **not** remove the failure mode - **R1 does.**

### 14.8 R3 - wedge-detector *(independent recovery)* - `2d795da45`
- **Files:** `overlay/usr/sbin/wedge-detector` + `overlay/etc/init.d/S99wedge-detector` (board-scoped, executable, `sysinit`-independent - started by `rcS`'s `S*` loop).
- **Detection:** every 15 s, count `D`-state tasks from `/proc/<pid>/stat` in **pure shell** (glob + one `read` per pid - **no fork/exec per pid, no `pidof`/`jct`/MQTT/`cmdline`**). State parsed as the field after the last `") "` (robust to comm spaces/parens); vanished/malformed entries are skipped (can only *lower* the count → never a false positive).
- **Thresholds (v1, measured):** baseline `D=1` (always `isp_fw_process`), load ~3.3-3.5; the observed wedge reached `D~8`, load 6.8-10.8. Trigger = **`D≥5` (primary) AND `load1≥6` (supporting, never alone)** for **8 consecutive checks (=120 s)**, after a **600 s post-boot grace**, **skipped while `/tmp/webupgrade` exists**.
- **Recovery:** on full confirmation, `echo b > /proc/sysrq-trigger` - an **immediate emergency reboot** (SysRq-b; **no sync/unmount**), reachable only through the complete sequence (any single normal check or parse error resets the streak to 0).
- **Watchdog relationship:** it **never touches `/dev/watchdog`**. The busybox HW watchdog (`K99watchdog`) is unchanged and remains the independent **total-hang** safety net. (A shell servicer can't set the HW timeout via `WDIOC_SETTIMEOUT`, and the jz-wdt default isn't safely knowable - hence SysRq-b, not un-feeding the timer.)
- **Limitations:** loop protection is the **600 s grace + 120 s confirmation only - NOT a hard cross-reboot counter**, because no flash-free persistent boot counter exists on this hardware (no RTC, no u-boot bootcount) and a flash write for it is intentionally excluded (cf. §13.4-A). A pathological loop is bounded to ≥~10 min streaming uptime/cycle, not prevented. Being RAM-only, the `/dev/kmsg` reason line is lost across the reboot.

### 14.9 Measured impact + RAM-only confirmation
- **R3 scan cost:** ~94 ms CPU (user+sys) per scan on cam5 (67 procs) once per 15 s = **~0.6 % of one core** (prudynt alone ≈25 %); ~183 ms wall on the load-3.3 unit.
- **R1:** *lower* CPU than before (`flock`+`mkdir` vs a `/proc`-wide `pidof` scan).
- **R2:** *lower* fork/`jct` activity (12→1 `jct`/poll).
- **Flash:** all three are **RAM-only** - R1 writes only tmpfs `/run/lock`; R2 writes nothing (a shell variable); R3 writes only `/dev/kmsg`, `/proc/sys/kernel/sysrq`, `/proc/sysrq-trigger`. **No new persistent runtime flash writes.**

### 14.10 Confirmed vs hypothesis
- **Confirmed:** the cascade (pidof/cmdline → `mmap_sem` → wedge); not-hardware / not-GC / not-memory; reboot-only recovery; the 12→1 `jct` measurement; the R3 baseline/thresholds and ~0.6 %/core cost; `flock -n` semantics; HW-watchdog behaviour; no flash-free boot counter on this hardware.
- **Hypothesis (needs symbols):** the exact kernel lost-wakeup site.

### 14.11 NOT IMPLEMENTED - optional future kernel / fleet-wide hardening (R4 / R5 / R6)
> None of the items below is implemented. They are recorded only as *possible future work*, to be pursued as a **separate project only if we decide to**. They are a different class from R1/R2/R3: all are kernel-config / kernel changes that are **fleet-wide** (they would modify `board/ingenic/xburst1/kernel/3.10.14/t23.generic.config`, shared by ~40 T23N boards) and **rebuild-only** - not board-scoped or testable without flashing. R1 already removes the observed failure mode and R3 gives bounded recovery, so none of these is required; they are purely optional hardening / diagnostics.

- **R4 (not implemented)** - `CONFIG_DETECT_HUNG_TASK=y` + a generous `hung_task_timeout` + hung-task **panic** (leveraging the existing `panic=10`): kernel-native bounded auto-recovery for *any* future hung task. The panic timeout would need tuning against legitimate long `D` waits (OTA flash-erase). Note: the existing `SOFTLOCKUP_PANIC` does not cover this class.
- **R5 (not implemented)** - `CONFIG_KALLSYMS`(+`_ALL`) + `CONFIG_DEBUG_INFO` + `CONFIG_FRAME_POINTER`: would make the *next* wedge produce a resolvable backtrace on-device (and enable R6). Modest size cost. Lowest-risk of the three (diagnostics only, no behaviour change) - the sensible first step if pursued.
- **R6 (not implemented)** - resolve the captured SysRq backtrace with the build's `System.map` (needs R5 or the build artifact), identify the wait-queue / mm site, and backport the relevant 3.10.x stable fix. Highest effort; **R4 is the pragmatic substitute** (bounded recovery) if R6 is not pursued.

## 15. Audio-reconfig supervisor deadlock + RTP data-plane watchdog (2026-09) - `13785375c`

Distinct from and more severe than the day/night wedge (§14) and the earlier audio-codec live-switch SDP staleness (patch 0014). Diagnosed and fixed 2026-09-06 on cam5 (Sonoff PT2). Full forensic record: `review/forensics-cam5-20260906/` (TIMELINE-AND-FINDINGS.md, RCA-AND-FIX.md, cam/ + mini/ captures) - kept outside the repo.

### 15.1 Observed failure & symptoms
Changing audio settings in the web UI and pressing **Save** (most likely an OPUS->AAC codec switch) froze the video stream for ALL clients - web UI, Home Assistant, and go2rtc - recoverable only by restarting prudynt / go2rtc. Live evidence: prudynt HEALTHY (encoder fps=16, `prudyntctl snapshot` returns a 317 KB JPEG, RTSP DESCRIBE=200 with a well-formed SDP matching the on-disk config), yet ZERO RTP reached any client (ffmpeg pulled 0 frames in 40 s; go2rtc read-timed-out every 5 s for 20+ min). Exactly ONE prudynt thread wedged: `ai_record` (audio capture) in uninterruptible D-state, holding `/dev/dsp`, blocking SIGTERM.

### 15.2 Root cause - PROVEN vs INFERRED (kept strictly separate)
PROVEN (source-verified vs themactep/prudynt-t `f4b3228` + our 0001-0017): on any audio-input reconfig (`global_restart_audio`: mic_format / mic_sample_rate / mic_bitrate / mic_enabled, or the 0007 privacy mic-mute) the SINGLE supervisor loop in `main.cpp` runs an UNBOUNDED `pthread_join(global_audio[0]->thread)`. `AudioWorker::run()` reads audio via `IMP_AI_GetFrame(..., BLOCK)` on `/dev/dsp`; on this Ingenic 3.10 vendor kernel that read can enter uninterruptible D-state, so the thread never observes `running=false` and never exits -> the join hangs the one supervisor thread FOREVER -> the whole restart state machine freezes and RTSP + video are never (re)created. This is the proven cause of "recoverable only by restarting prudynt". Zero AUDIO RTP is likewise proven (the StreamReplicator audio source reads a msgChannel the wedged worker never refills).

INFERRED, NOT proven: that the audio wedge ALSO directly stopped VIDEO RTP. RTSP stayed up (DESCRIBE=200) and the encoder kept running (fps=16), yet a fresh client got zero video RTP. The one real latent coupling (`IMPDeviceSource` ctor/deinit take the global `mutex_main` on the single live555 thread) was NOT active here - the wedge is in the capture branch, which holds no `mutex_main`, and the supervisor unlocks it before the join. The exact video-stall mechanism is unresolved (this kernel exposes no thread stacks; the camera was recovered). Most likely a single-threaded live555 event-loop stall under the go2rtc reconnect storm, but that half is inference.

### 15.3 Forensic evidence
Camera-side: full `/proc/<pid>` + per-thread dumps (D-thread = `ai_record` holding `/dev/dsp` fd22/23; `isp_fw_process` also D but that is normal), `prudyntctl` stats (fps=16), two snapshots, dmesg, logread, on-disk config, watchdog + init scripts. Host/go2rtc-side: authoritative RTSP SDP (H264 + inline SPS/PPS + AAC audio, matching config), ffmpeg 0-frames proof, 20+ min of go2rtc read-timeouts. Hard limit: no `/proc/<tid>/stack|wchan|syscall` on this kernel, so the exact wait-site is inferred. Refined wedge signature: D-state ALONE is NORMAL for `ai_record` (a healthy capture thread is almost always sampled inside its blocking read); the WEDGE is D with FROZEN `voluntary_ctxt_switches`, or directly `fps>0 with zero RTP delivered`.

### 15.4 Phase 1 controlled reproduction - INCONCLUSIVE
An approved low-risk on-camera repro (20x `mic_sample_rate` toggles via `prudyntctl`, runtime-only: source-verified that `handle_audio` never writes `/etc/prudynt.json` - Save is a separate action - and the config md5 was unchanged throughout) did NOT fire the wedge: the audio worker join+respawned cleanly every toggle (a fresh cycling `ai_record` tid each time). The wedge is a rarer race than 20 reconfigs hit. Reported INCONCLUSIVE, not extended. Consequence: the §15.2 video-stall coupling remains INFERRED - the run could neither prove nor refute it.

### 15.5 Fix - prudynt patch 0018 (bounded audio-worker join + wedge isolation) *(the proven fix)*
Replaces the unbounded join with a bounded wait (<=2000 ms, 10 ms poll) on a new `thread_exited` flag (set by `AudioWorker::thread_entry` after `run()` returns - a wedged worker never reaches it). Clean exit -> `pthread_join` as before; timeout -> `pthread_detach` + latch a new `wedged` flag, `imp_audio` deliberately RETAINED (still owned by the stuck thread). `!wedged` gates the input, audio-output (which blocks on `has_started.acquire()`) and backchannel starts AND the stop block, so a wedged audio thread can NEVER re-hang the supervisor or open a second `/dev/dsp`. Net: video + RTSP restart ALWAYS completes; a hard `/dev/dsp` D-state degrades audio only, until the next process restart. Portable (no `_GNU_SOURCE` / `pthread_timedjoin_np`).

### 15.6 Fix - prudynt patch 0019 (video RTP-egress counters) *(enables detection)*
Per-video-stream `rtp_pkts` / `rtp_bytes` (uint32, delta-based - this 32-bit MIPS SoC avoids 64-bit atomics) bumped with two relaxed atomic adds at `IMPDeviceSource::deliverFrame`, right before `FramedSource::afterGetting` hands the NAL to the RTP sink (video only via `if constexpr`), plus `subs` (= `hasDataCallback`, ch0's OWN RTSP-consumer flag). All three emitted in the existing `prudyntctl {"streamN":{"stats":null}}` JSON next to fps/Bps.

### 15.7 Fix - stream watchdog data-plane check (`overlay/etc/init.d/S32prudyntwd`) *(the safety net)*
New `check_video_rtp()` reads the `rtp_pkts` delta and treats "encoder alive (fps>0) but rtp_pkts FROZEN while ch0 has its OWN RTSP subscriber" as not-serving - the exact "encoder alive, zero RTP" blind spot the old fps + RTSP-OPTIONS checks missed. Gated on ch0's `subs==1` at both samples (NOT a channel-agnostic `:554` socket count - see §15.8). Fail-safe: every ambiguity (probe disabled / no subscriber / no counter field / query error) is INCONCLUSIVE = serving, so it can never self-inflict a restart. Feeds the UNCHANGED grace / DOWN_STREAK(2) / RESTART_LIMIT(3) / reboot ladder.

### 15.8 Verification (two adversarial rounds - caught a blocker)
`git apply --check` clean on a materialized post-0017 tree; `sh -n` clean, LF. Round-1 adversarial review CAUGHT a blocking watchdog bug in an earlier version: a channel-blind `:554` established-socket gate could reboot-loop a HEALTHY camera whose ch0 encoder was kept warm by a NON-RTSP consumer (recording / prebuffer / jpeg) while a ch1-only viewer held `:554`. FIXED by the ch0-specific `subs` gate (`VideoWorker` forces `fps=0` when nothing keeps ch0 warm, so `fps>0 && subs==1 && rtp_pkts frozen` is the true wedge signature). Round-2 PROVED the `subs` signal sound: the video `IMPDeviceSource` is on-demand per RTSP SETUP (`OnDemandServerMediaSubsession`, torn down when the last client leaves); the only `StreamReplicator` is audio-only. Documented benign residual: a genuine IDLE ch0 client holding RTSP PAUSE keeps `subs=1` with no RTP -> one restart (grace re-arms; not a loop).

### 15.9 Resource impact (from the committed diff)
CPU: negligible - 0019 adds two relaxed atomic adds per delivered video NAL (~60/s total); 0018 adds a per-restart idle sleep-poll (rare, config-driven); the watchdog adds one 4 s idle sleep + 2 `prudyntctl` reads per 60 s cycle. Flash: NONE - no added code path writes to `/etc/prudynt.json`, overlay, or any persistent file; prudynt logs to `/dev/log_main` (RAM) / `/dev/null`; the watchdog reads via the tmpfs IPC socket and processes in RAM; no `events.jsonl` (the flight-recorder was NOT implemented). The only abnormal-consumption scenario is a genuinely persistent data-plane fault that neither restart nor reboot clears -> bounded reboot cycling (~every 5 min) via the EXISTING recovery machinery; the added code still writes no flash.

### 15.10 Status
Committed `13785375c`, pushed to `origin/pt2-firmware` (three files: patches 0018 + 0019, and the watchdog). Scope limited by directive to P2 (0018, the proven fix) + P4 (0019 + watchdog, the detection safety net); additional designed-but-deferred items (bounded/interruptible capture read, dropping `mutex_main` from the source lifecycle, audio-SDP null-guard) are recorded in RCA-AND-FIX.md §5 as future work, NOT implemented. NOT built, flashed, or runtime-tested on the patched binary - that stays user-performed via GitHub Actions.
