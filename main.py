#!/usr/bin/env python3
"""cbBTC trend-following race bot (agent-fun/bars-replay/v1).

Strategy: dual-EMA trend following on 1-minute closes, full-size
positions, with hard exits. Buys when the fast EMA crosses above the
slow EMA by more than a hysteresis band AND the slow EMA itself is
rising over the last hour (trend filter: no buying into a falling
market); sells on the cross back, a stop-loss from entry, a
take-profit from entry, or a trailing stop from the peak since entry.
A cooldown after every sell blocks whipsaw re-entries. Two insurance
rules guarantee the bot actually trades in the live race even on a
pathological window: if it has still made no trade by bar 300 and
price sits above the slow EMA it buys, and by bar 600 it buys
regardless.

Deterministic: pure function of the bars and the account the harness
sends. No network, no wall clock, no files, no randomness, no
environment reads. Integer money everywhere; floats only for the
EMA/signal math, which is deterministic given the same inputs.
"""

import json
import sys

SYMBOL = "CBBTC"

# --- strategy parameters (tuned on the frozen dev window) ---
FAST = 30            # fast EMA length, bars
SLOW = 60            # slow EMA length, bars
HYST_BPS = 15        # hysteresis band around a crossover, bps
SL_BPS = 100         # stop-loss from entry price, bps
TP_BPS = 200         # take-profit from entry price, bps
TRAIL_BPS = 100      # trailing stop from peak since entry, bps
COOLDOWN_BARS = 30   # bars to wait after a sell before re-entering
MAX_SLIP_BPS = 150   # max slippage vs latest close on every order
WARMUP_BARS = 60     # bars before the first trade is allowed
INSURE1_BAR = 300    # no-trade insurance: buy if above slow EMA
INSURE2_BAR = 600    # no-trade insurance: buy regardless
SLOPE_BARS = 60      # trend filter: slow EMA must be rising over this many bars
SLOPE_MIN_BPS = 5    # minimum slow-EMA rise over SLOPE_BARS to buy, bps

MIN_NOTIONAL = 1000000  # 1 USDC in micro units


class Bot:
    def __init__(self):
        self.price_scale = 5
        self.qty_scale = 8
        self.cash = 0
        self.ema_fast = None
        self.ema_slow = None
        self.bar_n = 0
        self.entry_price = None   # dollars, float
        self.peak_price = None    # dollars, float
        self.cooldown = 0
        self.fills = 0            # filled orders so far
        self.seq_orders = 0
        self.warmup = WARMUP_BARS
        self.slow_hist = []       # slow EMA values, oldest first

    def on_hello(self, msg):
        for sym in msg.get("symbols", []):
            if sym.get("symbol") == SYMBOL:
                self.price_scale = int(sym.get("price_scale", 5))
                self.qty_scale = int(sym.get("qty_scale", 8))
        cap = msg.get("capital", {})
        self.cash = int(cap.get("quote_units", "1000000000"))
        warm = msg.get("warmup_bars")
        if isinstance(warm, int) and warm > self.warmup:
            self.warmup = warm

    def _price(self, raw):
        return int(raw) / (10 ** self.price_scale)

    def on_batch(self, msg):
        bars = msg.get("bars", [])
        bar = None
        for b in bars:
            if b.get("symbol") == SYMBOL:
                bar = b
                break
        if bar is None:
            return []
        account = msg.get("account", {})
        self.cash = int(account.get("cash_quote_units", "0"))
        qty = 0
        for pos in account.get("positions", []):
            if pos.get("symbol") == SYMBOL:
                qty = int(pos.get("qty", "0"))

        # Fills of the orders decided on the previous batch.
        for fill in msg.get("fills", []):
            if fill.get("symbol") != SYMBOL:
                continue
            if fill.get("status") != "filled":
                continue
            self.fills += 1
            if fill.get("side") == "buy":
                self.entry_price = self._price(fill["price"])
                self.peak_price = self.entry_price
            elif fill.get("side") == "sell":
                self.entry_price = None
                self.peak_price = None
                self.cooldown = COOLDOWN_BARS

        close = self._price(bar["c"])
        self.bar_n += 1
        if self.cooldown > 0:
            self.cooldown -= 1

        # EMA update (seeded on the first close).
        alpha_f = 2.0 / (FAST + 1)
        alpha_s = 2.0 / (SLOW + 1)
        if self.ema_fast is None:
            self.ema_fast = close
            self.ema_slow = close
        else:
            self.ema_fast = alpha_f * close + (1.0 - alpha_f) * self.ema_fast
            self.ema_slow = alpha_s * close + (1.0 - alpha_s) * self.ema_slow

        if self.bar_n <= self.warmup:
            return []

        self.slow_hist.append(self.ema_slow)
        if len(self.slow_hist) > SLOPE_BARS + 1:
            self.slow_hist.pop(0)
        trend_up = True
        if len(self.slow_hist) > SLOPE_BARS:
            old_slow = self.slow_hist[0]
            trend_up = self.ema_slow > old_slow * (1.0 + SLOPE_MIN_BPS / 10000.0)

        orders = []
        if qty > 0:
            # Long: update the peak first, then check every exit.
            if self.peak_price is None or close > self.peak_price:
                self.peak_price = close
            sell = False
            if self.ema_fast < self.ema_slow * (1.0 - HYST_BPS / 10000.0):
                sell = True
            elif self.entry_price is not None:
                if close <= self.entry_price * (1.0 - SL_BPS / 10000.0):
                    sell = True
                elif close >= self.entry_price * (1.0 + TP_BPS / 10000.0):
                    sell = True
                elif close <= self.peak_price * (1.0 - TRAIL_BPS / 10000.0):
                    sell = True
            if sell:
                self.seq_orders += 1
                orders.append({
                    "side": "sell",
                    "client_id": "s%d" % self.seq_orders,
                    "symbol": SYMBOL,
                    "qty": str(qty),
                    "max_slippage_bps": MAX_SLIP_BPS,
                })
        else:
            # Flat: enter on a confirmed uptrend, or by insurance.
            buy = False
            if self.cooldown == 0:
                if (self.ema_fast > self.ema_slow * (1.0 + HYST_BPS / 10000.0)
                        and trend_up):
                    buy = True
                elif self.fills == 0 and self.bar_n >= INSURE2_BAR:
                    buy = True
                elif (self.fills == 0 and self.bar_n >= INSURE1_BAR
                        and close > self.ema_slow):
                    buy = True
            if buy and self.cash >= MIN_NOTIONAL:
                self.seq_orders += 1
                orders.append({
                    "side": "buy",
                    "client_id": "b%d" % self.seq_orders,
                    "symbol": SYMBOL,
                    "notional_quote_units": str(self.cash),
                    "max_slippage_bps": MAX_SLIP_BPS,
                })
        return orders


def main():
    bot = Bot()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mtype = msg.get("type")
        if mtype == "hello":
            bot.on_hello(msg)
        elif mtype == "batch":
            orders = bot.on_batch(msg)
            out.write(json.dumps(
                {"type": "reply", "seq": msg["seq"], "orders": orders}
            ) + "\n")
            out.flush()
        elif mtype == "done":
            break


if __name__ == "__main__":
    main()
