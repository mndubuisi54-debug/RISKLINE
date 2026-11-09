#!/usr/bin/env python3
"""Riskline: a paper-trading bot.

Examples
    python bot.py --source sim --poll 0.2                    # offline simulated market, fast
    python bot.py --source binance --symbol BTCUSDT           # live BTC prices, local paper money
    python bot.py --source binance --mode backtest --bars 3000
    python bot.py --source alpaca --symbol AAPL --broker alpaca   # orders go to your Alpaca PAPER account

Stop with Ctrl+C; you get a summary. Positions on the local paper broker are
saved in ./state/, so restarting carries on where you left off (use --fresh to
start over).
"""
import argparse
import csv
import os
import signal
import sys
import time
from datetime import datetime

from brokers import PaperBroker, fill_to_row, make_broker
from feeds import INTERVALS, FeedError, make_feed
from strategies import make_strategy

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def color(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


green = lambda s: color(s, "32")
red = lambda s: color(s, "31")
dim = lambda s: color(s, "2")
bold = lambda s: color(s, "1")


def signed(v, fmt="{:+,.2f}"):
    s = fmt.format(v)
    return green(s) if v > 0 else red(s) if v < 0 else s


class Bot:
    def __init__(self, strategy, broker, size=0.5, stop_loss=0.02, take_profit=0.04,
                 trade_log=None, max_daily_loss=None):
        self.strategy, self.broker = strategy, broker
        self.size, self.sl, self.tp = size, stop_loss, take_profit
        self.trade_log = trade_log
        self.max_daily_loss = max_daily_loss
        self.fills = []
        self.last_bar_t = None
        self.halted = False
        self.day, self.day_start_equity = None, None
        self.verbose = True

    # ------------------------------------------------------------------ core
    def check_exits(self, low, high, close):
        """Stop loss / take profit. Uses the bar's low/high so intra-bar moves count."""
        qty, entry = self.broker.position()
        if qty <= 0 or not entry:
            return None
        if self.sl and low <= entry * (1 - self.sl):
            return self._record(self.broker.close(min(close, entry * (1 - self.sl)), "stop loss"))
        if self.tp and high >= entry * (1 + self.tp):
            return self._record(self.broker.close(max(close, entry * (1 + self.tp)), "take profit"))
        return None

    def on_bar_close(self, closes, price):
        """Run the strategy once per newly closed bar."""
        sig = self.strategy.signal(closes)
        qty, _ = self.broker.position()
        if sig == 1 and qty <= 0 and not self.halted:
            notional = self.broker.equity(price) * self.size
            return self._record(self.broker.buy(notional, price, "signal")), sig
        if sig == -1 and qty > 0:
            return self._record(self.broker.close(price, "signal")), sig
        return None, sig

    def check_daily_loss(self, price, now):
        if not self.max_daily_loss:
            return
        day = datetime.fromtimestamp(now).date()
        eq = self.broker.equity(price)
        if day != self.day:
            self.day, self.day_start_equity, self.halted = day, eq, False
        if not self.halted and eq <= self.day_start_equity * (1 - self.max_daily_loss):
            self.halted = True
            if self.broker.position()[0] > 0:
                self._record(self.broker.close(price, "daily loss limit"))
            if self.verbose:
                print(red(f"  Daily loss limit hit ({self.max_daily_loss:.1%}). No new trades until tomorrow."))

    def _record(self, fill):
        if not fill:
            return None
        self.fills.append(fill)
        if self.trade_log:
            new = not os.path.exists(self.trade_log)
            os.makedirs(os.path.dirname(self.trade_log) or ".", exist_ok=True)
            with open(self.trade_log, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["time", "side", "price", "qty", "fee", "pnl", "reason"])
                if new:
                    w.writeheader()
                w.writerow(fill_to_row(fill))
        return fill

    # ------------------------------------------------------------------ live
    def run_live(self, feed, poll, max_polls=None):
        warm = self.strategy.warmup + 2
        print(bold(f"\nRiskline · trading {feed.label} · {INTERVAL_NAME(feed.interval_sec)} bars · paper money"))
        print(dim(f"Strategy: {self.strategy.describe()}"))
        print(dim(f"Size {self.size:.0%} of equity · stop {pct_or_off(self.sl)} · "
                  f"take profit {pct_or_off(self.tp)} · checking every {poll:g}s · Ctrl+C to stop\n"))
        stop = {"flag": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
        polls, errors = 0, 0
        while not stop["flag"] and (max_polls is None or polls < max_polls):
            polls += 1
            try:
                bars, price = feed.fetch(warm)
                errors = 0
            except FeedError as e:
                errors += 1
                print(red(f"  {now_str()}  data error ({errors}): {e}"))
                if errors >= 10:
                    print(red("  Too many errors in a row, stopping."))
                    break
                time.sleep(min(60, poll * errors))
                continue
            if not bars or price is None:
                print(dim(f"  {now_str()}  no data yet (market closed?)"))
                time.sleep(poll)
                continue

            self.check_daily_loss(price, time.time())
            # Live stop/take-profit check on the latest traded price.
            self._print_fill(self.check_exits(price, price, price))

            newest = bars[-1]
            if newest.t != self.last_bar_t:
                if self.last_bar_t is not None:  # skip acting on the first bar we see
                    self._print_fill(self.check_exits(newest.l, newest.h, newest.c))
                    fill, sig = self.on_bar_close([b.c for b in bars], price)
                    self._print_fill(fill)
                    self._status(price, sig)
                else:
                    self._status(price, 0, first=True)
                self.last_bar_t = newest.t
            time.sleep(poll)
        self.summary(price if "price" in locals() and price else 0)

    def _status(self, price, sig, first=False):
        qty, entry = self.broker.position()
        eq = self.broker.equity(price)
        start = getattr(self.broker, "start_cash", eq) or eq
        pos = (green(f"LONG {qty:.6g} @ {entry:,.2f} ({(price / entry - 1):+.2%})") if qty > 0 else dim("flat"))
        sig_txt = {1: green("BUY signal"), -1: red("SELL signal"), 0: dim("no signal")}[sig]
        if first:
            sig_txt = dim("warming up")
        print(f"  {now_str()}  price {price:>12,.2f}   {sig_txt:<22}  {pos}   equity {eq:,.2f} "
              f"({signed((eq / start - 1) * 100, '{:+.2f}%')})")

    def _print_fill(self, f):
        if not f:
            return
        tag = green("  ▲ BUY ") if f.side == "BUY" else red("  ▼ SELL")
        pnl = "" if f.pnl is None else f"  P&L {signed(f.pnl)}"
        print(bold(f"{tag} {f.qty:.6g} @ {f.price:,.2f}  ({f.reason}){pnl}"))

    # ------------------------------------------------------------------ summary
    def summary(self, last_price):
        sells = [f for f in self.fills if f.side == "SELL"]
        wins = sum(1 for f in sells if f.pnl > 0)
        realized = sum(f.pnl for f in sells)
        print(bold("\nRiskline session summary"))
        print(f"  fills: {len(self.fills)}   round trips: {len(sells)}   "
              f"win rate: {wins / len(sells):.0%}" if sells else f"  fills: {len(self.fills)}   round trips: 0")
        print(f"  realized P&L this session: {signed(realized)}")
        if last_price:
            print(f"  equity now: {self.broker.equity(last_price):,.2f}")


def backtest(strategy, bars, args):
    """Replay historical bars through the same Bot logic with a fresh paper account."""
    clock = {"t": 0.0}
    broker = PaperBroker(args.cash, args.fee_bps, args.slippage_bps, clock=lambda: clock["t"])
    bot = Bot(strategy, broker, args.size, args.stop, args.take, max_daily_loss=args.max_daily_loss)
    bot.verbose = False
    closes, equity = [], []
    peak, max_dd = args.cash, 0.0
    for b in bars:
        clock["t"] = b.t
        if closes:  # exits use this bar's range, entry was earlier
            bot.check_exits(b.l, b.h, b.c)
        closes.append(b.c)
        bot.check_daily_loss(b.c, b.t)
        if len(closes) >= strategy.warmup:
            bot.on_bar_close(closes, b.c)
        eq = broker.equity(b.c)
        equity.append(eq)
        peak = max(peak, eq)
        max_dd = max(max_dd, 1 - eq / peak)
    if broker.qty > 0:
        bot._record(broker.close(bars[-1].c, "end of test"))

    sells = [f for f in bot.fills if f.side == "SELL"]
    wins = [f for f in sells if f.pnl > 0]
    final = broker.equity(bars[-1].c)
    bh = bars[-1].c / bars[strategy.warmup - 1].c - 1 if len(bars) >= strategy.warmup else 0
    span_h = (bars[-1].t - bars[0].t) / 3600
    first = datetime.fromtimestamp(bars[0].t).strftime("%Y-%m-%d %H:%M")
    last = datetime.fromtimestamp(bars[-1].t).strftime("%Y-%m-%d %H:%M")
    result = {
        "bars": len(bars), "from": first, "to": last, "hours": span_h,
        "return": final / args.cash - 1, "buy_hold": bh, "max_drawdown": max_dd,
        "round_trips": len(sells), "win_rate": len(wins) / len(sells) if sells else None,
        "fees": broker.fees_paid, "final_equity": final,
        "avg_win": sum(f.pnl for f in wins) / len(wins) if wins else 0,
        "avg_loss": (sum(f.pnl for f in sells if f.pnl <= 0) / (len(sells) - len(wins))) if len(sells) > len(wins) else 0,
    }
    return result, bot.fills


def print_backtest(label, strategy, r):
    print(bold(f"\nRiskline backtest · {label}"))
    print(dim(f"  {strategy.describe()}"))
    print(f"  period         {r['from']} → {r['to']}  ({r['bars']} bars, {r['hours']:.0f} h)")
    print(f"  bot return     {signed(r['return'] * 100, '{:+.2f}%')}   (final equity {r['final_equity']:,.2f})")
    print(f"  buy & hold     {signed(r['buy_hold'] * 100, '{:+.2f}%')}")
    dd = r["max_drawdown"] * 100
    print(f"  max drawdown   {red(f'-{dd:.2f}%')}")
    wr = "n/a" if r["win_rate"] is None else f"{r['win_rate']:.0%}"
    print(f"  round trips    {r['round_trips']}   win rate {wr}   "
          f"avg win {r['avg_win']:+,.2f}   avg loss {r['avg_loss']:+,.2f}")
    print(f"  fees paid      {r['fees']:,.2f}\n")


def INTERVAL_NAME(sec):
    return {v: k for k, v in INTERVALS.items()}.get(sec, f"{sec}s")


def pct_or_off(x):
    return f"{x:.1%}" if x else "off"


def now_str():
    return datetime.now().strftime("%H:%M:%S")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Riskline: paper-trading bot (no real money).",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    g = p.add_argument_group("market")
    g.add_argument("--source", choices=["sim", "binance", "coinbase", "alpaca"], default="binance",
                   help="where prices come from")
    g.add_argument("--symbol", help="BTCUSDT (binance), BTC-USD (coinbase), AAPL or BTC/USD (alpaca)")
    g.add_argument("--interval", choices=list(INTERVALS), default="1m", help="bar length")
    g.add_argument("--seed", type=int, default=7, help="random seed for --source sim")

    g = p.add_argument_group("mode")
    g.add_argument("--mode", choices=["live", "backtest"], default="live")
    g.add_argument("--bars", type=int, default=2000, help="history length for backtest")
    g.add_argument("--poll", type=float, default=10.0, help="seconds between price checks (live)")
    g.add_argument("--max-polls", type=int, help="stop after this many checks (for testing)")

    g = p.add_argument_group("strategy")
    g.add_argument("--strategy", choices=["sma", "rsi", "breakout"], default="sma")
    g.add_argument("--fast", type=int, default=12, help="fast SMA length")
    g.add_argument("--slow", type=int, default=40, help="slow SMA length")
    g.add_argument("--rsi-buy", type=float, default=30, help="RSI buy-below level")
    g.add_argument("--rsi-sell", type=float, default=65, help="RSI sell-above level")
    g.add_argument("--breakout-len", type=int, default=30, help="breakout channel length")

    g = p.add_argument_group("risk & account")
    g.add_argument("--broker", choices=["paper", "alpaca"], default="paper",
                   help="paper = simulated fills locally; alpaca = your Alpaca paper account")
    g.add_argument("--cash", type=float, default=10_000, help="starting paper cash")
    g.add_argument("--size", type=float, default=0.5, help="fraction of equity per trade (0-1)")
    g.add_argument("--stop", type=float, default=0.02, help="stop loss fraction, 0 = off")
    g.add_argument("--take", type=float, default=0.04, help="take profit fraction, 0 = off")
    g.add_argument("--max-daily-loss", type=float, default=0.05,
                   help="halt new trades for the day after this drawdown, 0 = off")
    g.add_argument("--fee-bps", type=float, default=10, help="fee per side in basis points")
    g.add_argument("--slippage-bps", type=float, default=5, help="slippage per side in basis points")
    g.add_argument("--fresh", action="store_true", help="ignore saved paper position and start over")
    a = p.parse_args(argv)
    if not 0 < a.size <= 1:
        p.error("--size must be between 0 and 1")
    if a.stop < 0 or a.take < 0 or a.stop >= 1:
        p.error("--stop and --take must be fractions, e.g. 0.02 for 2%")
    return a


def main(argv=None):
    args = parse_args(argv)
    try:
        strategy = make_strategy(args)
        feed = make_feed(args)
    except (ValueError, FeedError) as e:
        sys.exit(f"Error: {e}")

    if args.mode == "backtest":
        try:
            bars = feed.history(args.bars)
        except FeedError as e:
            sys.exit(f"Error fetching history: {e}")
        if len(bars) < strategy.warmup + 10:
            sys.exit(f"Only {len(bars)} bars available; need more than {strategy.warmup + 10}.")
        result, fills = backtest(strategy, bars, args)
        print_backtest(feed.label, strategy, result)
        return result

    sym = (args.symbol or args.source).replace("/", "").replace("-", "")
    state = os.path.join("state", f"paper_{args.source}_{sym}.json")
    if args.fresh and os.path.exists(state):
        os.remove(state)
    try:
        broker = make_broker(args, state)
    except (ValueError, FeedError) as e:
        sys.exit(f"Error: {e}")
    if isinstance(broker, PaperBroker) and broker.qty > 0:
        print(dim(f"Resuming saved paper position: {broker.qty:.6g} @ {broker.entry:,.2f}  (--fresh to reset)"))
    bot = Bot(strategy, broker, args.size, args.stop, args.take,
              trade_log=os.path.join("state", f"trades_{args.source}_{sym}.csv"),
              max_daily_loss=args.max_daily_loss)
    bot.run_live(feed, args.poll, args.max_polls)


if __name__ == "__main__":
    main()