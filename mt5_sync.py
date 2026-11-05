#!/usr/bin/env python3
"""ดึงเทรดที่ปิดแล้วจาก MT5 ส่งขึ้น Supabase (รันบน Windows ที่เปิด MT5 ค้างไว้)
    pip install -r requirements.txt
    python mt5_sync.py --days 90 --interval 60
"""
import argparse, os, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import MetaTrader5 as mt5
import requests

env = Path(__file__).with_name(".env")
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
OFFSET = float(os.environ.get("SERVER_UTC_OFFSET", "0"))


def utc(ts):  # เวลา MT5 (เวลาเซิร์ฟเวอร์) -> UTC จริง
    return datetime.fromtimestamp(ts - OFFSET * 3600, timezone.utc).isoformat()


def collect(days):
    if not mt5.initialize():
        raise RuntimeError(f"เชื่อมต่อ MT5 ไม่ได้: {mt5.last_error()}")
    try:
        now = datetime.now() + timedelta(days=1)
        deals = mt5.history_deals_get(now - timedelta(days=days + 1), now) or []
        still_open = {p.identifier for p in (mt5.positions_get() or [])}
        buy_type = mt5.DEAL_TYPE_BUY
        entry_in = mt5.DEAL_ENTRY_IN
    finally:
        mt5.shutdown()

    pos = {}
    for d in deals:
        if d.type > 1 or not d.symbol or d.position_id in still_open:
            continue  # ข้ามฝาก/ถอน และไม้ที่ยังเปิดอยู่
        pos.setdefault(d.position_id, []).append(d)

    rows = []
    for pid, ds in pos.items():
        ds.sort(key=lambda x: x.time)
        ins = [d for d in ds if d.entry == entry_in]
        outs = [d for d in ds if d.entry != entry_in]
        if not ins or not outs:
            continue
        rows.append(dict(
            id=pid, symbol=ins[0].symbol,
            side="buy" if ins[0].type == buy_type else "sell",
            volume=sum(d.volume for d in ins),
            open_time=utc(ins[0].time), close_time=utc(outs[-1].time),
            open_price=ins[0].price, close_price=outs[-1].price,
            profit=sum(d.profit for d in ds), commission=sum(d.commission for d in ds),
            swap=sum(d.swap for d in ds), fee=sum(d.fee for d in ds),
        ))
    return rows


def push(rows):
    h = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
         "Prefer": "resolution=merge-duplicates,return=minimal"}
    for i in range(0, len(rows), 500):  # upsert เฉพาะคอลัมน์เหล่านี้ จึงไม่ทับบันทึกรีวิว
        r = requests.post(f"{URL}/rest/v1/trades?on_conflict=id", headers=h, json=rows[i:i + 500], timeout=30)
        r.raise_for_status()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--interval", type=int, default=60, help="0 = รันครั้งเดียว")
    a = ap.parse_args()
    while True:
        try:
            rows = collect(a.days)
            push(rows)
            print(f"[{datetime.now():%H:%M:%S}] ซิงก์ {len(rows)} เทรด")
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] ผิดพลาด: {e}")
        if not a.interval:
            break
        time.sleep(a.interval)
