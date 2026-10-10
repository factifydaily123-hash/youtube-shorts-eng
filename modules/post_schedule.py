"""
Daily posting scheduler: ONE Short per day at a random time inside US waking hours.

How it works (GitHub Actions cron is only a "wake-up" every hour):
  * Each day (US Eastern date) gets its own pseudo-random target hour inside the
    window [WINDOW_START_HOUR, WINDOW_END_HOUR) ET  (default 10:00-22:00 ET =
    7 AM-7 PM Pacific, i.e. nobody-is-asleep time for the whole US).
  * The hourly wake-up runs `gate`:
        - already posted today?                 -> go=false
        - current ET hour == target hour        -> go=true
        - target hour already passed, not posted yet (missed/failed run)
          and still inside the window            -> go=true  (catch-up / retry)
        - otherwise                              -> go=false
  * When go=true the workflow also sleeps a random 0-45 min first, so the upload
    minute is random too (never ":00" / never the same minute two days in a row).
  * After a successful upload main.py calls mark_posted() -> post_state.json
    (committed back to the repo by the workflow).

Pure standard library. Env overrides:
  POST_WINDOW_START (default 10), POST_WINDOW_END (default 22), POST_TZ (America/New_York),
  POST_MAX_DELAY_MIN (default 45), SCHEDULE_SALT (any text, makes the daily pattern private).
"""

import hashlib
import json
import os
import random
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

STATE_FILE = os.getenv("POST_STATE_FILE", "post_state.json")
TZ = ZoneInfo(os.getenv("POST_TZ", "America/New_York"))
WINDOW_START = int(os.getenv("POST_WINDOW_START", "10"))
WINDOW_END = int(os.getenv("POST_WINDOW_END", "22"))        # exclusive
MAX_DELAY_MIN = int(os.getenv("POST_MAX_DELAY_MIN", "45"))
SALT = os.getenv("SCHEDULE_SALT") or os.getenv("GITHUB_REPOSITORY") or "shorts"


def _now(now=None):
    return (now or datetime.now(TZ)).astimezone(TZ)


def _today(now=None):
    return _now(now).strftime("%Y-%m-%d")


def target_hour(date_str):
    """Deterministic-but-unpredictable hour for that ET date (same answer all day)."""
    seed = int(hashlib.sha256(f"{SALT}:{date_str}".encode()).hexdigest(), 16)
    return random.Random(seed).choice(range(WINDOW_START, WINDOW_END))


def _load_state(path=None):
    try:
        with open(path or STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def posted_today(now=None, path=None):
    return _load_state(path).get("last_post_date_et") == _today(now)


def should_post_now(now=None, path=None):
    n = _now(now)
    if posted_today(n, path):
        return False, "already posted today"
    th = target_hour(_today(n))
    if not (WINDOW_START <= n.hour < WINDOW_END):
        return False, f"outside window (ET hour {n.hour}, target {th})"
    if n.hour == th:
        return True, f"target hour {th} ET reached"
    if n.hour > th:
        return True, f"target hour {th} ET already passed and nothing posted - catching up"
    return False, f"waiting for target hour {th} ET (now {n.hour})"


def mark_posted(video_id="", now=None, path=None):
    n = _now(now)
    data = {"last_post_date_et": _today(n), "last_post_time_et": n.strftime("%H:%M"),
            "video_id": video_id}
    with open(path or STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _gate_cli():
    manual = os.getenv("EVENT_NAME", "") == "workflow_dispatch"
    if manual:
        go, why, delay = True, "manual run", 0
    else:
        go, why = should_post_now()
        delay = random.randint(0, MAX_DELAY_MIN * 60) if go else 0
    print(f"[gate] go={go} delay={delay}s :: {why}")
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"go={'true' if go else 'false'}\ndelay={delay}\n")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "gate":
        _gate_cli()
    else:
        n = _now()
        print("ET now:", n.strftime("%Y-%m-%d %H:%M"), "| today's target hour:", target_hour(_today(n)))
        print(should_post_now())
