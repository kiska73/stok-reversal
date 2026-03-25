import time
import pandas as pd
import pandas_ta as ta
import math
import requests
from datetime import datetime, UTC
from pybit.unified_trading import HTTP

# ============================================================
# CONFIGURAZIONE (API IN CHIARO COME RICHIESTO)
# ============================================================
API_KEY      = "26tNwg57oCDvlNidYT"
API_SECRET   = "WQ84S2dhZ9FVoXkJ7WqWCt6F7HSXR4fsrqhH"

TELEGRAM_TOKEN   = "6916198243:AAFTF66uLYSeqviL5YnfGtbUkSjTwPzah6s"
TELEGRAM_CHAT_ID = "820279313"

SYMBOL = "ETHUSDT"
ORDER_VALUE_USDT = 1000
INTERVAL = "30"

# Parametri Strategia (TradingView)
RSI_LEN, STOCH_LEN, K_SMOOTH, D_SMOOTH = 30, 30, 27, 26
EMA_LEN = 14
TP_PERCENT = 8.4
SL_PERCENT = 2.4

# ============================================================
# SESSIONE E UTILS
# ============================================================
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

def log(msg):
    ts = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
    print(f"{ts} | {msg}")

def telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except: pass

def get_instrument_info():
    res = session.get_instruments_info(category="linear", symbol=SYMBOL)["result"]["list"][0]
    return float(res["priceFilter"]["tickSize"]), float(res["lotSizeFilter"]["qtyStep"])

TICK_SIZE, QTY_STEP = get_instrument_info()

# ============================================================
# LOGICA SEGNALE
# ============================================================
def get_signal():
    try:
        klines = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=100)
        df = pd.DataFrame(klines["result"]["list"], columns=["ts","open","high","low","close","vol","turnover"])
        df[["open","high","low","close"]] = df[["open","high","low","close"]].astype(float)
        df = df[::-1].reset_index(drop=True)

        # Calcolo Stoch RSI (Metodo preciso pandas_ta)
        stoch_rsi = ta.stochrsi(df["close"], length=RSI_LEN, rsi_length=RSI_LEN, k=K_SMOOTH, d=D_SMOOTH)
        k_col = f"STOCHRSIk_{RSI_LEN}_{RSI_LEN}_{K_SMOOTH}_{D_SMOOTH}"
        d_col = f"STOCHRSId_{RSI_LEN}_{RSI_LEN}_{K_SMOOTH}_{D_SMOOTH}"
        
        last_k = stoch_rsi[k_col].iloc[-1]
        last_d = stoch_rsi[d_col].iloc[-1]
        last_close = df["close"].iloc[-1]
        last_ema = ta.ema(df["close"], length=EMA_LEN).iloc[-1]

        log(f"Analisi: Prezzo {last_close:.2f} | EMA {last_ema:.2f} | K {last_k:.2f} | D {last_d:.2f}")

        # Segnale: Lo stocastico deve essere incrociato E il prezzo deve essere dalla parte giusta dell'EMA
        long_sig = (last_k > last_d) and (last_close > last_ema)
        short_sig = (last_k < last_d) and (last_close < last_ema)

        return long_sig, short_sig, last_close
    except Exception as e:
        log(f"Errore calcolo segnale: {e}")
        return False, False, 0

# ============================================================
# ESECUZIONE ORDINI
# ============================================================
def get_current_position():
    pos = session.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]
    for p in pos:
        size = float(p.get("size", 0))
        if size > 0: return p["side"], size
    return None, 0

def open_trade(side, price, close_old=False, old_qty=0):
    if close_old:
        rev_side = "Sell" if side == "Buy" else "Buy"
        session.place_order(category="linear", symbol=SYMBOL, side=rev_side, orderType="Market", qty=str(old_qty), reduceOnly=True)
        log(f"🔄 Inversione: Chiusa posizione {rev_side}")

    qty = round((ORDER_VALUE_USDT / price) / QTY_STEP) * QTY_STEP
    qty = round(qty, 3) # ETH accetta 3 decimali
    
    tp = round((price * (1 + TP_PERCENT/100 if side == "Buy" else 1 - TP_PERCENT/100)) / TICK_SIZE) * TICK_SIZE
    sl = round((price * (1 - SL_PERCENT/100 if side == "Buy" else 1 + SL_PERCENT/100)) / TICK_SIZE) * TICK_SIZE

    res = session.place_order(
        category="linear", symbol=SYMBOL, side=side, orderType="Market", qty=str(qty),
        takeProfit=f"{tp:.2f}", stopLoss=f"{sl:.2f}", positionIdx=0
    )
    if res["retCode"] == 0:
        msg = f"✅ {side.upper()} APERTO @ {price} | TP: {tp} | SL: {sl}"
        log(msg)
        telegram(msg)

# ============================================================
# LOOP PRINCIPALE
# ============================================================
if __name__ == "__main__":
    log("=== BOT AVVIATO (API IN CHIARO) ===")
    
    while True:
        try:
            # Aspetta chiusura candela + 10 secondi
            now = datetime.now(UTC)
            seconds_to_wait = (30 - (now.minute % 30)) * 60 - now.second + 10
            log(f"Prossimo controllo tra {seconds_to_wait} secondi...")
            time.sleep(max(seconds_to_wait, 10))

            long_sig, short_sig, price = get_signal()
            curr_side, curr_qty = get_current_position()

            if curr_side is None:
                if long_sig: open_trade("Buy", price)
                elif short_sig: open_trade("Sell", price)
            else:
                # Inversione
                if curr_side == "Buy" and short_sig:
                    open_trade("Sell", price, True, curr_qty)
                elif curr_side == "Sell" and long_sig:
                    open_trade("Buy", price, True, curr_qty)
                else:
                    log("Nessuna inversione rilevata.")

        except Exception as e:
            log(f"CRASH: {e}")
            time.sleep(30)
