import time
import pandas as pd
import pandas_ta as ta
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

# Parametri Pine Script
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
    res = session.get_instruments_info(category="linear", symbol=SYMBOL)["result"]["list"][0]
    return float(res["priceFilter"]["tickSize"]), float(res["lotSizeFilter"]["qtyStep"])

TICK_SIZE, QTY_STEP = get_instrument_info()

# ============================================================
# LOGICA SEGNALE & POSIZIONE
# ============================================================
def get_signal():
    try:
        klines = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=150)
        df = pd.DataFrame(klines["result"]["list"], columns=["ts","open","high","low","close","vol","turnover"])
        df["close"] = df["close"].astype(float)
        df = df[::-1].reset_index(drop=True)

        rsi_val = ta.rsi(df["close"], length=RSI_LEN)
        l, h = rsi_val.rolling(STOCH_LEN).min(), rsi_val.rolling(STOCH_LEN).max()
        stoch_rsi = (rsi_val - l) / (h - l + 1e-10) * 100
        k = ta.sma(stoch_rsi, length=K_SMOOTH)
        d = ta.sma(k, length=D_SMOOTH)
        ema = ta.ema(df["close"], length=EMA_LEN)

        k_n, k_p = k.iloc[-1], k.iloc[-2]
        d_n, d_p = d.iloc[-1], d.iloc[-2]
        p_n, e_n = df["close"].iloc[-1], ema.iloc[-1]

        bull = k_n > d_n and k_p <= (d_p + SLACK) and abs(k_p - d_p) >= DIST_MIN and p_n > e_n
        bear = k_n < d_n and k_p >= (d_p - SLACK) and abs(k_p - d_p) >= DIST_MIN and p_n < e_n
        
        return bull, bear, p_n
    except Exception as e:
        log(f"Errore segnale: {e}")
        return False, False, 0

def get_pos_detail():
    """Restituisce Side, Qty, EntryPrice"""
    res = session.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]
    for p in res:
        size = float(p.get("size", 0))
        if size > 0: return p["side"], size, float(p["avgPrice"])
    return None, 0, 0

# ============================================================
# ESECUZIONE ORDINI
# ============================================================
def execute_trade(side, price, old_side=None, old_qty=0, old_entry=0):
    # 1. Calcolo PNL se chiudiamo una posizione esistente
    if old_side and old_qty > 0:
        pnl = (price - old_entry) * old_qty if old_side == "Buy" else (old_entry - price) * old_qty
        emoji = "💰" if pnl > 0 else "📉"
        session.place_order(category="linear", symbol=SYMBOL, side="Sell" if old_side=="Buy" else "Buy", 
                            orderType="Market", qty=str(old_qty), reduceOnly=True)
        telegram(f"{emoji} *CHIUSO {old_side}*\nPrezzo: {price}\nProfit/Loss: `{pnl:.2f} USDT`")

    # 2. Apertura Nuova Posizione
    qty = round((ORDER_VALUE_USDT / price) / QTY_STEP) * QTY_STEP
    qty = round(qty, 3)
    tp = round((price * (1 + TP_PERCENT/100 if side == "Buy" else 1 - TP_PERCENT/100)) / TICK_SIZE) * TICK_SIZE
    sl = round((price * (1 - SL_PERCENT/100 if side == "Buy" else 1 + SL_PERCENT/100)) / TICK_SIZE) * TICK_SIZE

    res = session.place_order(category="linear", symbol=SYMBOL, side=side, orderType="Market", qty=str(qty),
                            takeProfit=f"{tp:.2f}", stopLoss=f"{sl:.2f}", positionIdx=0)
    
    if res["retCode"] == 0:
        telegram(f"🚀 *APERTO {side.upper()}*\nPrezzo: `{price:.2f}`\nQty: `{qty}`\n🎯 TP: `{tp:.2f}` | 🛡️ SL: `{sl:.2f}`")

# ============================================================
# MAIN LOOP
# ============================================================
if __name__ == "__main__":
    telegram("🤖 *BOT ETH AVVIATO*\nMonitoraggio attivo ogni 30 min.")
    
    while True:
        try:
            now = datetime.now(UTC)
            wait = (30 - (now.minute % 30)) * 60 - now.second + 12
            time.sleep(max(wait, 5))

            bull, bear, price = get_signal()
            side, qty, entry = get_pos_detail()

            if bull and side != "Buy":
                execute_trade("Buy", price, side, qty, entry)
            elif bear and side != "Sell":
                execute_trade("Sell", price, side, qty, entry)
            else:
                log(f"Check: Prezzo {price} | Posizione attuale: {side}")

        except Exception as e:
            log(f"Errore loop: {e}")
            time.sleep(10)
