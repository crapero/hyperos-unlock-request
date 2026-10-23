# hyperos-unlock-request

Get the **Xiaomi Community bootloader unlock permission** (HyperOS, Global devices) by sending the
"Apply for unlocking" request so that it **arrives at Xiaomi's server just after the daily quota
resets** at 00:00 Beijing time (UTC+8), from anywhere in the world.

Unlike scripts that fire at a fixed time or with a guessed "delay", this one **measures your network
latency**, keeps the connections **already open**, uses an **NTP-corrected clock** and computes when each
request must leave your computer so that it *lands* exactly where you want.

> **Warning**
> Unlocking the bootloader **wipes your phone**, may affect warranty, and Xiaomi stops sending
> HyperOS updates while it is unlocked. Use this tool at your own risk, on **your own account only**.

---

## Contents

- [How it works](#how-it-works)
- [What makes it different](#what-makes-it-different)
- [Requirements](#requirements)
- [Installation](#installation)
- [Getting your cookie](#getting-your-cookie)
- [Usage](#usage)
- [Calibrating `--offset`](#calibrating---offset)
- [Reading the output](#reading-the-output)
- [After you are approved](#after-you-are-approved)
- [FAQ](#faq)
- [Credits](#credits)
- [License](#license)

---

## How it works

Xiaomi approves only a small number of unlock applications per day. The quota resets at
**00:00:00 Beijing time** and is gone within seconds, so what matters is *when your request reaches the
server*.

1. **Checks your account** with a read-only endpoint (no unlock request is sent).
2. **Syncs the clock** with several NTP servers (median). The script keeps its own corrected clock on a
   monotonic counter, so a system clock adjustment cannot move the shots. **Your system clock is never
   modified.**
3. **One minute before the reset** it re-syncs the clock, opens one connection per shot (in parallel)
   and measures the round trip to Xiaomi's server (`sgp-api.buy.mi.com`, Singapore).
4. **Five seconds before** it refreshes the connections (with a hard deadline, so a hanging connection
   can never delay the shots).
5. **Fires** each shot at `00:00:00 − one-way latency + offset`, so it arrives `offset` ms after the reset.
6. **Verifies** the result with the read-only status endpoint.

### Why the first shot is the one that matters

Xiaomi evaluates **only the first request of an account** that reaches the server. Any other request in
roughly the next minute is answered with `apply_result=6` ("Please try again in a minute"). This was
observed directly: with several shots, exactly one got a real answer and all the others got `6`.

Consequences:

- The **earliest shot must not arrive before 00:00** – it would be evaluated against the previous
  (already full) quota and the good shots would then bounce with `6`.
- Extra, **later** shots cost nothing: they only cover a lost or failed first request.
- Sending dozens of requests in a burst does **not** increase your chances.

That is why all offsets are positive (arrival *after* 00:00) and the default is
`30,60,100,150,200,300`.

---

## What makes it different

Most quota scripts send the request at a fixed moment ("fire 1400 ms before midnight", "enter a
delay") and hope it lands in time. Every step between "send" and "arrive" is guessed. This script
measures or removes each of those steps:

| Problem in typical scripts | What happens | What this script does |
|---|---|---|
| **Fixed delay, latency not measured** | The right delay depends on where you live (~15 ms one way from Singapore, ~190 ms from South America), so users try 200, 300, 400 ms… for days | Measures the round trip one minute before and sends each shot at `00:00 − one-way latency + offset`, so the offset is the real **arrival** time |
| **Connection opened when firing** | The first request must do the TCP + TLS handshake (several round trips, up to ~1 s from far away), so it arrives much later than planned | Opens one connection per shot **in parallel** 60 s before, refreshes them 5 s before, and fires over the already open connections |
| **Preparation not time-bounded** | If setup is slow or a connection hangs, the shots go out late. (The first run of this very script fired 3.2 s late because six connections were opened one after another) | Setup starts 60 s ahead and runs in parallel; the final refresh has a **hard deadline 0.5 s before the first shot**: a hanging connection is abandoned, the other shots still fire on time, and the affected shot is reported |
| **NTP time used without compensation** | The NTP answer is already old when it arrives, so the clock is off by half the NTP round trip (often 50–200 ms) | Uses the NTP-computed offset (delay compensated), median of several servers, and **re-syncs 60 s before** the reset |
| **System clock can jump** | If the OS corrects the clock while waiting, the shot moves with it | Clock anchored to a monotonic counter: immune to system clock changes, without ever modifying the system clock |
| **OS sleep granularity** | `sleep()` can overshoot by ~15 ms on Windows, and parallel busy-waiting threads delay each other | Coarse sleep, then a yielding spin for the last 50 ms and fine thread switching: measured jitter **0–1 ms** |
| **Bursts of many requests** | Only the first request is evaluated; if it lands before 00:00 the attempt is wasted and the rest get `6` | A few shots, **all after 00:00**, with the earliest one placed just past the reset |
| **No feedback** | "Quota reached" says nothing about whether you were early or late | Every "quota" answer is labelled **ARRIVED EARLY** or **after the reset**, so you can tune `--offset` with real data |
| **Can't test without spending an attempt** | You only find out at midnight | `--check` uses a read-only endpoint and `--rehearsal` runs the whole timing pipeline ~90 s from now without requesting anything |

---

## Requirements

- A **Global** (non-China) Xiaomi / Redmi / POCO device running HyperOS.
  Devices or ROMs from mainland China are **not** supported by Xiaomi's global unlock flow.
- A Xiaomi account **older than 30 days**, signed in on the phone.
- In the Xiaomi Community app, region set to **Global** (Me → Set up → Change region) so that
  *Me → Unlock bootloader* is available.
- **Python 3.8+** on Windows, macOS, Linux (or Termux on Android).
- A stable connection. Do **not** use a VPN or proxy and do not switch networks near the reset time.

---

## Installation

```bash
git clone https://github.com/crapero/hyperos-unlock-request.git
cd hyperos-unlock-request
python -m pip install -r requirements.txt
```

On Windows, if `python` is not found, try `py` instead.

---

## Getting your cookie

The script authenticates with your Xiaomi Community session cookie (`new_bbs_serviceToken`). Put it in a
file named **`cookie.txt`** next to the script. Either format works:

```text
new_bbs_serviceToken=AbC...xyz;versionCode=500440;versionName=5.4.40;deviceId=593E...;
```

or just the token value:

```text
AbC...xyz
```

### Option A – Browser (easiest)

1. Open <https://c.mi.com/global/> in a desktop browser and sign in with the **same Xiaomi account**
   that is on your phone.
2. Press **F12** → **Application** (Chrome/Edge) or **Storage** (Firefox) → **Cookies**.
3. Find **`new_bbs_serviceToken`**, copy its **value** and paste it into `cookie.txt`.

### Option B – Capture it from the app

1. Run the Xiaomi Community app on an Android emulator (Android Studio, image **"Google APIs"**, not
   "Google Play") and intercept it with [HTTP Toolkit](https://httptoolkit.com) → *Android device via ADB*.
   Physical phones usually reject the interception certificate unless rooted.
2. In the app go to *Me → Unlock bootloader* and tap **Apply** (a "quota limit reached" answer is fine).
3. In HTTP Toolkit open the `POST sgp-api.buy.mi.com /bbs/api/global/apply/bl-auth` request and copy
   the whole value of the **`Cookie`** request header into `cookie.txt`.

Then verify it:

```bash
python hyperos_unlock_request.py --check
```

Expected: `Not approved yet; the account can apply (normal).`

> **Keep your cookie private.** Anyone with it can act as your Xiaomi account. `cookie.txt` is listed
> in `.gitignore` – never commit it, paste it in chats or show it in screenshots. Logging out of the
> account where you got it invalidates it.

---

## Usage

```bash
# 1. Verify the cookie (sends NO unlock request)
python hyperos_unlock_request.py --check

# 2. Full timing dry run with a target ~90 s from now (sends NO unlock request, no cookie needed)
python hyperos_unlock_request.py --rehearsal

# 3. Real run: start it any time before the reset and leave it running
python hyperos_unlock_request.py --log attempts.log
```

The script prints when the reset happens **in your local time** (e.g. 13:00 in Argentina, 21:30 in
India, 18:00 in Spain during summer time). Start it at least **two minutes** before; it waits on its own.
Keep the window open and make sure the computer does not sleep.

| Option | Description |
|---|---|
| `--check` (`--test`) | Show the account status and exit. No unlock request. |
| `--rehearsal` (`--dry-run`) | Full dry run ~90 s from now, measuring timing precision. No unlock request. |
| `--offset MS[,MS...]` | Arrival times in ms **after** 00:00 Beijing. Default `30,60,100,150,200,300`. |
| `--cookie FILE` | Cookie file. Default: `cookie.txt` next to the script. |
| `--log FILE` | Also append all output to `FILE` (line-buffered; the live countdown is not logged). |
| `--yes` (`-y`) | Never ask for confirmation (for Task Scheduler / cron). |
| `--no-ntp` | Do not query NTP; use the system clock as-is. |
| `--version` | Show the version. |

Exit codes: `0` approved / check OK, `1` not approved or error, `2` expired cookie, `130` cancelled.

---

## Calibrating `--offset`

The offsets are **arrival** times, so they mean the same for everyone; the script adapts the *send*
time to your own latency. Small uncertainties remain (NTP accuracy, asymmetric routes, the server's own
clock), typically a few tens of ms – that is why the first shot aims at **+30 ms** and not at 0.

Use the result of each attempt to adjust the **first** offset:

| Result of the evaluated shot | Meaning | Next attempt |
|---|---|---|
| `Quota limit reached` + **`ARRIVED EARLY`** | It landed before 00:00 | Increase the first offset (e.g. `50,80,120,...`) |
| `Quota limit reached` + `arrived after the reset` | Others were faster | Lower it a little (e.g. `15,40,80,...`), never below 0 |
| `APPROVED` | 🎉 | – |

Check `--rehearsal` first: every `jitter` should be within a couple of ms.

---

## Reading the output

Abridged output of a real successful run from Argentina (round trip ~385 ms to Singapore):

```text
[NTP] Your system clock is off by -932 ms (compensated, clock not modified).
[Latency] round trip ~385 ms -> one way ~192 ms
[Prep] Connections refreshed in 1.6 s
[Fire] 10 shot(s) scheduled (send = target - 192 ms one way + offset)...
[Shot 1 +30ms] sent 23:59:59.837 Beijing (jitter +0 ms) | HTTP 200 | code=0 | apply_result=1 | APPROVED!
[Shot 2 +50ms] sent 23:59:59.857 Beijing (jitter +0 ms) | HTTP 200 | code=0 | apply_result=6 | 'Please try again in a minute'
...
[Final status] APPROVED: this account has unlock permission (valid until 10/23/2027, mm/dd/yyyy).
```

`sent` is when the request **left your computer**. Shot 1 left at 23:59:59.837 and, with ~192 ms one
way, arrived at about 00:00:00.029 – 30 ms after the reset. Only shot 1 was evaluated; the rest got `6`.

### Response codes (`apply_result`)

| Code | App message | Script shows |
|---|---|---|
| 1 | Application successful | `APPROVED!` |
| 2, 4 | Account Error, please try again after MM/DD | `Account Error (applying blocked)` |
| 3 | Application quota limit reached | `Quota limit reached` (+ early/late hint) |
| 5, 7 | Application failed / Please try again later | `Application failed` |
| 6 | Please try again in a minute | `Please try again in a minute` |

Other `code` values: `100004` = cookie expired, `100001` = rejected, `100003` = possibly approved
(check again with `--check`).

---

## After you are approved

Approval belongs to your **account**, not to a device. Now link the phone and unlock:

1. **Back up everything** – unlocking erases the phone.
2. On the phone, signed in with the same account: turn **Wi-Fi off and mobile data on** (SIM inserted).
3. *Settings → Additional settings → Developer options → Mi Unlock status → Add account and device.*
   It should say "Added successfully". If not, sign out of the Xiaomi account, reboot, sign in and retry.
4. On a PC, download the official **Mi Unlock** tool (or [MiUnlockTool](https://github.com/offici5l/MiUnlockTool)),
   sign in, boot the phone into **fastboot** (Power + Volume down), connect it and press *Unlock*.
5. You will most likely be asked to **wait** (usually 72 h on HyperOS). It is a server-side countdown:
   do not remove the account or factory-reset the phone meanwhile. When it ends, repeat step 4.

---

## FAQ

**Does it only work from a specific country?**
No. Everyone outside mainland China applies through the same server in Singapore. The script measures
*your* latency and shows the reset in *your* local time. It has been used from South America with a
~385 ms round trip.

**Does it change my computer's clock?**
No. It measures how far your system clock is off and compensates internally. It re-measures one minute
before the reset because many computers drift (one test machine drifted ~120 ms per hour).

**Will everybody fire at the same milliseconds?**
Everybody targets the same *arrival* times (default +30 ms after 00:00), but each computer sends at a
different moment depending on its latency: ~15 ms early from Singapore, ~190 ms early from Argentina, etc.

**Why not send 100 requests to be safe?**
Because only the first one that reaches the server is evaluated (see
[above](#why-the-first-shot-is-the-one-that-matters)). More early requests only increase the risk of
wasting the attempt, and hammering the server may get the account an "Account Error" block.

**It said "quota limit reached". Is it really over for today?**
Usually yes, but several users report that the error was wrong. Try *Add account and device* on the
phone (mobile data on) – if it says "Added successfully", you were approved.

**How many attempts does it take?**
It depends on luck and competition. Many people need several days. Keep the first offset just after 0
and calibrate with the early/late hint.

**Is there any way to skip the quota or the waiting period?**
Not with this tool. It only automates exactly what the official app does, with better timing.

---

## Credits

- Request format and endpoints: [HyperOS-AAU](https://github.com/dranehm/HyperOS-AAU) and the
  community scripts from the [XDA thread](https://xdaforums.com/t/how-to-unlock-bootloader-on-xiaomi-hyperos-all-devices-except-cn.4654009/).
- Meaning of the response codes: [MiCommunityTool / micommunity](https://github.com/MiForge/MiCommunityTool)
  by offici5l.
- Background on Xiaomi's unlock policy: [Bootloader Unlock Wall of Shame](https://github.com/zenfyrdev/bootloader-unlock-wall-of-shame).

This project is not affiliated with or endorsed by Xiaomi.

## License

[MIT](LICENSE)
