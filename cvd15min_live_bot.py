"""
Live paper-trading bot for CVD_CROSS (cumulative volume delta crossing
its own rolling moving average) on 15-minute QQQ bars, with the HTF_TREND
confluence (daily 50-SMA trend alignment) - the ONE order-flow variant
that survived full validation (see orderflow_backtest_lab.py and
project_orderflow_strategy_findings memory): PF 1.430 full-sample, 1.334
early-half / 1.503 late-half walk-forward, 215 trades - the strongest
result across everything tested in that research. BLOCK, ABSORPTION, and
every other timeframe/confluence combination did NOT make this cut.

DEDICATED ALPACA PAPER ACCOUNT - its own, separate from every other bot
(see orochi_modea_live_bot.py's docstring for why sharing an account
between two QQQ-trading bots is a real hazard, not a theoretical one -
orb-bot-5min and orb-bot-15min both trade QQQ too, each on their own
account).

STATELESS-CONTAINER DESIGN (same principle as every other bot here -
GitHub Actions gives a fresh container each run, nothing persists):
rather than maintaining a running cumulative-delta state across runs,
this BACKFILLS its own signal history from scratch every single run -
pulls BACKFILL_TRADE_DAYS of QQQ trade ticks (~10 days, far more than the
20-bar CVD moving-average needs to stabilize), reclassifies via the tick
rule (same zero-plus tick test as the backtest - see
orderflow_data_lab.py's docstring for the real data caveats: IEX-only
tape, not the full consolidated SIP tape), rebuilds 15-min RTH bars + CVD
fresh, and checks the last LOOKBACK_BARS completed bars (not just the
newest) for a crossover - the same "check every bar since last look, not
just the newest" mitigation orb_live_bot.py uses against occasional
missed/delayed GitHub Actions runs.

Where the cumulative sum "starts" (10 trading days back here vs. the
backtest's ~500-day window) doesn't matter for crossover DETECTION - a
crossover is the running total's position relative to its own trailing
moving average, which is scale-invariant to the sum's starting point once
enough bars have accumulated to stabilize that average (20 bars = ~4
trading days; 10 days of backfill is a comfortable multiple of that).

A GENUINE DISCREPANCY FROM THE BACKTEST, found while writing this (not
swept under the rug): orderflow_backtest_lab.py's build_daily_trend()
compares each day's own EVENTUAL close against a SHIFTED 50-day SMA -
meaning an intraday bar from 10am on day D gets filtered using day D's
own 4pm close, which hasn't happened yet at 10am. That's a real, if
subtle, same-day lookahead in the backtested confluence. A live bot
CANNOT do this (it has no way to know today's own future close), so
fetch_daily_trend() below uses the most recently COMPLETED session's
close instead - the honest, lookahead-free version. In practice this
should rarely disagree (a 50-day SMA is a slow benchmark; whether a full
day ends up above/below it is usually already true at the open too), but
it's a real difference between what was backtested and what's live, not
just a restatement of the same thing. Worth a corrected re-validation
backtest (swap the lookahead-free version in and re-run walk-forward) if
early live results look meaningfully different from the backtest record.

Rules (exactly matching the validated backtest, modulo the fix above):
1. Every 5 minutes during market hours, rebuild the last ~10 days of
   15-minute QQQ bars (RTH only, 9:30-16:00 ET) with buy/sell volume
   classified via the tick rule (up-tick = buy, down-tick = sell,
   unchanged = inherit the prior classification).
2. Compute cumulative volume delta (CVD) and its 20-bar moving average.
   LONG fires where CVD crosses above its own MA from below; SHORT where
   it crosses below from above.
3. HTF_TREND confluence: only take a LONG if QQQ's most recently
   COMPLETED daily close is above its own trailing 50-day SMA, only take
   a SHORT if below. This is what took the strategy from PF 1.268 (no
   confluence) to PF 1.430 in backtest.
4. Stop = 2x ATR(14) computed on the 15-min timeframe itself (NOT a
   daily ATR - matches the backtest exactly). Target = entry +/- 2x that
   distance (2:1 R:R). Real Alpaca BRACKET order (entry+stop+target in
   one call, GTC), same mechanics as orb_live_bot.py.
5. At most one position at a time - if already in a position or have a
   pending order, skip. No day-based "already traded" tracking needed
   here (unlike ORB): a CVD crossover is a point-in-time event that
   can't recur on a later bar unless CVD genuinely crosses back, so
   "already in a position" alone prevents duplicate entries off one signal.

Environment variables required:
    ALPACA_API_KEY
    ALPACA_SECRET_KEY
"""

import logging
import os
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cvd15-live")

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
TRADING_BASE_URL = "https://paper-api.alpaca.markets"  # paper only - never change without a deliberate decision
DATA_BASE_URL = "https://data.alpaca.markets"
HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}

SYMBOL = "QQQ"
TIMEFRAME = "15min"
CVD_MA_LEN = 20
HTF_SMA_LEN = 50
ATR_LEN = 14
ATR_STOP_MULT = 2.0
RR_RATIO = 2.0
RISK_PER_TRADE_PCT = 1.0
# Caps position notional at MAX_LEVERAGE x equity, matching a standard
# Robinhood Gold / Reg-T margin account's OVERNIGHT buying power (2x
# equity) - not Robinhood's 4x day-trade buying power, since that only
# applies to intraday round trips on a PDT-flagged account and evaporates
# by end of day; this bot holds positions overnight (GTC, no session-
# close flatten), so 2x is the honest real-broker comparison. Added
# 2026-09-08 after orochi_modea_live_bot.py hit a live trade sized to ~4x
# equity (a tight stop_distance let 1%-risk sizing call for far more
# shares than the account's buying power realistically should allow) -
# only ever shrinks qty, same as the buying-power cap below; never grows it.
MAX_LEVERAGE = 2.0
BACKFILL_TRADE_DAYS = 10
BACKFILL_DAILY_DAYS = 90
LOOKBACK_BARS = 3  # check the last N completed bars for a crossover, not just the newest

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)


def market_is_open_now() -> bool:
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:  # Saturday/Sunday
        return False
    return MARKET_OPEN <= now_et.time() <= MARKET_CLOSE


def fetch_recent_trades(days_back: int) -> pd.DataFrame:
    now = datetime.now(UTC)
    start = now - timedelta(days=days_back)
    all_rows = []
    page_token = None
    while True:
        params = {
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10000,
            "feed": "iex",
        }
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(f"{DATA_BASE_URL}/v2/stocks/{SYMBOL}/trades", headers=HEADERS, params=params,
                             timeout=30)
        resp.raise_for_status()
        data = resp.json()
        all_rows.extend(data.get("trades", []))
        page_token = data.get("next_page_token")
        if not page_token:
            break
    if not all_rows:
        return pd.DataFrame()
    df = pd.DataFrame(all_rows)
    df = df.rename(columns={"p": "price", "s": "size"})
    df["t"] = pd.to_datetime(df["t"], utc=True)
    return df.sort_values("t").reset_index(drop=True)


def classify_ticks(df: pd.DataFrame) -> pd.DataFrame:
    prices = df["price"].values
    direction = [0] * len(prices)
    prev_dir = 0
    for i in range(len(prices)):
        if i == 0:
            direction[i] = 0
            continue
        if prices[i] > prices[i - 1]:
            prev_dir = 1
        elif prices[i] < prices[i - 1]:
            prev_dir = -1
        direction[i] = prev_dir
    df = df.copy()
    df["direction"] = direction
    df["buy_size"] = df["size"].where(df["direction"] == 1, 0)
    df["sell_size"] = df["size"].where(df["direction"] == -1, 0)
    return df


def prep_rth(trades_classified: pd.DataFrame) -> pd.DataFrame:
    df = trades_classified.copy()
    et = df["t"].dt.tz_convert(ET)
    df["hm"] = et.dt.strftime("%H:%M")
    return df[(df["hm"] >= "09:30") & (df["hm"] <= "16:00")].reset_index(drop=True)


def build_15min_bars(df_rth: pd.DataFrame) -> pd.DataFrame:
    d = df_rth.set_index("t")
    agg = d.resample(TIMEFRAME).agg(
        open=("price", "first"), high=("price", "max"), low=("price", "min"), close=("price", "last"),
        volume=("size", "sum"), buy_volume=("buy_size", "sum"), sell_volume=("sell_size", "sum"),
        trade_count=("size", "count"))
    agg = agg[agg["trade_count"] > 0].reset_index()
    agg["delta"] = agg["buy_volume"] - agg["sell_volume"]
    return agg


def add_atr(bars: pd.DataFrame) -> pd.DataFrame:
    bars = bars.copy()
    high, low, close = bars["high"], bars["low"], bars["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    bars["atr"] = tr.rolling(ATR_LEN).mean().shift(1)  # prior bars only - no lookahead
    return bars


def add_cvd_signal(bars: pd.DataFrame) -> pd.DataFrame:
    bars = bars.copy()
    bars["cvd"] = bars["delta"].cumsum()
    bars["cvd_ma"] = bars["cvd"].rolling(CVD_MA_LEN).mean()
    prev_cvd, prev_ma = bars["cvd"].shift(1), bars["cvd_ma"].shift(1)
    bull = (bars["cvd"] > bars["cvd_ma"]) & (prev_cvd <= prev_ma)
    bear = (bars["cvd"] < bars["cvd_ma"]) & (prev_cvd >= prev_ma)
    bars["signal"] = None
    bars.loc[bull, "signal"] = "LONG"
    bars.loc[bear, "signal"] = "SHORT"
    return bars


def fetch_daily_trend():
    """QQQ's most recently COMPLETED session's close vs. its own trailing 50-day SMA - lookahead-free
    by construction (a live bot can never know today's own future close). See module docstring's
    "GENUINE DISCREPANCY" note for how and why this differs from the backtest's own confluence."""
    now = datetime.now(UTC)
    start = now - timedelta(days=BACKFILL_DAILY_DAYS)
    params = {"timeframe": "1Day", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 200, "feed": "iex"}
    resp = requests.get(f"{DATA_BASE_URL}/v2/stocks/{SYMBOL}/bars", headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    bars = resp.json().get("bars", [])
    if not bars:
        log.warning("No daily bars returned - can't compute HTF_TREND, failing safe.")
        return None

    closes = [b["c"] for b in bars]
    last_bar_date = pd.to_datetime(bars[-1]["t"]).tz_convert(ET).date()
    now_et = datetime.now(ET)
    if last_bar_date == now_et.date() and market_is_open_now():
        closes = closes[:-1]  # drop today's still-forming bar - only completed sessions count

    if len(closes) < HTF_SMA_LEN + 1:
        log.warning("Only %d completed daily closes available (<%d needed) - failing safe.",
                     len(closes), HTF_SMA_LEN + 1)
        return None

    latest_close = closes[-1]
    sma = sum(closes[-(HTF_SMA_LEN + 1):-1]) / HTF_SMA_LEN  # the 50 sessions before latest_close, not including it
    if latest_close > sma:
        return "BULL"
    if latest_close < sma:
        return "BEAR"
    return None


def get_position():
    resp = requests.get(f"{TRADING_BASE_URL}/v2/positions/{SYMBOL}", headers=HEADERS, timeout=10)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def get_open_orders() -> list:
    resp = requests.get(f"{TRADING_BASE_URL}/v2/orders", headers=HEADERS,
                         params={"status": "open", "symbols": SYMBOL}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_account_info() -> dict:
    resp = requests.get(f"{TRADING_BASE_URL}/v2/account", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def place_bracket_order(direction: str, qty: int, stop: float, target: float) -> dict:
    side = "buy" if direction == "LONG" else "sell"
    body = {
        "symbol": SYMBOL,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "gtc",  # keeps the stop/target legs live even if the position carries past today's close
        "order_class": "bracket",
        "take_profit": {"limit_price": str(round(target, 2))},
        "stop_loss": {"stop_price": str(round(stop, 2))},
    }
    resp = requests.post(f"{TRADING_BASE_URL}/v2/orders", headers=HEADERS, json=body, timeout=15)
    if resp.status_code >= 400:
        log.error("Alpaca rejected the order (status %d): %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()


def check_and_trade():
    if not market_is_open_now():
        log.info("Outside regular market hours (9:30-16:00 ET, weekdays). No action.")
        return

    position = get_position()
    if position is not None and float(position["qty"]) != 0:
        log.info("Already in a position (%s %s shares). Bracket order manages the exit. No action.",
                  position["side"], position["qty"])
        return

    if get_open_orders():
        log.info("Open order(s) already pending on %s. No action.", SYMBOL)
        return

    trades = fetch_recent_trades(BACKFILL_TRADE_DAYS)
    if trades.empty:
        log.info("No trade data returned. No action.")
        return

    classified = classify_ticks(trades)
    df_rth = prep_rth(classified)
    bars = build_15min_bars(df_rth)

    # Drop the currently-forming (incomplete) bar BEFORE computing any indicator on it - a partial
    # bar's delta/high/low aren't representative of a complete bar, and folding one into the
    # cumulative sum would inject a spurious intermediate value.
    now_ts = pd.Timestamp.now(tz="UTC")
    current_bar_start = now_ts.floor(TIMEFRAME)
    bars = bars[bars["t"] < current_bar_start].reset_index(drop=True)

    if len(bars) < CVD_MA_LEN + LOOKBACK_BARS + 1:
        log.info("Only %d completed bars available (<%d needed for warmup). No action.",
                  len(bars), CVD_MA_LEN + LOOKBACK_BARS + 1)
        return

    bars = add_atr(bars)
    bars = add_cvd_signal(bars)

    recent = bars.tail(LOOKBACK_BARS)
    signal_rows = recent[recent["signal"].notna()]
    if signal_rows.empty:
        log.info("No CVD crossover in the last %d completed bars. No action.", LOOKBACK_BARS)
        return
    latest_signal_row = signal_rows.iloc[-1]
    direction = latest_signal_row["signal"]

    daily_trend = fetch_daily_trend()
    if daily_trend is None:
        log.warning("Could not determine HTF daily trend - failing safe (skipping this signal).")
        return
    if (direction == "LONG" and daily_trend != "BULL") or (direction == "SHORT" and daily_trend != "BEAR"):
        log.info("%s crossover found but daily trend is %s - HTF_TREND confluence blocks this. No action.",
                  direction, daily_trend)
        return

    atr = latest_signal_row["atr"]
    if pd.isna(atr) or atr <= 0:
        log.warning("ATR not available on the signal bar - skipping.")
        return

    # Entry reference is the CURRENT market price (the last trade seen in the backfill), not the
    # signal bar's own close - by the time this runs, price has likely moved since that bar closed.
    entry_ref = float(trades.iloc[-1]["price"])
    stop_distance = atr * ATR_STOP_MULT
    stop = entry_ref - stop_distance if direction == "LONG" else entry_ref + stop_distance
    target = entry_ref + stop_distance * RR_RATIO if direction == "LONG" else entry_ref - stop_distance * RR_RATIO

    # Same stale-signal guard as orb_live_bot.py: if price has already moved back through where the
    # stop would sit, Alpaca rejects the bracket order outright - skip locally instead, and this
    # doesn't burn any "attempt" state since no order gets recorded on a local skip.
    STOP_SANITY_BUFFER = 0.01
    if direction == "LONG" and entry_ref <= stop + STOP_SANITY_BUFFER:
        log.warning("Stale signal - price (%.2f) has fallen back through the stop (%.2f). Skipping.",
                     entry_ref, stop)
        return
    if direction == "SHORT" and entry_ref >= stop - STOP_SANITY_BUFFER:
        log.warning("Stale signal - price (%.2f) has risen back through the stop (%.2f). Skipping.",
                     entry_ref, stop)
        return

    account = get_account_info()
    equity = float(account["equity"])
    buying_power = float(account["buying_power"])
    risk_amount = equity * RISK_PER_TRADE_PCT / 100
    qty = int(risk_amount / stop_distance)
    max_affordable_qty = int(buying_power / entry_ref)
    qty = min(qty, max_affordable_qty)
    # Real incident 2026-09-08 (orochi_modea_live_bot.py): a tight stop
    # distance let risk-based sizing call for far more notional than
    # reasonable leverage should allow, only bounded by generous paper
    # buying power. Cap notional at MAX_LEVERAGE x equity too - same
    # "only ever shrinks qty" safety property as the buying-power cap.
    max_leverage_qty = int((equity * MAX_LEVERAGE) / entry_ref)
    qty = min(qty, max_leverage_qty)
    if qty <= 0:
        log.warning("Computed qty <= 0 (risk_amount=%.2f stop_distance=%.4f buying_power=%.2f) - skipping.",
                     risk_amount, stop_distance, buying_power)
        return

    log.info("%s CVD crossover confirmed (signal bar close=%.2f, HTF trend=%s) - placing bracket: qty=%d "
              "stop=%.2f target=%.2f", direction, latest_signal_row["close"], daily_trend, qty, stop, target)
    result = place_bracket_order(direction, qty, stop, target)
    log.info("Alpaca response: %s", result)


if __name__ == "__main__":
    check_and_trade()
