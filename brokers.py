"""Brokers: where orders go.

PaperBroker        fills orders locally at the live price (plus fees and
                   slippage). No account needed, works with any feed.
AlpacaPaperBroker  sends real orders to Alpaca's PAPER trading API. The URL is
                   fixed to the paper endpoint: this code cannot trade real money.

Both expose the same interface:
    position()                 -> (qty, avg_entry_price)   qty 0 when flat
    equity(price)              -> account value
    buy(notional, price, why)  -> Fill or None
    close(price, why)          -> Fill or None
"""
import json
import os
import time
import urllib.parse
from dataclasses import asdict, dataclass

from feeds import FeedError, alpaca_headers, http_json


@dataclass
class Fill:
    time: float
    side: str       # BUY or SELL
    price: float    # fill price after slippage
    qty: float
    fee: float
    pnl: float | None
    reason: str


class PaperBroker:
    def __init__(self, cash=10_000.0, fee_bps=10.0, slippage_bps=5.0, state_path=None, clock=time.time):
        self.fee = fee_bps / 10_000
        self.slip = slippage_bps / 10_000
        self.state_path = state_path
        self.clock = clock
        self.cash, self.qty, self.entry, self.cost_basis = float(cash), 0.0, 0.0, 0.0
        self.realized, self.fees_paid = 0.0, 0.0
        self.start_cash = float(cash)
        if state_path and os.path.exists(state_path):
            self._load()

    # ---- persistence: lets you stop and restart the bot without losing the position
    def _load(self):
        with open(self.state_path) as f:
            s = json.load(f)
        for k in ("cash", "qty", "entry", "cost_basis", "realized", "fees_paid", "start_cash"):
            setattr(self, k, float(s[k]))

    def _save(self):
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: getattr(self, k) for k in
                       ("cash", "qty", "entry", "cost_basis", "realized", "fees_paid", "start_cash")}, f, indent=2)
        os.replace(tmp, self.state_path)

    def position(self):
        return self.qty, self.entry

    def equity(self, price):
        return self.cash + self.qty * price

    def buy(self, notional, price, reason="signal"):
        if self.qty > 0:
            return None
        fill_px = price * (1 + self.slip)
        notional = min(notional, self.cash / (1 + self.fee))
        qty = notional / fill_px
        if qty <= 0 or notional < 1:
            return None
        fee = notional * self.fee
        self.cash -= notional + fee
        self.qty, self.entry = qty, fill_px
        self.cost_basis = notional + fee
        self.fees_paid += fee
        self._save()
        return Fill(self.clock(), "BUY", fill_px, qty, fee, None, reason)

    def close(self, price, reason="signal"):
        if self.qty <= 0:
            return None
        fill_px = price * (1 - self.slip)
        gross = self.qty * fill_px
        fee = gross * self.fee
        pnl = gross - fee - self.cost_basis
        qty = self.qty
        self.cash += gross - fee
        self.realized += pnl
        self.fees_paid += fee
        self.qty, self.entry, self.cost_basis = 0.0, 0.0, 0.0
        self._save()
        return Fill(self.clock(), "SELL", fill_px, qty, fee, pnl, reason)


class AlpacaPaperBroker:
    BASE = "https://paper-api.alpaca.markets"  # paper only, on purpose

    def __init__(self, symbol, key=None, secret=None):
        self.symbol = symbol.upper()
        self.pos_symbol = self.symbol.replace("/", "")  # positions use BTCUSD, orders BTC/USD
        self.is_crypto = "/" in self.symbol
        self.headers = alpaca_headers(key, secret)
        self._entry_cost = None
        acct = self._get("/v2/account")
        if acct.get("status") != "ACTIVE":
            raise FeedError(f"Alpaca paper account status is {acct.get('status')}")
        self.start_cash = float(acct["equity"])
        self.realized, self.fees_paid = 0.0, 0.0

    def _get(self, path):
        return http_json(self.BASE + path, headers=self.headers)

    def position(self):
        try:
            p = self._get(f"/v2/positions/{urllib.parse.quote(self.pos_symbol)}")
        except FeedError as e:
            if "HTTP 404" in str(e):
                return 0.0, 0.0
            raise
        return float(p["qty"]), float(p["avg_entry_price"])

    def equity(self, price=None):
        return float(self._get("/v2/account")["equity"])

    def _wait_fill(self, order_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            o = self._get(f"/v2/orders/{order_id}")
            if o["status"] == "filled":
                return o
            if o["status"] in ("canceled", "expired", "rejected"):
                raise FeedError(f"order {order_id} {o['status']}")
            time.sleep(1)
        raise FeedError(f"order {order_id} not filled after {timeout}s (market closed?)")

    def buy(self, notional, price, reason="signal"):
        qty, _ = self.position()
        if qty > 0:
            return None
        cash = float(self._get("/v2/account")["cash"])
        notional = round(min(notional, cash * 0.98), 2)
        if notional < 1:
            return None
        order = http_json(self.BASE + "/v2/orders", headers=self.headers, method="POST", body={
            "symbol": self.symbol, "notional": str(notional), "side": "buy", "type": "market",
            "time_in_force": "gtc" if self.is_crypto else "day",
        })
        o = self._wait_fill(order["id"])
        px, q = float(o["filled_avg_price"]), float(o["filled_qty"])
        self._entry_cost = px * q
        return Fill(time.time(), "BUY", px, q, 0.0, None, reason)

    def close(self, price, reason="signal"):
        qty, entry = self.position()
        if qty <= 0:
            return None
        order = http_json(self.BASE + f"/v2/positions/{urllib.parse.quote(self.pos_symbol)}",
                          headers=self.headers, method="DELETE")
        o = self._wait_fill(order["id"])
        px, q = float(o["filled_avg_price"]), float(o["filled_qty"])
        pnl = px * q - (self._entry_cost if self._entry_cost else entry * q)
        self.realized += pnl
        self._entry_cost = None
        return Fill(time.time(), "SELL", px, q, 0.0, pnl, reason)


def fill_to_row(f: Fill):
    d = asdict(f)
    d["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.time))
    return d


def make_broker(args, state_path, clock=time.time):
    if args.broker == "paper":
        return PaperBroker(args.cash, args.fee_bps, args.slippage_bps, state_path, clock)
    if args.broker == "alpaca":
        if args.source != "alpaca":
            raise ValueError("--broker alpaca needs --source alpaca so prices and orders match")
        return AlpacaPaperBroker(args.symbol or "AAPL")
    raise ValueError(f"unknown broker {args.broker!r}")