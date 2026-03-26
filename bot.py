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

# Parametri Pine Script Sincronizzati
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
        log(f"Errore recupero info strumento: {e}")
        return 0.01, 0.01, 2, 2

TICK_SIZE, QTY_STEP, PRICE_PRECISION, QTY_PRECISION = get_instrument_info()

# ============================================================
# LOGICA SEGNALE
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
        log(f"Errore calcolo segnale: {e}")
        return False, False, 0

def get_pos_detail():
    try:
        res = session.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]
        for p in res:
            size = float(p.get("size", 0))
            if size > 0: return p["side"], size, float(p["avgPrice"])
        return None, 0, 0
    except: return None, 0, 0

# ============================================================
# ESECUZIONE
# ============================================================
def execute_trade(side, price, old_side=None, old_qty=0, old_entry=0):
    if old_side and old_qty > 0:
        pnl = (price - old_entry) * old_qty if old_side == "Buy" else (old_entry - price) * old_qty
        emoji = "💰" if pnl > 0 else "📉"
        old_qty_str = f"{old_qty:.{QTY_PRECISION}f}"
        session.place_order(category="linear", symbol=SYMBOL, side="Sell" if old_side=="Buy" else "Buy", 
                            orderType="Market", qty=old_qty_str, reduceOnly=True)
        telegram(f"{emoji} *CHIUSO {old_side}*\nPrezzo: `{price:.2f}`\nPNL: `{pnl:.2f} USDT`")

    raw_qty = ORDER_VALUE_USDT / price
    qty = math.floor(raw_qty / QTY_STEP) * QTY_STEP
    qty_str = f"{qty:.{QTY_PRECISION}f}"

    tp_raw = price * (1 + TP_PERCENT/100 if side == "Buy" else 1 - TP_PERCENT/100)
    sl_raw = price * (1 - SL_PERCENT/100 if side == "Buy" else 1 + SL_PERCENT/100)
    
    tp = round(tp_raw / TICK_SIZE) * TICK_SIZE
    sl = round(sl_raw / TICK_SIZE) * TICK_SIZE
    
    res = session.place_order(
        category="linear", symbol=SYMBOL, side=side, orderType="Market", qty=qty_str,
        takeProfit=f"{tp:.{PRICE_PRECISION}f}", stopLoss=f"{sl:.{PRICE_PRECISION}f}", positionIdx=0
    )
    
    if res["retCode"] == 0:
        telegram(f"🚀 *APERTO {side.upper()}*\nPrezzo: `{price:.2f}`\nQty: `{qty_str}`\n🎯 TP: `{tp:.2f}` | 🛡️ SL: `{sl:.2f}`")
    else:
        telegram(f"❌ *ERRORE ORDINE*: {res['retMsg']}")

# ============================================================
# MAIN LOOP
# ============================================================
if __name__ == "__main__":
    telegram("🤖 *BOT ETH RIAVVIATO*\nConnessione Bybit OK (No IP Limit).")
    
    while True:
        try:
            now = datetime.now(UTC)
            # Attende chiusura candela 30m + 12 secondi
            wait = (30 - (now.minute % 30)) * 60 - now.second + 12
            if wait < 0: wait = 5 # Sicurezza per evitare wait negativi
            
            time.sleep(max(wait, 5))

            bull, bear, price = get_signal()
            side, qty, entry = get_pos_detail()

            if bull and side != "Buy":
                execute_trade("Buy", price, side, qty, entry)
            elif bear and side != "Sell":
                execute_trade("Sell", price, side, qty, entry)
            else:
                log(f"Check: {price:.2f} | Pos: {side}")

        except Exception as e:
            err_msg = str(e)
            log(f"Errore loop: {err_msg}")
            if "10006" in err_msg: # Rate limit
                time.sleep(60)
            elif "10010" in err_msg: # IP Limit ancora attivo
                telegram("⚠️ *ERRORE*: L'IP non è ancora autorizzato su Bybit!")
                time.sleep(300)
            else:
                time.sleep(20)
