
"""
ETH V6 CAPPED - Render 24/7 Version
Runs Flask on port 10000 (Render needs web) + trading loop in background thread
Keeps bot alive even when you close PC
"""
import threading, time, os, sys, requests, json, hmac, hashlib, csv
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify

# ========= CONFIG =========
SYMBOL = "ETHUSD"
PRODUCT_ID = 1699  # ETHUSD on Delta India Demo
QTY = 60  # 60 contracts = ~$145 notional for $100 account
LEVERAGE = 2
CAPPED_EQUITY = 12000
FIXED_NOTIONAL = 10000

DELTA_BASE = "https://cdn-ind.testnet.deltaex.org"
CANDLE_URL = f"https://api.india.delta.exchange/v2/history/candles"

app = Flask(__name__)

# Load keys from ENV (Render) or files (local)
def load_keys():
    key = os.getenv("DELTA_API_KEY","").strip()
    sec = os.getenv("DELTA_API_SECRET","").strip()
    if not key and os.path.exists("api_key.txt"):
        key = open("api_key.txt").read().strip()
    if not sec and os.path.exists("api_secret.txt"):
        sec = open("api_secret.txt").read().strip()
    return key, sec

API_KEY, API_SECRET = load_keys()

def sig(secret, msg):
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

def log_trade(side, price, sl, tp, pnl="", reason=""):
    with open("forward_trades.csv","a", newline="") as f:
        w=csv.writer(f)
        if os.path.getsize("forward_trades.csv")==0 if os.path.exists("forward_trades.csv") else True:
            pass
        w.writerow([datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), side, f"{price:.2f}", f"{sl:.2f}" if isinstance(sl,(int,float)) else sl, f"{tp:.2f}" if isinstance(tp,(int,float)) else tp, pnl, reason])

# ensure csv header
if not os.path.exists("forward_trades.csv"):
    open("forward_trades.csv","w").write("time,side,price,sl,tp,pnl,reason\n")

# ========= INDICATORS (same as TradingView) =========
def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def rsi(series, length=14):
    delta = series.diff()
    gain = delta.where(delta>0,0).rolling(length).mean()
    loss = -delta.where(delta<0,0).rolling(length).mean()
    rs = gain/loss
    return 100 - (100/(1+rs))

def atr(high, low, close, length=14):
    tr = (high - low).abs()
    tr = tr.where(tr > (high - close.shift()).abs(), (high - close.shift()).abs())
    tr = tr.where(tr > (low - close.shift()).abs(), (low - close.shift()).abs())
    return tr.rolling(length).mean()

def fetch_candles():
    # Get last 1000 1H candles
    end = int(time.time())
    start = end - 1000*3600
    params = {"resolution":"1h","symbol":SYMBOL,"start":start,"end":end}
    r = requests.get(CANDLE_URL, params=params, timeout=15)
    data = r.json()
    if "result" not in data:
        raise Exception(f"Candle fetch failed {data}")
    import pandas as pd
    df = pd.DataFrame(data["result"])
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.sort_values("time")
    return df

def place_order(side, size):
    path="/v2/orders"
    url=DELTA_BASE+path
    ts=str(int(time.time()))
    body={"product_id": PRODUCT_ID, "size": int(size), "side": side, "order_type":"market_order","time_in_force":"ioc"}
    body_str=json.dumps(body)
    signature=sig(API_SECRET, "POST"+ts+path+body_str)
    headers={"api-key": API_KEY, "timestamp": ts, "signature": signature, "Content-Type":"application/json"}
    r=requests.post(url, headers=headers, data=body_str, timeout=15)
    print(f"Order {side} {size}: {r.status_code} {r.text[:500]}")
    return r.status_code==200

# ========= MAIN LOOP =========
last_status = {"last_candle": "waiting", "pos": "FLAT", "next_wake": "", "last_trade": "none", "balance": "?"}

def trading_loop():
    global last_status
    import pandas as pd
    position=None
    print("ETH V6 RENDER BOT STARTED - waiting for candle close +70s", flush=True)
    last_status["next_wake"]="starting..."
    while True:
        try:
            now = datetime.now(timezone.utc)
            # wait until 1m10s after hour
            next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            wake = next_hour + timedelta(seconds=70)
            # if we just started and we are past 1m10s, run immediately
            if now.minute==0 and now.second<70:
                wake = now + timedelta(seconds=10)
            secs = (wake - now).total_seconds()
            if secs>0:
                last_status["next_wake"]=wake.strftime("%H:%M:%S UTC")
                print(f"Sleep {secs/60:.1f} min until {last_status['next_wake']}", flush=True)
                time.sleep(min(secs, 300))
                continue

            # fetch candles
            df = fetch_candles()
            if len(df)<900:
                print(f"Not enough candles {len(df)}")
                time.sleep(60)
                continue

            # indicators
            df["ema21"]=ema(df["close"],21)
            df["ema50"]=ema(df["close"],50)
            df["ema800"]=ema(df["close"],800)
            df["rsi"]=rsi(df["close"],14)
            df["atr"]=atr(df["high"], df["low"], df["close"],14)
            # ADX simple proxy: using RSI of DX
            # For simplicity use same logic as V6: ADX>12 check using DMI 14
            # Approximate: ADX = 100*EMA(|+DI - -DI| / (+DI+-DI))
            # We'll skip exact ADX and allow >12 always for render version to avoid pandas_ta dep
            # But we keep logic: we will compute ADX via manual
            high=df["high"]; low=df["low"]; close=df["close"]
            plus_dm = high.diff(); minus_dm = -low.diff()
            plus_dm = plus_dm.where((plus_dm>minus_dm)&(plus_dm>0),0)
            minus_dm = minus_dm.where((minus_dm>plus_dm)&(minus_dm>0),0)
            tr = atr(high,low,close,14)
            plus_di = 100*(plus_dm.ewm(span=14).mean()/tr)
            minus_di = 100*(minus_dm.ewm(span=14).mean()/tr)
            dx = 100*( (plus_di-minus_di).abs() / (plus_di+minus_di).replace(0,1) )
            df["adx"] = dx.ewm(span=14).mean()

            row = df.iloc[-2]  # last CLOSED candle
            prev_close = row["close"]
            ema800=row["ema800"]; ema21=row["ema21"]; ema50=row["ema50"]
            rsi_v=row["rsi"]; adx_v=row["adx"]; atr_v=row["atr"]
            low_v=row["low"]; high_v=row["high"]

            long_cond = (prev_close>ema800) and (low_v<=ema21) and (prev_close>ema21) and (prev_close>ema50) and (43<=rsi_v<=58) and (adx_v>12)
            short_cond = (prev_close<ema800) and (high_v>=ema21) and (prev_close<ema21) and (prev_close<ema50) and (42<=rsi_v<=57) and (adx_v>12)

            last_status["last_candle"]=f"{row['time']} C={prev_close:.2f} EMA800={ema800:.2f} RSI={rsi_v:.1f} ADX={adx_v:.1f} Long={long_cond} Short={short_cond}"

            # capped qty logic
            equity = 117.30 # default, will update if env set
            try:
                # try fetch wallet
                path="/v2/wallet/balances"
                ts=str(int(time.time()))
                signature=sig(API_SECRET, "GET"+ts+path)
                headers={"api-key": API_KEY, "timestamp": ts, "signature": signature}
                rb=requests.get(DELTA_BASE+path, headers=headers, timeout=10)
                if rb.status_code==200:
                    bals=rb.json().get("result",[])
                    for b in bals:
                        if b.get("asset_symbol")=="USD":
                            equity=float(b.get("wallet_balance",117.3))
                            last_status["balance"]=equity
            except Exception as e:
                print(f"Balance fetch err {e}")

            # qty capped logic
            if equity>=CAPPED_EQUITY:
                qty = int(FIXED_NOTIONAL * LEVERAGE / prev_close)
            else:
                qty = int(equity * 0.9 * LEVERAGE / prev_close)
            qty = max(qty, 1)
            if qty>60 and equity<CAPPED_EQUITY:
                qty=60  # limit for small demo

            # trailing logic (same as V6)
            if position:
                # check exit
                if position["side"]=="LONG":
                    # update trailing
                    if prev_close>position["highest"]:
                        position["highest"]=prev_close
                    eff_sl = position["highest"] - 1.4*atr_v + 0.8*atr_v # simplified
                    # TP/SL
                    if low_v <= eff_sl:
                        print(f"Exit LONG SL {eff_sl}")
                        place_order("sell", qty)
                        log_trade("EXIT_LONG", prev_close, eff_sl, position["tp"], reason="SL")
                        position=None
                        last_status["pos"]="FLAT"
                    elif high_v >= position["tp"]:
                        print(f"Exit LONG TP {position['tp']}")
                        place_order("sell", qty)
                        log_trade("EXIT_LONG", prev_close, position["sl"], position["tp"], reason="TP")
                        position=None
                        last_status["pos"]="FLAT"
                else:
                    if prev_close<position["lowest"]:
                        position["lowest"]=prev_close
                    eff_sl = position["lowest"] + 1.4*atr_v - 0.8*atr_v
                    if high_v >= eff_sl:
                        print(f"Exit SHORT SL {eff_sl}")
                        place_order("buy", qty)
                        log_trade("EXIT_SHORT", prev_close, eff_sl, position["tp"], reason="SL")
                        position=None
                        last_status["pos"]="FLAT"
                    elif low_v <= position["tp"]:
                        print(f"Exit SHORT TP {position['tp']}")
                        place_order("buy", qty)
                        log_trade("EXIT_SHORT", prev_close, position["sl"], position["tp"], reason="TP")
                        position=None
                        last_status["pos"]="FLAT"

            if not position:
                if long_cond:
                    sl = ema50 - 0.5*atr_v
                    tp = prev_close + 2.6*atr_v
                    print(f">>> LONG signal @ {prev_close} SL {sl:.2f} TP {tp:.2f} QTY {qty}")
                    if place_order("buy", qty):
                        log_trade("BUY_ENTRY", prev_close, sl, tp, reason="LONG")
                        position={"side":"LONG","entry":prev_close,"sl":sl,"tp":tp,"highest":prev_close,"lowest":prev_close}
                        last_status["pos"]="LONG"
                        last_status["last_trade"]=f"LONG {prev_close}"
                elif short_cond:
                    sl = ema50 + 0.5*atr_v
                    tp = prev_close - 2.6*atr_v
                    print(f">>> SHORT signal @ {prev_close} SL {sl:.2f} TP {tp:.2f} QTY {qty}")
                    if place_order("sell", qty):
                        log_trade("SELL_ENTRY", prev_close, sl, tp, reason="SHORT")
                        position={"side":"SHORT","entry":prev_close,"sl":sl,"tp":tp,"highest":prev_close,"lowest":prev_close}
                        last_status["pos"]="SHORT"
                        last_status["last_trade"]=f"SHORT {prev_close}"

        except Exception as e:
            print(f"Loop error: {e}")
            import traceback; traceback.print_exc()
        # sleep to next hour
        time.sleep(65)

@app.route("/")
def home():
    # ensure bot thread is running in THIS gunicorn worker
    start_bot()
    return jsonify({
        "status":"ETH V6 CAPPED BOT RUNNING",
        "symbol":SYMBOL,
        "last_candle":last_status.get("last_candle"),
        "position":last_status.get("pos"),
        "next_wake":last_status.get("next_wake"),
        "last_trade":last_status.get("last_trade"),
        "balance":last_status.get("balance"),
        "api_key_set": bool(API_KEY),
        "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    })

@app.route("/trades")
def trades():
    if not os.path.exists("forward_trades.csv"):
        return "no trades yet"
    return open("forward_trades.csv").read(), 200, {"Content-Type":"text/plain"}

# START THREAD FOR BOTH LOCAL AND GUNICORN (Render)
def start_bot():
    if not getattr(app, 'bot_started', False):
        app.bot_started = True
        t = threading.Thread(target=trading_loop, daemon=True)
        t.start()
        print(">>> TRADING THREAD STARTED - gunicorn compatible", flush=True)

start_bot()

if __name__=="__main__":
    port=int(os.getenv("PORT",10000))
    app.run(host="0.0.0.0", port=port)

