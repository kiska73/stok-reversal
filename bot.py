import time
import math
import requests
import pandas as pd
import os
import sys
from datetime import datetime, UTC, timedelta
from pybit.unified_trading import HTTP

# =====================================================
# CONFIG
# =====================================================

# Supporta sia vecchi nomi che nuovi nomi ENV
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "ETHUSDT"
ORDER_VALUE_USDT = 1000
INTERVAL = "30"

RSI_LEN = 14
STOCH_LEN = 14
K_SMOOTH = 21
D_SMOOTH = 27
EMA_LEN = 10

SLACK = 0.35

TP_PERCENT = 8.5
SL_PERCENT = 2.0

LIMIT_BUFFER = 0.0010
TESTNET = False

bull_memory = 0
bear_memory = 0

# =====================================================
# LOG
# =====================================================
def log(msg):
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} | {msg}")
    sys.stdout.flush()

# =====================================================
# TELEGRAM
# =====================================================
def telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown"
            },
            timeout=10
        )
    except:
        pass

# =====================================================
# CHECK ENV
# =====================================================
if not API_KEY or not API_SECRET:
    log("❌ Missing API Keys")
    sys.exit()

log("✅ API Keys loaded")

# =====================================================
# BYBIT SESSION
# =====================================================
session = HTTP(
    testnet=TESTNET,
    api_key=API_KEY,
    api_secret=API_SECRET
)

# =====================================================
# INSTRUMENT
# =====================================================
def get_instrument_info():
    try:
        res = session.get_instruments_info(
            category="linear",
            symbol=SYMBOL
        )

        info = res["result"]["list"][0]

        tick = float(info["priceFilter"]["tickSize"])
        qty_step = float(info["lotSizeFilter"]["qtyStep"])

        price_dec = len(str(tick).split(".")[1].rstrip("0")) if "." in str(tick) else 0
        qty_dec = len(str(qty_step).split(".")[1].rstrip("0")) if "." in str(qty_step) else 0

        log(f"✅ Instrument OK | Tick:{tick} QtyStep:{qty_step}")

        return tick, qty_step, price_dec, qty_dec

    except Exception as e:
        log(f"❌ Instrument error: {e}")
        return 0.01, 0.001, 2, 3

TICK_SIZE, QTY_STEP, PRICE_PRECISION, QTY_PRECISION = get_instrument_info()

# =====================================================
# INDICATORS
# =====================================================
def calculate_rsi(series, period=14):
    delta = series.diff()

    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0).rolling(period).mean()

    rs = gain / loss.replace(0, 1e-9)

    return 100 - (100 / (1 + rs))

def calculate_stoch_rsi(df):
    rsi = calculate_rsi(df["close"], RSI_LEN)

    lowest = rsi.rolling(STOCH_LEN).min()
    highest = rsi.rolling(STOCH_LEN).max()

    stoch = ((rsi - lowest) / (highest - lowest).replace(0, 1e-9)) * 100

    k = stoch.rolling(K_SMOOTH).mean()
    d = k.rolling(D_SMOOTH).mean()

    return k, d

# =====================================================
# MARKET DATA
# =====================================================
def get_market_data():
    try:
        res = session.get_kline(
            category="linear",
            symbol=SYMBOL,
            interval=INTERVAL,
            limit=300
        )

        df = pd.DataFrame(
            res["result"]["list"],
            columns=["ts","open","high","low","close","vol","turnover"]
        )

        df = df.astype(float)
        df = df.iloc[::-1].reset_index(drop=True)

        df["k"], df["d"] = calculate_stoch_rsi(df)
        df["ema"] = df["close"].ewm(span=EMA_LEN, adjust=False).mean()

        curr = df.iloc[-2]
        prev = df.iloc[-3]

        bull_cross = (
            prev["k"] <= prev["d"] + SLACK and curr["k"] > curr["d"]
        )

        bear_cross = (
            prev["k"] >= prev["d"] - SLACK and curr["k"] < curr["d"]
        )

        log(
            f"Market | Price:{curr['close']:.2f} "
            f"EMA:{curr['ema']:.2f} "
            f"K:{curr['k']:.1f} D:{curr['d']:.1f} "
            f"Bull:{bull_cross} Bear:{bear_cross}"
        )

        return bull_cross, bear_cross, float(curr["close"]), float(curr["ema"])

    except Exception as e:
        log(f"❌ Market error: {e}")
        return False, False, 0, 0

# =====================================================
# POSITION
# =====================================================
def get_position():
    try:
        res = session.get_positions(
            category="linear",
            symbol=SYMBOL
        )

        for p in res["result"]["list"]:
            size = float(p.get("size", 0))
            if size > 0:
                return p["side"], size, float(p["avgPrice"])

        return None, 0, 0

    except Exception as e:
        log(f"❌ Position error: {e}")
        return None, 0, 0

# =====================================================
# HELPERS
# =====================================================
def round_tick(price):
    return round(price / TICK_SIZE) * TICK_SIZE

def calculate_qty(price):
    qty = ORDER_VALUE_USDT / price
    qty = math.floor(qty / QTY_STEP) * QTY_STEP
    return max(qty, QTY_STEP)

def cancel_all():
    try:
        session.cancel_all_orders(
            category="linear",
            symbol=SYMBOL
        )
    except:
        pass

# =====================================================
# OPEN
# =====================================================
def open_position(side, price):
    qty = calculate_qty(price)

    if side == "Buy":
        entry = round_tick(price * (1 - LIMIT_BUFFER))
        tp = round_tick(entry * (1 + TP_PERCENT / 100))
        sl = round_tick(entry * (1 - SL_PERCENT / 100))
    else:
        entry = round_tick(price * (1 + LIMIT_BUFFER))
        tp = round_tick(entry * (1 - TP_PERCENT / 100))
        sl = round_tick(entry * (1 + SL_PERCENT / 100))

    try:
        cancel_all()
        time.sleep(0.5)

        session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side,
            orderType="Limit",
            qty=f"{qty:.{QTY_PRECISION}f}",
            price=f"{entry:.{PRICE_PRECISION}f}",
            takeProfit=f"{tp:.{PRICE_PRECISION}f}",
            stopLoss=f"{sl:.{PRICE_PRECISION}f}",
            timeInForce="GTC",
            positionIdx=0
        )

        log(f"🚀 OPEN {side} @ {entry}")
        telegram(f"🚀 OPEN {side} @ {entry}")

        return True

    except Exception as e:
        log(f"❌ Open error: {e}")
        return False

# =====================================================
# CLOSE
# =====================================================
def close_position(side, qty):
    close_side = "Sell" if side == "Buy" else "Buy"

    try:
        cancel_all()
        time.sleep(0.5)

        session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=close_side,
            orderType="Market",
            qty=f"{qty:.{QTY_PRECISION}f}",
            reduceOnly=True
        )

        log("🔴 Position closed")
        telegram("🔴 Position closed")

    except Exception as e:
        log(f"❌ Close error: {e}")

# =====================================================
# TIMER
# =====================================================
def wait_next_candle():
    while True:
        now = datetime.now(UTC)

        next_min = ((now.minute // 30) + 1) * 30

        if next_min == 60:
            next_candle = now.replace(
                minute=0,
                second=0,
                microsecond=0
            ) + timedelta(hours=1)
        else:
            next_candle = now.replace(
                minute=next_min,
                second=0,
                microsecond=0
            )

        wait = (next_candle - now).total_seconds() + 25

        log(f"⏳ Waiting next candle ({int(wait)} sec)")

        while wait > 0:
            chunk = min(300, wait)
            time.sleep(chunk)
            wait -= chunk

            if wait > 60:
                log(f"⏰ Alive | {int(wait)} sec remaining")

        return

# =====================================================
# MAIN
# =====================================================
if __name__ == "__main__":

    log("===== BOT STARTED =====")
    telegram("🚀 BOT STARTED")

    while True:
        try:
            wait_next_candle()

            bull_cross, bear_cross, price, ema = get_market_data()

            if price == 0:
                continue

            side, qty, avg = get_position()

            if bull_cross:
                bull_memory = 4
                log("🔵 Bull cross")
            else:
                bull_memory = max(0, bull_memory - 1)

            if bear_cross:
                bear_memory = 4
                log("🔴 Bear cross")
            else:
                bear_memory = max(0, bear_memory - 1)

            if side == "Buy" and bear_cross:
                close_position(side, qty)
                side = None

            elif side == "Sell" and bull_cross:
                close_position(side, qty)
                side = None

            if side is None:

                if bull_memory > 0 and price > ema * 0.999:
                    if open_position("Buy", price):
                        bull_memory = 0

                elif bear_memory > 0 and price < ema * 1.001:
                    if open_position("Sell", price):
                        bear_memory = 0

            log(
                f"Status | Price:{price:.2f} "
                f"EMA:{ema:.2f} "
                f"BullMem:{bull_memory} "
                f"BearMem:{bear_memory} "
                f"Pos:{side}"
            )

        except Exception as e:
            log(f"❌ Critical error: {e}")
            time.sleep(30)
