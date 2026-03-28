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
        p_p = len(str(tick_size).split(".")[1]) if "." in str(tick_size) else 0
        q_p = len(str(qty_step).split(".")[1]) if "." in str(qty_step) else 0
        return tick_size, qty_step, p_p, q_p
    except: return 0.01, 0.01, 2, 2

TICK_SIZE, QTY_STEP, PRICE_PRECISION, QTY_PRECISION = get_instrument_info()

# ============================================================
# LOGICA SEGNALE
# ============================================================
def get_market_data():
    try:
        klines = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=250)
        df = pd.DataFrame(klines["result"]["list"], columns=["ts","open","high","low","close","vol","turnover"])
        df["close"] = df["close"].astype(float)
        df = df.iloc[::-1].reset_index(drop=True)

        rsi_val = ta.rsi(df["close"], length=RSI_LEN)
        l_rsi, h_rsi = rsi_val.rolling(STOCH_LEN).min(), rsi_val.rolling(STOCH_LEN).max()
        stoch_rsi = (rsi_val - l_rsi) / (h_rsi - l_rsi + 1e-10) * 100
        df['k'] = ta.sma(stoch_rsi, length=K_SMOOTH)
        df['d'] = ta.sma(df['k'], length=D_SMOOTH)
        df['ema'] = ta.ema(df["close"], length=EMA_LEN)

        curr, prev = df.iloc[-1], df.iloc[-2]

        # INCROCI PURI (Senza EMA)
        bull_cross = curr['k'] > curr['d'] and prev['k'] <= (prev['d'] + SLACK) and abs(prev['k'] - prev['d']) >= DIST_MIN
        bear_cross = curr['k'] < curr['d'] and prev['k'] >= (prev['d'] - SLACK) and abs(prev['k'] - prev['d']) >= DIST_MIN
        
        return bull_cross, bear_cross, curr['close'], curr['ema']
    except Exception as e:
        log(f"Errore dati: {e}")
        return False, False, 0, 0

def get_pos():
    try:
        res = session.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]
        for p in res:
            size = float(p.get("size", 0))
            if size > 0: return p["side"], size, float(p["avgPrice"])
        return None, 0, 0
    except: return None, 0, 0

# ============================================================
# AZIONI
# ============================================================
def close_position(side, price, qty, entry):
    pnl = (price - entry) * qty if side == "Buy" else (entry - price) * qty
    emoji = "💰" if pnl > 0 else "📉"
    session.place_order(category="linear", symbol=SYMBOL, side="Sell" if side=="Buy" else "Buy", 
                        orderType="Market", qty=f"{qty:.{QTY_PRECISION}f}", reduceOnly=True)
    telegram(f"{emoji} *CHIUSO {side.upper()} (CROSS)*\nPrezzo: `{price:.2f}`\nPNL: `{pnl:.2f} USDT`")

def open_position(side, price):
    qty = math.floor((ORDER_VALUE_USDT / price) / QTY_STEP) * QTY_STEP
    tp = round((price * (1 + TP_PERCENT/100 if side == "Buy" else 1 - TP_PERCENT/100)) / TICK_SIZE) * TICK_SIZE
    sl = round((price * (1 - SL_PERCENT/100 if side == "Buy" else 1 + SL_PERCENT/100)) / TICK_SIZE) * TICK_SIZE

    res = session.place_order(
        category="linear", symbol=SYMBOL, side=side, orderType="Market", qty=f"{qty:.{QTY_PRECISION}f}",
        takeProfit=f"{tp:.{PRICE_PRECISION}f}", stopLoss=f"{sl:.{PRICE_PRECISION}f}", positionIdx=0
    )
    if res["retCode"] == 0:
        telegram(f"🚀 *APERTO {side.upper()}*\nPrezzo: `{price:.2f}`\n🎯 TP: `{tp:.2f}` | 🛡️ SL: `{sl:.2f}`")

# ============================================================
# MAIN LOOP (STEP BY STEP)
# ============================================================
if __name__ == "__main__":
    telegram("🤖 *BOT ETH* | Logica Close-then-Open attiva.")
    while True:
        try:
            now = datetime.now(UTC)
            wait = (30 - (now.minute % 30)) * 60 - now.second + 12
            if wait < 0: wait = 5
            time.sleep(max(wait, 5))

            bull, bear, price, ema = get_market_data()
            side, qty, entry = get_pos()

            # STEP 1: CHECK USCITA (Se incrocia, chiudi a prescindere dall'EMA)
            closed_just_now = False
            if side == "Buy" and bear:
                close_position(side, price, qty, entry)
                closed_just_now = True
                side, qty = None, 0 # Reset locale per permettere eventuale apertura immediata
            elif side == "Sell" and bull:
                close_position(side, price, qty, entry)
                closed_just_now = True
                side, qty = None, 0

            # STEP 2: CHECK ENTRATA (Con filtro EMA)
            if bull and price > ema and side != "Buy":
                open_position("Buy", price)
            elif bear and price < ema and side != "Sell":
                open_position("Sell", price)
            else:
                log(f"Check: {price:.2f} | EMA: {ema:.2f} | Pos: {side}")

        except Exception as e:
            log(f"Errore: {e}")
            time.sleep(20)
