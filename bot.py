import time
import math
import requests
import pandas as pd
import pandas_ta as ta
import os
import sys
from datetime import datetime, UTC, timedelta
from pybit.unified_trading import HTTP

# ============================================================
# ====================== CONFIGURAZIONE ======================
# ============================================================

API_KEY = os.getenv('BYBIT_API_KEY')
API_SECRET = os.getenv('BYBIT_API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

SYMBOL           = "ETHUSDT"
ORDER_VALUE_USDT = 1000
INTERVAL         = "30"

# Parametri indicatori
RSI_LEN          = 30
STOCH_LEN        = 30
K_SMOOTH         = 27
D_SMOOTH         = 26
EMA_LEN          = 10

SLACK            = 0.1
DIST_MIN         = 0.1

TP_PERCENT       = 8.4
SL_PERCENT       = 2.2

LIMIT_BUFFER     = 0.0010
MAX_WAIT_FILL    = 120
TESTNET          = False 

# ============================================================
# ====================== INIZIALIZZAZIONE ====================
# ============================================================

session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

# Variabili di Memoria
bull_memory = 0
bear_memory = 0

# ============================================================
# ====================== FUNZIONI UTILITY ====================
# ============================================================

def log(msg):
    ts = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
    line = f"{ts} | {msg}"
    print(line)
    print(line, file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()

def telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log(f"Errore invio Telegram: {e}")

def get_instrument_info():
    try:
        res = session.get_instruments_info(category="linear", symbol=SYMBOL)
        if res.get("retCode") != 0:
            raise Exception(f"Bybit error: {res.get('retMsg')}")
        
        info = res["result"]["list"][0]
        tick = float(info["priceFilter"]["tickSize"])
        qty_step = float(info["lotSizeFilter"]["qtyStep"])
        
        p_prec = len(str(tick).split(".")[1]) if "." in str(tick) else 0
        q_prec = len(str(qty_step).split(".")[1]) if "." in str(qty_step) else 0
        
        log(f"✓ Instrument info OK → Tick: {tick} | QtyStep: {qty_step} | Prec P:{p_prec} Q:{q_prec}")
        return tick, qty_step, p_prec, q_prec
    except Exception as e:
        log(f"✗ ERRORE get_instrument_info: {e}")
        telegram(f"❌ Errore instrument info: {e}")
        return 0.01, 0.001, 2, 3  # fallback

# Inizializzazione parametri mercato
TICK_SIZE, QTY_STEP, PRICE_PRECISION, QTY_PRECISION = get_instrument_info()

# ============================================================
# ====================== FUNZIONI DI TRADING =================
# ============================================================

def get_market_data():
    try:
        klines = session.get_kline(
            category="linear",
            symbol=SYMBOL,
            interval=INTERVAL,
            limit=300
        )
        
        df = pd.DataFrame(klines["result"]["list"],
                          columns=["ts", "open", "high", "low", "close", "vol", "turnover"])
        df = df.astype(float)
        df = df.iloc[::-1].reset_index(drop=True)  # dal più vecchio al più nuovo

        # Indicatori con pandas_ta (più stabile)
        stoch_rsi = ta.stochrsi(df["close"], length=STOCH_LEN, k=K_SMOOTH, d=D_SMOOTH, rsi_length=RSI_LEN)
        df["k"] = stoch_rsi[f'STOCHRSIk_{STOCH_LEN}_{K_SMOOTH}_{D_SMOOTH}']
        df["d"] = stoch_rsi[f'STOCHRSId_{STOCH_LEN}_{K_SMOOTH}_{D_SMOOTH}']
        
        df["ema"] = ta.ema(df["close"], length=EMA_LEN)

        # Ultima candela chiusa e precedente
        curr = df.iloc[-2]
        prev = df.iloc[-3]

        bull_cross = (prev["k"] <= prev["d"] + SLACK) and (curr["k"] > curr["d"]) and \
                     abs(prev["k"] - prev["d"]) >= DIST_MIN
        
        bear_cross = (prev["k"] >= prev["d"] - SLACK) and (curr["k"] < curr["d"]) and \
                     abs(prev["k"] - prev["d"]) >= DIST_MIN

        log(f"Dati mercato → Close: {curr['close']:.2f} | EMA: {curr['ema']:.2f} | K: {curr['k']:.2f} | D: {curr['d']:.2f} | BullCross: {bull_cross} | BearCross: {bear_cross}")

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
                return p["side"], size, float(p.get("avgPrice", 0))
        return None, 0.0, 0.0
    except Exception as e:
        log(f"✗ Position check error: {e}")
        return None, 0.0, 0.0

def calculate_qty(price):
    qty = ORDER_VALUE_USDT / price
    qty = math.floor(qty / QTY_STEP) * QTY_STEP
    return max(qty, QTY_STEP)

def cancel_all_orders():
    try:
        session.cancel_all_orders(category="linear", symbol=SYMBOL)
    except:
        pass

def wait_for_fill(expected_side=None, timeout=MAX_WAIT_FILL):
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(5)
        side, qty, _ = get_position()
        if expected_side is None:
            if qty < 0.0001:
                return True, 0.0
        elif side == expected_side and qty > 0.0001:
            return True, qty
    return False, 0.0

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
            qty=f"{target_qty:.{QTY_PRECISION}f}",
            price=f"{order_price:.{PRICE_PRECISION}f}",
            takeProfit=f"{tp:.{PRICE_PRECISION}f}",
            stopLoss=f"{sl:.{PRICE_PRECISION}f}",
            positionIdx=0, timeInForce="GTC"
        )
        
        log(f"✓ LIMIT OPEN {side} @ {order_price:.2f} | Qty: {target_qty}")
        
        filled, curr_qty = wait_for_fill(side)
        if not filled:
            log("⚠️ Order non filled in time → cancel + market")
            session.cancel_order(category="linear", symbol=SYMBOL, orderId=res["result"]["orderId"])
            remaining = target_qty - curr_qty
            if remaining > QTY_STEP:
                session.place_order(category="linear", symbol=SYMBOL, side=side, orderType="Market",
                                    qty=f"{remaining:.{QTY_PRECISION}f}", positionIdx=0)
        return True
    except Exception as e:
        log(f"✗ Open error: {e}")
        return False

def close_position(current_side, qty, price):
    close_side = "Sell" if current_side == "Buy" else "Buy"
    order_price = round(price * (1 + LIMIT_BUFFER if close_side == "Sell" else 1 - LIMIT_BUFFER) / TICK_SIZE) * TICK_SIZE
    try:
        cancel_all_orders()
        time.sleep(0.5)
        session.place_order(
            category="linear", symbol=SYMBOL, side=close_side, orderType="Limit",
            qty=f"{qty:.{QTY_PRECISION}f}", price=f"{order_price:.{PRICE_PRECISION}f}",
            reduceOnly=True, timeInForce="GTC"
        )
        
        filled, _ = wait_for_fill(None, timeout=90)
        if not filled:
            log("⚠️ Close non filled → Market order")
            cancel_all_orders()
            time.sleep(0.5)
            session.place_order(
                category="linear", symbol=SYMBOL, side=close_side, orderType="Market",
                qty=f"{qty:.{QTY_PRECISION}f}", reduceOnly=True
            )
        return True
    except Exception as e:
        log(f"✗ Close error: {e}")
        return False

def wait_next_candle():
    while True:
        now = datetime.now(UTC)
        # Calcolo prossimo inizio candela 30m
        minutes = ((now.minute // 30) + 1) * 30
        next_candle = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)
        wait_seconds = (next_candle - now).total_seconds() + 12  # buffer di 12 secondi
        
        if wait_seconds > 0:
            log(f"⏳ In attesa della prossima candela 30m ({int(wait_seconds)} secondi)...")
            time.sleep(wait_seconds)
            return

# ============================================================
# ====================== MAIN LOOP ===========================
# ============================================================

if __name__ == "__main__":
    print("=== BOT ETH REVERSAL STARTED ===", file=sys.stderr)
    sys.stderr.flush()
    
    log(f"Bot avviato | SYMBOL: {SYMBOL} | INTERVAL: {INTERVAL} | Value: {ORDER_VALUE_USDT} USDT | TESTNET: {TESTNET}")
    log(f"API_KEY presente: {bool(API_KEY)} | API_SECRET presente: {bool(API_SECRET)}")
    
    telegram(f"🚀 **BOT ETH REVERSAL STARTED** - {'TESTNET' if TESTNET else 'LIVE'}")

    while True:
        try:
            wait_next_candle()
            
            bull_cross, bear_cross, price, ema = get_market_data()
            if price == 0:
                time.sleep(10)
                continue

            time.sleep(1)

            side, qty, avg_p = get_position()
            time.sleep(1)

            # Gestione memoria
            if bull_cross:
                bull_memory = 2   # diamo 2 candele di validità
            else:
                bull_memory = max(bull_memory - 1, 0)

            if bear_cross:
                bear_memory = 2
            else:
                bear_memory = max(bear_memory - 1, 0)

            # Reverse close
            if side == "Buy" and bear_cross:
                log("🔄 REVERSE CLOSE: Segnale Bear → Chiudo Long")
                if close_position(side, qty, price):
                    side = None

            elif side == "Sell" and bull_cross:
                log("🔄 REVERSE CLOSE: Segnale Bull → Chiudo Short")
                if close_position(side, qty, price):
                    side = None

            # Entry
            if side is None:
                if bull_memory > 0 and price > ema:
                    log(f"📈 ENTRY LONG | Prezzo > EMA | Mem: {bull_memory}")
                    if open_position("Buy", price):
                        bull_memory = 0
                
                elif bear_memory > 0 and price < ema:
                    log(f"📉 ENTRY SHORT | Prezzo < EMA | Mem: {bear_memory}")
                    if open_position("Sell", price):
                        bear_memory = 0

            log(f"Check completato | Prezzo: {price:.2f} | EMA: {ema:.2f} | BullMem: {bull_memory} | BearMem: {bear_memory} | Pos: {side}")

        except Exception as e:
            log(f"✗ Errore critico nel loop: {e}")
            telegram(f"⚠️ Errore critico: {e}")
            time.sleep(30)
