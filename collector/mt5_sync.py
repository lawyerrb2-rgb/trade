#!/usr/bin/env python3
"""ดึงเทรดที่ปิดแล้วจากหลายบัญชี MT5 -> Supabase พร้อมแจ้งเตือน Telegram/Discord เมื่อซิงก์ล้มเหลว
    python mt5_sync.py                 # รันวนทุก 60 วินาที
    python mt5_sync.py --interval 0    # รันรอบเดียว
    python mt5_sync.py --test-alert    # ทดสอบการแจ้งเตือน
"""
import argparse, json, os, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import MetaTrader5 as mt5
import requests

HERE = Path(__file__).parent
if (HERE / ".env").exists():
    for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

E = os.environ.get
URL, KEY = E("SUPABASE_URL", "").rstrip("/"), E("SUPABASE_SERVICE_KEY", "")
DEFAULT_OFFSET = float(E("SERVER_UTC_OFFSET", "0"))
FAIL_AFTER = int(E("ALERT_AFTER_FAILS", "3"))
state = {}  # ชื่อบัญชี -> {"fails": จำนวนรอบที่ล้มเหลวติดกัน, "alerted": เวลาที่แจ้งล่าสุด}


def notify(text):
    text = f"[MT5 Journal] {text}"
    print(text)
    senders = []
    if E("TELEGRAM_BOT_TOKEN") and E("TELEGRAM_CHAT_ID"):
        senders.append(lambda: requests.post(f"https://api.telegram.org/bot{E('TELEGRAM_BOT_TOKEN')}/sendMessage",
                                             data={"chat_id": E("TELEGRAM_CHAT_ID"), "text": text}, timeout=15))
    if E("DISCORD_WEBHOOK_URL"):
        senders.append(lambda: requests.post(E("DISCORD_WEBHOOK_URL"), json={"content": text}, timeout=15))
    for s in senders:
        try:
            s().raise_for_status()
        except Exception as e:
            print("ส่งแจ้งเตือนไม่สำเร็จ:", e)


def report_ok(name):
    s = state.setdefault(name, {"fails": 0, "alerted": 0})
    if s["alerted"]:
        notify(f"✅ {name}: ซิงก์กลับมาปกติแล้ว")
    s["fails"], s["alerted"] = 0, 0


def report_fail(name, err):
    s = state.setdefault(name, {"fails": 0, "alerted": 0})
    s["fails"] += 1
    if s["fails"] >= FAIL_AFTER and (not s["alerted"] or time.time() - s["alerted"] > 6 * 3600):
        notify(f"⚠️ {name}: ซิงก์ล้มเหลว {s['fails']} รอบติดกัน\n{str(err)[:300]}")
        s["alerted"] = time.time()


def load_accounts():  # อ่านใหม่ทุกรอบ แก้ไฟล์แล้วไม่ต้องรีสตาร์ท
    f = HERE / "accounts.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else [{"name": "main"}]


def collect(acct):
    off = float(acct.get("utc_offset", DEFAULT_OFFSET))
    utc = lambda ts: datetime.fromtimestamp(ts - off * 3600, timezone.utc).isoformat()
    kw = {k: acct[k] for k in ("path", "password", "server") if acct.get(k)}
    if acct.get("login"):
        kw["login"] = int(acct["login"])
    if not mt5.initialize(**kw):
        raise RuntimeError(f"เชื่อมต่อ MT5 ไม่ได้: {mt5.last_error()}")
    try:
        info = mt5.account_info()
        if kw.get("login") and (info is None or info.login != kw["login"]):
            raise RuntimeError("ล็อกอินบัญชีไม่ตรงกับที่ตั้งไว้ (ตรวจรหัสผ่าน/เซิร์ฟเวอร์)")
        now = datetime.now() + timedelta(days=1)
        deals = mt5.history_deals_get(now - timedelta(days=int(acct.get("days", 90)) + 1), now) or []
        still_open = {p.identifier for p in (mt5.positions_get() or [])}
        buy_type, entry_in = mt5.DEAL_TYPE_BUY, mt5.DEAL_ENTRY_IN
    finally:
        mt5.shutdown()

    pos = {}
    for d in deals:
        if d.type > 1 or not d.symbol or d.position_id in still_open:
            continue
        pos.setdefault(d.position_id, []).append(d)
    rows = []
    for pid, ds in pos.items():
        ds.sort(key=lambda x: x.time)
        ins = [d for d in ds if d.entry == entry_in]
        outs = [d for d in ds if d.entry != entry_in]
        if not ins or not outs:
            continue
        rows.append(dict(
            account=acct.get("name", "main"), id=pid, symbol=ins[0].symbol,
            side="buy" if ins[0].type == buy_type else "sell", volume=sum(d.volume for d in ins),
            open_time=utc(ins[0].time), close_time=utc(outs[-1].time),
            open_price=ins[0].price, close_price=outs[-1].price,
            profit=sum(d.profit for d in ds), commission=sum(d.commission for d in ds),
            swap=sum(d.swap for d in ds), fee=sum(d.fee for d in ds)))
    return rows


def push(rows):
    h = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
         "Prefer": "resolution=merge-duplicates,return=minimal"}
    for i in range(0, len(rows), 500):  # upsert เฉพาะคอลัมน์เหล่านี้ จึงไม่ทับบันทึกรีวิว
        requests.post(f"{URL}/rest/v1/trades?on_conflict=account,id", headers=h,
                      json=rows[i:i + 500], timeout=30).raise_for_status()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60, help="0 = รันครั้งเดียว")
    ap.add_argument("--test-alert", action="store_true")
    args = ap.parse_args()
    if args.test_alert:
        notify("ทดสอบการแจ้งเตือน ถ้าเห็นข้อความนี้แสดงว่าตั้งค่าถูกต้อง")
        raise SystemExit
    if args.interval:
        notify(f"▶️ เริ่มทำงานแล้ว ({len(load_accounts())} บัญชี)")
    while True:
        for acct in load_accounts():
            name = acct.get("name", "main")
            try:
                rows = collect(acct)
                push(rows)
                print(f"[{datetime.now():%H:%M:%S}] {name}: ซิงก์ {len(rows)} เทรด")
                report_ok(name)
            except Exception as e:
                print(f"[{datetime.now():%H:%M:%S}] {name}: ผิดพลาด: {e}")
                report_fail(name, e)
        if E("HEARTBEAT_URL"):  # ถ้าสคริปต์/เครื่องดับ บริการภายนอกจะไม่ได้รับสัญญาณแล้วแจ้งเตือนแทน
            try:
                requests.get(E("HEARTBEAT_URL"), timeout=10)
            except Exception as e:
                print("heartbeat ล้มเหลว:", e)
        if not args.interval:
            break
        time.sleep(args.interval)
