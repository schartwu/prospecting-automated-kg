"""Email the announcements the weekly cleanup is about to delete, as a zipped CSV, then mark them archived.

cleanup_old_data deletes announcements nobody needs (not relevant, and not a cancellation/change of a relevant
project) 30 days after they were first seen, but only rows marked archived_at. This runs on Saturday 15:00
Bangkok (pg_cron egp-weekly-archive -> archive.yml), before the cleanup at Sunday 02:00, and takes rows older
than 23 days, so each row is archived about a week before it is deleted. If the email fails nothing is
marked and nothing is deleted; the next run picks the rows up again.

The CSV keeps what cannot be fetched again (the RSS only goes back about a week): type, project/plan number,
method, title, link, dates, department and the classifier's score/category/reason.
"""
import csv
import io
import os
import smtplib
import sys
import zipfile
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from egp.db import DB  # noqa: E402

COLUMNS = ["id", "announce_type", "project_no", "plan_no", "method", "title", "link", "pub_date",
           "dept_code", "dept_name", "first_seen_at", "score", "category", "reason"]
PAGE = 2000
ROWS_PER_EMAIL = 60000  # ~8 MB zipped; Gmail takes attachments up to 25 MB


def fetch_due(db):
    rows = []
    while True:
        page = db.rpc("archive_page", {"p_after": rows[-1]["id"] if rows else 0, "p_limit": PAGE})
        rows += page
        if len(page) < PAGE:
            return rows


def cell(v):
    """Text from e-GP opened in Excel: a leading = + - @ would run as a formula, so it is quoted as text."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def zipped_csv(rows, name):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows({k: cell(v) for k, v in r.items()} for r in rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, buf.getvalue().encode("utf-8-sig"))  # BOM so Excel shows Thai correctly
    return out.getvalue()


def main():
    to = [e.strip() for e in (os.environ.get("BACKUP_RECIPIENTS") or "").split(",") if e.strip()]
    if not to:
        sys.exit("BACKUP_RECIPIENTS is empty: set the repository variable to the email that keeps the archive")
    db = DB()
    now = datetime.now(ZoneInfo("Asia/Bangkok"))
    rows = fetch_due(db)
    print(f"{len(rows)} announcements due for deletion", flush=True)
    if not rows:
        return
    parts = [rows[i:i + ROWS_PER_EMAIL] for i in range(0, len(rows), ROWS_PER_EMAIL)]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(os.environ["SENDER_EMAIL"], os.environ["SENDER_PASSWORD"])
        for n, part in enumerate(parts, 1):
            days = sorted(r["first_seen_at"][:10] for r in part)
            label = f" ({n}/{len(parts)})" if len(parts) > 1 else ""
            name = f"egp_archive_{now:%Y%m%d}{f'_{n}' if len(parts) > 1 else ''}.csv"
            msg = EmailMessage()
            msg["From"] = os.environ["SENDER_EMAIL"]
            msg["To"] = ", ".join(to)
            msg["Subject"] = f"🗄️ e-GP Alert archive {now:%d/%m/%Y}{label}: ประกาศ {len(part):,} รายการก่อนลบ"
            by_type = {}
            for r in part:
                by_type[r["announce_type"]] = by_type.get(r["announce_type"], 0) + 1
            msg.set_content(
                "ประกาศที่ระบบจะลบในรอบล้างข้อมูลคืนนี้ (ไม่เกี่ยวกับสินค้า OfficeMate อายุเกิน 30 วัน)\n"
                "เก็บอีเมลนี้ไว้ถ้าต้องการใช้ภายหลัง: RSS ของ e-GP ย้อนหลังได้แค่ราว 1 สัปดาห์ ดึงซ้ำไม่ได้\n\n"
                f"เห็นครั้งแรกระหว่าง {days[0]} ถึง {days[-1]} · {len(part):,} รายการ\n"
                + "\n".join(f"- {t}: {c:,}" for t, c in sorted(by_type.items()))
                + "\n\nเปิดไฟล์: แตก zip แล้วเปิด CSV ด้วย Excel (UTF-8) · คอลัมน์ score/category/reason คือผลคัดกรอง\n")
            msg.add_attachment(zipped_csv(part, name), maintype="application", subtype="zip",
                               filename=name.replace(".csv", ".zip"))
            smtp.send_message(msg)
            # Marked only after its email went out; unmarked rows are not deleted and come back next week.
            for i in range(0, len(part), 1000):
                db.rpc("mark_archived", {"p_ids": [r["id"] for r in part[i:i + 1000]]})
            print(f"sent{label}: {len(part)} rows", flush=True)


if __name__ == "__main__":
    main()
