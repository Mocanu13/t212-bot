"""
Trading 212 DEMO aggressive scalper - 1-minute momentum breakouts.
Rule-based: no Anthropic/LLM calls, free Yahoo Finance data.

    pip install requests yfinance pandas
    export T212_API_KEY="..." T212_API_SECRET="..."
    python t212_scalper.py

Idea: buy 20-bar breakouts on volume, take small profits fast, cut losses
fast, time-stop stale trades, compound position size as the account grows.
Demo only. Fills on demo are idealized, so real results would be worse.
"""
import csv
import math
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

# ---------------- CONFIG ----------------
BASE_URL = "https://demo.trading212.com/api/v0"
API_KEY = os.environ.get("T212_API_KEY", "")
API_SECRET = os.environ.get("T212_API_SECRET", "")

WATCHLIST = {                      # T212 ticker -> Yahoo symbol (volatile names)
    "TSLA_US_EQ": "TSLA",
    "NVDA_US_EQ": "NVDA",
    "AMD_US_EQ": "AMD",
    "PLTR_US_EQ": "PLTR",
    "COIN_US_EQ": "COIN",
    "SMCI_US_EQ": "SMCI",
    "MSTR_US_EQ": "MSTR",
    "META_US_EQ": "META",
}

BREAKOUT_BARS = 20       # break above the highest high of last N minutes
VOLUME_MULT = 1.5        # current volume vs average of last N bars
TAKE_PROFIT = 0.004      # +0.4%
STOP_LOSS = 0.003        # -0.3%
MAX_HOLD_MIN = 15        # time stop
POSITION_PCT = 0.25      # 25% of equity per trade
MAX_POSITIONS = 4
CYCLE_SECONDS = 10
TARGET_VALUE = 100_000
HALT_BELOW = 3_000       # kill switch: stop opening trades under this equity
DRY_RUN = False
LOG_FILE = "scalper_log.csv"
# Set by the GitHub workflow; on your own PC leave the defaults.
FLATTEN_AT = os.environ.get("FLATTEN_AT", "15:55")      # NY time: sell everything
EXIT_AFTER_CLOSE = os.environ.get("EXIT_AFTER_CLOSE") == "1"
# ----------------------------------------
FLATTEN_HM = tuple(int(x) for x in FLATTEN_AT.split(":"))

assert "demo.trading212.com" in BASE_URL, "Refusing to run against non-demo URL."

# Minimum seconds between calls (T212 limits: cash 1/2s, portfolio 1/5s, orders ~50/min)
MIN_GAP = {"/equity/account/cash": 2.1, "/equity/portfolio": 5.1,
           "/equity/orders/market": 1.3}
_last_call = {}
session = requests.Session()
session.auth = (API_KEY, API_SECRET)
entry_time = {}          # ticker -> datetime of entry
NY = ZoneInfo("America/New_York")


def api(method, path, **kw):
    gap = MIN_GAP.get(path, 1.0)
    wait = gap - (time.time() - _last_call.get(path, 0))
    if wait > 0:
        time.sleep(wait)
    for attempt in range(5):
        _last_call[path] = time.time()
        r = session.request(method, BASE_URL + path, timeout=20, **kw)
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json() if r.text else {}
    raise RuntimeError(f"Rate limited on {path}")


def log(event, ticker="", qty="", price="", note=""):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "event", "ticker", "qty", "price", "note"])
        w.writerow([datetime.now().isoformat(timespec="seconds"),
                    event, ticker, qty, price, note])
    print(f"[{datetime.now():%H:%M:%S}] {event} {ticker} {qty} {price} {note}")


_flat = {"done": False}


def market_state():
    """'before' open, 'open' (trading allowed), or 'after' (flatten/finished)."""
    now = datetime.now(NY)
    if now.weekday() >= 5:
        return "after"
    hm = (now.hour, now.minute)
    if hm < (9, 31):
        return "before"
    if hm >= FLATTEN_HM:
        return "after"
    return "open"


def minutes_to_open():
    now = datetime.now(NY)
    return (9 * 60 + 31) - (now.hour * 60 + now.minute)


def order(ticker, qty, reason):
    if DRY_RUN:
        log("DRY_ORDER", ticker, qty, note=reason)
        return True
    try:
        api("POST", "/equity/orders/market", json={"ticker": ticker, "quantity": qty})
        log("ORDER", ticker, qty, note=reason)
        return True
    except Exception as e:
        log("ORDER_FAIL", ticker, qty, note=str(e))
        return False


def breakout_signals():
    """One batched Yahoo call for all symbols. Returns {t212_ticker: last_price}."""
    syms = list(WATCHLIST.values())
    df = yf.download(syms, period="1d", interval="1m", group_by="ticker",
                     progress=False, threads=True)
    hits = {}
    for t, sym in WATCHLIST.items():
        try:
            d = df[sym].dropna()
            if len(d) < BREAKOUT_BARS + 2:
                continue
            prev = d.iloc[-(BREAKOUT_BARS + 1):-1]
            last = d.iloc[-1]
            if last["Close"] > prev["High"].max() and \
               last["Volume"] > VOLUME_MULT * prev["Volume"].mean():
                hits[t] = float(last["Close"])
        except Exception:
            continue
    return hits


def run_once():
    state = market_state()
    if state == "before":
        _flat["done"] = False
        if EXIT_AFTER_CLOSE and minutes_to_open() > 20:
            log("DONE", note="started far too early (DST duplicate run), exiting")
            sys.exit(0)
        log("IDLE", note="waiting for US market open")
        return
    if state == "after":
        if not _flat["done"]:
            for p in api("GET", "/equity/portfolio"):
                if p["ticker"] in WATCHLIST:
                    order(p["ticker"], -p["quantity"], "EOD_FLATTEN")
            _flat["done"] = True
            entry_time.clear()
        if EXIT_AFTER_CLOSE:
            log("DONE", note="session over, exiting")
            sys.exit(0)
        log("IDLE", note="session over")
        return

    cash = api("GET", "/equity/account/cash")
    total, free = cash.get("total", 0), cash.get("free", 0)
    positions = {p["ticker"]: p for p in api("GET", "/equity/portfolio")
                 if p["ticker"] in WATCHLIST}
    log("ACCOUNT", note=f"total={total:.2f} free={free:.2f} "
                        f"progress={total / TARGET_VALUE * 100:.1f}% open={len(positions)}")

    # ---- exits ----
    for t, p in list(positions.items()):
        avg, cur, qty = p["averagePrice"], p["currentPrice"], p["quantity"]
        entry_time.setdefault(t, datetime.now())
        pnl = cur / avg - 1
        held_min = (datetime.now() - entry_time[t]).total_seconds() / 60
        reason = None
        if pnl >= TAKE_PROFIT:
            reason = f"TAKE_PROFIT {pnl:.2%}"
        elif pnl <= -STOP_LOSS:
            reason = f"STOP_LOSS {pnl:.2%}"
        elif held_min >= MAX_HOLD_MIN:
            reason = f"TIME_STOP {pnl:.2%}"
        if reason and order(t, -qty, reason):
            free += cur * qty
            positions.pop(t)
            entry_time.pop(t, None)

    # ---- entries ----
    if total < HALT_BELOW:
        log("HALTED", note="equity below kill-switch level")
        return
    if len(positions) >= MAX_POSITIONS:
        return
    for t, price in breakout_signals().items():
        if t in positions or len(positions) >= MAX_POSITIONS:
            continue
        qty = math.floor(min(total * POSITION_PCT, free) / price)
        if qty >= 1 and order(t, qty, "BREAKOUT"):
            free -= qty * price
            positions[t] = {}
            entry_time[t] = datetime.now()


def main():
    if not API_KEY or not API_SECRET:
        sys.exit("Set T212_API_KEY and T212_API_SECRET.")
    log("START", note=f"dry_run={DRY_RUN}")
    while True:
        t0 = time.time()
        try:
            run_once()
        except Exception as e:
            log("ERROR", note=str(e))
        time.sleep(max(1, CYCLE_SECONDS - (time.time() - t0)))


if __name__ == "__main__":
    main()
