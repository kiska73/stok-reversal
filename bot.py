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
    sys.exit(1)

# ====================== PARAMETRI TRADING ======================
SYMBOL           = "ETHUSDT"
ORDER_VALUE_USDT = 1000
INTERVAL         = "30"
RSI_LEN, STOCH_LEN, K_SMOOTH, D_SMOOTH, EMA_LEN = 14, 14, 21, 27, 10
SLACK, TP_PERCENT, SL_PERCENT = 0.35, 8.5, 2
LIMIT_BUFFER     = 0.0001 
TESTNET          = False 

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
        # Aggiunto emoji e formattazione per visibilità
        text = f"🤖 **BOT ETH:**\n{msg}"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Errore invio Telegram: {e}")

def get_instrument_info():
    try:
        res = session.get_instruments_info(category="linear", symbol=SYMBOL)
        info = res["result"]["list"][0]
        tick = float(info["priceFilter"]["tickSize"])
        qty_step = float(info["lotSizeFilter"]["qtyStep"])
        p_prec = len(str(tick).split('.')[1]) if '.' in str(tick) else 0
        q_prec = len(str(qty_step).split('.')[1]) if '.' in str(qty_step) else 0
        return tick, qty_step, p_prec, q_prec
    except: return 0.01, 0.001, 2, 3

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
            if float(p.get("size", 0)) > 0:
                return p.get("side"), float(p.get("size")), float(p.get("avgPrice"))
        return None, 0.0, 0.0
    except: return None, 0.0, 0.0

def place_trade(side):
    try:
        ticker = session.get_tickers(category="linear", symbol=SYMBOL)
        current_price = float(ticker["result"]["list"][0]["lastPrice"])
        
        qty = max(math.floor((ORDER_VALUE_USDT / current_price) / QTY_STEP) * QTY_STEP, QTY_STEP)
        is_buy = (side == "Buy")
        
        entry = round(current_price * (1 - LIMIT_BUFFER if is_buy else 1 + LIMIT_BUFFER) / TICK_SIZE) * TICK_SIZE
        tp = round(entry * (1 + TP_PERCENT/100 if is_buy else 1 - TP_PERCENT/100) / TICK_SIZE) * TICK_SIZE
        sl = round(entry * (1 - SL_PERCENT/100 if is_buy else 1 + SL_PERCENT/100) / TICK_SIZE) * TICK_SIZE
        
        session.cancel_all_orders(category="linear", symbol=SYMBOL)
        time.sleep(0.5)
        
        # 1. LIMIT
        resp = session.place_order(
            category="linear", symbol=SYMBOL, side=side, orderType="Limit",
            qty=f"{qty:.{QTY_PRECISION}f}", price=f"{entry:.{PRICE_PRECISION}f}",
            takeProfit=f"{tp:.{PRICE_PRECISION}f}", stopLoss=f"{sl:.{PRICE_PRECISION}f}",
            positionIdx=0, timeInForce="GTC"
        )
        order_id = resp["result"]["orderId"]
        log(f"⏳ LIMIT {side} @ {entry}. Monitoraggio 3 min...")
        telegram(f"⏳ Inserito ordine LIMIT **{side}** @ {entry}\n_(Prezzo spot: {current_price})_")

        start_time = time.time()
        is_filled = False
        while time.time() - start_time < 180:
            check = session.get_open_orders(category="linear", symbol=SYMBOL, orderId=order_id)
            if not check["result"]["list"]:
                is_filled = True
                break
            time.sleep(15)

        # 2. SE NON FILLATO -> MARKET
        if not is_filled:
            session.cancel_order(category="linear", symbol=SYMBOL, orderId=order_id)
            session.place_order(
                category="linear", symbol=SYMBOL, side=side, orderType="Market",
                qty=f"{qty:.{QTY_PRECISION}f}",
                takeProfit=f"{tp:.{PRICE_PRECISION}f}", stopLoss=f"{sl:.{PRICE_PRECISION}f}",
                positionIdx=0
            )
            telegram(f"⚡ LIMIT scaduto. Entrato a **MARKET {side}**")
        else:
            telegram(f"🎯 Ordine **{side}** eseguito con successo (Limit fill).")

    except Exception as e: 
        log(f"✗ Errore: {e}")
        telegram(f"❌ Errore durante l'ordine: {e}")

def check_startup_signal():
    global bull_memory, bear_memory
    log("🔍 Analisi avvio...")
    df = get_market_data()
    if df is None: return

    for i in range(-5, -1):
        curr = df.iloc[i]; prev = df.iloc[i-1]
        if (prev["k"] <= prev["d"] + SLACK) and (curr["k"] > curr["d"]): bull_memory = 4
        elif (prev["k"] >= prev["d"] - SLACK) and (curr["k"] < curr["d"]): bear_memory = 4
        else:
            bull_memory = max(bull_memory - 1, 0)
            bear_memory = max(bear_memory - 1, 0)

    last_close = df.iloc[-2]
    price = float(last_close["close"])
    ema = float(last_close["ema"])
    side, _, _ = get_position()

    status_msg = f"✅ Bot Avviato!\nPosizione attuale: {side if side else 'Nessuna'}\nBullMem: {bull_memory} | BearMem: {bear_memory}"
    telegram(status_msg)

if __name__ == "__main__":
    log("=== BOT ETH REVERSAL ATTIVO ===")
    check_startup_signal()
    
    while True:
        try:
            now = datetime.now(UTC)
            wait = (30 - (now.minute % 30)) * 60 - now.second + 15
            log(f"⏳ Prossimo check tra {int(wait)}s")
            time.sleep(max(wait, 10))

            df = get_market_data()
            if df is None: continue
            curr, prev = df.iloc[-2], df.iloc[-3]
            price_candle, ema = float(curr["close"]), float(curr["ema"])
            
            bull_cross = (prev["k"] <= prev["d"] + SLACK) and (curr["k"] > curr["d"])
            bear_cross = (prev["k"] >= prev["d"] - SLACK) and (curr["k"] < curr["d"])
            side, qty, avg_price = get_position()

            if bull_cross: 
                bull_memory = 4
                telegram("📈 Segnale **Bullish Cross** rilevato!")
            else: bull_memory = max(bull_memory - 1, 0)
            
            if bear_cross: 
                bear_memory = 4
                telegram("📉 Segnale **Bearish Cross** rilevato!")
            else: bear_memory = max(bear_memory - 1, 0)

            # Reversal
            if (side == "Buy" and bear_cross) or (side == "Sell" and bull_cross):
                telegram(f"🔄 Reversal! Chiudo posizione **{side}** a mercato.")
                session.place_order(category="linear", symbol=SYMBOL, side="Sell" if side=="Buy" else "Buy",
                                   orderType="Market", qty=f"{qty:.{QTY_PRECISION}f}", reduceOnly=True, positionIdx=0)
                side = None
                time.sleep(2)

            # Apertura
            if side is None:
                if bull_memory > 0 and price_candle > ema * 0.999:
                    telegram("🚀 Condizioni BUY soddisfatte. Provo entrata...")
                    place_trade("Buy")
                    bull_memory = 0
                elif bear_memory > 0 and price_candle < ema * 1.001:
                    telegram("🚀 Condizioni SELL soddisfatte. Provo entrata...")
                    place_trade("Sell")
                    bear_memory = 0
            
            log(f"Monitor → Price:{price_candle:.2f} | K:{curr['k']:.1f} | Pos:{side}")
        except Exception as e:
            log(f"Loop error: {e}")
            telegram(f"⚠️ Errore nel ciclo principale: {e}")
            time.sleep(30)
