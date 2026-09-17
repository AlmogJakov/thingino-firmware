# Remote WebRTC (over Tailscale) - Fixing ISP Upload Burst-Loss with CAKE Shaping

- **Date:** 2026-09-17
- **Machine:** mini-pc `10.100.102.10` (go2rtc in docker); WAN interface = `enp3s0`
- **Camera:** cam5 `10.100.102.12` | **Phone (Tailscale):** `100.78.198.81` | **home-server Tailscale:** `100.111.118.86`
- **Status:** Fix validated live (UDP proven by packet capture). Declarative persistence (systemd-networkd) applied and validated.

---

## TL;DR

Remote WebRTC viewing (UDP over Tailscale) broke (endless spinner) after an ISP change, while MSE and WebRTC-over-TCP kept working. Root cause: go2rtc/pion sends un-paced, no-FEC UDP **microbursts** (24 packets in 0.16 ms = ~1,400 Mbit/s instantaneous peak), and the ISP's shallow upload buffer drops those bursts. Fix: **traffic shaping on the mini-pc side** (the `tc` tool with the **CAKE** qdisc) that paces outbound traffic smoothly to 93 Mbit, so the burst never overflows the buffer and UDP works at full quality. Everything is on the mini-pc, 100% reversible, and touches nothing on the camera / router / ISP.

---

## Concepts (in plain words)

- **packet** - a small unit of data. Video is sent as thousands of packets per second.
- **UDP vs TCP** - **TCP** = "send with acknowledgement": a lost packet is resent (reliable, slower). **UDP** = "fire and forget": no ack, no resend. WebRTC uses UDP because live video values speed over perfection.
- **buffer** - a small "waiting room" in the router/line where packets wait their turn to leave.
- **microburst** - a very short spike of many packets leaving all at once.
- **bufferbloat** - a buffer that is too deep -> high latency. The current ISP is the opposite: a shallow buffer -> excellent low latency, but it drops bursts.
- **FEC** - Forward Error Correction: redundant data that lets lost packets be reconstructed without a resend. Moonlight has it; WebRTC (pion) does not by default.
- **`tc`** = *traffic control* - a built-in Linux command that controls how packets leave a network interface. A "traffic cop at the mini-pc's exit door".
- **qdisc** - the algorithm that cop uses (how packets wait and in what order they leave).
- **CAKE** - a "smart" qdisc that smooths bursts and releases traffic at a set rate. This is the pacer we added.
- **HTB** - a wrapper qdisc that splits traffic into two "lanes": an internet lane (gets CAKE at 93) and a local/home lane (untouched, full speed).
- **shaping / pacing** - deliberately slowing/spacing packets so they leave smoothly instead of in a spike.
- **goodput vs line rate** - goodput = useful throughput measured by an app (excludes overhead). line rate = the raw capacity of the link.

---

## The problem (with an analogy)

The mini-pc sending video is like a **factory shipping boxes**:
- On average ~4 Mbit/s - nothing.
- But it does not ship at a steady rate. On every **keyframe** (a full reference frame) it **dumps 24 boxes at once** onto the loading dock within 0.16 ms. In that instant the momentary rate spikes to **~1,400 Mbit/s**.
- The ISP's loading dock (the buffer) is small/shallow. When 24 boxes land at once, the overflow **falls off and is lost** (dropped packets).
- With TCP the sender checks receipts and resends what was lost -> it arrives complete. With UDP/WebRTC there is no ack -> the frame is incomplete -> the phone cannot assemble an image -> **spinner**.

So **Moonlight** worked (FEC + pacing) and **TCP/MSE** worked (resend) - only WebRTC's raw UDP failed.

---

## Why it appeared right after the ISP change

The previous ISP was probably more forgiving on upload (a deeper buffer that absorbed the burst). The current ISP keeps latency excellently low (only +5 ms under load - great for gaming) - but the price of a shallow buffer is that it **drops bursts**. So the same WebRTC traffic that survived on the previous ISP gets dropped on the current one. This is not "bad quality" from the ISP - it is a different buffer behavior.

> Honest caveat: there is no access into the ISP's equipment. "Shallow buffer / different queue policy" is the best-evidenced explanation (we measured +5 ms bufferbloat and burst-dependent loss), not a byte-level proof.

---

## What I did (the fix)

I placed a **pacer (CAKE) at the mini-pc's exit door** via `tc`. It takes the internet-bound traffic and releases it smoothly at **93 Mbit** instead of letting the 1,400 spike through. Now the 24 boxes reach the small dock spaced out in time -> they enter cleanly -> zero loss -> UDP works.

The choice of 93: the SQM rule of thumb = **90% of the true line rate** (0.9 x 104 ≈ 93). This guarantees CAKE is always the bottleneck -> always paces -> zero burst loss, even if the ISP line dips momentarily.

---

## Key facts

| Question | Answer |
|------|-------|
| **What changed?** | Only the scheduling of the mini-pc's outbound traffic. Nothing on the camera, the router, or the ISP side. |
| **Specific to WebRTC or all traffic?** | Shaping applies to all internet-bound egress from the mini-pc, but only affects bursts. Ordinary traffic is already below 93 and unaffected. |
| **Upload only, or download too?** | **Upload only** (egress from the mini-pc). Download is untouched. Makes sense - the problem was the mini-pc sending video upstream. |
| **Max upload before?** | True line ~**104 Mbit** (8 parallel streams; a single stream showed 96 and undersold it). Now capped at 93 (90%). The cost is ~11 Mbit, noticeable only during a heavy sustained upload. The stream (~4 Mbit) is nowhere near it. |
| **At what point/rate does it happen?** | Not at the average rate. At the instantaneous level - on every keyframe a ~1,400 Mbit spike for 0.16 ms overflows the buffer. That is why lowering quality (ch1) did not help - even a low bitrate bursts on keyframes. It only breaks again when the shaper exceeds the line (~104+). |
| **Chosen value for persistence** | **93 Mbit** (90% of 104). |

---

## Proof (live measurements)

- **True line rate:** ~104 Mbit (8 parallel upload streams, ~13 each).
- **Bufferbloat:** excellent - 3 ms idle -> 8 ms average under full load (max 10 ms) = shallow buffer confirmed.
- **Live packet capture (while viewing on the phone, go2rtc set to UDP-only):**
  - `udp:8555` (WebRTC): **5,317** video packets go2rtc->phone + **342** feedback packets phone->go2rtc.
  - `tcp:1984` (MSE): **0** packets.
  - Conclusion: genuine UDP, **not** a silent MSE fallback. The 342 return packets = the RTCP that was missing when broken.
- **Confirmed working** at shaping rates 40 / 85 / 92 / 100 / 110 Mbit. Breaks only once the shaper exceeds ~104.
- **Declarative persistence validated:** after writing the drop-in, networkd applied `cake root @93` (handle `8001:`) on enp3s0 itself.

---

## Commands

### A. Manual one-off (for testing only; does not survive reboot)

The simple form (identical to what the declarative method applies):
```bash
# One CAKE shaper on all egress at 93 Mbit. Immediate, but gone on reboot.
tc qdisc replace dev enp3s0 root cake bandwidth 93mbit besteffort
```

Or the two-lane form with LAN exemption (local traffic stays full-speed):
```bash
# Replace the NIC's default queue with an HTB root so we can split traffic into lanes.
# "default 10" = anything not matched by a filter goes to lane 1:10 (the internet lane).
tc qdisc replace dev enp3s0 root handle 1: htb default 10

# Lane 1:10 = internet-bound traffic, hard-capped at 93 Mbit (~90% of the ~104 line).
tc class add dev enp3s0 parent 1: classid 1:10 htb rate 93mbit ceil 93mbit

# Lane 1:20 = local/home traffic, effectively unlimited (1 Gbit) so LAN stays full-speed.
tc class add dev enp3s0 parent 1: classid 1:20 htb rate 1000mbit ceil 1000mbit

# Put CAKE on the internet lane: paces packets to 93 Mbit and smooths the microbursts.
tc qdisc add dev enp3s0 parent 1:10 handle 10: cake bandwidth 93mbit besteffort

# Put plain fq_codel on the local lane: normal fair-queueing, no shaping.
tc qdisc add dev enp3s0 parent 1:20 handle 20: fq_codel

# Route anything destined to the home subnet (10.100.102.0/24) to the un-shaped local lane.
tc filter add dev enp3s0 protocol ip parent 1:0 prio 1 u32 match ip dst 10.100.102.0/24 flowid 1:20
```

**Revert (both manual forms):**
```bash
tc qdisc del dev enp3s0 root    # tear down the live shaping tree -> kernel default queueing
```

---

### B. Persistence - Method A (recommended, the one applied): declarative with systemd-networkd

networkd manages the network (v255) and applies CAKE directly from a config file, **with no script and no trigger** - it applies it itself when it configures the interface (on every boot).

```bash
# Add a CAKE shaper to the existing enp3s0 config via a drop-in (declarative, no script).
mkdir -p /etc/systemd/network/10-netplan-enp3s0.network.d
tee /etc/systemd/network/10-netplan-enp3s0.network.d/cake.conf >/dev/null <<'EOF'
[CAKE]
Bandwidth=93M                        # shape egress to ~90% of the ~104 Mbit line
PriorityQueueingPreset=besteffort    # single tin, ignore DSCP (matches our tested config)
EOF

# Apply now. (On this box "networkctl reconfigure" hits a D-Bus quirk; networkd still
#  applied the drop-in on its own, and it always applies at boot. A manual apply guarantees it live:)
tc qdisc replace dev enp3s0 root cake bandwidth 93mbit besteffort
```

**Revert - Method A:**
```bash
# Remove the declarative drop-in so it won't apply at the next boot.
rm -rf /etc/systemd/network/10-netplan-enp3s0.network.d

# Remove the currently-active shaper now (restores the kernel's default queueing).
tc qdisc del dev enp3s0 root
```

---

### C. Persistence - Method B (alternative): script + systemd service (keeps LAN exemption)

Choose this **only** if you want the local/home egress to stay at full speed (no 93 cap). It requires a `tc filter`, which networkd cannot express declaratively - hence the script.

```bash
# 1) Create an idempotent script that (re)applies the exact shaping tree on demand.
tee /usr/local/sbin/go2rtc-shape.sh >/dev/null <<'EOF'
#!/bin/sh
IF=enp3s0                 # WAN-facing interface of the mini-pc
R=93mbit                  # shaped rate = ~90% of the ~104 Mbit line
LAN=10.100.102.0/24       # home subnet, kept un-shaped
tc qdisc del dev $IF root 2>/dev/null                                   # clear any existing tree (safe if none)
tc qdisc replace dev $IF root handle 1: htb default 10                  # HTB root, unmatched -> internet lane
tc class add dev $IF parent 1: classid 1:10 htb rate $R ceil $R         # internet lane capped at 93
tc class add dev $IF parent 1: classid 1:20 htb rate 1000mbit ceil 1000mbit  # local lane, unlimited
tc qdisc add dev $IF parent 1:10 handle 10: cake bandwidth $R besteffort     # CAKE paces + de-bursts internet lane
tc qdisc add dev $IF parent 1:20 handle 20: fq_codel                    # plain queue on local lane
tc filter add dev $IF protocol ip parent 1:0 prio 1 u32 match ip dst $LAN flowid 1:20  # home traffic -> local lane
EOF

# 2) Make the script executable.
chmod +x /usr/local/sbin/go2rtc-shape.sh

# 3) Create a systemd service that runs the script once, after the network is up.
tee /etc/systemd/system/go2rtc-shape.service >/dev/null <<'EOF'
[Unit]
Description=CAKE egress shaping for go2rtc WebRTC (un-burst UDP over the ISP link)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/go2rtc-shape.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# 4) Reload systemd so it picks up the new unit file.
systemctl daemon-reload

# 5) Enable it at every boot AND apply it right now.
systemctl enable --now go2rtc-shape.service
```

**Revert - Method B:**
```bash
# 1) Stop & disable the boot service so it won't re-apply on reboot.
systemctl disable --now go2rtc-shape.service

# 2) Remove the service + script files.
rm -f /etc/systemd/system/go2rtc-shape.service /usr/local/sbin/go2rtc-shape.sh
systemctl daemon-reload

# 3) Tear down the live shaping tree -> restores the kernel's default queueing.
tc qdisc del dev enp3s0 root
```

---

### D. Difference between Method A and Method B

| | **Method A - declarative (networkd)** | **Method B - script + service** |
|---|---|---|
| How it works | 2 config lines that networkd applies itself | a `.sh` file + a systemd unit that runs after `network-online` |
| Elegance | native, no script/service/trigger | less elegant (script + trigger) |
| tc structure | **single CAKE root** on all egress | **two-lane HTB** + CAKE + `filter` |
| LAN exemption (local at full speed) | no - networkd has no tc `filters` -> local egress also capped at 93 | yes - the `filter` exempts `10.100.102.0/24` |
| Survives reboot / power loss | yes | yes |
| Affects upload only | yes | yes |
| When to choose | **the default** - clean, and enough for everyone | only if you need local egress with no cap at all |

> Why the exemption barely matters here: the mini-pc **pulls** from the camera (download - unaffected), and serving a ~4 Mbit stream locally is far below 93. That is why Method A was chosen.

---

## Appendix: go2rtc config (no change required)

The fix is entirely the CAKE shaping above - **go2rtc needs no configuration change**. The default go2rtc config works fine over the paced UDP path (validated remotely after a reboot).

During diagnosis we *temporarily* forced **UDP-only** in `go2rtc.yaml`, purely to prove the traffic really rode UDP (and not a silent MSE/TCP fallback):

```yaml
webrtc:
  listen: ":8555"
  ice_servers: []
  filters:
    ips: [100.111.118.86]
    networks: [udp4]
```

That was a diagnostic scaffold only and has since been reverted to default. Optional (not needed for the fix): if you ever want to trim ICE negotiation down to just UDP host candidates (no STUN/srflx, no TCP), you can set `ice_servers: []` + `filters: { networks: [udp4] }`.
