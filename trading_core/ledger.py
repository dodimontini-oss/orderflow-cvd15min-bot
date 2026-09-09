"""FIFO matching for cumulative order fills, including standalone exits.

Order endpoints expose execution prices, not all account fee activities. These
results are explicitly gross of separate broker fees and should be labelled so.
"""
from collections import defaultdict, deque

def closed_trades(orders):
    unique={}
    def collect(order):
        if float(order.get('filled_qty') or 0)>0 and order.get('filled_avg_price'):
            unique[order.get('id',str(id(order)))]=order
        for leg in order.get('legs') or []:
            leg=dict(leg)
            leg.setdefault('symbol',order.get('symbol'))
            collect(leg)
    for order in orders: collect(order)
    lots=defaultdict(deque);closed=[]
    for order in sorted(unique.values(),key=lambda o:o.get('filled_at') or o.get('updated_at') or o.get('submitted_at') or ''):
        symbol=order['symbol'].replace('/','')
        sign=1 if order['side']=='buy' else -1
        qty=float(order['filled_qty']);price=float(order['filled_avg_price'])
        while qty>1e-10 and lots[symbol] and lots[symbol][0]['sign']!=sign:
            lot=lots[symbol][0];matched=min(qty,lot['qty'])
            closed.append({'closed_at':order.get('filled_at') or order.get('updated_at'),
                           'pnl':(price-lot['price'])*matched*lot['sign'],'symbol':symbol,'qty':matched})
            qty-=matched;lot['qty']-=matched
            if lot['qty']<=1e-10:lots[symbol].popleft()
        if qty>1e-10: lots[symbol].append({'sign':sign,'qty':qty,'price':price})
    return closed
