import time
import math
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, UTC
from pybit.unified_trading import HTTP

# ============================================================
# ====================== CONFIGURAZIONE ======================
# ============================================================

API_KEY          = "26tNwg57oCDvlNidYT"
API_SECRET       = "WQ84S2dhZ9FVoXkJ7WqWCt6F7HSXR4fsrqhH"
TELEGRAM_TOKEN   = "6916198243:AAFTF66uLYSeqviL5YnfGtbUkSjTwPzah6s"
TELEGRAM_CHAT_ID = "820279313"

SYMBOL           = "ETHUSDT"
ORDER_VALUE_USDT = 1000
INTERVAL         = "30"

RSI_LEN          = 30
STOCH_LEN        = 30
K_SMOOTH         = 27
D_SMOOTH         = 26

SLACK            = 1.0
DIST_MIN         = 0.2
EMA_LEN          = 14

TP_PERCENT       = 8.4
SL_PERCENT       = 2.4

LIMIT_BUFFER     = 0.0010      # 0.10% - buffer per migliorare prezzo Limit
MAX_WAIT_FILL    = 120         # secondi di attesa fill

# Cambia in True per testare senza rischiare soldi reali
TESTNET          = False

session = HTTP(
    testnet=TESTNET,
    api_key=API_KEY,
    api_secret=API_SECRET
)

# ============================================================
# ====================== LOG & TELEGRAM ======================
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
            timeout=10
        )
    except:
        pass

# ============================================================
# ====================== INSTRUMENT INFO =====================
# ============================================================

def get_instrument_info():
    try:
        res = session.get_instruments_info(category="linear", symbol=SYMBOL)["result"]["list"][0]
        tick = float(res["priceFilter"]["tickSize"])
        qty_step = float(res["lotSizeFilter"]["qtyStep"])
        p_prec = len(str(tick).split(".")[1]) if "." in str(tick) else 0
        q_prec = len(str(qty_step).split(".")[1]) if "." in str(qty_step) else 0
        return tick, qty_step, p_prec, q_prec
    except Exception as e:
        log(f"Instrument info error: {e}")
        return 0.01, 0.001, 2, 3

TICK_SIZE, QTY_STEP, PRICE_PRECISION, QTY_PRECISION = get_instrument_info()

# ============================================================
# ====================== MARKET DATA (LINEAR) ================
# ============================================================

def get_market_data():
    try:
        klines = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=250)
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
# ====================== POSIZIONE ===========================
# ============================================================

def get_position():
    try:
        res = session.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]
        for p in res:
            size = float(p.get("size", 0))
            if size > 0.0001:
                return p["side"], size, float(p.get("avgPrice", 0))
        return None, 0.0, 0.0
    except Exception as e:
        log(f"Get position error: {e}")
        return None, 0.0, 0.0

# ============================================================
# ====================== HELPER ==============================
# ============================================================

def calculate_qty(price):
    """Ritorna qty esatta per \~1000 USDT"""
    qty = ORDER_VALUE_USDT / price
    qty = math.floor(qty / QTY_STEP) * QTY_STEP
    return max(qty, QTY_STEP)

def cancel_all_orders():
    try:
        session.cancel_all_orders(category="linear", symbol=SYMBOL)
    except:
        pass

def wait_for_fill(expected_side=None, timeout=MAX_WAIT_FILL):
    """Polling veloce e affidabile"""
    start = time.time()
    while time.time() - start < timeout:
        side, qty, _ = get_position()
        if expected_side is None:               # CLOSE
            if qty < 0.0001:
                return True, 0.0
        elif side == expected_side and qty > 0.0001:
            return True, qty
        time.sleep(8)
    return False, 0.0

# ============================================================
# ====================== OPEN POSITION =======================
# ============================================================

def open_position(side, price):
    target_qty = calculate_qty(price)
    if target_qty <= 0:
        return False

    # Prezzo Limit con buffer
    if side == "Buy":
        order_price = round(price * (1 - LIMIT_BUFFER) / TICK_SIZE) * TICK_SIZE
    else:
        order_price = round(price * (1 + LIMIT_BUFFER) / TICK_SIZE) * TICK_SIZE

    # TP/SL calcolati sul prezzo del Limit (coerente con backtest)
    tp = round(order_price * (1 + TP_PERCENT/100 if side == "Buy" else 1 - TP_PERCENT/100) / TICK_SIZE) * TICK_SIZE
    sl = round(order_price * (1 - SL_PERCENT/100 if side == "Buy" else 1 + SL_PERCENT/100) / TICK_SIZE) * TICK_SIZE

    try:
        cancel_all_orders()                     # pulizia ordini vecchi
        res = session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side,
            orderType="Limit",
            qty=f"{target_qty:.{QTY_PRECISION}f}",
            price=f"{order_price:.{PRICE_PRECISION}f}",
            takeProfit=f"{tp:.{PRICE_PRECISION}f}",
            stopLoss=f"{sl:.{PRICE_PRECISION}f}",
            positionIdx=0,
            timeInForce="GTC"
        )
        order_id = res["result"]["orderId"]

        log(f"LIMIT OPEN {side} @ {order_price:.2f} | Target: {target_qty:.4f} ETH")
        telegram(f"📍 **LIMIT OPEN {side}** @ {order_price:.2f}")

        # Aspettiamo il fill
        filled, current_qty = wait_for_fill(side, timeout=MAX_WAIT_FILL)

        if filled and current_qty >= target_qty * 0.95:
            log(f"✅ OPEN {side} FILLED qty {current_qty:.4f}")
            telegram(f"✅ **{side} FILLED** qty {current_qty:.4f}")
            return True

        # Partial fill o timeout → completiamo con Market
        log(f"Partial/Timeout → completiamo con Market | Filled: {current_qty:.4f} / {target_qty:.4f}")
        remaining = target_qty - current_qty
        if remaining > QTY_STEP * 0.5:
            session.place_order(
                category="linear",
                symbol=SYMBOL,
                side=side,
                orderType="Market",
                qty=f"{remaining:.{QTY_PRECISION}f}",
                positionIdx=0
            )
            telegram(f"⚠️ **{side}** completato a MARKET (partial fill)")

        # Verifica finale
        _, final_qty, _ = get_position()
        log(f"Posizione finale {side} qty: {final_qty:.4f} ETH")
        return True

    except Exception as e:
        log(f"Open error: {e}")
        telegram(f"❌ Open error {side}")
        return False

# ============================================================
# ====================== CLOSE POSITION ======================
# ============================================================

def close_position(current_side, qty, price):
    close_side = "Sell" if current_side == "Buy" else "Buy"

    if close_side == "Sell":
        order_price = round(price * (1 + LIMIT_BUFFER) / TICK_SIZE) * TICK_SIZE
    else:
        order_price = round(price * (1 - LIMIT_BUFFER) / TICK_SIZE) * TICK_SIZE

    try:
        cancel_all_orders()
        session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=close_side,
            orderType="Limit",
            qty=f"{qty:.{QTY_PRECISION}f}",
            price=f"{order_price:.{PRICE_PRECISION}f}",
            reduceOnly=True,
            timeInForce="GTC"
        )
        log(f"LIMIT CLOSE {current_side} → {close_side} @ {order_price:.2f}")

        filled, _ = wait_for_fill(None, timeout=90)
        if filled:
            log(f"✅ CLOSE {current_side} completato con Limit")
            telegram(f"✅ **CHIUSO {current_side}** (Limit)")
            return True
        else:
            log("Close Limit timeout → Market fallback")
            cancel_all_orders()
            session.place_order(
                category="linear",
                symbol=SYMBOL,
                side=close_side,
                orderType="Market",
                qty=f"{qty:.{QTY_PRECISION}f}",
                reduceOnly=True
            )
            telegram(f"🔴 **CHIUSO {current_side}** (Market fallback)")
            return True

    except Exception as e:
        log(f"Close error: {e}")
        telegram(f"❌ Close error {current_side}")
        return False

# ============================================================
# ====================== WAIT NEXT CANDLE ====================
# ============================================================

def wait_next_candle():
    now = datetime.now(UTC)
    minutes_to_wait = 30 - (now.minute % 30)
    seconds_to_wait = (minutes_to_wait * 60) - now.second + 5
    if seconds_to_wait < 5:
        seconds_to_wait = 1805  # prossima candela completa
    time.sleep(seconds_to_wait)

# ============================================================
# ====================== MAIN LOOP ===========================
# ============================================================

if __name__ == "__main__":
    mode = "TESTNET" if TESTNET else "LIVE"
    telegram(f"🤖 **BOT ETH STARTED** - {mode} | Limit + Partial Fill Safe")
    log(f"Bot avviato in {mode} - Versione stabile")

    while True:
        try:
            wait_next_candle()
            bull, bear, price, ema = get_market_data()
            if price == 0:
                continue

            side, qty, _ = get_position()

            # === REVERSE ===
            if side == "Buy" and bear and price < ema:
                if close_position(side, qty, price):
                    open_position("Sell", price)
            elif side == "Sell" and bull and price > ema:
                if close_position(side, qty, price):
                    open_position("Buy", price)

            # === NUOVA POSIZIONE ===
            elif side is None:
                if bull and price > ema:
                    open_position("Buy", price)
                elif bear and price < ema:
                    open_position("Sell", price)

            # === MONITORAGGIO ===
            else:
                log(f"Monitor | Price: {price:.2f} | EMA: {ema:.2f} | Pos: {side} | Qty: {qty:.4f}")

        except Exception as e:
            log(f"Loop Error: {e}")
            telegram(f"⚠️ Loop error: {str(e)[:100]}")
            time.sleep(30)
