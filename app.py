import sqlite3
import smtplib
import ssl
import json
import threading
import time
import urllib.request
from email.mime.text import MIMEText
from datetime import datetime
from flask import Flask, request, render_template, redirect, url_for, jsonify

app = Flask(__name__)
DB = "subscribers.db"

import os
try:
    from config import EMAIL_SENDER, EMAIL_PASSWORD, STABLE_VERSION, BETA_VERSION, THS_REFRESH_TOKEN
except ImportError:
    EMAIL_SENDER      = os.environ["EMAIL_SENDER"]
    EMAIL_PASSWORD    = os.environ["EMAIL_PASSWORD"]
    STABLE_VERSION    = os.environ.get("STABLE_VERSION", "v1")
    BETA_VERSION      = os.environ.get("BETA_VERSION", "v2")
    THS_REFRESH_TOKEN = os.environ.get("THS_REFRESH_TOKEN", "")

ASSET_NAMES = {"XAG": "银价", "XAU": "金价"}
THS_API     = "https://quantapi.51ifind.com/api/v1"
THS_CODES   = {"XAU": "AUUSDO.LIFFE", "XAG": "AGUSDO.LIFFE"}

# ── 同花顺 Token 管理 ─────────────────────────────────────
_ths_lock   = threading.Lock()
_ths_token  = None
_ths_expiry = 0.0

def _renew_ths_token():
    global _ths_token, _ths_expiry
    req = urllib.request.Request(
        f"{THS_API}/get_access_token",
        data=b"{}",
        headers={"Content-Type": "application/json", "refresh_token": THS_REFRESH_TOKEN},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = json.loads(r.read())
    if resp.get("errorcode") != 0:
        raise RuntimeError(resp.get("errmsg"))
    _ths_token  = resp["data"]["access_token"]
    exp = datetime.strptime(resp["data"]["expired_time"], "%Y-%m-%d %H:%M:%S")
    _ths_expiry = exp.timestamp()
    print(f"[THS] token 已更新，到期 {resp['data']['expired_time']}")

def get_ths_token():
    with _ths_lock:
        if not _ths_token or time.time() > _ths_expiry - 3600:
            _renew_ths_token()
        return _ths_token

# ── 价格缓存 ──────────────────────────────────────────────
_price_cache = {
    "XAU": {"price": None, "open": None, "high": None, "low": None},
    "XAG": {"price": None, "open": None, "high": None, "low": None},
    "ratio": None, "updated_at": None, "source": None,
}

def fetch_prices_ths():
    token   = get_ths_token()
    codes   = f"{THS_CODES['XAU']},{THS_CODES['XAG']}"
    payload = json.dumps({"codes": codes, "indicators": "open,high,low,latest"}).encode()
    req = urllib.request.Request(
        f"{THS_API}/real_time_quotation",
        data=payload,
        headers={"Content-Type": "application/json", "access_token": token},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    if data.get("errorcode") != 0:
        raise RuntimeError(data.get("errmsg"))
    result = {}
    for tbl in data["tables"]:
        asset = "XAU" if tbl["thscode"].startswith("AU") else "XAG"
        t = tbl["table"]
        result[asset] = {
            "price": t["latest"][0],
            "open":  t["open"][0],
            "high":  t["high"][0],
            "low":   t["low"][0],
        }
    return result

def fetch_price_goldapi(asset):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        f"https://api.gold-api.com/price/{asset}",
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        return json.loads(r.read())["price"]

def refresh_price_cache():
    source = "同花顺"
    try:
        prices = fetch_prices_ths()
    except Exception as e:
        print(f"[THS] 获取失败，回退 gold-api: {e}")
        source = "gold-api"
        prices = {}
        for asset in ("XAU", "XAG"):
            try:
                p = fetch_price_goldapi(asset)
                prices[asset] = {"price": p, "open": None, "high": None, "low": None}
            except Exception as e2:
                print(f"[gold-api] {asset} 失败: {e2}")

    xau_price = prices.get("XAU", {}).get("price")
    xag_price = prices.get("XAG", {}).get("price")
    ratio = round(xau_price / xag_price, 2) if xau_price and xag_price else None

    for asset in ("XAU", "XAG"):
        if asset in prices:
            _price_cache[asset] = prices[asset]
    _price_cache["ratio"]      = ratio
    _price_cache["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _price_cache["source"]     = source
    return _price_cache

# ── 数据库初始化 ──────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                email     TEXT NOT NULL,
                asset     TEXT NOT NULL DEFAULT 'XAG',
                high      REAL NOT NULL,
                low       REAL NOT NULL,
                version   TEXT NOT NULL DEFAULT 'v1',
                sent_high INTEGER DEFAULT 0,
                sent_low  INTEGER DEFAULT 0
            )
        """)
        for col, typedef in [
            ("asset",   "TEXT NOT NULL DEFAULT 'XAG'"),
            ("version", "TEXT NOT NULL DEFAULT 'v1'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE subscribers ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass

# ── 发邮件 ────────────────────────────────────────────────
def send_email(to, subject, body):
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = to
        with smtplib.SMTP_SSL("smtp.qq.com", 465) as s:
            s.login(EMAIL_SENDER, EMAIL_PASSWORD)
            s.sendmail(EMAIL_SENDER, to, msg.as_string())
        print(f"[邮件] 已发送至 {to}")
    except Exception as e:
        print(f"[邮件] 发送失败：{e}")

# ── 后台监控线程 ──────────────────────────────────────────
def monitor_loop():
    print("[监控] 后台线程启动")
    while True:
        try:
            cache = refresh_price_cache()
            now   = cache["updated_at"]

            for asset in ("XAU", "XAG"):
                p = cache.get(asset, {}).get("price")
                if p:
                    print(f"[{now}] {ASSET_NAMES[asset]}：{p:.3f} USD")
            if cache["ratio"]:
                print(f"[{now}] 金银比：{cache['ratio']:.2f}")

            ratio_line = f"\n当前金银比：{cache['ratio']:.1f}" if cache.get("ratio") else ""

            with sqlite3.connect(DB) as conn:
                rows = conn.execute(
                    "SELECT id, email, asset, high, low, sent_high, sent_low FROM subscribers"
                ).fetchall()
                for sid, email, asset, high, low, sent_high, sent_low in rows:
                    price = cache.get(asset, {}).get("price")
                    if price is None:
                        continue
                    name   = ASSET_NAMES[asset]
                    buffer = high * 0.003

                    if price >= high and not sent_high:
                        send_email(email, f"{name}突破目标！",
                            f"{name}已涨至 {price:.3f} USD，超过你设定的 {high} USD{ratio_line}\n时间：{now}")
                        conn.execute("UPDATE subscribers SET sent_high=1 WHERE id=?", (sid,))

                    if price <= low and not sent_low:
                        send_email(email, f"{name}跌破目标！",
                            f"{name}已跌至 {price:.3f} USD，低于你设定的 {low} USD{ratio_line}\n时间：{now}")
                        conn.execute("UPDATE subscribers SET sent_low=1 WHERE id=?", (sid,))

                    if sent_high and price < high - buffer:
                        conn.execute("UPDATE subscribers SET sent_high=0 WHERE id=?", (sid,))
                    if sent_low and price > low + buffer:
                        conn.execute("UPDATE subscribers SET sent_low=0 WHERE id=?", (sid,))

        except Exception as e:
            print(f"[监控] 出错：{e}")

        time.sleep(5)

# ── 网页路由 ──────────────────────────────────────────────
@app.route("/")
def index():
    with sqlite3.connect(DB) as conn:
        count = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
    return render_template("index.html", count=count, version=STABLE_VERSION, is_beta=False)

@app.route("/beta")
def index_beta():
    with sqlite3.connect(DB) as conn:
        count = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
    return render_template("index.html", count=count, version=BETA_VERSION, is_beta=True)

@app.route("/api/prices")
def api_prices():
    return jsonify(_price_cache)

@app.route("/subscribe", methods=["POST"])
def subscribe():
    email   = request.form.get("email", "").strip()
    asset   = request.form.get("asset", "XAG")
    high    = float(request.form.get("high", 0))
    low     = float(request.form.get("low", 0))
    version = request.form.get("version", STABLE_VERSION)
    if asset not in ("XAG", "XAU"):
        asset = "XAG"
    if email and high > low > 0:
        with sqlite3.connect(DB) as conn:
            conn.execute("DELETE FROM subscribers WHERE email=? AND asset=?", (email, asset))
            conn.execute(
                "INSERT INTO subscribers (email, asset, high, low, version) VALUES (?,?,?,?,?)",
                (email, asset, high, low, version)
            )
    return redirect(url_for("success"))

@app.route("/success")
def success():
    return render_template("success.html")

# ── 启动 ──────────────────────────────────────────────────
init_db()
t = threading.Thread(target=monitor_loop, daemon=True)
t.start()

if __name__ == "__main__":
    app.run(debug=False, port=5000)
