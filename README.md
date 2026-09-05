Thingino — Sonoff CAM-PT2
-------------------------

Thingino (_/θinˈdʒiːno/_, _thin-jee-no_) is an open-source firmware for Ingenic SoC IP cameras.
This is the **`pt2-firmware`** branch — Thingino hardened for the **Sonoff CAM-PT2**
(Ingenic T23N / SC2336P / ATBM6012BX) for unattended, always-on use.

![Thingino Web UI][10]

## What's changed in this branch

Release notes for the Sonoff CAM-PT2 build. Full engineering detail lives in
[`PT2-FIX-SET.md`](PT2-FIX-SET.md).

### Streaming & startup reliability
- **Fixed a startup crash-loop** where prudynt never served RTSP — root-caused to a miscompiled libc ABI shim (now a pinned prebuilt, `-flto` dropped).
- **Reliable cold-boot streaming:** fail-fast if the encoder/ISP didn't come up, an anti-brick config restore, and a stream watchdog that checks real encoder frames (not just an RTSP probe) with a bounded restart→reboot ladder.
- **prudynt patches 0001–0017:** cleaner VPU teardown/reclaim, live-apply OSD changes without tearing down the encoder, and related stream-config fixes.

### Day / night & optics
- Day/night **survives reboot** and no longer rewrites config on every toggle (a tiny atomic sidecar → near-zero flash wear).
- Correct **IR-cut / colour / IR-LED** behaviour; fixed an executor race and a stale-marker dedup that could leave the camera stuck after a Night→Day switch, plus a white-LED lighting at boot.

### Physical privacy
- Reboot-safe **lens-park privacy:** tilts the lens away and interlocks the IR/white emitters and mic so a parked sensor isn't warmed; fail-open manual escape.

### Audio / video
- Fixed **OPUS WebRTC stutter** (RTP-timestamp jitter); **mic-mute** now works without restarting the audio worker; live **audio-codec switching** no longer breaks the stream.

### Home Assistant / MQTT
- Reliable MQTT — **confirmed (QoS1) publishes**, per-entity publish locking, stale-poll protection, an availability/reconcile backstop, and write-on-change toggles — no more stuck retained states or publish hangs.

### Web UI
- Faster, honest live status (CPU / RAM / storage / gain); status and query CGIs are bounded with timeouts so a slow backend can't hang the page.

### Robustness under memory pressure
- **OOM protection** for prudynt, the SSH listener, and the watchdog so recovery and remote access survive low-memory conditions.

### Timezone
- Local time instead of UTC (works around a uClibc DST-rule rejection).

### Kernel D-state wedge — resilience (new)
A rare Ingenic-3.10 kernel *lost-wakeup* could strand a task uninterruptibly and, via a `pidof` `/proc` scan behind it, wedge the day/night + config path until reboot.
- **R1** — replaced the `pidof`-based singleton guard with a `flock` lock, removing that failure mode (the actual fix).
- **R2** — cut Home-Assistant `jct` forks 12→1 per poll (less exposure to the trigger; behaviour unchanged).
- **R3** — an independent detector reboots (SysRq-b) only on a clear, sustained wedge signature, leaving the hardware watchdog untouched.

All three are RAM-only (no added flash writes).

## Building

Builds like upstream Thingino (Buildroot) — see [Building from sources][7].
Board profile: `configs/cameras/sonoff_pt2_t23n_sc2336p_atbm6012bx`.

## Documentation

- [`PT2-FIX-SET.md`](PT2-FIX-SET.md) — detailed engineering / incident record for this branch
- [Firmware Image Structure](docs/firmware-image-structure.md)
- [Camera Recovery](docs/camera-recovery.md)

## Resources

- [Project Website][0] · [Wiki][1] · [Discord][3] · [Telegram][4]

[0]: https://thingino.com/
[1]: https://github.com/themactep/thingino-firmware/wiki
[3]: https://discord.gg/xDmqS944zr
[4]: https://t.me/thingino
[7]: https://github.com/themactep/thingino-firmware/wiki/Building-from-sources
[10]: https://github.com/user-attachments/assets/5e74827c-47f9-4ea0-b523-d12a199a9974
