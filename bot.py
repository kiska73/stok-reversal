import time
import math
import requests
import pandas as pd
import os
import sys
from datetime import datetime, UTC, timedelta
from pybit.unified_trading import HTTP

# ====================== CONFIG ======================
API_KEY = os.getenv('BYBIT_API_KEY')
API_SECRET = os.getenv('BYBIT_API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

SYMBOL           = "ETHUSDT"
ORDER_VALUE_USDT = 1000
INTERVAL         = "30"

RSI_LEN          = 14
STOCH_LEN        = 14
K_SMOOTH         = 21
D_SMOOTH         = 27
EMA_LEN          = 10

SLACK            = 0.35
DIST_MIN         = 0.5

TP_PERCENT       = 8.4
SL_PERCENT       = 2.2

LIMIT_BUFFER     = 0.0010
TESTNET          = False 

session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

bull_memory = 0
bear_memory = 0

# ====================== LOG & NOTIFICHE ======================
def log(msg):
    ts = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
    line = f"{ts} | {msg}"
    print(line, flush=True)
    print(line, file=sys.stderr, flush=True)
    sys.stdout.flush()
    sys.stderr.flush()

def telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except:
        pass

# ====================== INSTRUMENT ======================
def get_instrument_info():
    try:
        res = session.get_instruments_info(category="linear", symbol=SYMBOL)
        info = res["result"]["list"][0]
        tick = float(info["priceFilter"]["tickSize"])
        qty_step = float(info["lotSizeFilter"]["qtyStep"])
        log(f"✓ Instrument OK → Tick: {tick} | QtyStep: {qty_step}")
        return tick, qty_step, 2, 3
    except Exception as e:
        log(f"✗ Instrument error: {e}")
        return 0.01, 0.001, 2, 3

TICK_SIZE, QTY_STEP, PRICE_PRECISION, QTY_PRECISION = get_instrument_info()

# ====================== INDICATORS ======================
def calculate_rsi(series, period=RSI_LEN):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_stoch_rsi(df):
    rsi = calculate_rsi(df["close"], RSI_LEN)
    lowest = rsi.rolling(STOCH_LEN).min()
    highest = rsi.rolling(STOCH_LEN).max()
    stoch = ((rsi - lowest) / (highest - lowest).clip(lower=1e-8)) * 100
    k = stoch.rolling(K_SMOOTH).mean()
    d = k.rolling(D_SMOOTH).mean()
    return k, d

# ====================== MARKET DATA ======================
def get_market_data():
    try:
        klines = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=300)
        df = pd.DataFrame(klines["result"]["list"], columns=["ts","open","high","low","close","vol","turnover"])
        df = df.astype(float)
        df = df.iloc[::-1].reset_index(drop=True)

        df["k"], df["d"] = calculate_stoch_rsi(df)
        df["ema"] = df["close"].ewm(span=EMA_LEN, adjust=False).mean()

        curr = df.iloc[-2]
        prev = df.iloc[-3]

        bull_cross = (prev["k"] <= prev["d"] + SLACK) and (curr["k"] > curr["d"])
        bear_cross = (prev["k"] >= prev["d"] - SLACK) and (curr["k"] < curr["d"])

        log(f"Market → Price:{curr['close']:.2f} | EMA:{curr['ema']:.2f} | K:{curr['k']:.1f} D:{curr['d']:.1f} | BullCross:{bull_cross} BearCross:{bear_cross}")

        return bull_cross, bear_cross, float(curr["close"]), float(curr["ema"])
    except Exception as e:
        log(f"✗ Market data error: {e}")
        return False, False, 0.0, 0.0

def get_position():
    try:
        res = session.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]
        for p in res:
            size = float(p.get("size", 0))
            if size > 0.0001:
                return p.get("side"), size, float(p.get("avgPrice", 0))
        return None, 0.0, 0.0
    except Exception as e:
        log(f"✗ Position error: {e}")
        return None, 0.0, 0.0

def calculate_qty(price):
    qty = ORDER_VALUE_USDT / price
    qty = math.floor(qty / QTY_STEP) * QTY_STEP
    return max(qty, QTY_STEP)

def cancel_all_orders():
    try: session.cancel_all_orders(category="linear", symbol=SYMBOL)
    except: pass

def open_position(side, price):
    target_qty = calculate_qty(price)
    order_price = round(price * (1 - LIMIT_BUFFER if side == "Buy" else 1 + LIMIT_BUFFER) / TICK_SIZE) * TICK_SIZE
    tp = round(order_price * (1 + TP_PERCENT/100 if side == "Buy" else 1 - TP_PERCENT/100) / TICK_SIZE) * TICK_SIZE
    sl = round(order_price * (1 - SL_PERCENT/100 if side == "Buy" else 1 + SL_PERCENT/100) / TICK_SIZE) * TICK_SIZE

    try:
        cancel_all_orders()
        time.sleep(0.5)
        res = session.place_order(
            category="linear", symbol=SYMBOL, side=side, orderType="Limit",
            qty=f"{target_qty:.{QTY_PRECISION}f}", price=f"{order_price:.{PRICE_PRECISION}f}",
            takeProfit=f"{tp:.{PRICE_PRECISION}f}", stopLoss=f"{sl:.{PRICE_PRECISION}f}",
            positionIdx=0, timeInForce="GTC"
        )
        log(f"🚀 OPEN {side} @ {order_price:.2f} | Qty: {target_qty}")
        telegram(f"🚀 **OPEN {side}**\nPrice: {order_price:.2f}\nQty: {target_qty}\nTP: {TP_PERCENT}% | SL: {SL_PERCENT}%")
        return True
    except Exception as e:
        log(f"✗ Open error: {e}")
        telegram(f"❌ Errore apertura {side}: {str(e)[:100]}")
        return False

def close_position(current_side, qty, price):
    close_side = "Sell" if current_side == "Buy" else "Buy"
    try:
        cancel_all_orders()
        time.sleep(0.5)
        session.place_order(
            category="linear", symbol=SYMBOL, side=close_side, orderType="Market",
            qty=f"{qty:.{QTY_PRECISION}f}", reduceOnly=True
        )
        log(f"🔴 CLOSE {close_side} Market")
        telegram(f"🔴 **CLOSE {close_side}** Market order executed")
        return True
    except Exception as e:
        log(f"✗ Close error: {e}")
        return False

def wait_next_candle():
    while True:
        now = datetime.now(UTC)
        minutes = ((now.minute // 30) + 1) * 30
        next_candle = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)
        wait_seconds = (next_candle - now).total_seconds() + 25

        log(f"⏳ Waiting next 30m candle ({int(wait_seconds)}s)...")

        slept = 0
        while slept < wait_seconds:
            time.sleep(min(300, wait_seconds - slept))  # ogni 5 minuti
            slept += 300
            remaining = int(wait_seconds - slept)
            if remaining > 60:
                log(f"   ⏰ Bot alive - {remaining}s until next check")
        
        return

# ====================== MAIN ======================
if __name__ == "__main__":
    log("=== BOT ETH REVERSAL STARTED ===")
    log(f"Mode: LIVE | Symbol: {SYMBOL} | Value: ${ORDER_VALUE_USDT}")
    telegram("🚀 **BOT STARTED** - LIVE")

    while True:
        try:
            wait_next_candle()
            
            bull_cross, bear_cross, price, ema = get_market_data()
            if price == 0:
                continue

            side, qty, _ = get_position()

            if bull_cross:
                bull_memory = 4
                log("🔵 Bull cross detected")
                telegram("🔵 **Bull cross** rilevato")
            else:
                bull_memory = max(bull_memory - 1, 0)

            if bear_cross:
                bear_memory = 4
                log("🔴 Bear cross detected")
                telegram("🔴 **Bear cross** rilevato")
            else:
                bear_memory = max(bear_memory - 1, 0)

            # Reverse close
            if side == "Buy" and bear_cross:
                log("🔄 REVERSE CLOSE Long → Bear signal")
                close_position(side, qty, price)
            elif side == "Sell" and bull_cross:
                log("🔄 REVERSE CLOSE Short → Bull signal")
                close_position(side, qty, price)

            # Entry
            if side is None:
                if bull_memory > 0 and price > ema * 0.999:
                    log(f"📈 ENTRY LONG at {price:.2f}")
                    open_position("Buy", price)
                    bull_memory = 0
                elif bear_memory > 0 and price < ema * 1.001:
                    log(f"📉 ENTRY SHORT at {price:.2f}")
                    open_position("Sell", price)
                    bear_memory = 0
                elif bull_memory > 0 or bear_memory > 0:
                    log(f"Signal present but skipped (BullMem:{bull_memory} BearMem:{bear_memory})")

            log(f"Status → Price:{price:.2f} | EMA:{ema:.2f} | BullMem:{bull_memory} | BearMem:{bear_memory} | Pos:{side}")

        except Exception as e:
            log(f"✗ Critical error: {e}")
            time.sleep(30)
