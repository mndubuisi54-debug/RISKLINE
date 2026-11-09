"""Offline tests. Run:  python -m unittest -v test_bot.py

Exchange responses are faked with samples in each API's documented format,
so these run without a network connection or API keys.
"""
import os
import tempfile
import time
import unittest
from unittest import mock

import brokers
import feeds
from bot import Bot, backtest, parse_args
from brokers import AlpacaPaperBroker, PaperBroker
from feeds import AlpacaFeed, BinanceFeed, CoinbaseFeed, SimFeed
from strategies import Breakout, RsiReversion, SmaCross, rsi, sma


class StrategyTests(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), 3.5)
        self.assertEqual(sma([1, 2, 3, 4], 2, end=3), 2.5)
        self.assertIsNone(sma([1], 2))

    def test_rsi_extremes(self):
        self.assertEqual(rsi(list(range(1, 40))), 100.0)
        self.assertLess(rsi(list(range(40, 1, -1))), 1)

    def test_sma_cross_up_and_down(self):
        s = SmaCross(2, 4)
        self.assertEqual(s.signal([10, 10, 10, 10, 9, 12]), 1)    # fast jumps above slow
        self.assertEqual(s.signal([10, 10, 10, 10, 11, 8]), -1)   # fast drops below slow
        self.assertEqual(s.signal([10, 10, 10, 10, 10, 10]), 0)

    def test_breakout(self):
        b = Breakout(5)
        self.assertEqual(b.signal([10, 11, 10, 11, 10, 11, 12]), 1)
        self.assertEqual(b.signal([10, 11, 10, 11, 10, 11, 9]), -1)

    def test_rsi_strategy(self):
        r = RsiReversion(30, 70)
        self.assertEqual(r.signal(list(range(100, 20, -1))), 1)
        self.assertEqual(r.signal(list(range(20, 100))), -1)


class PaperBrokerTests(unittest.TestCase):
    def test_round_trip_with_fees(self):
        b = PaperBroker(cash=1000, fee_bps=10, slippage_bps=0)
        f = b.buy(500, 100)
        self.assertAlmostEqual(f.qty, 5)
        self.assertAlmostEqual(b.cash, 1000 - 500 - 0.5)
        f = b.close(110)
        self.assertAlmostEqual(f.pnl, 550 - 0.55 - 500.5)
        self.assertEqual(b.qty, 0)
        self.assertAlmostEqual(b.equity(110), 1000 + f.pnl)

    def test_cannot_overspend_or_double_buy(self):
        b = PaperBroker(cash=100, fee_bps=0, slippage_bps=0)
        b.buy(10_000, 10)
        self.assertAlmostEqual(b.cash, 0)
        self.assertIsNone(b.buy(10, 10))

    def test_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            PaperBroker(cash=1000, state_path=path).buy(500, 50)
            b2 = PaperBroker(cash=1000, state_path=path)
            self.assertGreater(b2.qty, 0)
            self.assertLess(b2.cash, 1000)


class BotTests(unittest.TestCase):
    def test_stop_loss_and_take_profit(self):
        b = PaperBroker(cash=1000, fee_bps=0, slippage_bps=0)
        bot = Bot(SmaCross(2, 4), b, size=1.0, stop_loss=0.02, take_profit=0.05)
        b.buy(1000, 100)
        self.assertIsNone(bot.check_exits(99, 101, 100))
        f = bot.check_exits(97, 100, 98)
        self.assertEqual(f.reason, "stop loss")
        self.assertAlmostEqual(f.price, 98)  # filled at the stop level
        b.buy(900, 100)
        self.assertEqual(bot.check_exits(100, 106, 105).reason, "take profit")

    def test_daily_loss_halts_new_entries(self):
        b = PaperBroker(cash=1000, fee_bps=0, slippage_bps=0)
        bot = Bot(SmaCross(2, 4), b, size=1.0, stop_loss=0, take_profit=0, max_daily_loss=0.05)
        now = time.time()
        bot.check_daily_loss(100, now)
        b.buy(1000, 100)
        bot.check_daily_loss(90, now)
        self.assertTrue(bot.halted)
        self.assertEqual(b.qty, 0)
        fill, sig = bot.on_bar_close([10, 10, 10, 10, 9, 12], 12)
        self.assertEqual(sig, 1)
        self.assertIsNone(fill)  # signal ignored while halted

    def test_backtest_on_sim(self):
        args = parse_args(["--source", "sim", "--mode", "backtest"])
        bars = SimFeed(seed=3).history(1500)
        self.assertEqual(len(bars), 1500)
        for strat in (SmaCross(12, 40), RsiReversion(), Breakout(30)):
            r, fills = backtest(strat, bars, args)
            self.assertGreater(r["round_trips"], 0)
            self.assertGreater(r["final_equity"], 0)
            sides = [f.side for f in fills]
            self.assertTrue(all(a != b for a, b in zip(sides, sides[1:])), "buys and sells must alternate")


class FeedParsingTests(unittest.TestCase):
    def test_binance_drops_open_candle(self):
        now_ms = int(time.time() * 1000)
        rows = [
            [now_ms - 120_000, "100", "101", "99", "100.5", "3", now_ms - 60_001, "0", 1, "0", "0", "0"],
            [now_ms - 60_000, "100.5", "102", "100", "101.5", "4", now_ms - 1, "0", 1, "0", "0", "0"],
            [now_ms, "101.5", "103", "101", "102.7", "1", now_ms + 59_999, "0", 1, "0", "0", "0"],
        ]
        with mock.patch.object(feeds, "http_json", return_value=rows) as m:
            bars, live = BinanceFeed("BTC/USDT").fetch(2)
        self.assertIn("symbol=BTCUSDT", m.call_args[0][0])
        self.assertEqual([b.c for b in bars], [100.5, 101.5])
        self.assertEqual(live, 102.7)

    def test_coinbase_sorts_and_maps_columns(self):
        now = int(time.time()) // 60 * 60
        rows = [  # newest first: [time, low, high, open, close, volume]
            [now, 99, 104, 100, 103, 1],
            [now - 60, 98, 102, 99, 100, 2],
            [now - 120, 97, 100, 98, 99, 3],
        ]
        with mock.patch.object(feeds, "http_json", return_value=rows):
            bars, live = CoinbaseFeed("btc/usd").fetch(10)
        self.assertEqual([b.c for b in bars], [99, 100])
        self.assertEqual((bars[0].o, bars[0].h, bars[0].l), (98, 100, 97))
        self.assertEqual(live, 103)

    def test_alpaca_stock_and_crypto(self):
        env = {"ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s"}
        t0 = time.time() // 60 * 60 - 180
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        bars = [{"t": iso(t0 + 60 * i), "o": 1, "h": 2, "l": 0.5, "c": 1 + i, "v": 10} for i in range(4)][::-1]
        for sym, path in (("AAPL", "/v2/stocks/bars"), ("BTC/USD", "/v1beta3/crypto/us/bars")):
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(feeds, "http_json", return_value={"bars": {sym: bars}}) as m:
                got, live = AlpacaFeed(sym).fetch(10)
            url = m.call_args[0][0]
            self.assertIn(path, url)
            self.assertIn("sort=desc", url)
            self.assertEqual(m.call_args[1]["headers"]["APCA-API-KEY-ID"], "k")
            self.assertEqual([b.c for b in got], [1, 2, 3])  # oldest first, open bar dropped
            self.assertEqual(live, 4)

    def test_missing_alpaca_keys_message(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(feeds.FeedError) as e:
                AlpacaFeed("AAPL")
        self.assertIn("ALPACA_API_KEY", str(e.exception))


class AlpacaBrokerTests(unittest.TestCase):
    def test_only_paper_endpoint_and_order_flow(self):
        calls = []
        state = {"pos": None}

        def fake(url, headers=None, method="GET", body=None, **kw):
            calls.append((method, url, body))
            self.assertTrue(url.startswith("https://paper-api.alpaca.markets/"))
            if url.endswith("/v2/account"):
                return {"status": "ACTIVE", "equity": "100000", "cash": "100000"}
            if "/v2/positions/" in url and method == "GET":
                if state["pos"] is None:
                    raise feeds.FeedError("HTTP 404 from x: position does not exist")
                return state["pos"]
            if url.endswith("/v2/orders") and method == "POST":
                return {"id": "o1"}
            if "/v2/positions/" in url and method == "DELETE":
                return {"id": "o2"}
            if url.endswith("/v2/orders/o1"):
                state["pos"] = {"qty": "0.5", "avg_entry_price": "60000"}
                return {"status": "filled", "filled_avg_price": "60000", "filled_qty": "0.5"}
            if url.endswith("/v2/orders/o2"):
                state["pos"] = None
                return {"status": "filled", "filled_avg_price": "61000", "filled_qty": "0.5"}
            raise AssertionError(url)

        with mock.patch.dict(os.environ, {"ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s"}), \
             mock.patch.object(brokers, "http_json", side_effect=fake):
            b = AlpacaPaperBroker("BTC/USD")
            self.assertEqual(b.position(), (0.0, 0.0))
            f = b.buy(30000, 60000)
            order = [c for c in calls if c[0] == "POST"][0][2]
            self.assertEqual(order["time_in_force"], "gtc")
            self.assertEqual(order["symbol"], "BTC/USD")
            self.assertEqual(f.qty, 0.5)
            self.assertEqual(b.position(), (0.5, 60000.0))
            f = b.close(61000)
            self.assertAlmostEqual(f.pnl, 500)
            self.assertTrue(any(m == "DELETE" and u.endswith("/v2/positions/BTCUSD") for m, u, _ in calls))


if __name__ == "__main__":
    unittest.main()