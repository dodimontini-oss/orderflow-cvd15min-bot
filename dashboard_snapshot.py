"""
One-off JSON snapshot for the live dashboard - same script as
futures_orb/dashboard_snapshot.py (orb-bot-5min/orb-bot-15min), deployed
here with only BOT_ID/BOT_LABEL changed. Reads Alpaca's account,
positions, and bracket orders, matches each bracket entry to its REAL
filled exit leg (matters here for the same reason it matters for ORB -
naive chronological fill-pairing would mismatch entries/exits), and
prints ONE json blob between marker lines so it can be grepped out of the
Action's log cleanly.

Read-only: no orders are placed.

Environment variables required:
    ALPACA_API_KEY
    ALPACA_SECRET_KEY
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0,str(_Path(__file__).resolve().parents[1]))
from trading_core.history import alpaca_orders
from trading_core.ledger import closed_trades as reconcile_trades

import json
import os
from datetime import datetime, timezone

import requests

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}

BOT_ID = "orderflow_cvd15min"
BOT_LABEL = "Order Flow CVD 15-min (QQQ)"


def get_account() -> dict:
    resp = requests.get(f"{ALPACA_BASE_URL}/v2/account", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_positions() -> list:
    resp = requests.get(f"{ALPACA_BASE_URL}/v2/positions", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_all_orders():
    return alpaca_orders(ALPACA_BASE_URL, HEADERS)


def find_filled_leg(order: dict):
    for leg in order.get("legs") or []:
        if leg.get("status") == "filled":
            kind = "target" if leg.get("type") == "limit" else "stop"
            return kind, leg
    return None, None


def run():
    account = get_account()
    equity = float(account["equity"])

    positions = get_positions()
    orders = get_all_orders()
    entries = [o for o in orders if o.get("order_class") == "bracket"]

    open_positions = [{
        "symbol": p["symbol"],
        "side": p["side"].upper(),
        "qty": float(p["qty"]),
        "entry": float(p["avg_entry_price"]),
        "current": float(p["current_price"]),
        "unrealized_pl": float(p["unrealized_pl"]),
        "unrealized_pl_pct": float(p["unrealized_plpc"]) * 100,
    } for p in positions]

    closed_trades = reconcile_trades(orders)

    total_unrealized = sum(p["unrealized_pl"] for p in open_positions)
    realized_pl_alltime = sum(t["pnl"] for t in closed_trades)

    snapshot = {
        "bot_id": BOT_ID,
        "label": BOT_LABEL,
        "broker": "Alpaca (stocks)",
        "currency": "USD",
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "equity": equity,
        "balance": equity - total_unrealized,
        "unrealized_pl": total_unrealized,
        "realized_pl_alltime": realized_pl_alltime,
        "realized_pl_basis": "Gross execution P&L; separate broker fees excluded",
        "open_positions": open_positions,
        "closed_trades": closed_trades,
    }

    print("===SNAPSHOT_JSON_START===")
    print(json.dumps(snapshot))
    print("===SNAPSHOT_JSON_END===")


if __name__ == "__main__":
    run()
