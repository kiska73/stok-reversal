import time
import os
import pandas as pd
import pandas_ta as ta
import math
import random
from datetime import datetime, UTC, timedelta
from pybit.unified_trading import HTTP
from dotenv import load_dotenv
import requests

# Caricamento configurazione
load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "ETHUSDT"
ORDER_VALUE_USDT = 1000
INTERVAL = "30"

# Parametri indicatori (TradingView Style)
RSI_LEN = 30
STOCH_LEN = 30
K_SMOOTH = 27
D_SMOOTH = 26
EMA_LEN = 14

TP_PERCENT = 8.4
SL_PERCENT = 2.4

# Sessione Bybit
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

def log(msg):
    ts = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
    print(f"{ts} | {msg}")

def telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except: pass

# ==================== FUNZIONI TRADING ====================

def get_instrument_info():
    res = session.get_instruments_info(category="linear", symbol=SYMBOL)["result"]["list"][0]
    tick_size = float(res["priceFilter"]["tickSize"])
    step_size = float(res["lotSizeFilter"]["qtyStep"])
    return tick_size, step_size

TICK_SIZE, QTY_STEP = get_instrument_info()

def get_data():
    try:
        klines = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=100)
        df = pd.DataFrame(klines["result"]["list"], columns=["ts","open","high","low","close","vol","turnover"])
        df[["open","high","low","close"]] = df[["open","high","low","close"]].astype(float)
        return df[::-1].reset_index(drop=True)
    except Exception as e:
        log(f"Errore download dati: {e}")
        return None

def get_signal(df):
    # Calcolo Stoch RSI con libreria precisa
    stoch_rsi = ta.stochrsi(df["close"], length=RSI_LEN, rsi_length=RSI_LEN, k=K_SMOOTH, d=D_SMOOTH)
    k_col = f"STOCHRSIk_{RSI_LEN}_{RSI_LEN}_{K_SMOOTH}_{D_SMOOTH}"
    d_col = f"STOCHRSId_{RSI_LEN}_{RSI_LEN}_{K_SMOOTH}_{D_SMOOTH}"
    
    k, d = stoch_rsi[k_col], stoch_rsi[d_col]
    ema = ta.ema(df["close"], length=EMA_LEN)

    last_k, last_d = k.iloc[-1], d.iloc[-1]
    last_close = df["close"].iloc[-1]
    last_ema = ema.iloc[-1]

    # LOGICA: Stocastico incrociato E prezzo confermato da EMA
    long_sig = (last_k > last_d) and (last_close > last_ema)
    short_sig = (last_k < last_d) and (last_close < last_ema)

    log(f"Analisi: Prezzo {last_close:.2f} | EMA {last_ema:.2f} | K {last_k:.2f} | D {last_d:.2f}")
    return long_sig, short_sig

def get_current_position():
    pos = session.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]
    for p in pos:
        size = float(p.get("size", 0))
        if size > 0: return p["side"], size
    return None, 0

def execute_order(side, close_existing=False, existing_qty=0):
    if close_existing:
        close_side = "Sell" if side == "Buy" else "Buy" # Chiudo per poi girarmi
        session.place_order(category="linear", symbol=SYMBOL, side=close_side, 
                            orderType="Market", qty=str(existing_qty), reduceOnly=True)
        log(f"Chiusa posizione precedente: {close_side}")

    # Apertura nuova
    ticker = session.get_tickers(category="linear", symbol=SYMBOL)["result"]["list"][0]
    price = float(ticker["lastPrice"])
    qty = round((ORDER_VALUE_USDT / price) // QTY_STEP * QTY_STEP, 4)
    
    tp = round(price * (1 + TP_PERCENT/100 if side == "Buy" else 1 - TP_PERCENT/100) / TICK_SIZE) * TICK_SIZE
    sl = round(price * (1 - SL_PERCENT/100 if side == "Buy" else 1 + SL_PERCENT/100) / TICK_SIZE) * TICK_SIZE

    res = session.place_order(
        category="linear", symbol=SYMBOL, side=side, orderType="Market", qty=str(qty),
        takeProfit=f"{tp:.2f}", stopLoss=f"{sl:.2f}", positionIdx=0
    )
    if res["retCode"] == 0:
        telegram(f"🚀 {side} APERTO: {qty} ETH @ {price} | TP: {tp} SL: {sl}")

# ==================== LOOP PRINCIPALE ====================

if __name__ == "__main__":
    log("Bot Avviato con successo.")
    
    while True:
        try:
            # Timing: aspetta chiusura candela (ogni 30 min) + 10 secondi per aggiornamento server
            now = datetime.now(UTC)
            minutes_to_wait = 30 - (now.minute % 30)
            seconds_to_wait = (minutes_to_wait * 60) - now.second + 10
            
            log(f"In attesa della prossima candela ({seconds_to_wait}s)...")
            time.sleep(seconds_to_wait)

            df = get_data()
            if df is None: continue

            long_sig, short_sig = get_signal(df)
            curr_side, curr_qty = get_current_position()

            if curr_side is None:
                if long_sig: execute_order("Buy")
                elif short_sig: execute_order("Sell")
            else:
                # Gestione Inversione
                if curr_side == "Buy" and short_sig:
                    execute_order("Sell", True, curr_qty)
                elif curr_side == "Sell" and long_sig:
                    execute_order("Buy", True, curr_qty)
                else:
                    log("Segnale conferma posizione attuale o nessun segnale opposto.")

        except Exception as e:
            log(f"ERRORE: {e}")
            time.sleep(10)
