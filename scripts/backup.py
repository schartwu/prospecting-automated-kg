"""Weekly backup of what cannot be fetched again from e-GP: the Lead Loop statuses and users.

The Supabase Free plan keeps no backups we can restore ourselves, and this repository is public, so the
files are emailed (to BACKUP_RECIPIENTS) instead of being stored as a run artifact. Link tokens are left
out; new ones are issued on restore.
"""
import csv
import io
import os
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from egp.db import DB  # noqa: E402

TABLES = {
    "lead_events": "id,lead_key,status,reasons,bid_price,note,user_id,created_at",
    "lead_users": "id,name,email,role,active,created_at",
}


def cell(v):
    """Notes are typed by users and the file is opened in Excel: a leading = + - @ would run as a formula."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def to_csv(rows, columns):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns)
    w.writeheader()
    for r in rows:
        w.writerow({k: cell("|".join(v) if isinstance(v, list) else v) for k, v in r.items()})
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel shows Thai correctly


def fetch_all(db, table, select):
    rows, page = [], 1000
    while True:
        part = db.select(table, select=select, order="id", offset=len(rows), limit=page)
        rows += part
        if len(part) < page:
            return rows


def main():
    db = DB()
    now = datetime.now(ZoneInfo("Asia/Bangkok"))
    to = [e.strip() for e in (os.environ.get("BACKUP_RECIPIENTS") or "").split(",") if e.strip()]
    if not to:
        sys.exit("BACKUP_RECIPIENTS is empty: set the repository variable to the email that keeps the backups")
    msg = EmailMessage()
    msg["From"] = os.environ["SENDER_EMAIL"]
    msg["To"] = ", ".join(to)
    counts = {}
    files = []
    for table, select in TABLES.items():
        rows = fetch_all(db, table, select)
        counts[table] = len(rows)
        files.append((f"{table}_{now:%Y%m%d}.csv", to_csv(rows, select.split(","))))
    msg["Subject"] = (f"🗄️ e-GP Alert backup {now:%d/%m/%Y}: สถานะ {counts['lead_events']} รายการ · "
                      f"ผู้ใช้ {counts['lead_users']} คน")
    msg.set_content(
        "สำรองข้อมูล KG Lead Loop รายสัปดาห์ (ข้อมูลที่ดึงจาก e-GP ใหม่ไม่ได้)\n\n"
        + "\n".join(f"- {name}: {counts[name.rsplit('_', 1)[0]]} แถว" for name, _ in files)
        + "\n\nเก็บอีเมลนี้ไว้ ถ้าต้องกู้คืน นำ CSV เข้า Supabase (Table Editor → Import data from CSV)"
          " ไฟล์ผู้ใช้ไม่มีลิงก์ส่วนตัว ระบบจะออกลิงก์ใหม่ให้เมื่อกู้คืน\n")
    for name, data in files:
        msg.add_attachment(data, maintype="text", subtype="csv", filename=name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(os.environ["SENDER_EMAIL"], os.environ["SENDER_PASSWORD"])
        smtp.send_message(msg)
    print("backup sent:", counts)


if __name__ == "__main__":
    main()
