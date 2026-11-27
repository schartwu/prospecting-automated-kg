"""Email each Sale (GXX) the leads newly handed to them in KG Lead (phase 2, 27 Nov 2026).

DV / Tele hand a lead to a Sale on the web app (lead_assignments). Every hour this script sends each Sale one
email listing the leads handed to them since the last email, with their own link to each lead; nothing new,
no email. A hand-out counts once it is still the lead's latest one, so a lead handed to G01 and moved to G02
before the hour only reaches G02. Sent hand-outs get notified_at, so they are not sent again.

  python scripts/assign_notify.py                  # list who would get what (no email, no links printed)
  python scripts/assign_notify.py --to a@b.com     # test: every Sale's email goes to a@b.com, nothing marked
  python scripts/assign_notify.py --send           # the real run (hourly)
  --app-url https://.../preview/                   # links to the test page instead of the web app
"""

import argparse
import html
import os
import sys
from datetime import date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from egp.db import DB  # noqa: E402
from digest import LEAD_APP_URL, SENDER_NAME, TYPE_LABEL, send, update_button  # noqa: E402

BKK = ZoneInfo("Asia/Bangkok")
STAGE = {"P0": 0, "B0": 1, "15": 2, "D0": 3}
TH_MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]


def th_date(iso):
    d = date.fromisoformat(str(iso)[:10])
    return f"{d.day} {TH_MONTHS[d.month - 1]} {(d.year + 543) % 100:02d}"


def th_time(ts):
    t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(BKK)
    return f"{th_date(t.date().isoformat())} {t:%H:%M}"


def money(v):
    try:
        return f"฿{float(v):,.0f}"
    except (TypeError, ValueError):
        return "ไม่ระบุงบ"


def pending(db):
    """The latest hand-out of each lead, when it gives the lead to someone and has not been emailed yet."""
    rows = db.select("lead_assignments", select="id,lead_key,assignee_id,assigned_by,assigned_at,notified_at",
                     order="assigned_at.asc,id.asc")
    latest = {}
    for r in rows:
        latest[r["lead_key"]] = r
    return [r for r in latest.values() if r["assignee_id"] and not r["notified_at"]]


def lead_rows(db, keys):
    """key -> the lead's latest announcement (as on the web app), its budget the project's."""
    rows = db.rpc("web_list_keys", {"p_keys": keys}) or []
    best = {}
    for r in rows:
        for k in {r["lead_key"], f"P:{r['linked_plan']}" if r.get("linked_plan") else None} & set(keys):
            if k not in best or STAGE.get(r["type"], -1) > STAGE.get(best[k]["type"], -1):
                best[k] = r
    return best


def email_html(person, items, app_url, today):
    lines = []
    for h, r in items:
        link = f"{app_url}#{urlencode({'t': person['token'], 'lead': h['lead_key']})}"
        if r:
            left = (date.fromisoformat(r["bid_date"]) - today).days if r.get("bid_date") else None
            when = (f"ยื่นซอง {th_date(r['bid_date'])}{' ' + html.escape(str(r['bid_time'])) if r.get('bid_time') else ''}"
                    + (f" · <b style='color:#b3261e'>อีก {left} วัน</b>" if left is not None and 0 <= left <= 3
                       else f" · อีก {left} วัน" if left is not None and left > 3 else "")) if r.get("bid_date") else ""
            title = (f"<span style='color:#666;font-size:12px'>{html.escape(TYPE_LABEL.get(r['type'], r['type']))}"
                     f" · คะแนน {html.escape(str(r.get('score', '')))}</span><br><b>{html.escape(r['title'] or '')}</b>"
                     f"<div style='color:#555;font-size:13px'>{html.escape(r.get('dept_name') or '')}"
                     f" · {html.escape(r.get('province') or 'ไม่ระบุจังหวัด')}"
                     f"{' · ' + when if when else ''}</div>")
            budget = money(r.get("budget_thb"))
        else:
            title, budget = f"<b>{html.escape(h['lead_key'])}</b>", "-"
        lines.append(
            "<tr>"
            f"<td style='padding:8px;border-bottom:1px solid #ddd;vertical-align:top'>{title}"
            f"<div style='color:#666;font-size:12px'>มอบหมายโดย {html.escape(h['by'])} · {th_time(h['assigned_at'])}</div></td>"
            f"<td style='padding:8px;border-bottom:1px solid #ddd;vertical-align:top;text-align:right;white-space:nowrap'>"
            f"{budget}{update_button(link, 'เปิดงาน')}</td></tr>")
    return (f"<div style='font-family:Tahoma,Arial,sans-serif;font-size:14px;color:#15212a;max-width:720px'>"
            f"<p>เรียน {html.escape(person['name'])}</p>"
            f"<p>DV / Tele มอบหมายงานใหม่ให้คุณ <b>{len(items)} งาน</b> กด “เปิดงาน” แล้วกด “รับเรื่อง” เมื่อเริ่มงาน "
            f"และอัปเดตสถานะตามขั้นตอน งานทั้งหมดของคุณอยู่ที่หน้า “งานของฉัน”</p>"
            f"<table style='border-collapse:collapse;width:100%'>{''.join(lines)}</table>"
            f"<p style='color:#666;font-size:12px;margin-top:16px'>ลิงก์ในอีเมลนี้เป็นลิงก์ส่วนตัวของคุณ กรุณาอย่าส่งต่อ · "
            f"อีเมลอัตโนมัติจาก KG Lead (e-GP Alert) ส่งเมื่อมีงานใหม่เท่านั้น</p></div>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="email each Sale and mark the hand-outs sent")
    ap.add_argument("--to", help="test: send every Sale's email to these addresses (comma separated), mark nothing")
    ap.add_argument("--app-url", default=LEAD_APP_URL)
    args = ap.parse_args()
    app_url = args.app_url.split("#")[0]

    db = DB()
    todo = pending(db)
    people = {p["id"]: p for p in db.select("lead_users", select="id,name,email,token,role,can_assign,active")}
    for h in todo:
        h["by"] = people.get(h["assigned_by"], {}).get("name", "")
    by_person = {}
    for h in todo:
        p = people.get(h["assignee_id"])
        # only an active Sale with a real address; anyone else is skipped (and stays unsent)
        if not p or not p["active"] or p["role"] != "sale" or p["can_assign"] or p["email"].endswith(".local"):
            print(f"::warning::hand-out {h['id']} ({h['lead_key']}) goes to an account that gets no email; skipped")
            continue
        by_person.setdefault(p["id"], []).append(h)
    if not by_person:
        print("no new hand-outs; nothing to send")
        return

    leads = lead_rows(db, sorted({h["lead_key"] for hs in by_person.values() for h in hs}))
    now, today = datetime.now(BKK), datetime.now(BKK).date()
    tests = [e.strip() for e in (args.to or "").split(",") if e.strip()]
    for pid, hs in by_person.items():
        p = people[pid]
        # nearest bid day first, then the order they were handed out
        items = sorted(((h, leads.get(h["lead_key"])) for h in hs),
                       key=lambda x: ((x[1] or {}).get("bid_date") or "9999", x[0]["assigned_at"]))
        subject = f"📌 KG Lead: งานใหม่ที่ได้รับมอบหมาย {len(items)} งาน ({now:%d/%m/%Y %H:%M})"
        if not (args.send or tests):
            print(f"{p['name']}: {len(items)} lead(s) -> {', '.join(h['lead_key'] for h in hs)}")
            continue
        body = email_html(p, items, app_url, today)
        if tests:
            send(f"[ทดสอบ ถึง {p['name']}] {subject}", body, None, tests, sender_name=SENDER_NAME)
            print(f"test copy for {p['name']} ({len(items)} lead(s)) sent")
            continue
        send(subject, body, None, [p["email"]], sender_name=SENDER_NAME)
        ids = ",".join(str(h["id"]) for h in hs)
        db.update("lead_assignments", {"notified_at": datetime.now(BKK).isoformat()}, id=f"in.({ids})")
        print(f"{p['name']}: {len(items)} lead(s) sent")


if __name__ == "__main__":
    main()
