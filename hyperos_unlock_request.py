# -*- coding: utf-8 -*-
"""
hyperos-unlock-request
======================

Sends the Xiaomi Community "Apply for unlocking" (bootloader unlock permission)
request for Global HyperOS devices, timed to the millisecond so it ARRIVES at
Xiaomi's server just after the daily quota resets at 00:00 Beijing time (UTC+8).

It measures your network latency, keeps the connections open in advance and
uses an NTP-corrected clock, so it works the same from any country.

Quick start:
    python -m pip install -r requirements.txt
    python hyperos_unlock_request.py --check       # verify your cookie (sends NO unlock request)
    python hyperos_unlock_request.py --rehearsal   # full timing dry run in ~90 s (sends NO unlock request)
    python hyperos_unlock_request.py               # wait for the next reset and send the request

More options:
    --offset 30,60,100     arrival times in ms AFTER 00:00 Beijing (default below)
    --cookie FILE          cookie file (default: cookie.txt next to this script)
    --log FILE             also append all output to FILE
    --yes                  never ask for confirmation (for schedulers)
    --no-ntp               use the system clock as-is

Your cookie is your Xiaomi session. Never share it or commit it to git.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys

# Never crash on characters the console/log encoding cannot represent.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

__version__ = "1.0.0"

try:
    import requests
except ImportError:
    sys.exit("Missing dependency 'requests'. Install with:  python -m pip install requests ntplib")

try:
    import ntplib
except ImportError:
    ntplib = None

# ----------------------------------------------------------------------------- constants

BASE = "https://sgp-api.buy.mi.com"
APPLY_URL = BASE + "/bbs/api/global/apply/bl-auth"         # the actual unlock-permission request
STATE_URL = BASE + "/bbs/api/global/user/bl-switch/state"  # read-only account status
BODY = json.dumps({"is_retry": False})
BEIJING = timezone(timedelta(hours=8))
NTP_SERVERS = ("time.google.com", "pool.ntp.org", "time.windows.com")

# Arrival offsets in ms AFTER 00:00:00.000 Beijing.
#
# Xiaomi evaluates only the FIRST request of an account that reaches the server;
# any other request within roughly the next minute gets apply_result=6
# ("Please try again in a minute"). So:
#   * the earliest request is the one that counts and must NOT arrive before 00:00
#     (it would be evaluated against the previous, already full, quota);
#   * the extra, later requests cost nothing and only cover a lost/failed first one.
DEFAULT_OFFSETS_MS = [30, 60, 100, 150, 200, 300]

# ----------------------------------------------------------------------------- clock
#
# Real time (NTP) is anchored to a monotonic counter (perf_counter), so a system
# clock adjustment during the wait cannot move the shots. It is re-anchored with
# NTP one minute before the target to cancel any drift. The system clock itself
# is never modified.

ntp_offset = 0.0
_anchor_wall = time.time()
_anchor_perf = time.perf_counter()


def _set_anchor(offset: float) -> None:
    global _anchor_wall, _anchor_perf
    _anchor_perf = time.perf_counter()
    _anchor_wall = time.time() + offset


def now() -> float:
    """NTP-corrected epoch seconds, immune to system clock jumps."""
    return _anchor_wall + (time.perf_counter() - _anchor_perf)


def fmt_beijing(ts: float) -> str:
    return datetime.fromtimestamp(ts, BEIJING).strftime("%H:%M:%S.%f")[:-3]


def fmt_local(ts: float, with_date: bool = True) -> str:
    """Time in the user's own local time zone (whatever country they are in)."""
    dt = datetime.fromtimestamp(ts).astimezone()
    off = dt.utcoffset() or timedelta(0)
    sign = "-" if off < timedelta(0) else "+"
    mins = abs(int(off.total_seconds())) // 60
    zone = f"UTC{sign}{mins // 60:02d}:{mins % 60:02d}"  # portable; %Z names vary by OS and language
    return f"{dt:%Y-%m-%d %H:%M:%S} ({zone})" if with_date else f"{dt:%H:%M:%S} ({zone})"


def sync_ntp(skip: bool = False) -> None:
    global ntp_offset
    if skip:
        _set_anchor(0.0)
        print("[NTP] Skipped (--no-ntp): using the system clock as-is.")
        return
    if ntplib is None:
        _set_anchor(0.0)
        print("[NTP] 'ntplib' is not installed: using the system clock as-is "
              "(python -m pip install ntplib for better accuracy).")
        return
    client = ntplib.NTPClient()
    offsets = []
    for server in NTP_SERVERS:
        try:
            offsets.append(client.request(server, version=3, timeout=3).offset)
        except Exception as e:
            print(f"[NTP] {server}: {e}")
    if offsets:
        ntp_offset = statistics.median(offsets)
        _set_anchor(ntp_offset)
        print(f"[NTP] Your system clock is off by {ntp_offset * 1000:+.0f} ms (compensated, clock not modified).")
    else:
        _set_anchor(0.0)
        print("[NTP] Could not reach any NTP server: using the system clock as-is.")


def next_beijing_midnight() -> float:
    n = datetime.fromtimestamp(now(), BEIJING)
    return (n + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def wait_until(target: float) -> None:
    """Coarse sleep, then a yielding spin for the last ~50 ms (OS sleep granularity can be ~15 ms)."""
    while True:
        remaining = target - now()
        if remaining <= 0:
            return
        if remaining > 0.05:
            time.sleep(remaining - 0.03)
        else:
            time.sleep(0)  # yield so parallel shots do not starve each other

# ----------------------------------------------------------------------------- cookie / http


def load_cookie(path: Path, required: bool = True) -> str:
    """
    Accepts either the full 'Cookie:' header copied from a sniffer, or just the
    value of new_bbs_serviceToken copied from the browser.
    """
    if not path.exists():
        if not required:
            return ""
        sys.exit(f"Cookie file not found: {path}\n"
                 "Create it and paste your cookie inside (see README: 'Getting your cookie').")
    cookie = path.read_text(encoding="utf-8").strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:].strip()
    if not cookie:
        if not required:
            return ""
        sys.exit(f"{path} is empty.")
    if "=" not in cookie:  # only the token value was pasted
        cookie = f"new_bbs_serviceToken={cookie};"
    if "new_bbs_serviceToken=" not in cookie:
        print("[!] The cookie has no 'new_bbs_serviceToken=': make sure you copied all of it.")
    return cookie


def make_session(cookie: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "Connection": "Keep-Alive",
        "Content-Type": "application/json; charset=utf-8",
        "Cookie": cookie,
        "User-Agent": "okhttp/4.12.0",
    })
    return s


def _json(resp: requests.Response):
    """Response JSON only if it is an object {...}, otherwise None."""
    try:
        j = resp.json()
    except ValueError:
        return None
    return j if isinstance(j, dict) else None


def measure_rtt(session: requests.Session, n: int = 6) -> float:
    """Median round trip (s) over an already open keep-alive connection."""
    samples = []
    for i in range(n + 1):
        try:
            t0 = time.perf_counter()
            session.head(BASE, timeout=5)
            if i > 0:  # the first one may include the TLS handshake
                samples.append(time.perf_counter() - t0)
        except Exception:
            pass
    if not samples:
        print("[Latency] Could not measure it: assuming 350 ms round trip.")
        return 0.350
    return statistics.median(samples)


def warm_all(sessions: list, http_timeout: float = 8, deadline: float | None = None) -> tuple[float, list]:
    """
    Opens/refreshes every connection IN PARALLEL. With 'deadline' (real time) it
    never waits past that moment, even if a connection hangs.
    Returns (seconds taken, list of shot numbers whose connection did not finish).
    """
    done = [False] * len(sessions)

    def hit(i, s):
        try:
            s.head(BASE, timeout=http_timeout)
        except Exception:
            pass
        done[i] = True

    t0 = time.perf_counter()
    threads = [threading.Thread(target=hit, args=(i, s), daemon=True) for i, s in enumerate(sessions)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=http_timeout + 2 if deadline is None else max(0.0, deadline - now()))
    return time.perf_counter() - t0, [i + 1 for i, d in enumerate(done) if not d]

# ----------------------------------------------------------------------------- responses


def check_state(session: requests.Session) -> tuple[str, str]:
    """
    Read-only account status (sends NO unlock request).
    Returns (state, text). state: approved | can_apply | blocked | too_new | expired | unknown | error
    """
    try:
        resp = session.get(STATE_URL, timeout=10)
    except Exception as e:
        return "error", f"Network error: {e}"
    j = _json(resp)
    if j is None:
        return "error", f"HTTP {resp.status_code} | unrecognized response: {resp.text[:120]!r}"
    if j.get("code") == 100004 or j.get("msg") == "need login":
        return "expired", "Cookie expired or invalid: get a new one."
    data = j.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    is_pass, button, deadline = data.get("is_pass"), data.get("button_state"), data.get("deadline_format", "")
    if is_pass == 1:
        return "approved", f"APPROVED: this account has unlock permission (valid until {deadline}, mm/dd/yyyy)."
    if is_pass == 4:
        if button == 1:
            return "can_apply", "Not approved yet; the account can apply (normal)."
        if button == 2:
            return "blocked", f"Account Error: applying is blocked until {deadline} (mm/dd, Beijing time)."
        if button == 3:
            return "too_new", "The account is less than 30 days old: Xiaomi will not let it apply yet."
    return "unknown", f"Unrecognized status: {json.dumps(j, ensure_ascii=False)[:200]}"


def describe(resp: requests.Response) -> tuple[str, str, float | None]:
    """
    Interprets the bl-auth response.
    Returns (kind, text, deadline). kind: approved | quota | blocked | rate | maybe | rejected | expired | other
    Codes as worded by the official app (see the micommunity library by offici5l):
      1 success | 2,4 "Account Error" | 3 quota limit reached | 5 failed
      6 "Please try again in a minute" | 7 "Please try again later"
    """
    j = _json(resp)
    if j is None:
        return "other", f"HTTP {resp.status_code} | non-JSON response: {resp.text[:120]!r}", None
    code, msg = j.get("code"), j.get("msg", "?")
    data = j.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    result = data.get("apply_result")
    deadline = data.get("deadline")
    deadline = float(deadline) if isinstance(deadline, (int, float)) and deadline > 0 else None
    head = f"HTTP {resp.status_code} | code={code} | apply_result={result}"

    if code == 100004 or msg == "need login":
        return "expired", f"{head} | Cookie expired: get a new one.", deadline
    if code == 100001:
        return "rejected", f"{head} | Rejected by the server: {json.dumps(j, ensure_ascii=False)[:150]}", deadline
    if code == 100003:
        return "maybe", f"{head} | Possibly approved (verify with --check).", deadline
    if code != 0:
        return "other", f"{head} | Unknown code: {json.dumps(j, ensure_ascii=False)[:150]}", deadline

    if result == 1:
        return "approved", f"{head} | APPROVED!", deadline
    if result == 3:
        when = f" | retry from {fmt_local(deadline)}" if deadline else ""
        return "quota", f"{head} | Quota limit reached{when}", deadline
    if result in (2, 4):
        when = f" until {fmt_local(deadline)}" if deadline else ""
        return "blocked", f"{head} | Account Error (applying blocked){when}", deadline
    if result == 6:
        return "rate", f"{head} | 'Please try again in a minute' (another request was evaluated first)", deadline
    if result in (5, 7):
        return "other", f"{head} | 'Application failed, please try again later'", deadline
    return "other", f"{head} | Unrecognized response: {json.dumps(j, ensure_ascii=False)[:150]}", deadline

# ----------------------------------------------------------------------------- shooting


def fire(idx: int, session: requests.Session, send_at: float, offset_ms: int, target: float,
         results: list, rehearsal: bool) -> None:
    wait_until(send_at)
    sent = now()
    jitter_ms = (sent - send_at) * 1000
    try:
        if rehearsal:
            t0 = time.perf_counter()
            r = session.head(BASE, timeout=10)
            kind = "rehearsal"
            text = (f"REHEARSAL (nothing requested) | HTTP {r.status_code} | "
                    f"round trip {(time.perf_counter() - t0) * 1000:.0f} ms")
        else:
            resp = session.post(APPLY_URL, data=BODY, timeout=10)
            kind, text, deadline = describe(resp)
            # Calibration hint: the retry date tells which side of the reset the request landed on.
            if kind == "quota" and deadline is not None:
                if abs(deadline - target) <= 2:
                    text += "  <- ARRIVED EARLY (before the reset): increase --offset"
                elif abs(deadline - (target + 86400)) <= 2:
                    text += "  <- arrived after the reset (quota already full)"
                else:
                    text += "  <- (non-standard retry date: says nothing about early/late)"
    except Exception as e:
        kind, text = "other", f"Network error: {e}"
    results.append((idx, kind))
    print(f"[Shot {idx} {offset_ms:+d}ms] sent {fmt_beijing(sent)} Beijing (jitter {jitter_ms:+.0f} ms) | {text}")


class _Tee:
    """Duplicates output to the console and a log file (the live countdown goes to the console only)."""

    def __init__(self, console, logfile):
        self.console, self.logfile = console, logfile

    def write(self, data):
        try:
            self.console.write(data)
        except Exception:
            pass
        if data.startswith("\r"):
            return
        try:
            self.logfile.write(data)
        except Exception:
            pass

    def flush(self):
        for s in (self.console, self.logfile):
            try:
                s.flush()
            except Exception:
                pass


def parse_offsets(text: str) -> list[int]:
    try:
        offsets = sorted(int(x) for x in text.split(",") if x.strip())
    except ValueError:
        sys.exit("--offset must be one number or several separated by commas, e.g. --offset 30,60,100")
    if not offsets:
        sys.exit("--offset is empty.")
    if min(offsets) < 0:
        print("[!] Warning: a negative offset can arrive BEFORE 00:00 and waste the day's attempt.")
    return offsets


def main() -> int:
    sys.setswitchinterval(0.0005)  # finer thread switching = more punctual shots

    ap = argparse.ArgumentParser(
        prog="hyperos_unlock_request.py",
        description="Send the Xiaomi Community bootloader unlock request so it arrives just after "
                    "the daily quota reset (00:00 Beijing time).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", "--test", dest="check", action="store_true",
                      help="show the account status and exit (sends NO unlock request)")
    mode.add_argument("--rehearsal", "--dry-run", dest="rehearsal", action="store_true",
                      help="full timing dry run with a target ~90 s from now (sends NO unlock request)")
    ap.add_argument("--offset", metavar="MS[,MS...]", default=",".join(map(str, DEFAULT_OFFSETS_MS)),
                    help="arrival times in ms AFTER 00:00 Beijing (default: %(default)s)")
    ap.add_argument("--cookie", metavar="FILE", type=Path, default=Path(__file__).with_name("cookie.txt"),
                    help="cookie file (default: cookie.txt next to this script)")
    ap.add_argument("--log", metavar="FILE", default=None, help="also append all output to FILE")
    ap.add_argument("--yes", "-y", action="store_true", help="never ask for confirmation")
    ap.add_argument("--no-ntp", action="store_true", help="do not query NTP; use the system clock as-is")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args()

    offsets = parse_offsets(args.offset)

    if args.log:
        logf = open(args.log, "a", encoding="utf-8", errors="replace", buffering=1)  # line-buffered
        logf.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        sys.stdout = _Tee(sys.stdout, logf)
        sys.stderr = _Tee(sys.stderr, logf)

    cookie = load_cookie(args.cookie, required=not args.rehearsal)
    sync_ntp(skip=args.no_ntp)

    if not args.rehearsal:
        print("[Status] Checking your account (no unlock request)...")
        state, text = check_state(make_session(cookie))
        print(f"[Status] {text}")
        if state == "expired":
            print("Cookie rejected. Get a new one (see README).")
            return 2
        if state == "approved":
            print("Nothing else to do. Next: Developer options -> Mi Unlock status -> Add account and device.")
            return 0
        if args.check:
            if state == "can_apply":
                print(f"Cookie OK. Start the script without --check before {fmt_local(next_beijing_midnight() - 60, False)}.")
                return 0
            print("Could not confirm that the cookie works: see the message above.")
            return 1
        if state in ("blocked", "too_new"):
            if args.yes:
                print(f"[--yes] Status '{state}': continuing anyway.")
            else:
                ans = input("With this status the request will most likely fail. Continue anyway? (y/N): ")
                if ans.strip().lower() not in ("y", "yes"):
                    return 1

    if args.rehearsal:
        target = float(int(now()) + 90)
        print(f"[REHEARSAL] Simulated target: {fmt_local(target, False)} (nothing will be requested)")
    else:
        target = next_beijing_midnight()
        print(f"[Target] 00:00:00 Beijing = {fmt_local(target)} (your local time)")
        if target - now() < 90:
            print("[!] Less than 90 s left: there may not be enough time to prepare properly.")

    sessions = [make_session(cookie) for _ in offsets]  # one connection per shot

    def countdown_until(t_stop: float) -> None:
        while now() < t_stop:
            print(f"\r  {timedelta(seconds=int(target - now()))} left  "
                  "(keep this window open, do not let the computer sleep)   ", end="", flush=True)
            time.sleep(min(1.0, max(t_stop - now(), 0)))
        print()

    # 1) One minute before: re-sync the clock, open connections in parallel, measure latency.
    countdown_until(target - 60)
    print("[Prep] Re-syncing clock...")
    sync_ntp(skip=args.no_ntp)
    print("[Prep] Opening connections in parallel...")
    took, _ = warm_all(sessions)
    print(f"[Prep] Connections open in {took:.1f} s")
    rtt = measure_rtt(sessions[0], 6)
    one_way = rtt / 2
    print(f"[Latency] round trip ~{rtt * 1000:.0f} ms -> one way ~{one_way * 1000:.0f} ms")

    first_send = target - one_way + offsets[0] / 1000

    # 2) Five seconds before: refresh connections (bounded: never waits past 0.5 s before the first shot).
    countdown_until(target - 5)
    took, pending = warm_all(sessions, http_timeout=4, deadline=first_send - 0.5)
    print(f"[Prep] Connections refreshed in {took:.1f} s")
    if pending:
        print(f"[!] Connection(s) for shot(s) {pending} did not respond in time: they may go out late.")
    if first_send - now() < 0.3:
        print(f"[!] Only {(first_send - now()) * 1000:.0f} ms left before the first shot.")

    results: list = []
    threads = []
    for i, (off, sess) in enumerate(zip(offsets, sessions), start=1):
        send_at = target - one_way + off / 1000
        t = threading.Thread(target=fire, args=(i, sess, send_at, off, target, results, args.rehearsal), daemon=True)
        t.start()
        threads.append(t)
    print(f"[Fire] {len(threads)} shot(s) scheduled (send = target - {one_way * 1000:.0f} ms one way + offset)...")
    for t in threads:
        t.join(timeout=20)
    if any(t.is_alive() for t in threads):
        print("[!] Some shots have not received a response yet: the summary may be incomplete.")

    print("=" * 60)
    if args.rehearsal:
        print("Rehearsal finished. If every 'jitter' is close to 0 ms, the real run will fire on time.")
        return 0

    kinds = [k for _, k in results]
    time.sleep(1)
    state, text = check_state(sessions[0])  # the most reliable confirmation
    print(f"[Final status] {text}")

    if state == "approved":
        print("APPROVED. Next: on the phone, Developer options -> Mi Unlock status -> Add account and device.")
        return 0
    if "approved" in kinds:
        print("A shot answered APPROVED but the status check does not confirm it yet"
              + (" (network error)." if state == "error" else "."))
        print("Run with --check again in a few minutes.")
    elif "maybe" in kinds:
        print("The server answered 'possibly approved'. Run with --check again in a few minutes.")
    elif "expired" in kinds:
        print("The cookie expired: get a new one before the next attempt.")
    elif "blocked" in kinds or state == "blocked":
        print("Xiaomi blocked applications for this account for a while (see the date above).")
    elif "quota" in kinds:
        print("Quota full this time. Try again at the next reset.")
        print("If it says 'ARRIVED EARLY', increase --offset; if 'after the reset', lower it a little.")
        print("Tip: also try 'Add account and device' on the phone - the quota error is sometimes wrong.")
    elif "rate" in kinds:
        print("Xiaomi answered 'try again in a minute': another request from this account was evaluated first.")
    else:
        print("Unexpected result: check the messages above.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
