Thingino - Sonoff CAM-PT2
-------------------------

Thingino (_/θinˈdʒiːno/_, _thin-jee-no_) is an open-source firmware for Ingenic SoC IP cameras.
This is the **`pt2-firmware`** branch - Thingino hardened for the **Sonoff CAM-PT2**
(Ingenic T23N / SC2336P / ATBM6012BX) for unattended, always-on use.

![Thingino Web UI][10]

## What's changed in this branch

Release notes for the Sonoff CAM-PT2 build. Full engineering detail lives in
[`PT2-FIX-SET.md`](PT2-FIX-SET.md).

### Streaming & startup reliability
- **Fixed a startup crash-loop** where prudynt never served RTSP - root-caused to a miscompiled libc ABI shim (now a pinned prebuilt, `-flto` dropped).
- **Reliable cold-boot streaming:** fail-fast if the encoder/ISP didn't come up, an anti-brick config restore, and a stream watchdog that checks real encoder frames (not just an RTSP probe) with a bounded restart→reboot ladder.
- **prudynt patches 0001-0020:** cleaner VPU teardown/reclaim, live-apply OSD changes without tearing down the encoder, related stream-config fixes, and the audio-reconfig / RTP-watchdog / mic-speaker-isolation work below.

### Day / night & optics
- Day/night **survives reboot** and no longer rewrites config on every toggle (a tiny atomic sidecar → near-zero flash wear).
- Correct **IR-cut / colour / IR-LED** behaviour; fixed an executor race and a stale-marker dedup that could leave the camera stuck after a Night→Day switch, plus a white-LED lighting at boot.

### Physical privacy
- Reboot-safe **lens-park privacy:** tilts the lens away and interlocks the IR/white emitters and mic so a parked sensor isn't warmed; fail-open manual escape.

### Audio / video
- Fixed **OPUS WebRTC stutter** (RTP-timestamp jitter); **mic-mute** now works without restarting the audio worker; live **audio-codec switching** no longer breaks the stream.

### Home Assistant / MQTT
- Reliable MQTT - **confirmed (QoS1) publishes**, per-entity publish locking, stale-poll protection, an availability/reconcile backstop, and write-on-change toggles - no more stuck retained states or publish hangs.

### Web UI
- Faster, honest live status (CPU / RAM / storage / gain); status and query CGIs are bounded with timeouts so a slow backend can't hang the page.

### Robustness under memory pressure
- **OOM protection** for prudynt, the SSH listener, and the watchdog so recovery and remote access survive low-memory conditions.

### Timezone
- Local time instead of UTC (works around a uClibc DST-rule rejection).

### Kernel D-state wedge - resilience (new)
A rare Ingenic-3.10 kernel *lost-wakeup* could strand a task uninterruptibly and, via a `pidof` `/proc` scan behind it, wedge the day/night + config path until reboot.
- **R1** - replaced the `pidof`-based singleton guard with a `flock` lock, removing that failure mode (the actual fix).
- **R2** - cut Home-Assistant `jct` forks 12→1 per poll (less exposure to the trigger; behaviour unchanged).
- **R3** - an independent detector reboots (SysRq-b) only on a clear, sustained wedge signature, leaving the hardware watchdog untouched.

All three are RAM-only (no added flash writes).

### Audio-reconfig resilience & RTP data-plane watchdog (new)
An audio-settings **Save** (codec / sample-rate / bitrate / mic toggle) restarts the audio worker; if that worker was wedged in an uninterruptible `/dev/dsp` read, the single supervisor thread's `pthread_join` hung forever and the whole restart path froze - so RTSP + video stopped being served until prudynt was restarted.
- **prudynt patch 0018** - the audio-worker join is now **bounded**: on timeout it detaches and latches a `wedged` flag that gates every audio (re)start, so a stuck audio thread can never hang the supervisor or open a second `/dev/dsp`. **Video and RTSP always keep running;** audio degrades until the next restart.
- **prudynt patch 0019 + stream watchdog** - a **data-plane** liveness signal: prudynt exports a real per-stream RTP-egress counter (plus a "has an RTSP subscriber" flag), and the watchdog now treats "encoder alive (fps>0) but zero RTP delivered to a subscribed client" as not-serving - a blind spot the old fps/OPTIONS checks missed. Fail-safe (any ambiguity stays "serving"), feeding the existing grace/streak/reboot ladder.
- **prudynt patch 0020 - mic/speaker isolation** - the audio-output (speaker) and two-way-audio (backchannel) workers are now decoupled from the mic-capture wedge: each gates its own (re)start and stop on its own `wedged` latch and every wait is bounded, so a stuck mic can no longer disable speaker/talk recovery and no audio wedge can freeze the supervisor.

RAM-only (no added flash writes). The supervisor-deadlock fix is proven; the exact way a wedge also stalled *video* delivery is not fully proven, so the watchdog is the safety net that catches a silent no-RTP condition regardless of cause. Engineering detail: [`PT2-FIX-SET.md`](PT2-FIX-SET.md) §15-16.

> **Reviewed before build.** This change set (R1-R3, patches 0018/0019/0020, the watchdog) went through three adversarial code-review rounds; the first two caught and fixed four defects (two in patch 0020 itself - a moved deadlock and a use-after-free - plus a motion-alert-suppression regression and a false-reboot watchdog path), and an independent third round returned no blockers. See [`PT2-FIX-SET.md`](PT2-FIX-SET.md) §16.

### Known limitation / future work: two-way audio over WebRTC
Two-way audio (browser mic -> camera speaker) via the go2rtc WebRTC bridge currently works reliably only for the first talk session after a go2rtc restart, because go2rtc reconnects a shared upstream producer when a mic track is added mid-stream. This is a **go2rtc-side** streaming-bridge limitation, not a camera fault (the camera serves concurrent sessions and a fresh backchannel fine), and it is **not** addressed in this firmware - it is documented as future work with a proposed fix (a dedicated on-demand talk source) in the engineering record. The camera's single-speaker semantics are intentionally preserved.

## Building

Builds like upstream Thingino (Buildroot) - see [Building from sources][7].
Board profile: `configs/cameras/sonoff_pt2_t23n_sc2336p_atbm6012bx`.

## Documentation

- [`PT2-FIX-SET.md`](PT2-FIX-SET.md) - detailed engineering / incident record for this branch
- [Firmware Image Structure](docs/firmware-image-structure.md)
- [Camera Recovery](docs/camera-recovery.md)

## Resources

- [Project Website][0] · [Wiki][1] · [Discord][3] · [Telegram][4]

## Recommended settings

Starting-point values that work well on the Sonoff CAM-PT2. Set them in the web UI
under **Audio Settings**; each change requires a prudynt audio-thread restart, and
**Save configuration to file** persists them.

### Audio

Microphone (sound captured by the camera):

| Setting | Value |
| --- | --- |
| Codec | OPUS |
| Sampling, Hz | 16000 |
| Bitrate, kbps | 32 |
| Mic volume | 90 |
| Mic gain | 25 |
| ALC gain | 0 |
| Noise suppression | 2 |
| Compression gain, dB | 0 |
| Target level, dBfs | 10 |
| AGC Enabled | Off |
| High pass filter | On |
| Force stereo | Off |

Speaker (sound played on the camera speaker):

| Setting | Value |
| --- | --- |
| Speaker volume | 65 |
| Speaker gain | 25 |
| Speaker sampling, Hz | 16000 |

[0]: https://thingino.com/
[1]: https://github.com/themactep/thingino-firmware/wiki
[3]: https://discord.gg/xDmqS944zr
[4]: https://t.me/thingino
[7]: https://github.com/themactep/thingino-firmware/wiki/Building-from-sources
[10]: https://github.com/user-attachments/assets/5e74827c-47f9-4ea0-b523-d12a199a9974
