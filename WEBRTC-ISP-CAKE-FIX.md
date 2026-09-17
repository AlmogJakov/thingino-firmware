# WebRTC מרחוק (Tailscale) - תיקון אובדן-בּרסטים בקו ה-ISP ע"י CAKE Shaping

- **תאריך:** 2026-09-17
- **מכונה:** mini-pc `10.100.102.10` (go2rtc ב-docker); ממשק WAN = `enp3s0`
- **מצלמה:** cam5 `10.100.102.12` | **טלפון (Tailscale):** `100.78.198.81` | **home-server Tailscale:** `100.111.118.86`
- **סטטוס:** הפתרון אומת חי (UDP הוכח בלכידת חבילות). קיבוע דקלרטיבי (systemd-networkd) הופעל ואומת.

---

## TL;DR

צפייה מרחוק ב-WebRTC (UDP מעל Tailscale) נשברה (ספינר אינסופי) אחרי מעבר ספק-אינטרנט (ISP), בעוד MSE ו-WebRTC-מעל-TCP המשיכו לעבוד. השורש: go2rtc/pion שולח **מיקרו-בּרסטים** של UDP בלי FEC (24 חבילות ב-0.16ms = שיא רגעי של ~1,400 מגהביט/שנייה), וה-buffer הרדוד של קו-ההעלאה של הספק זורק את הבּרסטים האלה. הפתרון: **traffic shaping בצד המיני-פיסי** (הכלי `tc` עם ה-qdisc בשם **CAKE**) שמפזר את התעבורה היוצאת ל-93 מגה חלק - כך הבּרסט לא מציף את ה-buffer, ו-UDP עובד באיכות מלאה. הפתרון כולו על המיני-פיסי, הפיך ב-100%, ולא נוגע במצלמה/בראוטר/בספק.

---

## המושגים (במילים פשוטות)

- **חבילה (packet)** - פיסת מידע קטנה. וידאו נשלח כאלפי חבילות בשנייה.
- **UDP מול TCP** - **TCP** = "שליחה עם אישור": אם חבילה אבדה, נשלחת שוב (אמין, איטי יותר). **UDP** = "שגר ושכח": אין אישור, אין שליחה-חוזרת. WebRTC משתמש ב-UDP כי לוידאו-חי חשוב מהיר, לא מושלם.
- **buffer (חוצץ)** - "חדר-המתנה" קטן בראוטר/בקו, שבו חבילות מחכות לתורן לצאת.
- **microburst (מיקרו-בּרסט)** - התפרצות: הרבה חבילות שיוצאות בבת-אחת ברגע זעיר.
- **bufferbloat** - buffer עמוק מדי → latency גבוה. אצל הספק הנוכחי זה הפוך: buffer רדוד → latency נמוך מצוין אבל זורק בּרסטים.
- **FEC** - קוד-תיקון-שגיאות: מידע-גיבוי שמאפשר לשחזר חבילות אבודות בלי לשלוח שוב. ל-Moonlight יש, ל-WebRTC (pion) אין כברירת-מחדל.
- **`tc`** = *traffic control* - פקודה מובנית בלינוקס ששולטת איך חבילות יוצאות מכרטיס-הרשת. "שוטר-תנועה בדלת-היציאה" של המיני-פיסי.
- **qdisc** - האלגוריתם של אותו שוטר (איך החבילות מחכות ובאיזה סדר יוצאות).
- **CAKE** - qdisc "חכם" שמפזר בּרסטים ומשחרר תעבורה בקצב קבוע. זה המפזר ששמנו.
- **HTB** - qdisc-עוטף שמפצל את התעבורה לשני "נתיבים": נתיב-אינטרנט (מקבל CAKE ב-93) ונתיב מקומי-לבית (לא-נגוע, מהירות מלאה).
- **shaping / pacing** - להאט/לרווח חבילות בכוונה כדי שיצאו חלק במקום בהתפרצות.
- **goodput מול line rate** - goodput = תפוקה שימושית שנמדדת ע"י אפליקציה (מנכה overhead). line rate = הקיבולת הגולמית של הקו.

---

## הבעיה (עם משל)

המיני-פיסי ששולח וידאו הוא כמו **מפעל ששולח קופסאות**:
- בממוצע ~4 מגהביט/שנייה - כלום.
- אבל הוא לא שולח בקצב אחיד. בכל **keyframe** (פריים-מפתח) הוא **מטיל 24 קופסאות בבת-אחת** על רציף-הטעינה תוך 0.16ms. באותו רגע הקצב הרגעי מזנק ל-**~1,400 מגהביט/שנייה**.
- רציף-הטעינה של הספק (ה-buffer) קטן/רדוד. כשנוחתות 24 קופסאות בבת-אחת, העודף **נופל ואבד** (חבילות נזרקות).
- ב-TCP השולח בודק קבלות ושולח שוב את מה שאבד → מגיע בשלמות. ב-UDP/WebRTC אין אישור → הפריים חסר → הטלפון לא מרכיב תמונה → **ספינר**.

לכן גם **Moonlight** עבד (FEC+פיזור) וגם **TCP/MSE** עבד (שליחה-חוזרת) - רק ה-UDP-הגולמי של WebRTC נפל.

---

## למה זה צץ דווקא אחרי מעבר הספק

הספק הקודם כנראה היה סלחני יותר בהעלאה (buffer עמוק יותר שספג את הבּרסט). הספק הנוכחי שומר latency נמוך מצוין (רק +5ms תחת עומס - מעולה למשחקים) - אבל המחיר של buffer רדוד הוא שהוא **זורק בּרסטים**. אז אותה תעבורת WebRTC ששרדה אצל הספק הקודם, נזרקת אצל הנוכחי. זו לא "איכות גרועה" של הספק - זו התנהגות buffer שונה.

> הסתייגות כנה: אין גישה לתוך הציוד של הספק. "buffer רדוד / מדיניות-תור שונה" הוא ההסבר עם הכי הרבה ראיות (מדדנו +5ms bufferbloat ואובדן תלוי-בּרסט), לא הוכחה ברמת-הבייט.

---

## מה עשיתי (הפתרון)

שמתי **מפזר (CAKE) בדלת-היציאה של המיני-פיסי** דרך `tc`. הוא לוקח את התעבורה היוצאת-לאינטרנט ומשחרר אותה חלק בקצב **93 מגה** במקום לתת לשיא ה-1,400 לעבור. עכשיו ה-24 קופסאות מגיעות לרציף הקטן מרוּוחות בזמן → נכנסות יפה → אפס אובדן → UDP עובד.

הבחירה של 93: כלל-האצבע של SQM = **90% מקצב-הקו האמיתי** (0.9 × 104 ≈ 93). זה מבטיח ש-CAKE תמיד צוואר-הבקבוק → תמיד מפזר → אפס אובדן בּרסטים, גם אם הקו של הספק צונח רגע.

---

## עובדות מפתח

| שאלה | תשובה |
|------|-------|
| **מה השתנה?** | רק תזמון התעבורה היוצאת של המיני-פיסי. שום דבר במצלמה, בראוטר, או בצד הספק. |
| **ספציפי ל-WebRTC או כל הטראפיק?** | ה-shaping חל על כל התעבורה היוצאת-לאינטרנט מהמיני-פיסי, אבל פוגע רק בבּרסטים. תעבורה רגילה כבר מתחת ל-93 ולא מושפעת. |
| **העלאה או גם הורדה?** | רק **העלאה** (egress מהמיני-פיסי). הורדה לא נגועה. הגיוני - הבעיה הייתה שהמיני-פיסי שולח וידאו למעלה. |
| **מקס' העלאה לפני?** | הקו האמיתי ~**104 מגה** (8 זרמים במקביל; זרם-בודד הראה 96 והמעיט). עכשיו מוגבל ל-93 (90%). המחיר ~11 מגה, מורגש רק בהעלאה כבדה מתמשכת. הסטרים (~4 מגה) רחוק מאוד מזה. |
| **באיזה שלב/קצב הבעיה קורה?** | לא בקצב הממוצע. ברמה הרגעית - בכל keyframe שיא ~1,400 מגה למשך 0.16ms מציף את ה-buffer. לכן הורדת איכות (ch1) לא עזרה - גם ביטרייט נמוך מתפרץ על keyframes. חוזר-להישבר רק כשהמפזר > הקו (~104+). |
| **הערך שנבחר לקיבוע** | **93 מגהביט** (90% מ-104). |

---

## הוכחה (מדידות חיות)

- **קצב-קו אמיתי:** ~104 מגה (8 זרמי-העלאה במקביל, ~13 כל אחד).
- **Bufferbloat:** מעולה - 3ms במנוחה → 8ms ממוצע תחת עומס מלא (max 10ms) = buffer רדוד מאושר.
- **לכידת חבילות חיה (בזמן צפייה בטלפון, go2rtc ב-UDP-בלבד):**
  - `udp:8555` (WebRTC): **5,317** חבילות וידאו go2rtc→טלפון + **342** חבילות feedback טלפון→go2rtc.
  - `tcp:1984` (MSE): **0** חבילות.
  - מסקנה: UDP אמיתי, **לא** נפילה חשאית ל-MSE. ה-342 חבילות-החזרה = ה-RTCP שהיה חסר כשזה נשבר.
- **אומת עובד** בקצבי shaping 40 / 85 / 92 / 100 / 110 מגה. נשבר רק כשהמפזר עובר את ~104.
- **הקיבוע הדקלרטיבי אומת:** אחרי כתיבת ה-drop-in, networkd יישם בעצמו `cake root @93` (handle `8001:`) על enp3s0.

---

## הפקודות

### א. עיצוב ידני חד-פעמי (לבדיקה בלבד; לא שורד reboot)

הצורה הפשוטה (זהה למה שהשיטה הדקלרטיבית מחילה):
```bash
# One CAKE shaper on all egress at 93 Mbit. Immediate, but gone on reboot.
tc qdisc replace dev enp3s0 root cake bandwidth 93mbit besteffort
```

או הצורה הדו-נתיבית עם הפרדת-LAN (מקומי במהירות מלאה):
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

**החזרה (לשתי הצורות הידניות):**
```bash
tc qdisc del dev enp3s0 root    # tear down the live shaping tree -> kernel default queueing
```

---

### ב. קיבוע - שיטה A (מומלצת, זו שהופעלה): דקלרטיבי עם systemd-networkd

networkd מנהל את הרשת (גרסה 255) ומחיל CAKE ישירות מקובץ-קונפיג, **בלי שום סקריפט ובלי trigger** - הוא מיישם את זה בעצמו כשהוא מקנפג את הממשק (בכל boot).

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

**החזרה - שיטה A:**
```bash
# Remove the declarative drop-in so it won't apply at the next boot.
rm -rf /etc/systemd/network/10-netplan-enp3s0.network.d

# Remove the currently-active shaper now (restores the kernel's default queueing).
tc qdisc del dev enp3s0 root
```

---

### ג. קיבוע - שיטה B (חלופה): סקריפט + systemd service (שומר הפרדת-LAN)

בחר בזה **רק** אם אתה רוצה שה-egress המקומי-לבית יישאר במהירות מלאה (ללא הגבלת 93). זה מחייב `tc filter`, ש-networkd לא תומך בו דקלרטיבית - ולכן צריך סקריפט.

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

**החזרה - שיטה B:**
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

### ד. ההבדל בין שיטה A לשיטה B

| | **שיטה A - דקלרטיבי (networkd)** | **שיטה B - סקריפט + service** |
|---|---|---|
| איך זה עובד | 2 שורות קונפיג ש-networkd מיישם בעצמו | קובץ `.sh` + יחידת systemd שרצה אחרי `network-online` |
| אלגנטיות | נייטיבי, בלי סקריפט/שירות/trigger | פחות אלגנטי (סקריפט + trigger) |
| מבנה ה-tc | **CAKE root יחיד** על כל ה-egress | **HTB דו-נתיבי** + CAKE + `filter` |
| הפרדת-LAN (מקומי במהירות מלאה) | ❌ אין `filters` ב-networkd → גם egress מקומי מוגבל ל-93 | ✅ ה-`filter` פוטר את `10.100.102.0/24` |
| שורד reboot / הפסקת-חשמל | ✅ | ✅ |
| משפיע רק על העלאה | ✅ | ✅ |
| מתי לבחור | **ברירת-המחדל** - נקי, ומספיק לכולם | רק אם צריך egress מקומי ללא שום הגבלה |

> למה ההפרדה כמעט לא משנה במקרה שלנו: המיני-פיסי **מושך** מהמצלמה (הורדה - לא מושפע כלל), והגשה מקומית של סטרים ~4 מגה רחוקה מ-93. לכן שיטה A נבחרה.

---

## נספח: קונפיג ה-go2rtc

בזמן האבחון שינינו את `go2rtc.yaml` ל-**UDP-בלבד** כדי לוודא שהבדיקה באמת על UDP:

```yaml
webrtc:
  listen: ":8555"
  ice_servers: []
  filters:
    ips: [100.111.118.86]
    networks: [udp4]
```

לקונפיג-הקבע המומלץ (טוב גם למקומי וגם למרחוק) עדיף להסיר את הגבלת ה-IP הבודד ולהשאיר UDP host על כל הממשקים בלי TCP/srflx: `ice_servers: []` + `filters: { networks: [udp4] }`. גיבוי הקונפיג המקורי: `go2rtc.yaml.bak.caketest`.
