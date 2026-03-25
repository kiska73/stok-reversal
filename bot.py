import time
import requests
import pandas as pd
import math
from datetime import datetime, timedelta, UTC
from pybit.unified_trading import HTTP
import traceback
import random

# ============================================================
# CONFIGURAZIONE
# ============================================================

API_KEY          = "26tNwg57oCDvlNidYT"
API_SECRET       = "WQ84S2dhZ9FVoXkJ7WqWCt6F7HSXR4fsrqhH"
TELEGRAM_TOKEN   = "6916198243:AAFTF66uLYSeqviL5YnfGtbUkSjTwPzah6s"
TELEGRAM_CHAT_ID = "820279313"

SYMBOL = "ETHUSDT"
ORDER_VALUE_USDT = 1000
INTERVAL = "30"

RSI_LENGTH   = 30
STOCH_LENGTH = 30
SMOOTH_K     = 27
SMOOTH_D     = 26

SLACK    = 1.0
DIST_MIN = 0.2
EMA_LENGTH = 14
USE_EMA  = True

TP_PERCENT = 8.4
SL_PERCENT = 2.4

# ============================================================
# CONNESSIONE
# ============================================================

session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET, recv_window=30000)

def log(msg):
    ts = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
    print(f"{ts} | {msg}")

def telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        log(f"Errore Telegram: {e}")

# ====================== AVVIO ======================
log("=== BOT AVVIATO ===")

info = session.get_instruments_info(category="linear", symbol=SYMBOL)["result"]["list"][0]

MIN_QTY   = float(info["lotSizeFilter"]["minOrderQty"])
QTY_STEP  = float(info["lotSizeFilter"]["qtyStep"])
TICK_SIZE = float(info["priceFilter"]["tickSize"])

# 🔥 FIX DECIMALI
QTY_DECIMALS  = len(str(QTY_STEP).split('.')[1]) if '.' in str(QTY_STEP) else 0
PRICE_DECIMALS = len(str(TICK_SIZE).split('.')[1]) if '.' in str(TICK_SIZE) else 0

log(f"{SYMBOL} → minQty={MIN_QTY} | step={QTY_STEP} | tick={TICK_SIZE}")
log(f"DECIMALI → qty={QTY_DECIMALS} | price={PRICE_DECIMALS}")

# ============================================================
# QTY
# ============================================================

def calc_qty(price):
    if price <= 0:
        return 0.0

    raw_qty = ORDER_VALUE_USDT / price

    qty = math.floor(raw_qty / QTY_STEP) * QTY_STEP
    qty = max(qty, MIN_QTY)

    notional = qty * price
    if notional < 5:
        log(f"❌ Notional troppo basso: {notional}")
        return 0.0

    final_qty = round(qty, QTY_DECIMALS)

    log(f"QTY → raw={raw_qty:.6f} | final={final_qty}")
    return final_qty

# ============================================================
# RETRY
# ============================================================

def bybit_request(func, *args, max_retries=10, **kwargs):
    for attempt in range(max_retries):
        try:
            result = func(*args, **kwargs)

            if result is None:
                raise Exception("Response None")

            if result.get("retCode") != 0:
                raise Exception(f"{result.get('retCode')} - {result.get('retMsg')}")

            return result

        except Exception as e:
            wait = (2 ** attempt) + random.uniform(0.5, 1.5)
            log(f"Retry {attempt+1} → {e} | wait {wait:.1f}s")
            time.sleep(wait)

    return None

# ============================================================
# ORDINE
# ============================================================

def open_position_market(side):
    try:
        ticker = bybit_request(session.get_tickers, category="linear", symbol=SYMBOL)
        price = float(ticker["result"]["list"][0]["lastPrice"])

        qty = calc_qty(price)
        if qty <= 0:
            return False

        # 🔥 FIX FORMAT
        qty_str = f"{qty:.{QTY_DECIMALS}f}"

        tp_raw = price * (1 + TP_PERCENT/100) if side == "Buy" else price * (1 - TP_PERCENT/100)
        sl_raw = price * (1 - SL_PERCENT/100) if side == "Buy" else price * (1 + SL_PERCENT/100)

        tp = round(tp_raw / TICK_SIZE) * TICK_SIZE
        sl = round(sl_raw / TICK_SIZE) * TICK_SIZE

        tp_str = f"{tp:.{PRICE_DECIMALS}f}"
        sl_str = f"{sl:.{PRICE_DECIMALS}f}"

        log(f"INVIO ORDINE → {side} {qty_str} @ {price}")

        order = bybit_request(
            session.place_order,
            category="linear",
            symbol=SYMBOL,
            side=side,
            orderType="Market",
            qty=qty_str,
            takeProfit=tp_str,
            stopLoss=sl_str,
            tpslMode="Full",
            tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice",
            positionIdx=0
        )

        if not order:
            log("❌ ORDER FAIL (None)")
            return False

        log(f"✅ ORDER OK {order['result']['orderId']}")
        telegram(f"{side} {qty_str} @ {price}")
        return True

    except Exception as e:
        log(f"ERRORE ORDINE: {e}")
        return False

# ============================================================
# DATA
# ============================================================

def get_df():
    klines = bybit_request(session.get_kline, category="linear", symbol=SYMBOL, interval=INTERVAL, limit=200)
    if not klines:
        return None

    df = pd.DataFrame(klines["result"]["list"],
                      columns=["ts","open","high","low","close","vol","turnover"])

    df = df.astype({"close": float})
    return df[::-1].reset_index(drop=True)

# ============================================================
# RSI
# ============================================================

def rsi(series, length):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/length).mean()
    avg_loss = loss.ewm(alpha=1/length).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ============================================================
# SEGNALE
# ============================================================

def get_signal(df):
    r = rsi(df["close"], RSI_LENGTH)

    low_r  = r.rolling(STOCH_LENGTH).min()
    high_r = r.rolling(STOCH_LENGTH).max()

    stoch = 100 * (r - low_r) / (high_r - low_r + 1e-10)

    k = stoch.rolling(SMOOTH_K).mean()
    d = k.rolling(SMOOTH_D).mean()

    ema = df["close"].ewm(span=EMA_LENGTH).mean()

    k_now, k_prev = k.iloc[-1], k.iloc[-2]
    d_now, d_prev = d.iloc[-1], d.iloc[-2]

    price = df["close"].iloc[-1]
    ema_price = ema.iloc[-1]

    bull = (k_now > d_now) and (k_prev <= d_prev)
    bear = (k_now < d_now) and (k_prev >= d_prev)

    if USE_EMA:
        bull = bull and price > ema_price
        bear = bear and price < ema_price

    return bull, bear

# ============================================================
# LOOP
# ============================================================

def main_loop():
    df = get_df()
    if df is None:
        return

    long_sig, short_sig = get_signal(df)

    if long_sig:
        open_position_market("Buy")

    elif short_sig:
        open_position_market("Sell")

# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    while True:
        try:
            main_loop()
            time.sleep(60)
        except Exception as e:
            log(f"CRASH: {e}")
            time.sleep(30)
