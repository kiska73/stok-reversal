import time
import math
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, UTC
from pybit.unified_trading import HTTP

# ============================================================
# CONFIGURAZIONE
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

LIMIT_BUFFER = 0.001        # 0.1% - regola per entrare/uscire meglio
MAX_WAIT_FILL = 120         # secondi per attendere fill (aumentato perché ora anche close è Limit)

session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ============================================================
# LOG & TELEGRAM
# ============================================================

def log(msg):
    ts = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
    print(f"{ts} | {msg}")

def telegram(msg):
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=8
        )
    except Exception as e:
        print(f"Telegram error: {e}")

# ============================================================
# INSTRUMENT INFO
# ============================================================

def get_instrument_info():
    try:
        res = session.get_instruments_info(category="linear", symbol=SYMBOL)["result"]["list"][0]
        tick = float(res["priceFilter"]["tickSize"])
        qty = float(res["lotSizeFilter"]["qtyStep"])
        p_prec = len(str(tick).split(".")[1]) if "." in str(tick) else 0
        q_prec = len(str(qty).split(".")[1]) if "." in str(qty) else 0
        return tick, qty, p_prec, q_prec
    except Exception as e:
        log(f"Instrument info error: {e}")
        return 0.01, 0.001, 2, 3

TICK_SIZE, QTY_STEP, PRICE_PRECISION, QTY_PRECISION = get_instrument_info()

# ============================================================
# DATI MERCATO (SPOT)
# ============================================================

def get_market_data():
    try:
        klines = session.get_kline(category="spot", symbol=SYMBOL, interval=INTERVAL, limit=250)
        df = pd.DataFrame(klines["result"]["list"], columns=["ts","open","high","low","close","vol","turnover"])
        df["close"] = df["close"].astype(float)
        df = df.iloc[::-1].reset_index(drop=True)

        rsi_val = ta.rsi(df["close"], length=RSI_LEN)
        lowest = rsi_val.rolling(STOCH_LEN).min()
        highest = rsi_val.rolling(STOCH_LEN).max()
        range_rsi = (highest - lowest).clip(lower=1e-5)
        stoch_rsi = (rsi_val - lowest) / range_rsi * 100

        df["k"] = ta.sma(stoch_rsi, length=K_SMOOTH)
        df["d"] = ta.sma(df["k"], length=D_SMOOTH)
        df["ema"] = ta.ema(df["close"], length=EMA_LEN)

        curr = df.iloc[-2]
        prev = df.iloc[-3]

        bull_cross = curr["k"] > curr["d"] and prev["k"] <= prev["d"] + SLACK and abs(prev["k"] - prev["d"]) >= DIST_MIN
        bear_cross = curr["k"] < curr["d"] and prev["k"] >= prev["d"] - SLACK and abs(prev["k"] - prev["d"]) >= DIST_MIN

        return bull_cross, bear_cross, float(curr["close"]), float(curr["ema"])
    except Exception as e:
        log(f"Market data error: {e}")
        return False, False, 0.0, 0.0

# ============================================================
# POSIZIONE
# ============================================================

def get_position():
    try:
        res = session.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]
        for p in res:
            size = float(p.get("size", 0))
            if size > 0:
                return p["side"], size, float(p.get("avgPrice", 0))
        return None, 0.0, 0.0
    except Exception as e:
        log(f"Get position error: {e}")
        return None, 0.0, 0.0

# ============================================================
# HELPER PER LIMIT ORDER
# ============================================================

def wait_for_fill(expected_side, timeout=MAX_WAIT_FILL):
    start = time.time()
    while time.time() - start < timeout:
        side, qty, _ = get_position()
        if side == expected_side and qty > 0:
            return True
        time.sleep(10)
    return False

def cancel_order(order_id):
    try:
        session.cancel_order(category="linear", symbol=SYMBOL, orderId=order_id)
        log(f"Order {order_id} cancelled (not filled)")
    except Exception as e:
        log(f"Cancel order error: {e}")

# ============================================================
# CLOSE POSITION - ORA LIMIT (miglior prezzo di uscita)
# ============================================================

def close_position(current_side, qty, price):
    close_side = "Sell" if current_side == "Buy" else "Buy"
    
    # Limit price per migliorare l'uscita
    if close_side == "Sell":   # chiudo long → vendo un po' più alto
        order_price = round(price * (1 + LIMIT_BUFFER) / TICK_SIZE) * TICK_SIZE
    else:                      # chiudo short → compro un po' più basso
        order_price = round(price * (1 - LIMIT_BUFFER) / TICK_SIZE) * TICK_SIZE

    try:
        res = session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=close_side,
            orderType="Limit",
            qty=f"{qty:.{QTY_PRECISION}f}",
            price=f"{order_price:.{PRICE_PRECISION}f}",
            reduceOnly=True,
            timeInForce="GTC"
        )
        order_id = res["result"]["orderId"]
        log(f"LIMIT CLOSE piazzato {current_side} → {close_side} @ {order_price:.2f}")
        telegram(f"📍 **LIMIT CLOSE** {current_side} → {close_side} @ {order_price:.2f}")

        # Polling per verificare se filled
        if wait_for_fill(None, timeout=60):  # aspettiamo che la posizione sparisca
            log(f"✅ CLOSE LIMIT FILLED per {current_side}")
            telegram(f"✅ **CHIUSO {current_side}** con Limit")
            return True
        else:
            # Fallback a Market se non filled
            log(f"⚠️ Close Limit non filled → fallback Market")
            session.place_order(
                category="linear",
                symbol=SYMBOL,
                side=close_side,
                orderType="Market",
                qty=f"{qty:.{QTY_PRECISION}f}",
                reduceOnly=True
            )
            log(f"🔴 CHIUSO {current_side} con Market (fallback)")
            telegram(f"🔴 **CHIUSO {current_side}** (Market fallback)")
            return True

    except Exception as e:
        log(f"Close Limit error: {e}")
        telegram(f"❌ Errore close {current_side}")
        return False

# ============================================================
# OPEN POSITION - LIMIT + POLLING
# ============================================================

def open_position(side, price):
    qty = math.floor((ORDER_VALUE_USDT / price) / QTY_STEP) * QTY_STEP
    if qty <= 0:
        log("Qty too small, skip")
        return False

    tp = price * (1 + TP_PERCENT/100 if side == "Buy" else 1 - TP_PERCENT/100)
    sl = price * (1 - SL_PERCENT/100 if side == "Buy" else 1 + SL_PERCENT/100)
    tp = round(tp / TICK_SIZE) * TICK_SIZE
    sl = round(sl / TICK_SIZE) * TICK_SIZE

    if side == "Buy":
        order_price = round(price * (1 - LIMIT_BUFFER) / TICK_SIZE) * TICK_SIZE
    else:
        order_price = round(price * (1 + LIMIT_BUFFER) / TICK_SIZE) * TICK_SIZE

    try:
        res = session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side,
            orderType="Limit",
            qty=f"{qty:.{QTY_PRECISION}f}",
            price=f"{order_price:.{PRICE_PRECISION}f}",
            takeProfit=f"{tp:.{PRICE_PRECISION}f}",
            stopLoss=f"{sl:.{PRICE_PRECISION}f}",
            positionIdx=0,
            timeInForce="GTC"
        )
        order_id = res["result"]["orderId"]
        log(f"LIMIT OPEN piazzato {side} @ {order_price:.2f} | OrderID: {order_id}")
        telegram(f"📍 **LIMIT OPEN {side}** @ {order_price:.2f}")

        if wait_for_fill(side):
            log(f"✅ LIMIT OPEN FILLED {side} @ \~{price:.2f}")
            telegram(f"✅ **{side} FILLED** @ \~{price:.2f}")
            return True
        else:
            cancel_order(order_id)
            log(f"⚠️ Open Limit NON filled → cancellato")
            return False

    except Exception as e:
        log(f"Open Limit error: {e}")
        telegram(f"❌ Errore apertura {side}")
        return False

# ============================================================
# WAIT NEXT CANDLE
# ============================================================

def wait_next_candle():
    now = datetime.now(UTC)
    wait = (30 - (now.minute % 30)) * 60 - now.second + 8
    if wait < 0:
        wait = 8
    time.sleep(wait)

# ============================================================
# MAIN LOOP
# ============================================================

if __name__ == "__main__":
    telegram("🤖 **BOT ETH STARTED** - Limit Entry + Limit Close su Reverse")
    log("Bot avviato - Tutte le operazioni con Limit + polling")

    while True:
        try:
            wait_next_candle()
            bull, bear, price, ema = get_market_data()
            side, qty, entry = get_position()

            # REVERSE: Close Limit + Open Limit
            if side == "Buy" and bear and price < ema:
                close_position(side, qty, price)
                open_position("Sell", price)
            elif side == "Sell" and bull and price > ema:
                close_position(side, qty, price)
                open_position("Buy", price)

            # NUOVA POSIZIONE
            elif side is None:
                if bull and price > ema:
                    open_position("Buy", price)
                elif bear and price < ema:
                    open_position("Sell", price)
            else:
                log(f"Monitor | price={price:.2f} ema={ema:.2f} pos={side} qty={qty:.4f}")

        except Exception as e:
            log(f"Loop error: {e}")
            telegram(f"⚠️ Errore generale: {str(e)[:100]}")
            time.sleep(20)
