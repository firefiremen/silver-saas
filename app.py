import sqlite3
import smtplib
import ssl
import json
import threading
import time
import urllib.request
from email.mime.text import MIMEText
from datetime import datetime
from flask import Flask, request, render_template, redirect, url_for

app = Flask(__name__)
DB = "subscribers.db"

from config import EMAIL_SENDER, EMAIL_PASSWORD

# ── 数据库初始化 ──────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                email     TEXT NOT NULL,
                high      REAL NOT NULL,
                low       REAL NOT NULL,
                sent_high INTEGER DEFAULT 0,
                sent_low  INTEGER DEFAULT 0
            )
        """)

# ── 获取银价 ──────────────────────────────────────────────
def fetch_price():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    req = urllib.request.Request("https://api.gold-api.com/price/XAG", headers=headers)
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        return json.loads(r.read())["price"]

# ── 发邮件 ────────────────────────────────────────────────
def send_email(to, subject, body):
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = EMAIL_SENDER
        msg["To"] = to
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
            price = fetch_price()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now}] 银价：{price:.3f} USD")

            with sqlite3.connect(DB) as conn:
                rows = conn.execute("SELECT id, email, high, low, sent_high, sent_low FROM subscribers").fetchall()
                for sid, email, high, low, sent_high, sent_low in rows:
                    if price >= high and not sent_high:
                        send_email(email, "银价突破目标！",
                            f"银价已涨至 {price:.3f} USD，超过你设定的 {high} USD\n时间：{now}")
                        conn.execute("UPDATE subscribers SET sent_high=1 WHERE id=?", (sid,))

                    if price <= low and not sent_low:
                        send_email(email, "银价跌破目标！",
                            f"银价已跌至 {price:.3f} USD，低于你设定的 {low} USD\n时间：{now}")
                        conn.execute("UPDATE subscribers SET sent_low=1 WHERE id=?", (sid,))

                    # 价格回归区间，重置发送状态
                    if sent_high and price < high - 0.3:
                        conn.execute("UPDATE subscribers SET sent_high=0 WHERE id=?", (sid,))
                    if sent_low and price > low + 0.3:
                        conn.execute("UPDATE subscribers SET sent_low=0 WHERE id=?", (sid,))

        except Exception as e:
            print(f"[监控] 出错：{e}")

        time.sleep(60)

# ── 网页路由 ──────────────────────────────────────────────
@app.route("/")
def index():
    with sqlite3.connect(DB) as conn:
        count = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
    return render_template("index.html", count=count)

@app.route("/subscribe", methods=["POST"])
def subscribe():
    email = request.form.get("email", "").strip()
    high  = float(request.form.get("high", 0))
    low   = float(request.form.get("low", 0))
    if email and high > low > 0:
        with sqlite3.connect(DB) as conn:
            conn.execute("INSERT INTO subscribers (email, high, low) VALUES (?,?,?)", (email, high, low))
    return redirect(url_for("success"))

@app.route("/success")
def success():
    return render_template("success.html")

# ── 启动 ──────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    app.run(debug=False, port=5000)
