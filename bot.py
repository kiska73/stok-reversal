import time
import math
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, UTC
from pybit.unified_trading import HTTP

# ============================================================
# CONFIG
# ============================================================

API_KEY      = "26tNwg57oCDvlNidYT"
API_SECRET   = "WQ84S2dhZ9FVoXkJ7WqWCt6F7HSXR4fsrqhH"
TELEGRAM_TOKEN   = "6916198243:AAFTF66uLYSeqviL5YnfGtbUkSjTwPzah6s"
TELEGRAM_CHAT_ID = "820279313"


SYMBOL = "ETHUSDT"

ORDER_VALUE_USDT = 1000
INTERVAL = "30"

RSI_LEN = 30
STOCH_LEN = 30
K_SMOOTH = 27
D_SMOOTH = 26

SLACK = 1.0
DIST_MIN = 0.2

EMA_LEN = 14

TP_PERCENT = 8.4
SL_PERCENT = 2.4

session = HTTP(
    testnet=False,
    api_key=API_KEY,
    api_secret=API_SECRET
)

# ============================================================
# LOG
# ============================================================

def log(msg):
    ts = datetime.now(UTC).strftime('%H:%M:%S')
    print(f"{ts} | {msg}")

# ============================================================
# TELEGRAM
# ============================================================

def telegram(msg):
    if TELEGRAM_TOKEN == "":
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown"
            },
            timeout=5
        )
    except:
        pass

# ============================================================
# INSTRUMENT INFO
# ============================================================

def get_instrument_info():

    try:

        res = session.get_instruments_info(
            category="linear",
            symbol=SYMBOL
        )["result"]["list"][0]

        tick = float(res["priceFilter"]["tickSize"])
        qty = float(res["lotSizeFilter"]["qtyStep"])

        p_prec = len(str(tick).split(".")[1]) if "." in str(tick) else 0
        q_prec = len(str(qty).split(".")[1]) if "." in str(qty) else 0

        return tick, qty, p_prec, q_prec

    except:

        return 0.01, 0.01, 2, 3


TICK_SIZE, QTY_STEP, PRICE_PRECISION, QTY_PRECISION = get_instrument_info()

# ============================================================
# MARKET DATA (SPOT)
# ============================================================

def get_market_data():

    try:

        klines = session.get_kline(
            category="spot",
            symbol=SYMBOL,
            interval=INTERVAL,
            limit=250
        )

        df = pd.DataFrame(
            klines["result"]["list"],
            columns=["ts","open","high","low","close","vol","turnover"]
        )

        df["close"] = df["close"].astype(float)

        df = df.iloc[::-1].reset_index(drop=True)

        # RSI
        rsi_val = ta.rsi(df["close"], length=RSI_LEN)

        lowest_rsi = rsi_val.rolling(STOCH_LEN).min()
        highest_rsi = rsi_val.rolling(STOCH_LEN).max()

        range_rsi = (highest_rsi - lowest_rsi).clip(lower=0.00001)

        stoch_rsi = (rsi_val - lowest_rsi) / range_rsi * 100

        df["k"] = ta.sma(stoch_rsi, length=K_SMOOTH)
        df["d"] = ta.sma(df["k"], length=D_SMOOTH)

        df["ema"] = ta.ema(df["close"], length=EMA_LEN)

        # SOLO CANDELE CHIUSE (replica Pine)
        curr = df.iloc[-2]
        prev = df.iloc[-3]

        bull_cross = (
            curr["k"] > curr["d"] and
            prev["k"] <= prev["d"] + SLACK and
            abs(prev["k"] - prev["d"]) >= DIST_MIN
        )

        bear_cross = (
            curr["k"] < curr["d"] and
            prev["k"] >= prev["d"] - SLACK and
            abs(prev["k"] - prev["d"]) >= DIST_MIN
        )

        return bull_cross, bear_cross, curr["close"], curr["ema"]

    except Exception as e:

        log(f"Data error {e}")

        return False, False, 0, 0


# ============================================================
# POSITION
# ============================================================

def get_position():

    try:

        res = session.get_positions(
            category="linear",
            symbol=SYMBOL
        )["result"]["list"]

        for p in res:

            size = float(p["size"])

            if size > 0:

                return p["side"], size, float(p["avgPrice"])

        return None, 0, 0

    except:

        return None, 0, 0


# ============================================================
# CLOSE
# ============================================================

def close_position(side, qty):

    try:

        session.place_order(
            category="linear",
            symbol=SYMBOL,
            side="Sell" if side == "Buy" else "Buy",
            orderType="Market",
            qty=f"{qty:.{QTY_PRECISION}f}",
            reduceOnly=True
        )

        log("Position closed")

    except Exception as e:

        log(f"Close error {e}")


# ============================================================
# OPEN
# ============================================================

def open_position(side, price):

    qty = math.floor((ORDER_VALUE_USDT / price) / QTY_STEP) * QTY_STEP

    tp = price * (1 + TP_PERCENT/100 if side == "Buy" else 1 - TP_PERCENT/100)
    sl = price * (1 - SL_PERCENT/100 if side == "Buy" else 1 + SL_PERCENT/100)

    tp = round(tp / TICK_SIZE) * TICK_SIZE
    sl = round(sl / TICK_SIZE) * TICK_SIZE

    try:

        session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side,
            orderType="Market",
            qty=f"{qty:.{QTY_PRECISION}f}",
            takeProfit=f"{tp:.{PRICE_PRECISION}f}",
            stopLoss=f"{sl:.{PRICE_PRECISION}f}",
            positionIdx=0
        )

        log(f"OPEN {side} @ {price}")

    except Exception as e:

        log(f"Open error {e}")


# ============================================================
# WAIT FOR CANDLE CLOSE
# ============================================================

def wait_next_candle():

    now = datetime.now(UTC)

    wait = (30 - (now.minute % 30)) * 60 - now.second + 30

    if wait < 0:
        wait = 5

    time.sleep(wait)


# ============================================================
# MAIN LOOP
# ============================================================

if __name__ == "__main__":

    telegram("BOT ETH STARTED")

    while True:

        try:

            wait_next_candle()

            bull, bear, price, ema = get_market_data()

            side, qty, entry = get_position()

            # REVERSE CLOSE
            if side == "Buy" and bear:
                close_position(side, qty)
                side = None

            elif side == "Sell" and bull:
                close_position(side, qty)
                side = None

            # ENTRY
            if bull and price > ema and side != "Buy":

                open_position("Buy", price)

            elif bear and price < ema and side != "Sell":

                open_position("Sell", price)

            else:

                log(f"Check price={price} ema={ema} pos={side}")

        except Exception as e:

            log(f"Loop error {e}")

            time.sleep(20)
