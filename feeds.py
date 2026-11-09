"""Price feeds.

Every feed has:
    fetch(limit) -> (bars, live_price)
        bars:       up to `limit` CLOSED bars, oldest first
        live_price: the most recent traded price (may be inside the open bar)
    interval_sec   bar length in seconds
    label          human-readable market name

Only standard-library modules are used, so nothing needs installing.
"""
import json
import math
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

USER_AGENT = "riskline/1.0 (paper-trading bot)"

INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


@dataclass
class Bar:
    t: float  # bar open time, unix seconds
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


class FeedError(RuntimeError):
    pass


def http_json(url, headers=None, method="GET", body=None, timeout=15, retries=3):
    """Small JSON-over-HTTP helper with retries for transient failures."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            # 4xx (except rate limit) won't fix itself on retry.
            if e.code != 429 and 400 <= e.code < 500:
                raise FeedError(f"HTTP {e.code} from {url.split('?')[0]}: {detail}") from None
            last = FeedError(f"HTTP {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = FeedError(f"network error contacting {url.split('?')[0]}: {e}")
        time.sleep(1.5 * (attempt + 1))
    raise last


# --------------------------------------------------------------------------- #
# Simulated market (works offline, used for testing)
# --------------------------------------------------------------------------- #
class SimFeed:
    """Random-walk market with shifting trends. Each fetch() advances one bar."""

    def __init__(self, symbol="SIM/USD", interval="1m", seed=7, start_price=100.0, prefill=500):
        self.label = f"{symbol} (simulated)"
        self.interval_sec = INTERVALS[interval]
        self.rng = random.Random(seed)
        self.price = start_price
        self.drift, self.vol, self.regime_left = 0.0, 0.0018, 120
        self.t = time.time() - prefill * self.interval_sec
        self.bars = []
        for _ in range(prefill):
            self._new_bar()

    def _step(self):
        r = self.rng
        self.regime_left -= 1
        if self.regime_left <= 0:
            x = r.random()
            self.drift = 0.00045 if x < 0.35 else (-0.0004 if x < 0.7 else 0.0)
            self.regime_left = 60 + int(r.random() * 220)
        self.vol = min(0.006, max(0.0008, self.vol * (0.97 + 0.06 * r.random())))
        self.price *= math.exp(self.drift - self.vol ** 2 / 2 + self.vol * r.gauss(0, 1))
        return self.price

    def _new_bar(self):
        o = self.price
        ticks = [self._step() for _ in range(4)]
        self.bars.append(Bar(self.t, o, max(o, *ticks), min(o, *ticks), ticks[-1]))
        self.t += self.interval_sec

    def history(self, limit):
        while len(self.bars) < limit:
            self._new_bar()
        return self.bars[-limit:]

    def fetch(self, limit):
        self._new_bar()
        return self.bars[-limit:], self.price


# --------------------------------------------------------------------------- #
# Binance public market data (no API key needed)
# --------------------------------------------------------------------------- #
class BinanceFeed:
    BASE = "https://api.binance.com"

    def __init__(self, symbol="BTCUSDT", interval="1m", base=None):
        self.symbol = symbol.replace("/", "").replace("-", "").upper()
        self.interval = interval
        self.interval_sec = INTERVALS[interval]
        self.base = base or os.environ.get("BINANCE_BASE_URL", self.BASE)
        self.label = f"{self.symbol} on Binance"

    def _klines(self, limit, end_ms=None):
        q = {"symbol": self.symbol, "interval": self.interval, "limit": min(limit, 1000)}
        if end_ms:
            q["endTime"] = end_ms
        return http_json(f"{self.base}/api/v3/klines?{urllib.parse.urlencode(q)}")

    @staticmethod
    def _parse(rows, now_ms):
        bars, live = [], None
        for k in rows:
            open_ms, o, h, l, c, v, close_ms = k[0], *map(float, k[1:6]), k[6]
            live = c
            if close_ms < now_ms:  # only closed candles count as bars
                bars.append(Bar(open_ms / 1000, o, h, l, c, v))
        return bars, live

    def fetch(self, limit):
        rows = self._klines(limit + 1)
        return self._parse(rows, int(time.time() * 1000))

    def history(self, limit):
        """Up to `limit` closed bars, paging backwards past Binance's 1000-row cap."""
        now_ms = int(time.time() * 1000)
        out, end = [], None
        while len(out) < limit:
            rows = self._klines(min(1000, limit - len(out) + 1), end)
            if not rows:
                break
            bars, _ = self._parse(rows, now_ms)
            out = bars + out
            end = rows[0][0] - 1
            if len(rows) < 2:
                break
        return out[-limit:]


# --------------------------------------------------------------------------- #
# Coinbase Exchange public market data (no API key needed)
# --------------------------------------------------------------------------- #
class CoinbaseFeed:
    BASE = "https://api.exchange.coinbase.com"
    GRANULARITIES = {60, 300, 900, 3600, 21600, 86400}

    def __init__(self, symbol="BTC-USD", interval="1m"):
        self.product = symbol.replace("/", "-").upper()
        self.interval_sec = INTERVALS[interval]
        if self.interval_sec not in self.GRANULARITIES:
            raise ValueError("Coinbase supports 1m, 5m, 15m, 1h, 1d intervals")
        self.label = f"{self.product} on Coinbase"

    def _candles(self, start=None, end=None):
        q = {"granularity": self.interval_sec}
        if start is not None:
            q["start"] = datetime.fromtimestamp(start, timezone.utc).isoformat()
            q["end"] = datetime.fromtimestamp(end, timezone.utc).isoformat()
        rows = http_json(f"{self.BASE}/products/{self.product}/candles?{urllib.parse.urlencode(q)}")
        # rows: [time, low, high, open, close, volume], newest first
        return [Bar(r[0], float(r[3]), float(r[2]), float(r[1]), float(r[4]), float(r[5])) for r in rows]

    def _closed(self, bars):
        now = time.time()
        bars = sorted(bars, key=lambda b: b.t)
        live = bars[-1].c if bars else None
        return [b for b in bars if b.t + self.interval_sec <= now], live

    def fetch(self, limit):
        bars, live = self._closed(self._candles())
        return bars[-limit:], live

    def history(self, limit):
        """Pages backwards 300 candles at a time."""
        out, end = {}, time.time()
        while len(out) < limit:
            start = end - 300 * self.interval_sec
            got = self._candles(start, end)
            if not got:
                break
            for b in got:
                out[b.t] = b
            end = start
            time.sleep(0.35)  # stay under the public rate limit
        bars, _ = self._closed(out.values())
        return bars[-limit:]


# --------------------------------------------------------------------------- #
# Alpaca market data (stocks or crypto; free paper account keys)
# --------------------------------------------------------------------------- #
class AlpacaFeed:
    DATA = "https://data.alpaca.markets"
    TIMEFRAMES = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour", "4h": "4Hour", "1d": "1Day"}

    def __init__(self, symbol="AAPL", interval="1m", key=None, secret=None):
        self.symbol = symbol.upper()
        self.is_crypto = "/" in self.symbol
        self.interval_sec = INTERVALS[interval]
        self.timeframe = self.TIMEFRAMES[interval]
        self.headers = alpaca_headers(key, secret)
        kind = "crypto" if self.is_crypto else "stock"
        self.label = f"{self.symbol} ({kind}) via Alpaca"

    def _bars(self, limit):
        lookback = max(limit * self.interval_sec * 4, 10 * 86400)  # covers nights/weekends
        start = (datetime.now(timezone.utc) - timedelta(seconds=lookback)).strftime("%Y-%m-%dT%H:%M:%SZ")
        q = {"symbols": self.symbol, "timeframe": self.timeframe, "start": start,
             "limit": min(limit, 10000), "sort": "desc"}
        if self.is_crypto:
            url = f"{self.DATA}/v1beta3/crypto/us/bars"
        else:
            url = f"{self.DATA}/v2/stocks/bars"
            q["feed"] = os.environ.get("ALPACA_STOCK_FEED", "iex")  # iex is free
        data = http_json(f"{url}?{urllib.parse.urlencode(q)}", headers=self.headers)
        rows = (data.get("bars") or {}).get(self.symbol, [])
        bars = [Bar(_rfc3339(r["t"]), r["o"], r["h"], r["l"], r["c"], r.get("v", 0)) for r in rows]
        return sorted(bars, key=lambda b: b.t)

    def fetch(self, limit):
        bars = self._bars(limit + 1)
        live = bars[-1].c if bars else None
        now = time.time()
        return [b for b in bars if b.t + self.interval_sec <= now][-limit:], live

    def history(self, limit):
        return self.fetch(limit)[0]


def alpaca_headers(key=None, secret=None):
    key = key or os.environ.get("ALPACA_API_KEY")
    secret = secret or os.environ.get("ALPACA_API_SECRET")
    if not key or not secret:
        raise FeedError("Set ALPACA_API_KEY and ALPACA_API_SECRET (paper-account keys) first.")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _rfc3339(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def make_feed(args):
    if args.source == "sim":
        return SimFeed(args.symbol or "SIM/USD", args.interval, seed=args.seed)
    if args.source == "binance":
        return BinanceFeed(args.symbol or "BTCUSDT", args.interval)
    if args.source == "coinbase":
        return CoinbaseFeed(args.symbol or "BTC-USD", args.interval)
    if args.source == "alpaca":
        return AlpacaFeed(args.symbol or "AAPL", args.interval)
    raise ValueError(f"unknown source {args.source!r}")