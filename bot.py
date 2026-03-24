import time
import requests
import pandas as pd
import math
from datetime import datetime, timedelta
from pybit.unified_trading import HTTP
import traceback
import random

# ============================================================
# CONFIGURAZIONE (allineata al Pine Script)
# ============================================================

API_KEY       = "26tNwg57oCDvlNidYT"
API_SECRET    = "WQ84S2dhZ9FVoXkJ7WqWCt6F7HSXR4fsrqhH"
TELEGRAM_TOKEN   = "6916198243:AAFTF66uLYSeqviL5YnfGtbUkSjTwPzah6s"
TELEGRAM_CHAT_ID = "820279313"

SYMBOL = "ETHUSDT"
ORDER_VALUE_USDT = 500
INTERVAL = "30"

# Indicatori (esattamente come Pine)
RSI_LENGTH   = 30
STOCH_LENGTH = 30
SMOOTH_K     = 27
SMOOTH_D     = 26

SLACK    = 1.0
DIST_MIN = 0.2          # modificato come nel tuo ultimo Pine
EMA_LENGTH = 14
USE_EMA  = True

TP_PERCENT = 8.4
SL_PERCENT = 2.4

# ============================================================
# CONNESSIONE + UTILITY
# ============================================================

session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

def log(msg):
    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} | {msg}")

def telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10
        )
    except:
        pass

log("=== BOT Stoch RSI Reversal - ALLINEATO AL PINE ===")
telegram("Bot Stoch RSI Reversal avviato\nONE-WAY MODE | EMA14 | NO memoria segnali")

info = session.get_instruments_info(category="linear", symbol=SYMBOL)["result"]["list"][0]
MIN_QTY   = float(info["lotSizeFilter"]["minOrderQty"])
QTY_STEP  = float(info["lotSizeFilter"]["qtyStep"])
TICK_SIZE = float(info["priceFilter"]["tickSize"])

log(f"{SYMBOL} → minQty={MIN_QTY}, qtyStep={QTY_STEP}, tickSize={TICK_SIZE}")

# ============================================================
# FUNZIONI BYBIT CON RETRY
# ============================================================

def bybit_request(func, *args, max_retries=10, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            err_msg = str(e)
            if "10006" in err_msg or "rate limit" in err_msg.lower():
                wait = (2 ** attempt) + random.uniform(0, 1)
                log(f"Rate limit → attendo {wait:.1f}s")
                time.sleep(wait)
                continue
            else:
                log(f"Errore API: {e}")
                time.sleep(5)
    return None

# ============================================================
# CALCOLO QUANTITÀ
# ============================================================

def calc_qty(price):
    qty = ORDER_VALUE_USDT / price
    qty = math.floor(qty / QTY_STEP) * QTY_STEP
    return round(max(qty, MIN_QTY), 8)

# ============================================================
# POSIZIONE (ONE-WAY) - ROBUSTA ANCHE CON MANIPOLAZIONI MANUALI
# ============================================================

def get_current_position():
    try:
        pos = bybit_request(session.get_positions, category="linear", symbol=SYMBOL)
        if not pos or not pos.get("result", {}).get("list"):
            return None, 0.0, 0.0  # side, size, entry_price

        p = pos["result"]["list"][0]
        size = float(p.get("size", 0))
        side = p.get("side") if size != 0 else None
        entry_price = float(p.get("avgPrice", 0)) if size != 0 else 0.0

        return side, size, entry_price
    except:
        return None, 0.0, 0.0

# ============================================================
# CHIUSURA MARKET
# ============================================================

def close_position_market(reason=""):
    side, size, _ = get_current_position()
    if not side or size == 0:
        return False

    close_side = "Sell" if side == "Buy" else "Buy"
    qty_str = str(abs(size)).rstrip("0").rstrip(".")

    log(f"CHIUSURA MARKET {close_side} {qty_str} | Motivo: {reason}")
    telegram(f"POSIZIONE CHIUSA {close_side} {qty_str}\nMotivo: {reason}")

    bybit_request(session.place_order,
                  category="linear",
                  symbol=SYMBOL,
                  side=close_side,
                  orderType="Market",
                  qty=qty_str,
                  reduceOnly=True)
    time.sleep(2)
    return True

# ============================================================
# APERTURA MARKET + TP/SL
# ============================================================

def open_position_market(side):
    ticker = bybit_request(session.get_tickers, category="linear", symbol=SYMBOL)
    if not ticker:
        return False

    price = float(ticker["result"]["list"][0]["lastPrice"])
    qty = calc_qty(price)
    qty_str = str(qty).rstrip("0").rstrip(".")

    log(f"APERTURA {side.upper()} {qty_str} @ {price:.2f}")
    order = bybit_request(session.place_order,
                          category="linear",
                          symbol=SYMBOL,
                          side=side,
                          orderType="Market",
                          qty=qty_str,
                          reduceOnly=False)

    if not order:
        telegram("ERRORE: ordine di apertura fallito!")
        return False

    # TP/SL
    tp = price * (1 + TP_PERCENT/100) if side == "Buy" else price * (1 - TP_PERCENT/100)
    sl = price * (1 - SL_PERCENT/100) if side == "Buy" else price * (1 + SL_PERCENT/100)

    tp = round(tp / TICK_SIZE) * TICK_SIZE
    sl = round(sl / TICK_SIZE) * TICK_SIZE

    bybit_request(session.set_trading_stop,
                  category="linear",
                  symbol=SYMBOL,
                  takeProfit=str(tp),
                  stopLoss=str(sl),
                  tpslMode="Full",
                  tpTriggerBy="LastPrice",
                  slTriggerBy="LastPrice")

    telegram(f"NUOVA POSIZIONE {side.upper()} {qty_str} @ {price:.2f}\nTP {tp:.2f} | SL {sl:.2f}")
    return True

# ============================================================
# CANDELE + INDICATORI (identici al Pine)
# ============================================================

def get_df():
    klines = bybit_request(session.get_kline,
                           category="linear",
                           symbol=SYMBOL,
                           interval=INTERVAL,
                           limit=200)
    if not klines:
        return None

    data = klines["result"]["list"]
    df = pd.DataFrame(data, columns=["ts","open","high","low","close","vol","turnover"])
    df = df.astype({"open":float,"high":float,"low":float,"close":float,"vol":float})
    df = df[::-1].reset_index(drop=True)
    return df

def rsi(series, length):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/length, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ============================================================
# SEGNALI (allineati al Pine - NO memoria)
# ============================================================

def get_signal(df):
    if df is None or len(df) < 100:
        return False, False, False, False

    r = rsi(df["close"], RSI_LENGTH)

    low_r  = r.rolling(STOCH_LENGTH).min()
    high_r = r.rolling(STOCH_LENGTH).max()
    stoch = 100 * (r - low_r) / (high_r - low_r + 1e-10)

    k = stoch.rolling(SMOOTH_K).mean()
    d = k.rolling(SMOOTH_D).mean()

    ema = df["close"].ewm(span=EMA_LENGTH, adjust=False).mean()

    k_now, k_prev = k.iloc[-1], k.iloc[-2]
    d_now, d_prev = d.iloc[-1], d.iloc[-2]
    price = df["close"].iloc[-1]
    ema_price = ema.iloc[-1]

    # Cross fresco come nel Pine (barstate.isconfirmed)
    bull_cross = (k_now > d_now) and (k_prev <= d_prev + SLACK) and abs(k_prev - d_prev) >= DIST_MIN
    bear_cross = (k_now < d_now) and (k_prev >= d_prev - SLACK) and abs(k_prev - d_prev) >= DIST_MIN

    ema_bull_ok = (not USE_EMA) or (price > ema_price)
    ema_bear_ok = (not USE_EMA) or (price < ema_price)

    entry_long  = bull_cross and ema_bull_ok
    entry_short = bear_cross and ema_bear_ok

    exit_long   = bear_cross   # per reverse/close
    exit_short  = bull_cross

    return entry_long, entry_short, exit_long, exit_short

# ============================================================
# ATTESA PROSSIMA CANDELA
# ============================================================

def wait_next_candle():
    now = datetime.utcnow()
    minutes = now.minute

    next_run = now.replace(second=0, microsecond=0)
    if minutes < 30:
        next_run = next_run.replace(minute=30)
    else:
        next_run = next_run.replace(minute=0) + timedelta(hours=1)

    next_run += timedelta(seconds=20)   # piccolo ritardo di sicurezza
    sleep_time = (next_run - datetime.utcnow()).total_seconds()

    if sleep_time > 0:
        log(f"Attesa prossima candela 30m tra {int(sleep_time)} secondi...")
        time.sleep(sleep_time)

# ============================================================
# MAIN LOOP - GESTISCE ANCHE AZIONI MANUALI
# ============================================================

def main_loop():
    side, size, entry_price = get_current_position()

    df = get_df()
    entry_long, entry_short, exit_long, exit_short = get_signal(df)

    pos_str = side if side else "FLAT"
    log(f"Segnali → Entry L:{entry_long} S:{entry_short} | Exit L:{exit_long} S:{exit_short} | Posizione: {pos_str} size={size}")

    # === REVERSE / CLOSE (come nel Pine) ===
    if side == "Buy" and exit_long:
        close_position_market("Bear cross")
        time.sleep(2)
        if entry_short:          # se nello stesso bar c'è anche segnale short
            open_position_market("Sell")
        return

    if side == "Sell" and exit_short:
        close_position_market("Bull cross")
        time.sleep(2)
        if entry_long:
            open_position_market("Buy")
        return

    # === ENTRATA SOLO SE FLAT ===
    if size == 0:
        if entry_long:
            open_position_market("Buy")
        elif entry_short:
            open_position_market("Sell")
        return

    log("Nessuna azione da eseguire")

# ============================================================
# START
# ============================================================

time.sleep(5)
main_loop()

while True:
    try:
        wait_next_candle()
        main_loop()
    except KeyboardInterrupt:
        log("Bot fermato manualmente")
        telegram("Bot fermato manualmente dall'utente")
        break
    except Exception as e:
        log(f"ERRORE CRITICO: {e}")
        log(traceback.format_exc())
        telegram(f"ERRORE CRITICO nel bot:\n{e}")
        time.sleep(60)
