import time
import math
import requests
import pandas as pd
import os
import sys
from datetime import datetime, UTC, timedelta
from pybit.unified_trading import HTTP

# ====================== CONFIGURAZIONE CHIAVI ======================
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

print("=== DIAGNOSI AVVIO BOT ===")
if not API_KEY or not API_SECRET:
    print("❌ ERRORE CRITICO: Variabili d'ambiente mancanti!")
    print(f"API_KEY: {'OK' if API_KEY else 'MANCANTE'}")
    print(f"API_SECRET: {'OK' if API_SECRET else 'MANCANTE'}")
    sys.exit(1)
else:
    print(f"✅ API Key caricate (Inizio: {API_KEY[:5]}...)")

# ====================== PARAMETRI TRADING ======================
SYMBOL           = "ETHUSDT"
ORDER_VALUE_USDT = 1000
INTERVAL         = "30"
RSI_LEN, STOCH_LEN, K_SMOOTH, D_SMOOTH, EMA_LEN = 14, 14, 21, 27, 10
SLACK, TP_PERCENT, SL_PERCENT = 0.35, 8.5, 2
LIMIT_BUFFER = 0.0010
TESTNET = False 

session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)
bull_memory = 0
bear_memory = 0

def log(msg):
    ts = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
    print(f"{ts} | {msg}", flush=True)

def telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except:
        pass

def get_instrument_info():
    try:
        res = session.get_instruments_info(category="linear", symbol=SYMBOL)
        info = res["result"]["list"][0]
        tick = float(info["priceFilter"]["tickSize"])
        qty_step = float(info["lotSizeFilter"]["qtyStep"])
        p_prec = len(str(tick).split('.')[1]) if '.' in str(tick) else 0
        q_prec = len(str(qty_step).split('.')[1]) if '.' in str(qty_step) else 0
        return tick, qty_step, p_prec, q_prec
    except:
        return 0.01, 0.001, 2, 3

TICK_SIZE, QTY_STEP, PRICE_PRECISION, QTY_PRECISION = get_instrument_info()

def calculate_stoch_rsi(df):
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_LEN).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_LEN).mean()
    rs = gain / loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    low_r, high_r = rsi.rolling(STOCH_LEN).min(), rsi.rolling(STOCH_LEN).max()
    stoch = ((rsi - low_r) / (high_r - low_r).clip(lower=1e-8)) * 100
    k = stoch.rolling(K_SMOOTH).mean()
    d = k.rolling(D_SMOOTH).mean()
    return k, d

def get_market_data():
    try:
        resp = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=100)
        df = pd.DataFrame(resp["result"]["list"], columns=["ts","open","high","low","close","vol","turn"])
        df = df.astype(float).iloc[::-1].reset_index(drop=True)
        df["k"], df["d"] = calculate_stoch_rsi(df)
        df["ema"] = df["close"].ewm(span=EMA_LEN, adjust=False).mean()
        return df
    except Exception as e:
        log(f"✗ Errore dati: {e}")
        return None

def get_position():
    try:
        res = session.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]
        for p in res:
            size = float(p.get("size", 0))
            if size > 0:
                return p.get("side"), size, float(p.get("avgPrice", 0))
        return None, 0.0, 0.0
    except:
        return None, 0.0, 0.0

def place_trade(side, price):
    qty = max(math.floor((ORDER_VALUE_USDT / price) / QTY_STEP) * QTY_STEP, QTY_STEP)
    is_buy = (side == "Buy")
    entry = round(price * (1 - LIMIT_BUFFER if is_buy else 1 + LIMIT_BUFFER) / TICK_SIZE) * TICK_SIZE
    tp = round(entry * (1 + TP_PERCENT/100 if is_buy else 1 - TP_PERCENT/100) / TICK_SIZE) * TICK_SIZE
    sl = round(entry * (1 - SL_PERCENT/100 if is_buy else 1 + SL_PERCENT/100) / TICK_SIZE) * TICK_SIZE
    try:
        session.cancel_all_orders(category="linear", symbol=SYMBOL)
        time.sleep(0.5)
        session.place_order(
            category="linear", symbol=SYMBOL, side=side, orderType="Limit",
            qty=f"{qty:.{QTY_PRECISION}f}", price=f"{entry:.{PRICE_PRECISION}f}",
            takeProfit=f"{tp:.{PRICE_PRECISION}f}", stopLoss=f"{sl:.{PRICE_PRECISION}f}",
            positionIdx=0, timeInForce="GTC"
        )
        log(f"🚀 ENTRY {side} @ {entry}")
        telegram(f"🚀 **ENTRY {side}** @ {entry}")
    except Exception as e:
        log(f"✗ Errore Ordine: {e}")

def check_startup_signal():
    global bull_memory, bear_memory
    log("🔍 Analisi segnali recenti per avvio immediato...")
    df = get_market_data()
    if df is None: return

    # Analisi delle ultime candele chiuse per ricostruire la memoria
    for i in range(-5, -1):
        curr = df.iloc[i]
        prev = df.iloc[i-1]
        bull_c = (prev["k"] <= prev["d"] + SLACK) and (curr["k"] > curr["d"])
        bear_c = (prev["k"] >= prev["d"] - SLACK) and (curr["k"] < curr["d"])
        
        if bull_c: bull_memory = 4
        elif bear_c: bear_memory = 4
        else:
            bull_memory = max(bull_memory - 1, 0)
            bear_memory = max(bear_memory - 1, 0)

    price = float(df.iloc[-2]["close"])
    ema = float(df.iloc[-2]["ema"])
    side, _, _ = get_position()

    if side is None:
        if bull_memory > 0 and price > ema * 0.999:
            log("⚡ Segnale valido trovato all'avvio! Apro LONG.")
            place_trade("Buy", price)
            bull_memory = 0
        elif bear_memory > 0 and price < ema * 1.001:
            log("⚡ Segnale valido trovato all'avvio! Apro SHORT.")
            place_trade("Sell", price)
            bear_memory = 0
    log(f"Diagnosi completata. BullMem: {bull_memory} | BearMem: {bear_memory}")

# ====================== MAIN LOOP ======================
if __name__ == "__main__":
    log("=== BOT ETH REVERSAL ATTIVO ===")
    
    # Controllo immediato all'accensione
    check_startup_signal()

    while True:
        try:
            now = datetime.now(UTC)
            # Aspetta il prossimo multiplo di 30 minuti + 10 secondi di sicurezza
            wait = (30 - (now.minute % 30)) * 60 - now.second + 10
            log(f"⏳ In attesa del prossimo controllo tra {int(wait)}s")
            time.sleep(max(wait, 10))

            df = get_market_data()
            if df is None: continue
            
            curr, prev = df.iloc[-2], df.iloc[-3]
            price, ema = float(curr["close"]), float(curr["ema"])
            
            bull_cross = (prev["k"] <= prev["d"] + SLACK) and (curr["k"] > curr["d"])
            bear_cross = (prev["k"] >= prev["d"] - SLACK) and (curr["k"] < curr["d"])

            side, qty, _ = get_position()

            if bull_cross: bull_memory = 4
            else: bull_memory = max(bull_memory - 1, 0)
            
            if bear_cross: bear_memory = 4
            else: bear_memory = max(bear_memory - 1, 0)

            # Logica Reversal (Chiudi se segnale opposto)
            if (side == "Buy" and bear_cross) or (side == "Sell" and bull_cross):
                session.place_order(category="linear", symbol=SYMBOL, 
                                   side="Sell" if side=="Buy" else "Buy",
                                   orderType="Market", qty=f"{qty:.{QTY_PRECISION}f}", 
                                   reduceOnly=True, positionIdx=0)
                log(f"🔴 REVERSAL: Chiusa posizione {side}")
                side = None

            # Logica Entry
            if side is None:
                if bull_memory > 0 and price > ema * 0.999:
                    place_trade("Buy", price)
                    bull_memory = 0
                elif bear_memory > 0 and price < ema * 1.001:
                    place_trade("Sell", price)
                    bear_memory = 0
            
            log(f"Monitor → Price:{price:.2f} | K:{curr['k']:.1f} D:{curr['d']:.1f} | Pos:{side}")

        except Exception as e:
            log(f"Loop error: {e}")
            time.sleep(30)
