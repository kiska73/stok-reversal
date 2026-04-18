import time
import math
import requests
import pandas as pd
import os
import sys
from datetime import datetime, UTC, timedelta
from pybit.unified_trading import HTTP

# ====================== CONFIGURAZIONE CHIAVI ======================
API_KEY = os.getenv('BYBIT_API_KEY')
API_SECRET = os.getenv('BYBIT_API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Controllo Identità API all'avvio
print("=== DIAGNOSI AVVIO BOT ===")
if not API_KEY or not API_SECRET:
    print("❌ ERRORE CRITICO: Variabili d'ambiente mancanti su Render!")
    print(f"BYBIT_API_KEY: {'PRESENTE' if API_KEY else 'MANCANTE'}")
    print(f"BYBIT_API_SECRET: {'PRESENTE' if API_SECRET else 'MANCANTE'}")
    sys.exit("Il bot si ferma: configura le API Key su Render.")
else:
    print(f"✅ API Key caricate (Inizio: {API_KEY[:5]}...)")

# ====================== PARAMETRI TRADING ======================
SYMBOL           = "ETHUSDT"
ORDER_VALUE_USDT = 1000
INTERVAL         = "30"

RSI_LEN          = 14
STOCH_LEN        = 14
K_SMOOTH         = 21
D_SMOOTH         = 27
EMA_LEN          = 10

SLACK            = 0.35
TP_PERCENT       = 8.5
SL_PERCENT       = 2

LIMIT_BUFFER     = 0.0010
TESTNET          = False 

# Inizializzazione Sessione
session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

bull_memory = 0
bear_memory = 0

# ====================== LOG & NOTIFICHE ======================
def log(msg):
    ts = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
    # Stampo solo su stdout per evitare duplicati nei log di Render
    print(f"{ts} | {msg}", flush=True)

def telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        log(f"! Errore Telegram: {e}")

# ====================== INFO STRUMENTO ======================
def get_instrument_info():
    try:
        res = session.get_instruments_info(category="linear", symbol=SYMBOL)
        info = res["result"]["list"][0]
        tick = float(info["priceFilter"]["tickSize"])
        qty_step = float(info["lotSizeFilter"]["qtyStep"])
        
        # Calcolo precisione decimale
        p_prec = len(str(tick).split('.')[1]) if '.' in str(tick) else 0
        q_prec = len(str(qty_step).split('.')[1]) if '.' in str(qty_step) else 0
        
        log(f"✓ {SYMBOL} → Tick:{tick} Step:{qty_step} Prec:[P:{p_prec}, Q:{q_prec}]")
        return tick, qty_step, p_prec, q_prec
    except Exception as e:
        log(f"✗ Errore info strumento: {e}")
        return 0.01, 0.001, 2, 3

TICK_SIZE, QTY_STEP, PRICE_PRECISION, QTY_PRECISION = get_instrument_info()

# ====================== INDICATORI ======================
def calculate_stoch_rsi(df):
    # RSI Standard
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_LEN).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_LEN).mean()
    rs = gain / loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    
    # Stochastic su RSI
    lowest_rsi = rsi.rolling(STOCH_LEN).min()
    highest_rsi = rsi.rolling(STOCH_LEN).max()
    stoch = ((rsi - lowest_rsi) / (highest_rsi - lowest_rsi).clip(lower=1e-8)) * 100
    
    k = stoch.rolling(K_SMOOTH).mean()
    d = k.rolling(D_SMOOTH).mean()
    return k, d

# ====================== DATI MERCATO ======================
def get_market_data():
    try:
        resp = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=100)
        df = pd.DataFrame(resp["result"]["list"], columns=["ts","open","high","low","close","vol","turn"])
        df = df.astype(float).iloc[::-1].reset_index(drop=True)

        df["k"], df["d"] = calculate_stoch_rsi(df)
        df["ema"] = df["close"].ewm(span=EMA_LEN, adjust=False).mean()

        curr = df.iloc[-2] # Candela chiusa
        prev = df.iloc[-3]

        bull_cross = (prev["k"] <= prev["d"] + SLACK) and (curr["k"] > curr["d"])
        bear_cross = (prev["k"] >= prev["d"] - SLACK) and (curr["k"] < curr["d"])

        log(f"Mercato → Prezzo:{curr['close']:.2f} | EMA:{curr['ema']:.2f} | K:{curr['k']:.1f} D:{curr['d']:.1f}")
        return bull_cross, bear_cross, float(curr["close"]), float(curr["ema"])
    except Exception as e:
        log(f"✗ Errore dati mercato: {e}")
        return False, False, 0.0, 0.0

def get_position():
    try:
        res = session.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]
        for p in res:
            size = float(p.get("size", 0))
            if size > 0:
                return p.get("side"), size, float(p.get("avgPrice", 0))
        return None, 0.0, 0.0
    except Exception as e:
        log(f"✗ Errore API Privata (Chiavi?): {e}")
        return None, 0.0, 0.0

# ====================== AZIONI TRADING ======================
def place_trade(side, price):
    qty = max(math.floor((ORDER_VALUE_USDT / price) / QTY_STEP) * QTY_STEP, QTY_STEP)
    
    # Calcolo prezzi con arrotondamento Bybit
    is_buy = (side == "Buy")
    entry_p = round(price * (1 - LIMIT_BUFFER if is_buy else 1 + LIMIT_BUFFER) / TICK_SIZE) * TICK_SIZE
    tp_p = round(entry_p * (1 + TP_PERCENT/100 if is_buy else 1 - TP_PERCENT/100) / TICK_SIZE) * TICK_SIZE
    sl_p = round(entry_p * (1 - SL_PERCENT/100 if is_buy else 1 + SL_PERCENT/100) / TICK_SIZE) * TICK_SIZE

    try:
        session.cancel_all_orders(category="linear", symbol=SYMBOL)
        time.sleep(0.5)
        session.place_order(
            category="linear", symbol=SYMBOL, side=side, orderType="Limit",
            qty=f"{qty:.{QTY_PRECISION}f}", price=f"{entry_p:.{PRICE_PRECISION}f}",
            takeProfit=f"{tp_p:.{PRICE_PRECISION}f}", stopLoss=f"{sl_p:.{PRICE_PRECISION}f}",
            positionIdx=0, timeInForce="GTC"
        )
        msg = f"🚀 **ENTRY {side}** @ {entry_p:.2f}\nTP: {tp_p:.2f} | SL: {sl_p:.2f}"
        log(msg.replace("**", ""))
        telegram(msg)
    except Exception as e:
        log(f"✗ Errore Ordine: {e}")

def close_all(side, qty):
    try:
        session.cancel_all_orders(category="linear", symbol=SYMBOL)
        time.sleep(0.5)
        close_side = "Sell" if side == "Buy" else "Buy"
        session.place_order(
            category="linear", symbol=SYMBOL, side=close_side, orderType="Market",
            qty=f"{qty:.{QTY_PRECISION}f}", reduceOnly=True, positionIdx=0
        )
        log(f"🔴 CHIUSURA {side} eseguita.")
        telegram(f"🔴 **CHIUSURA {side}** @ Market")
    except Exception as e:
        log(f"✗ Errore Chiusura: {e}")

# ====================== LOOP PRINCIPALE ======================
if __name__ == "__main__":
    log("=== BOT ETH REVERSAL ATTIVO ===")
    telegram("🤖 **Bot Avviato correttamente**")

    while True:
        try:
            # Calcolo attesa candela
            ora = datetime.now(UTC)
            minuti_attesa = 30 - (ora.minute % 30)
            prossimo_check = (ora + timedelta(minutes=minuti_attesa)).replace(second=5, microsecond=0)
            secondi_sonno = (prossimo_check - ora).total_seconds()
            
            log(f"⏳ In attesa della candela delle {prossimo_check.strftime('%H:%M:%S')}...")
            time.sleep(max(secondi_sonno, 10))

            bull_cross, bear_cross, price, ema = get_market_data()
            if price == 0: continue

            pos_side, pos_qty, _ = get_position()

            # Logica Segnali
            if bull_cross:
                bull_memory = 4
                log("🔵 Segnale: Bull Cross rilevato")
            else:
                bull_memory = max(bull_memory - 1, 0)

            if bear_cross:
                bear_memory = 4
                log("🔴 Segnale: Bear Cross rilevato")
            else:
                bear_memory = max(bear_memory - 1, 0)

            # Esecuzione Reversal (Chiudi se segnale opposto)
            if pos_side == "Buy" and bear_cross:
                close_all(pos_side, pos_qty)
                pos_side = None
            elif pos_side == "Sell" and bull_cross:
                close_all(pos_side, pos_qty)
                pos_side = None

            # Esecuzione Entrata
            if pos_side is None:
                if bull_memory > 0 and price > ema * 0.999:
                    place_trade("Buy", price)
                    bull_memory = 0
                elif bear_memory > 0 and price < ema * 1.001:
                    place_trade("Sell", price)
                    bear_memory = 0

            log(f"Status → Pos:{pos_side} | BullMem:{bull_memory} | BearMem:{bear_memory}")

        except Exception as e:
            log(f"✗ Errore Loop: {e}")
            time.sleep(60)
