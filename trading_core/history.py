"""Read-only broker history helpers with no data-science dependencies."""

import requests


def alpaca_orders(base: str, headers: dict, symbol: str | None = None, http=requests) -> list:
    params = {"status": "all", "limit": 500, "direction": "desc", "nested": "true"}
    if symbol:
        params["symbols"] = symbol
    orders, seen = [], set()
    while True:
        response = http.get(f"{base}/v2/orders", headers=headers, params=params, timeout=20)
        response.raise_for_status()
        page = response.json()
        fresh = [order for order in page if order["id"] not in seen]
        orders.extend(fresh)
        seen.update(order["id"] for order in fresh)
        if len(page) < 500:
            return orders
        if not fresh:
            raise RuntimeError("Alpaca order-history pagination did not advance")
        params["until"] = page[-1]["submitted_at"]
