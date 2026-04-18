import time
import math
import requests
import pandas as pd
import os
import sys
from datetime import datetime, timezone, timedelta
from pybit.unified_trading import HTTP

# =====================================================
# ENV (RENDER)
# =====================================================
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not API_KEY or not API_SECRET:
    print("❌ Missing API keys")
    sys.exit()

# =====================================================
# CONFIG
# =====================================================
SYMBOL = "ETHUSDT"
INTERVAL = "30"
ORDER_VALUE_USDT = 1000

RSI_LEN = 14
STOCH_LEN = 14
K_SMOOTH = 5
D_SMOOTH = 5
EMA_LEN = 10

SLACK = 1.0
TP_PERCENT = 8.5
SL_PERCENT = 2.0
LIMIT_BUFFER = 0.001

TESTNET = False

bull_memory = 0
bear_memory = 0

session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

# =====================================================
# LOG
# =====================================================
def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} | {msg}", flush=True)

# =====================================================
# TELEGRAM
# =====================================================
def telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10
        )
    except:
        pass

# =====================================================
# INDICATORS
# =====================================================
def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / (loss.replace(0, 1e-9))
    return 100 - (100 / (1 + rs))

def stoch_rsi(df):
    r = rsi(df["close"], RSI_LEN)
    low = r.rolling(STOCH_LEN).min()
    high = r.rolling(STOCH_LEN).max()

    stoch = ((r - low) / (high - low + 1e-9)) * 100

    k = stoch.rolling(K_SMOOTH).mean()
    d = k.rolling(D_SMOOTH).mean()
    return k, d

# =====================================================
# DATA
# =====================================================
def get_data():
    res = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=200)

    df = pd.DataFrame(res["result"]["list"])
    df = df.iloc[::-1].reset_index(drop=True)
    df = df.astype(float)

    df["k"], df["d"] = stoch_rsi(df)
    df["ema"] = df["close"].ewm(span=EMA_LEN).mean()

    c = df.iloc[-2]
    p = df.iloc[-3]

    bull = p["k"] <= p["d"] + SLACK and c["k"] > c["d"]
    bear = p["k"] >= p["d"] - SLACK and c["k"] < c["d"]

    return bull, bear, float(c["close"]), float(c["ema"])

# =====================================================
# POSITION
# =====================================================
def get_position():
    res = session.get_positions(category="linear", symbol=SYMBOL)

    for p in res["result"]["list"]:
        if float(p["size"]) > 0:
            return p["side"], float(p["size"]), float(p["avgPrice"])

    return None, 0, 0

# =====================================================
# HELPERS
# =====================================================
def qty(price):
    return ORDER_VALUE_USDT / price

# =====================================================
# ORDER
# =====================================================
def open_trade(side, price):
    try:
        q = qty(price)

        entry = price * (1 - LIMIT_BUFFER if side == "Buy" else 1 + LIMIT_BUFFER)
        tp = entry * (1 + TP_PERCENT/100 if side == "Buy" else 1 - TP_PERCENT/100)
        sl = entry * (1 - SL_PERCENT/100 if side == "Buy" else 1 + SL_PERCENT/100)

        session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side,
            orderType="Market",
            qty=str(round(q, 3)),
            takeProfit=str(round(tp, 2)),
            stopLoss=str(round(sl, 2))
        )

        log(f"🚀 OPEN {side} @ {price}")
        telegram(f"OPEN {side} @ {price}")

        return True

    except Exception as e:
        log(f"❌ OPEN ERROR {e}")
        return False

# =====================================================
# START CHECK (ENTRY IMMEDIATO)
# =====================================================
def startup():
    bull, bear, price, ema = get_data()
    side, size, _ = get_position()

    log(f"⚡ START CHECK Price {price} EMA {ema}")

    if side is None:
        if bull:
            open_trade("Buy", price)
        elif bear:
            open_trade("Sell", price)

# =====================================================
# WAIT CANDLE
# =====================================================
def wait():
    now = datetime.now(timezone.utc)
    nxt = (now.minute // 30 + 1) * 30

    if nxt == 60:
        nxt_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        nxt_time = now.replace(minute=nxt, second=0, microsecond=0)

    time.sleep(max(1, (nxt_time - now).total_seconds()))

# =====================================================
# MAIN
# =====================================================
log("BOT STARTED")
telegram("BOT STARTED")

startup()

while True:
    try:
        wait()

        bull, bear, price, ema = get_data()
        side, size, _ = get_position()

        if bull:
            bull_memory = 3
        else:
            bull_memory = max(0, bull_memory - 1)

        if bear:
            bear_memory = 3
        else:
            bear_memory = max(0, bear_memory - 1)

        if side is None:

            if bull_memory > 0:
                open_trade("Buy", price)
                bull_memory = 0

            elif bear_memory > 0:
                open_trade("Sell", price)
                bear_memory = 0

        log(f"{price} EMA {ema} POS {side}")

    except Exception as e:
        log(f"ERROR {e}")
        time.sleep(10)
