"""Trading strategies.

Each strategy looks at a list of closed-bar closing prices (oldest first)
and returns a signal for the most recent bar:
    +1  -> buy (enter long)
    -1  -> sell (exit long)
     0  -> do nothing
"""


def sma(values, n, end=None):
    """Simple moving average of the n values ending just before index `end`."""
    end = len(values) if end is None else end
    if n <= 0 or end < n:
        return None
    return sum(values[end - n:end]) / n


def rsi(closes, n=14):
    """Wilder's RSI of the last bar. Returns None until there is enough data."""
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(ch, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-ch, 0.0)) / n
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


class SmaCross:
    name = "sma"

    def __init__(self, fast=12, slow=40):
        if not 2 <= fast < slow:
            raise ValueError("need 2 <= fast < slow")
        self.fast, self.slow = fast, slow

    @property
    def warmup(self):
        return self.slow + 1

    def describe(self):
        return (f"Buy when the {self.fast}-bar average crosses above the "
                f"{self.slow}-bar average; sell when it crosses back below.")

    def signal(self, closes):
        n = len(closes)
        f, s = sma(closes, self.fast), sma(closes, self.slow)
        pf, ps = sma(closes, self.fast, n - 1), sma(closes, self.slow, n - 1)
        if None in (f, s, pf, ps):
            return 0
        if pf <= ps and f > s:
            return 1
        if pf >= ps and f < s:
            return -1
        return 0


class RsiReversion:
    name = "rsi"

    def __init__(self, buy_below=30, sell_above=65, period=14):
        if not 0 < buy_below < sell_above < 100:
            raise ValueError("need 0 < buy_below < sell_above < 100")
        self.lo, self.hi, self.period = buy_below, sell_above, period

    @property
    def warmup(self):
        # RSI needs a run-in period to settle down.
        return self.period * 5

    def describe(self):
        return (f"Buy when RSI({self.period}) drops below {self.lo} (oversold); "
                f"sell when it rises above {self.hi} (overbought).")

    def signal(self, closes):
        r = rsi(closes, self.period)
        if r is None:
            return 0
        if r < self.lo:
            return 1
        if r > self.hi:
            return -1
        return 0


class Breakout:
    name = "breakout"

    def __init__(self, length=30):
        if length < 5:
            raise ValueError("breakout length must be >= 5")
        self.n = length
        self.exit_n = max(3, round(length / 2))

    @property
    def warmup(self):
        return self.n + 1

    def describe(self):
        return (f"Buy when price closes above the previous {self.n}-bar high; "
                f"sell when it closes below the previous {self.exit_n}-bar low.")

    def signal(self, closes):
        if len(closes) < self.n + 1:
            return 0
        p = closes[-1]
        if p > max(closes[-self.n - 1:-1]):
            return 1
        if p < min(closes[-self.exit_n - 1:-1]):
            return -1
        return 0


def make_strategy(args):
    if args.strategy == "sma":
        return SmaCross(args.fast, args.slow)
    if args.strategy == "rsi":
        return RsiReversion(args.rsi_buy, args.rsi_sell)
    if args.strategy == "breakout":
        return Breakout(args.breakout_len)
    raise ValueError(f"unknown strategy {args.strategy!r}")