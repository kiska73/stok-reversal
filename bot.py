import time
import requests
import pandas as pd
import math
from datetime import datetime, UTC, timedelta
from pybit.unified_trading import HTTP
import traceback
import random

# ============================================================
# ==================== CONFIGURAZIONE ========================
# ============================================================

API_KEY          = "26tNwg57oCDvlNidYT"
API_SECRET       = "WQ84S2dhZ9FVoXkJ7WqWCt6F7HSXR4fsrqhH"

TELEGRAM_TOKEN   = "6916198243:AAFTF66uLYSeqviL5YnfGtbUkSjTwPzah6s"
TELEGRAM_CHAT_ID = "820279313"

SYMBOL = "ETHUSDT"
ORDER_VALUE_USDT = 1000
INTERVAL = "30"                     # 30 minuti

RSI_LENGTH   = 30
STOCH_LENGTH = 30
SMOOTH_K     = 27
SMOOTH_D     = 26

EMA_LENGTH = 14
USE_EMA    = True

TP_PERCENT = 8.4
SL_PERCENT = 2.4

# ============================================================
# ====================== LOG & TELEGRAM ======================
# ============================================================

def log(msg):
    ts = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
    print(f"{ts} | {msg}")

def telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except:
        pass

# ============================================================
# ====================== SESSIONE BYBIT ======================
# ============================================================

log("=== BOT AVVIATO - MODALITÀ SOLDI VERI ===")
telegram("⚠️ BOT avviato in MODALITÀ SOLDI VERI su ETHUSDT - Una posizione alla volta")

session = HTTP(
    testnet=False,
    demo=False,          # False = soldi veri
    api_key=API_KEY,
    api_secret=API_SECRET,
    recv_window=30000
)

# ============================================================
# ================== INFO STRUMENTO ==========================
# ============================================================

info = session.get_instruments_info(category="linear", symbol=SYMBOL)["result"]["list"][0]
MIN_QTY   = float(info["lotSizeFilter"]["minOrderQty"])
QTY_STEP  = float(info["lotSizeFilter"]["qtyStep"])
TICK_SIZE = float(info["priceFilter"]["tickSize"])

QTY_DECIMALS  = len(str(QTY_STEP).split('.')[1]) if '.' in str(QTY_STEP) else 0
PRICE_DECIMALS = len(str(TICK_SIZE).split('.')[1]) if '.' in str(TICK_SIZE) else 0

log(f"{SYMBOL} → minQty={MIN_QTY} | step={QTY_STEP} | tick={TICK_SIZE}")

# ============================================================
# ====================== FUNZIONI BASE =======================
# ============================================================

def calc_qty(price):
    if price <= 0: return 0.0
    raw = ORDER_VALUE_USDT / price
    qty = math.floor(raw / QTY_STEP) * QTY_STEP
    qty = max(qty, MIN_QTY)
    if qty * price < 5:
        log("❌ Notional troppo basso")
        return 0.0
    return round(qty, QTY_DECIMALS)

def bybit_request(func, *args, max_retries=8, **kwargs):
    for attempt in range(max_retries):
        try:
            result = func(*args, **kwargs)
            if result and result.get("retCode") == 0:
                return result
            raise Exception(f"retCode {result.get('retCode')} - {result.get('retMsg')}")
        except Exception as e:
            wait = (2 ** attempt) + random.uniform(0.5, 1.5)
            log(f"Retry {attempt+1} → {e}")
            time.sleep(wait)
    return None

def has_open_position():
    """Restituisce True se c'è già una posizione aperta su ETHUSDT"""
    try:
        pos = bybit_request(session.get_positions, category="linear", symbol=SYMBOL)
        if not pos:
            return False
        for p in pos["result"]["list"]:
            size = float(p.get("size", 0))
            if size != 0:
                side = p.get("side", "")
                log(f"📍 Posizione aperta rilevata: {side} size={size}")
                return True
        return False
    except:
        return False

# ============================================================
# ====================== INDICATORI ==========================
# ============================================================

def rsi(series, length):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/length, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_df():
    klines = bybit_request(session.get_kline, category="linear", symbol=SYMBOL, interval=INTERVAL, limit=200)
    if not klines: return None
    df = pd.DataFrame(klines["result"]["list"], columns=["ts","open","high","low","close","vol","turnover"])
    df["close"] = df["close"].astype(float)
    return df[::-1].reset_index(drop=True)

def get_signal(df):
    if len(df) < 50:
        return False, False

    r = rsi(df["close"], RSI_LENGTH)
    low_r  = r.rolling(STOCH_LENGTH).min()
    high_r = r.rolling(STOCH_LENGTH).max()
    stoch = 100 * (r - low_r) / (high_r - low_r + 1e-10)

    k = stoch.rolling(SMOOTH_K).mean()
    d = k.rolling(SMOOTH_D).mean()
    ema = df["close"].ewm(span=EMA_LENGTH).mean()

    price = df["close"].iloc[-1]
    ema_price = ema.iloc[-1]

    # Segnale crossover + possibilità sulle candele successive (ultime 3 candele)
    bull = False
    bear = False
    for i in range(1, 4):   # controlla le ultime 3 candele
        if i >= len(k): break
        k_now, k_prev = k.iloc[-i], k.iloc[-i-1]
        d_now, d_prev = d.iloc[-i], d.iloc[-i-1]
        p = df["close"].iloc[-i]
        e = ema.iloc[-i]

        cross_bull = (k_now > d_now) and (k_prev <= d_prev)
        cross_bear = (k_now < d_now) and (k_prev >= d_prev)

        if USE_EMA:
            cross_bull = cross_bull and (p > e)
            cross_bear = cross_bear and (p < e)

        if cross_bull:
            bull = True
        if cross_bear:
            bear = True

    return bull, bear

# ============================================================
# ====================== APERTURA POSIZIONE ==================
# ============================================================

def open_position_market(side):
    if has_open_position():
        log("⛔ C'è già una posizione aperta → non apro nuova")
        return False

    try:
        ticker = bybit_request(session.get_tickers, category="linear", symbol=SYMBOL)
        price = float(ticker["result"]["list"][0]["lastPrice"])
        qty = calc_qty(price)
        if qty <= 0: return False

        qty_str = f"{qty:.{QTY_DECIMALS}f}"
        tp = round(price * (1 + TP_PERCENT/100 if side == "Buy" else 1 - TP_PERCENT/100) / TICK_SIZE) * TICK_SIZE
        sl = round(price * (1 - SL_PERCENT/100 if side == "Buy" else 1 + SL_PERCENT/100) / TICK_SIZE) * TICK_SIZE

        log(f"📤 Invio ordine REAL → {side} {qty_str} @ mercato")

        order = bybit_request(
            session.place_order,
            category="linear", symbol=SYMBOL, side=side, orderType="Market", qty=qty_str,
            takeProfit=f"{tp:.{PRICE_DECIMALS}f}", stopLoss=f"{sl:.{PRICE_DECIMALS}f}",
            tpslMode="Full", tpTriggerBy="LastPrice", slTriggerBy="LastPrice", positionIdx=0
        )

        if order:
            log(f"✅ ORDINE ESEGUITO → {side} {qty_str}")
            telegram(f"🟢 {side.upper()} {qty_str} ETHUSDT @ {price:.2f} (TP {TP_PERCENT}% | SL {SL_PERCENT}%)")
            return True
        return False
    except Exception as e:
        log(f"❌ Errore apertura: {e}")
        return False

# ============================================================
# ====================== TIMING CANDela 30m +20s =============
# ============================================================

def wait_for_next_candle():
    """Aspetta esattamente la chiusura della prossima candela 30m + 20 secondi"""
    now = datetime.now(UTC)
    # Prossima chiusura candela 30m
    minutes = (now.minute // 30) * 30 + 30
    next_candle = now.replace(minute=minutes % 60, second=0, microsecond=0)
    if minutes >= 60:
        next_candle += timedelta(hours=1)

    wait_seconds = (next_candle - now).total_seconds() + 20  # +20 secondi di sicurezza
    if wait_seconds < 0:
        wait_seconds += 1800  # sicurezza

    log(f"⏳ Aspetto chiusura candela 30m +20s → sleep {wait_seconds/60:.1f} minuti")
    time.sleep(wait_seconds)

# ============================================================
# ====================== LOOP PRINCIPALE =====================
# ============================================================

if __name__ == "__main__":
    log("=== BOT IN ESECUZIONE - Scan ogni chiusura 30m +20s ===")

    while True:
        try:
            wait_for_next_candle()          # Aspetta chiusura candela +20s

            df = get_df()
            if df is None or len(df) < 50:
                log("❌ Dati insufficienti")
                continue

            long_sig, short_sig = get_signal(df)

            if long_sig and not has_open_position():
                log("🟢 Segnale LONG valido → apro posizione")
                open_position_market("Buy")
            elif short_sig and not has_open_position():
                log("🔴 Segnale SHORT valido → apro posizione")
                open_position_market("Sell")
            else:
                if has_open_position():
                    log("📍 Posizione aperta → tengo (TP/SL gestiti da Bybit)")
                else:
                    log("⏳ Nessun segnale valido")

        except Exception as e:
            log(f"CRASH: {e}")
            telegram(f"⚠️ BOT crash: {e}")
            time.sleep(30)
