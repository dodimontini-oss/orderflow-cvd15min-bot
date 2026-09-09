"""Broker reconciliation. No credentials or network work at import time."""
import hashlib
import math
import time
from decimal import Decimal, ROUND_DOWN
import pandas as pd
import requests

TERMINAL = {"filled", "canceled", "expired", "rejected"}

def positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Expected finite positive price, quantity or risk")
    return value

def quantity(value):
    # Never round a sell above the balance actually held.
    return format(Decimal(str(positive(value))).quantize(Decimal("0.000000001"), rounding=ROUND_DOWN), "f")

def signal_id(strategy, symbol, timestamp, risk_distance=None):
    digest = hashlib.sha256(f"{strategy}|{symbol}|{pd.Timestamp(timestamp).isoformat()}".encode()).hexdigest()[:20]
    # Encode the ORIGINAL stop distance in broker-persisted order metadata.
    suffix = "" if risk_distance is None else "_" + format(positive(risk_distance), ".10g")
    return f"{strategy[:8]}-{digest}{suffix}"

def order_risk(order):
    identifier = order.get("client_order_id", "")
    if not identifier.startswith(("dc-", "gc-")) or "_" not in identifier:
        return None
    return positive(identifier.rsplit("_", 1)[1])

class Alpaca:
    def __init__(self, base, headers, http=requests):
        if base.rstrip("/") != "https://paper-api.alpaca.markets":
            raise ValueError("Only the paper endpoint is supported")
        self.base, self.headers, self.http = base, headers, http

    def get(self, path, params=None, missing=False):
        r = self.http.get(self.base + path, headers=self.headers, params=params, timeout=20)
        if missing and r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def by_client(self, identifier):
        return self.get("/v2/orders:by_client_order_id", {"client_order_id": identifier}, missing=True)

    def submit(self, body):
        identifier = body["client_order_id"]
        existing = self.by_client(identifier)
        if existing is not None:
            return existing
        try:
            r = self.http.post(self.base + "/v2/orders", headers=self.headers, json=body, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException:
            # Never blindly resubmit an uncertain mutation.
            existing = self.by_client(identifier)
            if existing is not None:
                return existing
            raise

    def orders(self, symbol=None, status="all"):
        params = {"status": status, "limit": 500, "direction": "desc", "nested": "true"}
        if symbol:
            params["symbols"] = symbol
        out, seen = [], set()
        while True:
            page = self.get("/v2/orders", params)
            fresh = [o for o in page if o["id"] not in seen]
            out.extend(fresh)
            seen.update(o["id"] for o in fresh)
            if len(page) < 500:
                break
            if not fresh:
                raise RuntimeError("Order history pagination did not advance")
            params["until"] = page[-1]["submitted_at"]
        return out

    def position(self, symbol):
        return self.get("/v2/positions/" + symbol.replace("/", ""), missing=True)

    def latest_entry(self, symbol):
        candidates = [o for o in self.orders(symbol) if o.get("side") == "buy" and float(o.get("filled_qty") or 0) > 0]
        return max(candidates, key=lambda o: o.get("filled_at") or o["submitted_at"], default=None)

    def cancel(self, order):
        r = self.http.delete(self.base + "/v2/orders/" + order["id"], headers=self.headers, timeout=20)
        if r.status_code not in (404, 422):
            r.raise_for_status()
        for _ in range(20):
            state = self.get("/v2/orders/" + order["id"])
            if state["status"] in TERMINAL:
                return state
            time.sleep(.25)
        raise RuntimeError("Cancellation is still pending; closing quantity is not safe to infer")

    def close(self, symbol):
        # Broker position close prevents an oversized opposite-side opening order.
        original_orders = self.orders(symbol, "open")
        for order in original_orders:
            self.cancel(order)
        current = self.position(symbol)
        if current is None or float(current["qty"]) == 0:
            return {"status": "already_flat"}
        try:
            r = self.http.delete(self.base + "/v2/positions/" + symbol.replace("/", ""), headers=self.headers, timeout=20)
            r.raise_for_status()
        except requests.exceptions.RequestException:
            self.restore_protection(symbol, original_orders)
            raise
        order = r.json()
        # A failed or uncertain liquidation must make the run fail visibly.
        for _ in range(20):
            state = self.get("/v2/orders/" + order["id"])
            if state["status"] == "filled":
                if self.position(symbol) is not None:
                    raise RuntimeError("Liquidation filled but residual position remains")
                return state
            if state["status"] in TERMINAL:
                self.restore_protection(symbol, original_orders)
                raise RuntimeError("Liquidation did not fill; position needs protection")
            time.sleep(.25)
        raise RuntimeError("Liquidation pending; reconcile before any new entry")

    def restore_protection(self, symbol, original_orders):
        """Restore canceled legs only after reconciling an uncertain close request.

        If a market close is pending, do not reserve its quantity with new exits.
        Network failures propagate: no program can guarantee a remote stop while
        the broker is unreachable.
        """
        current = self.position(symbol)
        if current is None:
            return
        if self.orders(symbol,"open"):
            return
        flattened=[]
        for order in original_orders:
            flattened.append(order)
            flattened.extend(order.get("legs") or [])
        stop=next((o.get("stop_price") for o in flattened if o.get("stop_price")),None)
        target=next((o.get("limit_price") for o in flattened if o.get("type")=="limit" and o.get("limit_price")),None)
        if stop is None and target is None:
            raise RuntimeError("Closing failed and no original protective levels are available")
        body={"symbol":symbol,"qty":quantity(abs(float(current["qty"]))),
              "side":"sell" if current["side"]=="long" else "buy","time_in_force":"gtc",
              "client_order_id":signal_id("restore",symbol,pd.Timestamp.now(tz="UTC"))}
        if stop is not None and target is not None:
            body.update(type="limit",order_class="oco",take_profit={"limit_price":target},stop_loss={"stop_price":stop})
        elif stop is not None:
            body.update(type="stop",stop_price=stop)
        else:
            body.update(type="limit",limit_price=target)
        self.submit(body)

    def session(self, now=None):
        now = pd.Timestamp.now(tz="America/New_York") if now is None else pd.Timestamp(now).tz_convert("America/New_York")
        rows = self.get("/v2/calendar", {"start": str(now.date()), "end": str(now.date())})
        if not rows:
            return None
        day = rows[0]
        opening = pd.Timestamp(f"{day['date']} {day['open']}", tz="America/New_York")
        closing = pd.Timestamp(f"{day['date']} {day['close']}", tz="America/New_York")
        return opening, closing

def closed_rth(df, minutes=5, now=None):
    if df.empty:
        return df
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    t = pd.to_datetime(df["t"], utc=True)
    hm = t.dt.tz_convert("America/New_York").dt.strftime("%H:%M")
    return df[(hm >= "09:30") & (hm < "16:00") & (t + pd.Timedelta(minutes=minutes) <= now)].copy().reset_index(drop=True)

def value_area(day, fraction=.7):
    if day.empty or day.volume.sum() <= 0:
        return None
    width = max(.05, (day.high.max() - day.low.min()) / 40)
    bins = (day.typical / width).apply(math.floor)
    volumes = day.groupby(bins).volume.sum()
    lo = hi = int(volumes.idxmax())
    total = volumes.loc[lo]
    while total < fraction * volumes.sum() and (lo > volumes.index.min() or hi < volumes.index.max()):
        if lo > volumes.index.min() and (hi >= volumes.index.max() or volumes.get(lo-1, 0) >= volumes.get(hi+1, 0)):
            lo -= 1
            total += volumes.get(lo, 0)
        else:
            hi += 1
            total += volumes.get(hi, 0)
    return {"vah": (hi+1)*width, "val": lo*width, "poc": (int(volumes.idxmax())+.5)*width}
