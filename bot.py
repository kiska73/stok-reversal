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
INTERVAL = "30"

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

log("=== BOT AVVIATO - MODALITÀ SOLDI VERI CON INVERSIONI ===")
telegram("⚠️ BOT avviato - Modalità SOLDI VERI con gestione inversioni")

session = HTTP(
    testnet=False,
    demo=False,
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

def get_current_position():
    """Restituisce ('Buy', qty) o ('Sell', qty) o (None, 0)"""
    try:
        pos = bybit_request(session.get_positions, category="linear", symbol=SYMBOL)
        if not pos:
            return None, 0.0
        for p in pos["result"]["list"]:
            size = float(p.get("size", 0))
            if size > 0:
                side = p.get("side")  # "Buy" o "Sell"
                log(f"📍 Posizione aperta: {side} | size = {size}")
                return side, size
        return None, 0.0
    except Exception as e:
        log(f"Errore lettura posizione: {e}")
        return None, 0.0

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

    for i in range(1, 4):   # ultime 3 candele
        if i >= len(k): break
        k_now, k_prev = k.iloc[-i], k.iloc[-i-1]
        d_now, d_prev = d.iloc[-i], d.iloc[-i-1]
        price = df["close"].iloc[-i]
        ema_price = ema.iloc[-i]

        cross_bull = (k_now > d_now) and (k_prev <= d_prev)
        cross_bear = (k_now < d_now) and (k_prev >= d_prev)

        if USE_EMA:
            cross_bull = cross_bull and (price > ema_price)
            cross_bear = cross_bear and (price < ema_price)

        if cross_bull:
            return True, False
        if cross_bear:
            return False, True

    return False, False

# ============================================================
# ====================== CHIUSURA POSIZIONE ==================
# ============================================================

def close_position(side, qty):
    """Chiude la posizione corrente inviando un ordine market nella direzione opposta"""
    try:
        close_side = "Sell" if side == "Buy" else "Buy"
        qty_str = f"{qty:.{QTY_DECIMALS}f}"

        log(f"🔄 Chiusura posizione {side} → invio ordine {close_side} {qty_str}")

        order = bybit_request(
            session.place_order,
            category="linear",
            symbol=SYMBOL,
            side=close_side,
            orderType="Market",
            qty=qty_str,
            positionIdx=0,
            reduceOnly=True
        )

        if order:
            log(f"✅ Posizione {side} chiusa con successo")
            telegram(f"🔴 Chiusa posizione {side} | Aperto {close_side}")
            return True
        return False
    except Exception as e:
        log(f"❌ Errore chiusura posizione: {e}")
        return False

# ============================================================
# ====================== APERTURA POSIZIONE ==================
# ============================================================

def open_position_market(side):
    try:
        ticker = bybit_request(session.get_tickers, category="linear", symbol=SYMBOL)
        price = float(ticker["result"]["list"][0]["lastPrice"])
        qty = calc_qty(price)
        if qty <= 0:
            return False

        qty_str = f"{qty:.{QTY_DECIMALS}f}"
        tp = round(price * (1 + TP_PERCENT/100 if side == "Buy" else 1 - TP_PERCENT/100) / TICK_SIZE) * TICK_SIZE
        sl = round(price * (1 - SL_PERCENT/100 if side == "Buy" else 1 + SL_PERCENT/100) / TICK_SIZE) * TICK_SIZE

        log(f"📤 Apertura nuova posizione → {side} {qty_str}")

        order = bybit_request(
            session.place_order,
            category="linear",
            symbol=SYMBOL,
            side=side,
            orderType="Market",
            qty=qty_str,
            takeProfit=f"{tp:.{PRICE_DECIMALS}f}",
            stopLoss=f"{sl:.{PRICE_DECIMALS}f}",
            tpslMode="Full",
            tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice",
            positionIdx=0
        )

        if order:
            log(f"✅ Posizione {side} aperta con successo")
            telegram(f"🟢 {side.upper()} {qty_str} ETHUSDT @ {price:.2f} (TP {TP_PERCENT}% | SL {SL_PERCENT}%)")
            return True
        return False
    except Exception as e:
        log(f"❌ Errore apertura: {e}")
        return False

# ============================================================
# ====================== TIMING CANDELA ======================
# ============================================================

def wait_for_next_candle():
    now = datetime.now(UTC)
    if now.minute < 30:
        next_candle = now.replace(minute=30, second=0, microsecond=0)
    else:
        next_candle = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    wait_seconds = (next_candle - now).total_seconds() + 20
    next_time_str = next_candle.strftime('%H:%M:%S')
    log(f"⏳ Prossima scansione alle {next_time_str} UTC (+20s) → attesa {wait_seconds:.0f}s")
    time.sleep(wait_seconds)

# ============================================================
# ====================== LOOP PRINCIPALE =====================
# ============================================================

if __name__ == "__main__":
    log("=== BOT IN ESECUZIONE - Gestione inversioni attiva ===")

    while True:
        try:
            wait_for_next_candle()

            df = get_df()
            if df is None or len(df) < 50:
                log("❌ Dati insufficienti")
                continue

            long_sig, short_sig = get_signal(df)
            current_side, current_qty = get_current_position()

            if current_side is None:
                # Nessuna posizione → apri normalmente
                if long_sig:
                    log("🟢 Segnale LONG → nessuna posizione → APERTURA")
                    open_position_market("Buy")
                elif short_sig:
                    log("🔴 Segnale SHORT → nessuna posizione → APERTURA")
                    open_position_market("Sell")
                else:
                    log("⏳ Nessun segnale")

            else:
                # C'è già una posizione → gestisci inversione
                if (current_side == "Buy" and short_sig):
                    log("🔄 Inversione rilevata: LONG → SHORT")
                    telegram("🔄 Inversione LONG → SHORT")
                    if close_position(current_side, current_qty):
                        open_position_market("Sell")

                elif (current_side == "Sell" and long_sig):
                    log("🔄 Inversione rilevata: SHORT → LONG")
                    telegram("🔄 Inversione SHORT → LONG")
                    if close_position(current_side, current_qty):
                        open_position_market("Buy")

                else:
                    log(f"📍 Posizione {current_side} confermata → nessun segnale opposto")

        except Exception as e:
            log(f"CRASH: {e}")
            telegram(f"⚠️ BOT crash: {e}")
            time.sleep(30)
