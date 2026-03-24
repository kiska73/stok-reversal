import time
import requests
import pandas as pd
import math
from datetime import datetime, timedelta, UTC
from pybit.unified_trading import HTTP
import traceback
import random

# ============================================================
# CONFIGURAZIONE
# ============================================================

# 🚨 INSERISCI QUI LE TUE NUOVE CHIAVI (NON USARE QUELLE VECCHIE)
API_KEY          = "26tNwg57oCDvlNidYT"
API_SECRET       = "WQ84S2dhZ9FVoXkJ7WqWCt6F7HSXR4fsrqhH"
TELEGRAM_TOKEN   = "6916198243:AAFTF66uLYSeqviL5YnfGtbUkSjTwPzah6s"
TELEGRAM_CHAT_ID = "820279313"

SYMBOL = "ETHUSDT"
ORDER_VALUE_USDT = 1000
INTERVAL = "30"

# Indicatori (allineati al Pine Script)
RSI_LENGTH   = 30
STOCH_LENGTH = 30
SMOOTH_K     = 27
SMOOTH_D     = 26

SLACK    = 1.0
DIST_MIN = 0.2
EMA_LENGTH = 14
USE_EMA  = True

TP_PERCENT = 8.4
SL_PERCENT = 2.4

# ============================================================
# CONNESSIONE E LOG UTILS
# ============================================================

session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET, recv_window=30000)

def log(msg):
    ts = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
    print(f"{ts} | {msg}")
    with open("bot_debug.log", "a", encoding="utf-8") as f:
        f.write(f"{ts} | {msg}\n")

def telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        log(f"Errore Telegram: {e}")

# ====================== AVVIO ======================
log("=== BOT Stoch RSI Reversal - DEBUG COMPLETO ATTIVATO ===")
telegram("🚀 Bot avviato\n"
         f"TP {TP_PERCENT}% | SL {SL_PERCENT}%")

info = session.get_instruments_info(category="linear", symbol=SYMBOL)["result"]["list"][0]
MIN_QTY   = float(info["lotSizeFilter"]["minOrderQty"])
QTY_STEP  = float(info["lotSizeFilter"]["qtyStep"])
TICK_SIZE = float(info["priceFilter"]["tickSize"])

log(f"{SYMBOL} → minQty={MIN_QTY} | qtyStep={QTY_STEP} | tickSize={TICK_SIZE}")

# ============================================================
# CALCOLO QTY + BYBIT REQUEST
# ============================================================

def calc_qty(price):
    if price <= 0:
        return 0.0
    raw_qty = ORDER_VALUE_USDT / price
    qty = math.floor(raw_qty / QTY_STEP) * QTY_STEP
    qty = max(qty, MIN_QTY)
    notional = qty * price
    if notional < 5:
        log(f"❌ Notional troppo basso: {notional:.2f}$")
        return 0.0
    final_qty = round(qty, 8)
    log(f"🔢 Calc Qty → Prezzo {price:.2f} | Raw {raw_qty:.6f} → Qty {final_qty} | Notional {notional:.2f}$")
    return final_qty

def bybit_request(func, *args, max_retries=15, **kwargs):
    for attempt in range(max_retries):
        try:
            result = func(*args, **kwargs)
            if isinstance(result, dict) and result.get("retCode") != 0:
                raise Exception(f"Bybit Error {result['retCode']}: {result.get('retMsg')}")
            return result
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ["rate limit", "timeout", "10006", "110007", "170131", "insufficient"]):
                wait = (2 ** attempt) + random.uniform(0.5, 2)
                log(f"⏳ Rate limit / balance error → attendo {wait:.1f}s (tentativo {attempt+1})")
                time.sleep(wait)
                continue
            log(f"Errore API: {e}")
            time.sleep(3)
    return None

# ============================================================
# APERTURA POSIZIONE (TP/SL in una sola chiamata)
# ============================================================

def open_position_market(side):
    try:
        ticker = bybit_request(session.get_tickers, category="linear", symbol=SYMBOL)
        price = float(ticker["result"]["list"][0]["lastPrice"])

        qty = calc_qty(price)
        if qty <= 0:
            telegram("❌ Qty = 0 → ordine annullato")
            return False

        # Formattazione sicura della quantità
        qty_str = f"{qty:g}"

        # Calcolo TP e SL grezzi
        tp_raw = price * (1 + TP_PERCENT/100) if side == "Buy" else price * (1 - TP_PERCENT/100)
        sl_raw = price * (1 - SL_PERCENT/100) if side == "Buy" else price * (1 + SL_PERCENT/100)
        
        # Calcolo decimali richiesti dal TICK_SIZE
        decimals = len(str(TICK_SIZE).split('.')[1]) if '.' in str(TICK_SIZE) else 0
        
        # Arrotondamento ai tick size
        tp = round(tp_raw / TICK_SIZE) * TICK_SIZE
        sl = round(sl_raw / TICK_SIZE) * TICK_SIZE
        
        # Formattazione stringhe con i decimali esatti
        tp_str = f"{tp:.{decimals}f}"
        sl_str = f"{sl:.{decimals}f}"

        log(f"🟢 Tentativo apertura {side.upper()} | Prezzo {price:.2f} | Qty {qty_str}")

        order_params = {
            "category": "linear",
            "symbol": SYMBOL,
            "side": side,
            "orderType": "Market",
            "qty": qty_str,
            "takeProfit": tp_str,
            "stopLoss": sl_str,
            "tpslMode": "Full",
            "tpTriggerBy": "LastPrice",
            "slTriggerBy": "LastPrice",
            "positionIdx": 0  # Fondamentale per la modalità One-Way
        }

        log(f"📤 Parametri inviati a Bybit: {order_params}")

        order = bybit_request(session.place_order, **order_params)

        if not order or order.get("retCode") != 0:
            ret_code = order.get("retCode") if order else "None"
            ret_msg = order.get("retMsg", "No message") if order else "place_order returned None"
            log(f"❌ Bybit rifiutato → retCode={ret_code} | retMsg={ret_msg}")
            telegram(f"❌ Ordine rifiutato\nretCode: {ret_code}\n{ret_msg}")
            return False

        log(f"✅ POSIZIONE APERTA CON SUCCESSO! OrderId: {order['result']['orderId']}")
        telegram(f"🟢 NUOVA POSIZIONE {side.upper()}\nQty: {qty_str} @ {price:.2f}\nTP: {tp:.2f} | SL: {sl:.2f}")
        return True

    except Exception as e:
        log(f"❌ Eccezione durante apertura: {e}")
        telegram(f"❌ Eccezione apertura:\n{e}")
        traceback.print_exc()
        return False

# ============================================================
# INDICATORI E SEGNALE
# ============================================================

def get_df():
    klines = bybit_request(session.get_kline, category="linear", symbol=SYMBOL, interval=INTERVAL, limit=200)
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

def get_signal(df):
    if df is None or len(df) < 100:
        log("❌ get_signal: df troppo piccolo o None")
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
    price     = df["close"].iloc[-1]
    ema_price = ema.iloc[-1]

    log(f"DEBUG SIGNAL | k_now={k_now:.4f}  k_prev={k_prev:.4f}  |  d_now={d_now:.4f}  d_prev={d_prev:.4f}")
    log(f"DEBUG SIGNAL | Distanza cross = {abs(k_prev - d_prev):.4f}  (min richiesta: {DIST_MIN})")
    log(f"DEBUG SIGNAL | EMA check → Price={price:.2f}  EMA={ema_price:.2f}  → Bull ok: {price > ema_price}  |  Bear ok: {price < ema_price}")

    bull_cross = (k_now > d_now) and (k_prev <= d_prev + SLACK) and abs(k_prev - d_prev) >= DIST_MIN
    bear_cross = (k_now < d_now) and (k_prev >= d_prev - SLACK) and abs(k_prev - d_prev) >= DIST_MIN

    ema_bull_ok = (not USE_EMA) or (price > ema_price)
    ema_bear_ok = (not USE_EMA) or (price < ema_price)

    entry_long  = bull_cross and ema_bull_ok
    entry_short = bear_cross and ema_bear_ok

    log(f"✅ SEGNALE FINALE → entry_long={entry_long}  |  entry_short={entry_short}  |  bear_cross={bear_cross}  |  ema_bear_ok={ema_bear_ok}")

    return entry_long, entry_short, bear_cross, bull_cross

# ============================================================
# POSIZIONE + CHIUSURA + ATTESA
# ============================================================

def get_current_position():
    try:
        pos = bybit_request(session.get_positions, category="linear", symbol=SYMBOL)
        if not pos or not pos.get("result", {}).get("list"):
            return None, 0.0, 0.0
        p = pos["result"]["list"][0]
        size = float(p.get("size", 0))
        side = p.get("side") if size != 0 else None
        return side, size, float(p.get("avgPrice", 0))
    except:
        return None, 0.0, 0.0

def close_position_market(reason=""):
    side, size, _ = get_current_position()
    if not side or size == 0:
        return False
    close_side = "Sell" if side == "Buy" else "Buy"
    
    # Formattazione sicura
    qty_str = f"{abs(size):g}"
    
    log(f"🔴 CHIUSURA MARKET {close_side} {qty_str} | Motivo: {reason}")
    telegram(f"🔴 POSIZIONE CHIUSA\n{close_side} {qty_str}\nMotivo: {reason}")
    
    bybit_request(session.place_order, category="linear", symbol=SYMBOL,
                  side=close_side, orderType="Market", qty=qty_str, reduceOnly=True,
                  positionIdx=0) # Fondamentale per la modalità One-Way
    time.sleep(2)
    return True

def wait_next_candle():
    now = datetime.now(UTC)
    minutes = now.minute
    next_run = now.replace(second=0, microsecond=0)
    if minutes < 30:
        next_run = next_run.replace(minute=30)
    else:
        next_run = next_run.replace(minute=0) + timedelta(hours=1)
    next_run += timedelta(seconds=20)
    sleep_time = (next_run - datetime.now(UTC)).total_seconds()
    if sleep_time > 0:
        log(f"⏳ Attesa prossima candela 30m tra {int(sleep_time)} secondi...")
        time.sleep(sleep_time)

# ============================================================
# MAIN LOOP
# ============================================================

def main_loop():
    side, size, _ = get_current_position()
    df = get_df()
    if df is None:
        log("❌ Errore: impossibile scaricare i dati delle candele")
        return

    entry_long, entry_short, exit_long, exit_short = get_signal(df)

    pos_str = side if side else "FLAT"
    log(f"Segnali → L:{entry_long} S:{entry_short} | Exit L:{exit_long} S:{exit_short} | Pos: {pos_str} (size={size})")

    if entry_short:
        if size == 0:
            log("🔥 CONDIZIONE SHORT SODDISFATTA + POSIZIONE FLAT → DOVREBBE APRIRE ORA!")
        else:
            log(f"⚠️ Segnale SHORT presente ma posizione ancora aperta (size={size}) → skip apertura")

    if side == "Buy" and exit_long:
        close_position_market("Bear cross")
        time.sleep(2)
        if entry_short:
            open_position_market("Sell")
        return

    if side == "Sell" and exit_short:
        close_position_market("Bull cross")
        time.sleep(2)
        if entry_long:
            open_position_market("Buy")
        return

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

if __name__ == "__main__":
    time.sleep(5)
    main_loop()

    while True:
        try:
            wait_next_candle()
            main_loop()
        except KeyboardInterrupt:
            log("Bot fermato manualmente")
            telegram("🛑 Bot fermato manualmente")
            break
        except Exception as e:
            log(f"ERRORE CRITICO: {e}")
            telegram(f"⚠️ ERRORE CRITICO nel bot:\n{e}")
            traceback.print_exc()
            time.sleep(60)
