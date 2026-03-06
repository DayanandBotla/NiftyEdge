"""
NiftyEdge Pro v2 — Complete Backend
Client ID: 1108455416
Real Dhan WebSocket live price feed + Strategy Engine
Railway-ready + Full Session Persistence
"""
import os, json, time, threading, struct, requests
from datetime import datetime, time as dtime
from flask import Flask, render_template, jsonify, request
from flask_cors import CORS

try:
    import websocket
    WS_OK = True
except ImportError:
    WS_OK = False

app = Flask(__name__)
CORS(app)

# ════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════
CONFIG = {
    "CLIENT_ID":   "1108455416",
    "TOKEN":       os.environ.get("DHAN_TOKEN", ""),
    "MODE":        os.environ.get("TRADE_MODE", "paper"),
    "INDEX":       os.environ.get("INDEX", "NIFTY"),
    "LOTS":        int(os.environ.get("LOTS", "1")),
    "CAPITAL":     float(os.environ.get("CAPITAL", "50000")),
    "RISK_PCT":    float(os.environ.get("RISK_PCT", "1")),
    "SL_PCT":      float(os.environ.get("SL_PCT", "25")),
    "TGT_PCT":     float(os.environ.get("TGT_PCT", "70")),
    "OR_MINUTES":  int(os.environ.get("OR_MINUTES", "15")),
    "GAP_MAX_PCT": float(os.environ.get("GAP_MAX_PCT", "0.5")),
    "VIX_MIN":     float(os.environ.get("VIX_MIN", "10")),
    "VIX_MAX":     float(os.environ.get("VIX_MAX", "22")),
    "TRAIL_EVERY": float(os.environ.get("TRAIL_EVERY", "500")),
}

SECID = {"NIFTY": "13", "BANKNIFTY": "25", "FINNIFTY": "27", "VIX": "1333"}
LOT   = {"NIFTY": 50,   "BANKNIFTY": 15,   "FINNIFTY": 40}

DHAN_API = "https://api.dhan.co"
DHAN_WS  = "wss://api-feed.dhan.co"

# ════════════════════════════════════════════
# PERSISTENCE — survives token refresh & restarts
# ════════════════════════════════════════════
SESSION_FILE = lambda: f"session_{datetime.now().strftime('%Y-%m-%d')}.json"
TRADES_FILE  = lambda: f"trades_{datetime.now().strftime('%Y-%m-%d')}.json"

def save_session():
    """Write critical state to disk. Called after every trade and every 5 alerts."""
    try:
        data = {
            "date":          datetime.now().strftime("%Y-%m-%d"),
            "gross_win":     STATE["gross_win"],
            "gross_loss":    STATE["gross_loss"],
            "daily_loss":    STATE["daily_loss"],
            "signals_count": STATE["signals_count"],
            "blocked_count": STATE["blocked_count"],
            "skipped_count": STATE["skipped_count"],
            "or_high":       STATE["or_high"],
            "or_low":        STATE["or_low"],
            "or_fetched":    STATE["or_fetched"],
            "alerts":        STATE["alerts"][:40],
            "position":      STATE["position"],
            "signal":        STATE["signal"],
            "entry_price":   STATE["entry_price"],
            "sl_price":      STATE["sl_price"],
            "target_price":  STATE["target_price"],
            "trail_high":    STATE["trail_high"],
            "saved_at":      datetime.now().strftime("%H:%M:%S"),
        }
        with open(SESSION_FILE(), "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[PERSIST] save_session failed: {e}")

def save_trade(trade):
    """Immediately append a closed trade to today's trade file."""
    try:
        trades = []
        tf = TRADES_FILE()
        if os.path.exists(tf):
            with open(tf) as f:
                trades = json.load(f)
        trades.append(trade)
        with open(tf, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        print(f"[PERSIST] save_trade failed: {e}")

def load_session():
    """
    On startup: restore today's session from disk.
    Token refresh / server restart / Railway sleep = zero data loss.
    Previous day's file is never touched — new date = fresh start.
    """
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        sf = SESSION_FILE()
        tf = TRADES_FILE()

        if os.path.exists(sf):
            with open(sf) as f:
                data = json.load(f)

            if data.get("date") == today:
                STATE["gross_win"]     = data.get("gross_win", 0)
                STATE["gross_loss"]    = data.get("gross_loss", 0)
                STATE["daily_loss"]    = data.get("daily_loss", 0)
                STATE["signals_count"] = data.get("signals_count", 0)
                STATE["blocked_count"] = data.get("blocked_count", 0)
                STATE["skipped_count"] = data.get("skipped_count", 0)
                STATE["or_high"]       = data.get("or_high")
                STATE["or_low"]        = data.get("or_low")
                STATE["or_fetched"]    = bool(data.get("or_fetched", False))
                STATE["alerts"]        = data.get("alerts", [])
                # Restore open position if any was active
                pos = data.get("position")
                if pos:
                    STATE["position"]     = pos
                    STATE["signal"]       = data.get("signal")
                    STATE["entry_price"]  = data.get("entry_price", 0)
                    STATE["sl_price"]     = data.get("sl_price", 0)
                    STATE["target_price"] = data.get("target_price", 0)
                    STATE["trail_high"]   = data.get("trail_high", 0)
                pnl = STATE["gross_win"] - STATE["gross_loss"]
                print(f"[PERSIST] ✅ Session restored — P&L:₹{pnl:.0f} | Signals:{STATE['signals_count']}")
                add_alert("success",
                    f"Session restored — P&L:₹{pnl:.0f} | OR:{STATE['or_high']}/{STATE['or_low']}")

        if os.path.exists(tf):
            with open(tf) as f:
                STATE["trades"] = json.load(f)
            print(f"[PERSIST] ✅ {len(STATE['trades'])} trades restored from disk.")

    except Exception as e:
        print(f"[PERSIST] load_session failed (fresh start): {e}")

# ════════════════════════════════════════════
# STATE
# ════════════════════════════════════════════
STATE = {
    "ws_connected": False, "token_valid": False, "last_tick": None,
    "nifty": 0.0, "nifty_prev": 0.0, "nifty_open": 0.0,
    "banknifty": 0.0, "vix": 0.0, "ema15": 0.0,
    "candles_15m": [],
    "strategy_on": False, "or_high": None, "or_low": None,
    "or_fetched": False, "sweep_dir": None, "sweep_candles": 0,
    "signal": None, "position": None,
    "entry_price": 0.0, "sl_price": 0.0, "target_price": 0.0, "trail_high": 0.0,
    "trades": [], "alerts": [],
    "gross_win": 0.0, "gross_loss": 0.0, "daily_loss": 0.0,
    "signals_count": 0, "blocked_count": 0, "skipped_count": 0,
}

# ════════════════════════════════════════════
# DHAN REST HELPERS
# ════════════════════════════════════════════
def hdrs():
    return {"access-token": CONFIG["TOKEN"], "client-id": CONFIG["CLIENT_ID"],
            "Content-Type": "application/json", "Accept": "application/json"}

def validate_token():
    if not CONFIG["TOKEN"]:
        STATE["token_valid"] = False
        add_alert("danger", "No DHAN_TOKEN set. Add it to Railway Variables.")
        return False
    try:
        r = requests.get(f"{DHAN_API}/v2/fundlimit", headers=hdrs(), timeout=5)
        if r.status_code == 200:
            STATE["token_valid"] = True
            add_alert("success", f"Token valid — Dhan connected. Client:{CONFIG['CLIENT_ID']}")
            return True
        STATE["token_valid"] = False
        add_alert("danger", f"Token invalid (HTTP {r.status_code}). Update DHAN_TOKEN in Railway.")
        return False
    except Exception as e:
        STATE["token_valid"] = False
        add_alert("danger", f"Token check error: {e}")
        return False

def fetch_prev_close():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        payload = {"securityId": SECID.get(CONFIG["INDEX"],"13"),
                   "exchangeSegment":"IDX_I","instrument":"INDEX",
                   "expiryCode":0,"oi":False,"fromDate":today,"toDate":today}
        r = requests.post(f"{DHAN_API}/v2/charts/eod", headers=hdrs(), json=payload, timeout=5)
        if r.status_code == 200:
            closes = r.json().get("close", [])
            if len(closes) >= 2:
                STATE["nifty_prev"] = closes[-2]
                add_alert("info", f"Prev close: {STATE['nifty_prev']}")
    except Exception as e:
        add_alert("warn", f"Prev close fetch failed: {e}")

def fetch_15min_candles():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        payload = {"securityId": SECID.get(CONFIG["INDEX"],"13"),
                   "exchangeSegment":"IDX_I","instrument":"INDEX",
                   "interval":"15","oi":False,"fromDate":today,"toDate":today}
        r = requests.post(f"{DHAN_API}/v2/charts/intraday", headers=hdrs(), json=payload, timeout=5)
        if r.status_code == 200:
            d = r.json()
            candles = [{"t":d["timestamp"][i],"o":d["open"][i],"h":d["high"][i],
                        "l":d["low"][i],"c":d["close"][i],"v":d.get("volume",[0]*999)[i]}
                       for i in range(len(d.get("timestamp",[])))]
            STATE["candles_15m"] = candles
            _calc_ema()
            return candles
    except Exception as e:
        add_alert("warn", f"Candle fetch failed: {e}")
    return []

def _calc_ema():
    closes = [c["c"] for c in STATE["candles_15m"]]
    if len(closes) < 2: return
    k, ema = 2/(20+1), closes[0]
    for c in closes[1:]: ema = c*k + ema*(1-k)
    STATE["ema15"] = round(ema, 2)

def calc_or():
    n = max(1, CONFIG["OR_MINUTES"]//15)
    cs = STATE["candles_15m"][:n]
    if not cs: return False
    STATE["or_high"] = max(c["h"] for c in cs)
    STATE["or_low"]  = min(c["l"] for c in cs)
    rng = round(STATE["or_high"]-STATE["or_low"], 2)
    add_alert("success", f"OR set — H:{STATE['or_high']} L:{STATE['or_low']} Rng:{rng}pts")
    return True

def nearest_expiry():
    from datetime import timedelta
    today = datetime.now()
    d = (3 - today.weekday()) % 7
    if d == 0 and today.hour >= 15: d = 7
    return (today + timedelta(days=d)).strftime("%Y-%m-%d")

def get_atm_option(sig):
    strike = round(STATE["nifty"]/50)*50
    try:
        r = requests.get(f"{DHAN_API}/v2/optionchain", headers=hdrs(),
            params={"UnderlyingScrip":SECID.get(CONFIG["INDEX"],"13"),
                    "UnderlyingSeg":"IDX_I","Expiry":nearest_expiry()}, timeout=5)
        if r.status_code == 200:
            for row in r.json().get("data",[]):
                if row.get("strikePrice") == strike:
                    det = row.get("callDetail" if sig=="CE" else "putDetail",{})
                    return {"security_id": det.get("securityId"),
                            "ltp": det.get("lastTradedPrice",0), "strike": strike}
    except: pass
    return {"security_id": None, "ltp": 100, "strike": strike}

def place_order(security_id, txn, qty):
    if CONFIG["MODE"] == "paper":
        add_alert("warn", f"[PAPER] {txn} qty:{qty}")
        return {"orderId": f"PAPER_{int(time.time())}"}
    if not STATE["token_valid"]:
        add_alert("danger", "Token invalid — order skipped.")
        return None
    try:
        payload = {"dhanClientId":CONFIG["CLIENT_ID"],"transactionType":txn,
                   "exchangeSegment":"NSE_FNO","productType":"INTRADAY",
                   "orderType":"MARKET","validity":"DAY",
                   "securityId":str(security_id),"quantity":qty,
                   "price":0,"triggerPrice":0,"disclosedQuantity":0,"afterMarketOrder":False}
        r = requests.post(f"{DHAN_API}/v2/orders", headers=hdrs(), json=payload, timeout=5)
        if r.status_code in [200,201]:
            d = r.json()
            add_alert("success", f"[LIVE] {txn} order — ID:{d.get('orderId')}")
            return d
        add_alert("danger", f"Order error {r.status_code}: {r.text[:80]}")
    except Exception as e:
        add_alert("danger", f"Order exception: {e}")
    return None

# ════════════════════════════════════════════
# WEBSOCKET
# ════════════════════════════════════════════
class DhanFeed:
    def __init__(self):
        self.ws = None
        self.alive = False

    def _sub_msg(self):
        return json.dumps({"RequestCode":21,"InstrumentCount":3,"InstrumentList":[
            {"ExchangeSegment":"IDX_I","SecurityId":SECID["NIFTY"]},
            {"ExchangeSegment":"IDX_I","SecurityId":SECID["BANKNIFTY"]},
            {"ExchangeSegment":"IDX_I","SecurityId":SECID["VIX"]},
        ]})

    def on_open(self, ws):
        ws.send(json.dumps({"LoginReq":{"MsgCode":42,
            "ClientId":CONFIG["CLIENT_ID"],"Token":CONFIG["TOKEN"]}}))
        time.sleep(0.4)
        ws.send(self._sub_msg())
        STATE["ws_connected"] = True
        add_alert("success", "WebSocket live — Nifty/BankNifty/VIX ticks flowing.")

    def on_message(self, ws, msg):
        try:
            data = self._parse(msg)
            if not data: return
            sid = str(data.get("sid",""))
            ltp = float(data.get("ltp",0))
            if ltp <= 0: return
            if sid == SECID["NIFTY"]:
                STATE["nifty"] = round(ltp,2)
                if not STATE["nifty_open"]: STATE["nifty_open"] = ltp
            elif sid == SECID["BANKNIFTY"]:
                STATE["banknifty"] = round(ltp,2)
            elif sid == SECID["VIX"]:
                STATE["vix"] = round(ltp,2)
            STATE["last_tick"] = datetime.now().strftime("%H:%M:%S")
        except: pass

    def _parse(self, msg):
        if isinstance(msg, bytes) and len(msg) >= 53:
            return {"sid": str(struct.unpack_from(">I",msg,1)[0]),
                    "ltp": struct.unpack_from(">d",msg,5)[0]}
        if isinstance(msg, str):
            try: return json.loads(msg)
            except: pass
        return {}

    def on_error(self, ws, e):
        STATE["ws_connected"] = False
        add_alert("warn", f"WS error: {e}")

    def on_close(self, ws, code, msg):
        STATE["ws_connected"] = False
        add_alert("warn", "WS closed. Reconnecting in 5s…")
        if self.alive:
            time.sleep(5)
            self._run()

    def _run(self):
        if not WS_OK or not CONFIG["TOKEN"]: return
        self.ws = websocket.WebSocketApp(DHAN_WS,
            header={"access-token":CONFIG["TOKEN"],"client-id":CONFIG["CLIENT_ID"]},
            on_open=self.on_open, on_message=self.on_message,
            on_error=self.on_error, on_close=self.on_close)
        self.ws.run_forever(ping_interval=30, ping_timeout=10)

    def start(self):
        self.alive = True
        threading.Thread(target=self._run, daemon=True).start()
        add_alert("info", "WebSocket connecting to Dhan feed…")

    def stop(self):
        self.alive = False
        if self.ws: self.ws.close()

feed = DhanFeed()

# ════════════════════════════════════════════
# FILTERS
# ════════════════════════════════════════════
def gap_ok():
    if not STATE["nifty_prev"] or not STATE["nifty"]: return True
    return abs((STATE["nifty"]-STATE["nifty_prev"])/STATE["nifty_prev"]*100) <= CONFIG["GAP_MAX_PCT"]

def vix_ok():
    if not STATE["vix"]: return True
    return CONFIG["VIX_MIN"] <= STATE["vix"] <= CONFIG["VIX_MAX"]

def trend_ok(sig):
    if not STATE["ema15"]: return True
    return STATE["nifty"] > STATE["ema15"] if sig=="CE" else STATE["nifty"] < STATE["ema15"]

def time_ok():
    n = datetime.now().time()
    return (dtime(9,30)<=n<=dtime(11,30)) or (dtime(14,15)<=n<=dtime(14,45))

def limit_ok():
    return STATE["daily_loss"] < CONFIG["CAPITAL"]*CONFIG["RISK_PCT"]/100*3

def all_ok(sig):
    return gap_ok() and vix_ok() and trend_ok(sig) and time_ok() and limit_ok()

# ════════════════════════════════════════════
# STRATEGY ENGINE
# ════════════════════════════════════════════
def strategy_loop():
    add_alert("info", "Strategy engine started.")
    while STATE["strategy_on"]:
        now = datetime.now()
        t   = now.time()

        if not STATE["nifty_prev"] and STATE["token_valid"]:
            fetch_prev_close()

        if not STATE["or_fetched"] and t >= dtime(9,30):
            if fetch_15min_candles() and calc_or():
                STATE["or_fetched"] = True
            else:
                add_alert("warn", "Candle data not ready yet…")

        if not time_ok():
            if t >= dtime(15,15) and STATE["position"]:
                _eod_close()
            time.sleep(10)
            continue

        if not gap_ok():
            add_alert("warn", "GAP filter active — no trades.")
            time.sleep(60); continue
        if not vix_ok():
            add_alert("warn", f"VIX {STATE['vix']:.1f} — outside range.")
            time.sleep(30); continue

        if STATE["position"]:
            _monitor()
            time.sleep(3); continue

        if STATE["or_high"] and not STATE["signal"]:
            price = STATE["nifty"]
            if price > STATE["or_high"] and STATE["sweep_dir"] != "SWEEP_UP":
                STATE["sweep_dir"] = "SWEEP_UP"
                STATE["sweep_candles"] = 0
                add_alert("warn", f"⚡ UPSIDE SWEEP — {price} > OR High {STATE['or_high']}")
            elif price < STATE["or_low"] and STATE["sweep_dir"] != "SWEEP_DOWN":
                STATE["sweep_dir"] = "SWEEP_DOWN"
                STATE["sweep_candles"] = 0
                add_alert("warn", f"⚡ DOWNSIDE SWEEP — {price} < OR Low {STATE['or_low']}")

            if STATE["sweep_dir"]:
                STATE["sweep_candles"] += 1
                returned = (
                    (STATE["sweep_dir"]=="SWEEP_UP"   and price < STATE["or_high"]) or
                    (STATE["sweep_dir"]=="SWEEP_DOWN" and price > STATE["or_low"])
                )
                if returned:
                    sig = "PE" if STATE["sweep_dir"]=="SWEEP_UP" else "CE"
                    cs = STATE["candles_15m"]
                    vol_ok = True
                    if len(cs) >= 3:
                        avg = sum(c["v"] for c in cs[-3:])/3
                        vol_ok = cs[-1]["v"] > avg*1.3 if avg > 0 else True

                    if not vol_ok:
                        add_alert("warn", "Volume low — signal skipped.")
                        STATE["skipped_count"] += 1; _rsweep()
                    elif not trend_ok(sig):
                        add_alert("block", f"TREND FILTER: {sig} counter-trend. Blocked.")
                        STATE["blocked_count"] += 1; _rsweep()
                    else:
                        _execute(sig)
                elif STATE["sweep_candles"] >= 3:
                    add_alert("warn", "3-CANDLE RULE: No return inside OR. Signal void.")
                    STATE["skipped_count"] += 1; _rsweep()

        time.sleep(3)
    add_alert("info", "Strategy engine stopped.")

def _execute(sig):
    if not all_ok(sig):
        STATE["blocked_count"] += 1
        add_alert("block", f"Final filter check failed for {sig}.")
        _rsweep(); return

    opt = get_atm_option(sig)
    ltp = opt["ltp"]
    if ltp <= 0:
        add_alert("danger", "ATM LTP = 0. Cannot execute.")
        _rsweep(); return

    sl  = round(ltp*(1 - CONFIG["SL_PCT"]/100), 2)
    tgt = round(ltp*(1 + CONFIG["TGT_PCT"]/100), 2)
    qty = CONFIG["LOTS"] * LOT.get(CONFIG["INDEX"], 50)

    add_alert("success",
        f"SIGNAL {sig} {opt['strike']} | Entry:₹{ltp} | SL:₹{sl} | Tgt:₹{tgt} | {CONFIG['LOTS']}L | {CONFIG['MODE'].upper()}")

    order = place_order(opt["security_id"], "BUY", qty)
    if order:
        STATE.update({
            "signal": sig, "entry_price": ltp, "sl_price": sl,
            "target_price": tgt, "trail_high": ltp,
            "signals_count": STATE["signals_count"]+1,
            "position": {
                "symbol": f"{CONFIG['INDEX']}{opt['strike']}{sig}",
                "type": sig, "strike": opt["strike"], "qty": qty,
                "entry": ltp, "sl": sl, "target": tgt,
                "security_id": opt["security_id"],
                "order_id": order.get("orderId","SIM"),
                "entry_time": datetime.now().strftime("%H:%M"),
            }
        })
        save_session()   # save immediately when position opens
        _rsweep()

def _monitor():
    pos = STATE["position"]
    if not pos: return
    if CONFIG["MODE"] == "paper":
        import random
        cur = STATE["entry_price"] * (1 + random.uniform(-0.04, 0.06))
    else:
        cur = get_atm_option(pos["type"]).get("ltp", STATE["entry_price"])

    if cur > STATE["trail_high"]:
        STATE["trail_high"] = cur
        profit = STATE["trail_high"] - STATE["entry_price"]
        if profit*pos["qty"] >= CONFIG["TRAIL_EVERY"]:
            new_sl = round(STATE["entry_price"] + profit*0.5, 2)
            if new_sl > STATE["sl_price"]:
                STATE["sl_price"] = new_sl
                add_alert("info", f"Trailing SL → ₹{new_sl}")

    if cur <= STATE["sl_price"]:
        _close(cur, "SL Hit ⛔")
    elif cur >= STATE["target_price"]:
        _close(cur, "Target Hit ✅")

def _close(exit_px, reason):
    pos = STATE["position"]
    if not pos: return
    place_order(pos["security_id"], "SELL", pos["qty"])
    pnl = round((exit_px - pos["entry"]) * pos["qty"], 2)

    trade_record = {
        "time":      pos["entry_time"],
        "exit_time": datetime.now().strftime("%H:%M"),
        "type":      pos["type"],
        "strike":    pos["strike"],
        "entry":     pos["entry"],
        "exit":      exit_px,
        "lots":      CONFIG["LOTS"],
        "pnl":       pnl,
        "reason":    reason,
        "mode":      CONFIG["MODE"],
    }
    STATE["trades"].insert(0, trade_record)
    STATE["trades"] = STATE["trades"][:50]

    # ── PERSIST: write trade to disk immediately ──
    save_trade(trade_record)

    if pnl >= 0:
        STATE["gross_win"] += pnl
    else:
        STATE["gross_loss"] += abs(pnl)
        STATE["daily_loss"] += abs(pnl)

    add_alert("success" if pnl>=0 else "danger",
        f"{reason} | Exit:₹{exit_px} | P&L:{'+' if pnl>=0 else ''}₹{pnl}")

    STATE.update({"position":None,"signal":None,"entry_price":0})

    # ── PERSIST: save full session after close ──
    save_session()

def _eod_close():
    if STATE["position"]:
        opt = get_atm_option(STATE["position"]["type"])
        _close(opt.get("ltp", STATE["entry_price"]), "EOD ⏰")

def _rsweep():
    STATE["sweep_dir"] = None
    STATE["sweep_candles"] = 0

# ════════════════════════════════════════════
# ALERTS
# ════════════════════════════════════════════
def add_alert(level, msg):
    STATE["alerts"].insert(0, {
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "msg": msg,
    })
    STATE["alerts"] = STATE["alerts"][:40]
    print(f"[{level.upper()}] {msg}")
    # ── PERSIST: save every 5 alerts ──
    if len(STATE["alerts"]) % 5 == 0:
        save_session()

# ════════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/state")
def get_state():
    pnl  = round(STATE["gross_win"] - STATE["gross_loss"], 2)
    wins = sum(1 for t in STATE["trades"] if t["pnl"] >= 0)
    wr   = round(wins/len(STATE["trades"])*100) if STATE["trades"] else 0
    gap  = round(abs((STATE["nifty"]-STATE["nifty_prev"])/max(STATE["nifty_prev"],1)*100),2) if STATE["nifty_prev"] else 0
    return jsonify({
        "ws_connected": STATE["ws_connected"], "token_valid": STATE["token_valid"],
        "last_tick": STATE["last_tick"], "nifty": STATE["nifty"],
        "nifty_prev": STATE["nifty_prev"], "banknifty": STATE["banknifty"],
        "vix": STATE["vix"], "ema15": STATE["ema15"],
        "strategy_on": STATE["strategy_on"], "mode": CONFIG["MODE"],
        "signal": STATE["signal"], "or_high": STATE["or_high"], "or_low": STATE["or_low"],
        "sweep_dir": STATE["sweep_dir"], "sweep_candles": STATE["sweep_candles"],
        "position": STATE["position"], "pnl": pnl, "gross_win": STATE["gross_win"],
        "gross_loss": STATE["gross_loss"], "win_rate": wr,
        "trades": STATE["trades"][:20], "trade_count": len(STATE["trades"]),
        "signals_count": STATE["signals_count"], "blocked_count": STATE["blocked_count"],
        "skipped_count": STATE["skipped_count"],
        "filters": {"gap": gap_ok(), "gap_pct": gap, "vix": vix_ok(),
                    "time": time_ok(), "limit": limit_ok()},
        "alerts": STATE["alerts"][:20],
        "config": {"client_id": CONFIG["CLIENT_ID"], "mode": CONFIG["MODE"],
                   "index": CONFIG["INDEX"], "lots": CONFIG["LOTS"],
                   "capital": CONFIG["CAPITAL"], "risk_pct": CONFIG["RISK_PCT"]},
    })

@app.route("/api/strategy/start", methods=["POST"])
def start():
    if STATE["strategy_on"]: return jsonify({"status":"already_running"})
    STATE["strategy_on"] = True
    threading.Thread(target=strategy_loop, daemon=True).start()
    return jsonify({"status":"started"})

@app.route("/api/strategy/stop", methods=["POST"])
def stop():
    STATE["strategy_on"] = False
    return jsonify({"status":"stopped"})

@app.route("/api/square_off", methods=["POST"])
def sq():
    _eod_close()
    return jsonify({"status":"squared_off"})

@app.route("/api/config", methods=["GET","POST"])
def cfg():
    if request.method == "POST":
        for k in ["LOTS","CAPITAL","RISK_PCT","SL_PCT","TGT_PCT","MODE","INDEX","OR_MINUTES"]:
            if k in (request.json or {}): CONFIG[k] = request.json[k]
        add_alert("info","Config updated.")
        return jsonify({"status":"updated"})
    return jsonify({k:v for k,v in CONFIG.items() if "TOKEN" not in k})

@app.route("/api/reset", methods=["POST"])
def reset():
    for k in ["trades","alerts"]: STATE[k]=[]
    for k in ["gross_win","gross_loss","daily_loss","signals_count",
              "blocked_count","skipped_count","entry_price"]: STATE[k]=0
    for k in ["or_high","or_low","sweep_dir","signal","position"]: STATE[k]=None
    STATE["or_fetched"]=False; STATE["sweep_candles"]=0
    # Also wipe today's files so reset is complete
    for f in [SESSION_FILE(), TRADES_FILE()]:
        try:
            if os.path.exists(f): os.remove(f)
        except: pass
    add_alert("info","Session reset — disk files cleared.")
    return jsonify({"status":"reset"})

@app.route("/api/health")
def health():
    return jsonify({"status":"ok","ws":STATE["ws_connected"],"token":STATE["token_valid"],
                    "nifty":STATE["nifty"],"last_tick":STATE["last_tick"],"mode":CONFIG["MODE"]})

@app.route("/api/session/export")
def export_session():
    """Download today's complete trade log as JSON."""
    tf = TRADES_FILE()
    trades = []
    if os.path.exists(tf):
        with open(tf) as f:
            trades = json.load(f)
    wins  = [t for t in trades if t.get("pnl",0) > 0]
    losses= [t for t in trades if t.get("pnl",0) <= 0]
    return jsonify({
        "date":   datetime.now().strftime("%Y-%m-%d"),
        "trades": trades,
        "summary": {
            "total_trades": len(trades),
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     round(len(wins)/len(trades)*100,1) if trades else 0,
            "gross_win":    round(sum(t["pnl"] for t in wins), 2),
            "gross_loss":   round(sum(t["pnl"] for t in losses), 2),
            "net_pnl":      round(sum(t.get("pnl",0) for t in trades), 2),
        }
    })

@app.route("/api/session/save", methods=["POST"])
def force_save():
    """Manually trigger a session save — use if you want to be sure."""
    save_session()
    return jsonify({"status":"saved","time":datetime.now().strftime("%H:%M:%S")})

# ════════════════════════════════════════════
# STARTUP
# ════════════════════════════════════════════
def startup():
    print(f"\n{'='*48}\n  NiftyEdge Pro v2 | Client: {CONFIG['CLIENT_ID']}\n"
          f"  Mode:{CONFIG['MODE']} | Token:{'SET ✅' if CONFIG['TOKEN'] else 'MISSING ❌'}\n{'='*48}\n")

    # ── PERSIST: restore today's session before anything else ──
    load_session()

    if CONFIG["TOKEN"]:
        if validate_token():
            feed.start()
            time.sleep(2)
            fetch_prev_close()
    else:
        add_alert("danger","DHAN_TOKEN missing — add to Railway Variables. Running simulation.")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=startup, daemon=True).start()
    app.run(host="0.0.0.0", port=port, debug=False)
