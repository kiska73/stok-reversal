import time
import requests
import pandas as pd
import math
from datetime import datetime, UTC
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
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10
        )
    except:
        pass

# ============================================================
# ====================== AVVIO =========================
# ============================================================

log("=== BOT AVVIATO - MODALITÀ SOLDI VERI ===")
telegram("⚠️ BOT avviato in MODALITÀ SOLDI VERI su ETHUSDT")

try:
    session = HTTP(
        testnet=False,
        demo=False,          # False = soldi veri
        api_key=API_KEY,
        api_secret=API_SECRET,
        recv_window=30000
    )
    log("✅ Sessione creata in modalità REAL (mainnet)")

    # Test connessione
    ticker = session.get_tickers(category="linear", symbol=SYMBOL)
    price = float(ticker["result"]["list"][0]["lastPrice"])
    log(f"✅ Connesso a Bybit REAL - Prezzo ETHUSDT: {price:.2f} USDT")

except Exception as e:
    log(f"❌ ERRORE ALL'AVVIO: {e}")
    telegram(f"❌ BOT non avviato - Errore: {e}")
    print(traceback.format_exc())
    exit(1)

# ============================================================
# ================== INFO STRUMENTO ==========================
# ============================================================

info = session.get_instruments_info(category="linear", symbol=SYMBOL)["result"]["list"][0]

MIN_QTY   = float(info["lotSizeFilter"]["minOrderQty"])
QTY_STEP  = float(info["lotSizeFilter"]["qtyStep"])
TICK_SIZE = float(info["priceFilter"]["tickSize"])

QTY_DECIMALS  = len(str(QTY_STEP).split('.')[1]) if '.' in str(QTY_STEP) else 0
PRICE_DECIMALS = len(str(TICK_SIZE).split('.')[1]) if '.' in str(TICK_SIZE) else 0

log(f"{SYMBOL} → minQty={MIN_QTY} | qtyStep={QTY_STEP} | tickSize={TICK_SIZE}")

# ============================================================
# ====================== CALCOLO QTY =========================
# ============================================================

def calc_qty(price):
    if price <= 0:
        return 0.0
    raw_qty = ORDER_VALUE_USDT / price
    qty = math.floor(raw_qty / QTY_STEP) * QTY_STEP
    qty = max(qty, MIN_QTY)

    if qty * price < 5:
        log("❌ Notional troppo basso (< 5 USDT)")
        return 0.0

    return round(qty, QTY_DECIMALS)

# ============================================================
# ====================== RETRY WRAPPER =======================
# ============================================================

def bybit_request(func, *args, max_retries=8, **kwargs):
    for attempt in range(max_retries):
        try:
            result = func(*args, **kwargs)
            if result and result.get("retCode") == 0:
                return result
            else:
                raise Exception(f"retCode: {result.get('retCode')} - {result.get('retMsg')}")
        except Exception as e:
            wait = (2 ** attempt) + random.uniform(0.5, 1.5)
            log(f"Retry {attempt+1} → {e} | attesa {wait:.1f}s")
            time.sleep(wait)
    log("❌ Tutte le retry fallite")
    return None

# ============================================================
# ====================== APERTURA POSIZIONE ==================
# ============================================================

def open_position_market(side):
    try:
        ticker = bybit_request(session.get_tickers, category="linear", symbol=SYMBOL)
        if not ticker:
            return False

        price = float(ticker["result"]["list"][0]["lastPrice"])
        qty = calc_qty(price)
        if qty <= 0:
            return False

        qty_str = f"{qty:.{QTY_DECIMALS}f}"

        tp_raw = price * (1 + TP_PERCENT/100) if side == "Buy" else price * (1 - TP_PERCENT/100)
        sl_raw = price * (1 - SL_PERCENT/100) if side == "Buy" else price * (1 + SL_PERCENT/100)

        tp = round(tp_raw / TICK_SIZE) * TICK_SIZE
        sl = round(sl_raw / TICK_SIZE) * TICK_SIZE

        log(f"📤 Invio ordine REAL → {side} {qty_str} ETHUSDT @ mercato")

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
            log(f"✅ ORDINE ESEGUITO → {side} {qty_str} | OrderID: {order['result']['orderId']}")
            telegram(f"🟢 {side.upper()} {qty_str} ETHUSDT @ {price:.2f} (TP {TP_PERCENT}% | SL {SL_PERCENT}%)")
            return True
        return False

    except Exception as e:
        log(f"❌ Errore ordine: {e}")
        telegram(f"❌ Errore ordine {side}: {e}")
        return False

# ============================================================
# ====================== INDICATORI & SIGNAL =================
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
    if not klines:
        return None
    df = pd.DataFrame(klines["result"]["list"], columns=["ts","open","high","low","close","vol","turnover"])
    df["close"] = df["close"].astype(float)
    return df[::-1].reset_index(drop=True)

def get_signal(df):
    if len(df) < 50:
        return False, False

    r = rsi(df["close"], RSI_LENGTH)
    low_r = r.rolling(STOCH_LENGTH).min()
    high_r = r.rolling(STOCH_LENGTH).max()
    stoch = 100 * (r - low_r) / (high_r - low_r + 1e-10)

    k = stoch.rolling(SMOOTH_K).mean()
    d = k.rolling(SMOOTH_D).mean()
    ema = df["close"].ewm(span=EMA_LENGTH).mean()

    k_now, k_prev = k.iloc[-1], k.iloc[-2]
    d_now, d_prev = d.iloc[-1], d.iloc[-2]
    price = df["close"].iloc[-1]
    ema_price = ema.iloc[-1]

    bull = (k_now > d_now) and (k_prev <= d_prev) and (price > ema_price if USE_EMA else True)
    bear = (k_now < d_now) and (k_prev >= d_prev) and (price < ema_price if USE_EMA else True)

    return bull, bear

# ============================================================
# ====================== LOOP PRINCIPALE =====================
# ============================================================

def main_loop():
    df = get_df()
    if df is None:
        log("❌ Impossibile scaricare i dati kline")
        return

    long_sig, short_sig = get_signal(df)

    if long_sig:
        log("🟢 Segnale LONG rilevato → apro posizione REAL")
        open_position_market("Buy")
    elif short_sig:
        log("🔴 Segnale SHORT rilevato → apro posizione REAL")
        open_position_market("Sell")
    else:
        log("⏳ Nessun segnale al momento")

# ============================================================
# ====================== START =========================
# ============================================================

if __name__ == "__main__":
    log("=== BOT IN ESECUZIONE - Modalità SOLDI VERI ===")
    while True:
        try:
            main_loop()
            time.sleep(60)
        except Exception as e:
            log(f"CRASH: {e}")
            telegram(f"⚠️ BOT crash: {e}")
            time.sleep(30)
