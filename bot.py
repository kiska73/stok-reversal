import time
import pandas as pd
import pandas_ta as ta
import math
import requests
from datetime import datetime, UTC
from pybit.unified_trading import HTTP

# ============================================================
# CONFIGURAZIONE API
# ============================================================
API_KEY      = "26tNwg57oCDvlNidYT"
API_SECRET   = "WQ84S2dhZ9FVoXkJ7WqWCt6F7HSXR4fsrqhH"
TELEGRAM_TOKEN   = "6916198243:AAFTF66uLYSeqviL5YnfGtbUkSjTwPzah6s"
TELEGRAM_CHAT_ID = "820279313"

SYMBOL = "ETHUSDT"
ORDER_VALUE_USDT = 1000
INTERVAL = "30"

# Parametri Pine Script Rigorosi
RSI_LEN, STOCH_LEN, K_SMOOTH, D_SMOOTH = 30, 30, 27, 26
SLACK, DIST_MIN = 1.0, 0.2
EMA_LEN = 14
TP_PERCENT, SL_PERCENT = 8.4, 2.4

session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

def log(msg):
    ts = datetime.now(UTC).strftime('%H:%M:%S')
    print(f"{ts} | {msg}")

def telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=5)
    except: pass

def get_instrument_info():
    try:
        res = session.get_instruments_info(category="linear", symbol=SYMBOL)["result"]["list"][0]
        tick_size = float(res["priceFilter"]["tickSize"])
        qty_step = float(res["lotSizeFilter"]["qtyStep"])
        price_p = len(str(tick_size).split(".")[1]) if "." in str(tick_size) else 0
        qty_p = len(str(qty_step).split(".")[1]) if "." in str(qty_step) else 0
        return tick_size, qty_step, price_p, qty_p
    except Exception as e:
        log(f"Errore info: {e}")
        return 0.01, 0.01, 2, 2

TICK_SIZE, QTY_STEP, PRICE_PRECISION, QTY_PRECISION = get_instrument_info()

# ============================================================
# CALCOLO INDICATORI (COPIA ESATTA PINE SCRIPT)
# ============================================================
def get_signal():
    try:
        # Buffer ampio per stabilizzare SMA ed EMA
        klines = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=250)
        df = pd.DataFrame(klines["result"]["list"], columns=["ts","open","high","low","close","vol","turnover"])
        df["close"] = df["close"].astype(float)
        
        # IMPORTANTE: Inversione cronologica Bybit (Past -> Present)
        df = df.iloc[::-1].reset_index(drop=True)

        # 1. RSI
        rsi_val = ta.rsi(df["close"], length=RSI_LEN)

        # 2. STOCH RSI (Formula manuale dal tuo Pine)
        lowest_rsi = rsi_val.rolling(window=STOCH_LEN).min()
        highest_rsi = rsi_val.rolling(window=STOCH_LEN).max()
        range_rsi = (highest_rsi - lowest_rsi).apply(lambda x: max(x, 0.00001))
        stoch_rsi = (rsi_val - lowest_rsi) / range_rsi * 100

        # 3. K e D Smoothing (SMA come ta.sma)
        k = ta.sma(stoch_rsi, length=K_SMOOTH)
        d = ta.sma(k, length=D_SMOOTH)
        ema = ta.ema(df["close"], length=EMA_LEN)

        # Valori attuali [indice -1] e precedenti [indice -2] (corrisponde a k[1] in Pine)
        k_now, k_prev = k.iloc[-1], k.iloc[-2]
        d_now, d_prev = d.iloc[-1], d.iloc[-2]
        price_now = df["close"].iloc[-1]
        ema_now = ema.iloc[-1]

        # Logica Cross Pine Script
        # k > d and k[1] <= d[1] + slack and math.abs(k[1]-d[1]) >= dist_min
        bull_cross = k_now > d_now and k_prev <= (d_prev + SLACK) and abs(k_prev - d_prev) >= DIST_MIN
        bear_cross = k_now < d_now and k_prev >= (d_prev - SLACK) and abs(k_prev - d_prev) >= DIST_MIN
        
        ema_bull_ok = price_now > ema_now
        ema_bear_ok = price_now < ema_now

        log(f"K: {k_now:.2f} (Prev: {k_prev:.2f}) | D: {d_now:.2f} (Prev: {d_prev:.2f})")
        log(f"Prezzo: {price_now:.2f} | EMA: {ema_now:.2f}")

        return (bull_cross and ema_bull_ok), (bear_cross and ema_bear_ok), price_now

    except Exception as e:
        log(f"Errore: {e}")
        return False, False, 0

# ============================================================
# ESECUZIONE (GESTIONE REVERSE)
# ============================================================
def get_pos_detail():
    try:
        res = session.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]
        for p in res:
            size = float(p.get("size", 0))
            if size > 0: return p["side"], size, float(p["avgPrice"])
        return None, 0, 0
    except: return None, 0, 0

def execute_trade(side, price, old_side=None, old_qty=0, old_entry=0):
    # Reverse Close
    if old_side and old_qty > 0:
        pnl = (price - old_entry) * old_qty if old_side == "Buy" else (old_entry - price) * old_qty
        emoji = "💰" if pnl > 0 else "📉"
        session.place_order(category="linear", symbol=SYMBOL, side="Sell" if old_side=="Buy" else "Buy", 
                            orderType="Market", qty=f"{old_qty:.{QTY_PRECISION}f}", reduceOnly=True)
        telegram(f"{emoji} *CHIUSO {old_side}*\nProfit/Loss: `{pnl:.2f} USDT`")

    # Apertura Nuova
    qty = math.floor((ORDER_VALUE_USDT / price) / QTY_STEP) * QTY_STEP
    qty_str = f"{qty:.{QTY_PRECISION}f}"
    
    tp = round((price * (1 + TP_PERCENT/100 if side == "Buy" else 1 - TP_PERCENT/100)) / TICK_SIZE) * TICK_SIZE
    sl = round((price * (1 - SL_PERCENT/100 if side == "Buy" else 1 + SL_PERCENT/100)) / TICK_SIZE) * TICK_SIZE

    res = session.place_order(
        category="linear", symbol=SYMBOL, side=side, orderType="Market", qty=qty_str,
        takeProfit=f"{tp:.{PRICE_PRECISION}f}", stopLoss=f"{sl:.{PRICE_PRECISION}f}", positionIdx=0
    )
    
    if res["retCode"] == 0:
        telegram(f"🚀 *APERTO {side.upper()}*\nPrezzo: `{price:.2f}`\n🎯 TP: `{tp:.{PRICE_PRECISION}f}` | 🛡️ SL: `{sl:.{PRICE_PRECISION}f}`")

# ============================================================
# MAIN LOOP
# ============================================================
if __name__ == "__main__":
    telegram("🤖 *BOT ETH ONLINE*\nSincronizzato al 100% con Pine Script.")
    while True:
        try:
            now = datetime.now(UTC)
            wait = (30 - (now.minute % 30)) * 60 - now.second + 12
            if wait < 0: wait = 5
            time.sleep(max(wait, 5))

            bull, bear, price = get_signal()
            side, qty, entry = get_pos_detail()

            if bull and side != "Buy":
                execute_trade("Buy", price, side, qty, entry)
            elif bear and side != "Sell":
                execute_trade("Sell", price, side, qty, entry)
            else:
                log(f"Monitoraggio... Prezzo: {price:.2f} | Pos: {side}")

        except Exception as e:
            log(f"Errore: {e}")
            time.sleep(20)
