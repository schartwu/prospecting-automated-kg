"""Read budget, bid date and (for plans) the expected announcement month from the documents of relevant
D0 / B0 / 15 / P0 announcements, the winner and winning price from W0, and the province from all of them.

- D0 / B0 / 15: code patterns on the PDF text first (free). When they find nothing usable, Gemini
  (Flash-Lite first) reads the text; scanned PDFs with no text layer are sent to Gemini as files (Flash
  first, Flash-Lite after), unless a later stage of the project already has a budget.
- P0: the plan page is an HTML table of plan lines (plan no, project, budget, expected month). One page
  covers many plan lines, so it is downloaded once and every relevant line on it gets its row.
- W0: most are an HTML page the code reads ("ได้แก่ <winner> โดยเสนอราคา ... บาท"); e-bidding results are
  a PDF whose text comes out jumbled, so Gemini (Flash-Lite first) reads those.
- Province: the feed names only the top-level department, so it comes from the title and the document
  (egp/provinces.py). Central offices usually name none; the digest keeps those.

Every announcement gets a details row, including failures (with the error), so it is not retried forever.

Before the documents, one page per department still named "หน่วยงานรหัส ..." (local governments found by
scripts/probe_local.py) is read for its name. The document server answers HTTP 511 when read too fast
(seen 30 Oct 2026 after ~100 quick reads), so reads stay one at a time, pause after a refusal, and stop at
--max-minutes; what is left waits for the next evening (30-day window).
"""
import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import date

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from egp import docs, provinces  # noqa: E402
from egp.db import DB  # noqa: E402
from egp.gemini import FLASH_FIRST, LITE_FIRST, Gemini, QuotaExhausted  # noqa: E402

TYPES = ["D0", "B0", "15", "P0", "W0", "W2"]  # W2: winners changed, read like a W0
# Scans: Flash first, Flash-Lite once every Flash model is busy or used up (4 Nov 2026; before, scans waited
# for Flash and 503 "high demand" kept them waiting for days). Lite's numbers off images slipped on
# 2026-10-15 (dropped digits, a line item instead of the total), so its reads are tagged
# ai:<lite model>:scan, and scans whose number would not show are skipped (later_stage_budget).
SCAN_MODELS = FLASH_FIRST
# Gemini 3 models think before answering and those tokens count against maxOutputTokens: with 1,024 the
# JSON of 7 scans came back cut off on 3 Nov 2026 ("Unterminated string"). Quota counts requests, not tokens.
OUTPUT_TOKENS = 8192
BACK_OFF_SECONDS = 20  # after a refused or timed-out read


def transient(e: Exception) -> bool:
    """Server-side hiccups (HTTP 5xx such as 511, timeouts): leave the row unsaved so the next run retries."""
    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        return True
    return isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code >= 500


SCAN_PROMPT_NOTE = "\n(เอกสารแนบเป็นไฟล์ PDF แบบสแกน ให้อ่านจากภาพในไฟล์)"


def ai_extract(gemini, text, pub, pdfs=()):
    """Gemini reads the text, or the PDF files themselves when there is no text layer."""
    def check(ans):
        if ans.get("bid_date"):
            date.fromisoformat(ans["bid_date"])  # raises ValueError on a malformed date
    if text.strip():
        prompt, attachments = docs.EXTRACT_PROMPT.format(text=text[:8000]), ()
    else:
        prompt = docs.EXTRACT_PROMPT.format(text="") + SCAN_PROMPT_NOTE
        attachments = [("application/pdf", b) for b in pdfs[:2]]
    # Scans go to Flash first (SCAN_MODELS); plain text is easy and goes to Flash-Lite first.
    models = SCAN_MODELS if attachments else LITE_FIRST
    ans, model = gemini.json(prompt, docs.EXTRACT_SCHEMA, models, validate=check, max_output=OUTPUT_TOKENS,
                             attachments=attachments)
    bid = date.fromisoformat(ans["bid_date"]) if ans.get("bid_date") else None
    return ans.get("budget_thb"), (bid if docs.plausible(bid, pub) else None), ans.get("bid_time"), \
        ans.get("item_count"), model


class Enricher:
    def __init__(self, db: DB, gemini: Gemini, use_ai: bool):
        self.db, self.gemini, self.ai_ok = db, gemini, use_ai
        self.stats = defaultdict(int)
        self.units = []  # purchasing units named by the pages read (dept_units, scripts/units.py)

    def save(self, detail, dept_code=None):
        self.db.upsert("announcement_details", [detail], on_conflict="announcement_id")
        extracted = detail.get("extracted") or {}
        self.note_unit(extracted.get("unit"), dept_code, extracted.get("province"))

    def note_unit(self, unit, dept_code, province=None):
        """Remember which purchasing unit published a page; saved in batches (RPC note_units)."""
        if not unit:
            return
        self.units.append({"code": unit, "parent_code": dept_code,
                           "province": provinces.from_dept(unit) or province})
        if len(self.units) >= 100:
            self.flush_units()

    def flush_units(self):
        if self.units:
            self.stats["units noted"] += self.db.rpc("note_units", {"p_rows": self.units})
            self.units = []

    def fix_department_name(self, r, text):
        if r.get("dept_needs_name") and r.get("dept_code"):
            name = docs.parse_department(text)
            if name:
                self.db.rpc("set_department_name", {"p_code": r["dept_code"], "p_name": name})

    def document(self, r):
        """D0 / B0 / 15: one document per announcement."""
        pub = date.fromisoformat(r["pub_date"]) if r.get("pub_date") else None
        detail = {"announcement_id": r["id"], "budget_thb": None, "bid_date": None, "item_count": None,
                  "extracted": {}, "error": None}
        try:
            raw, info, pdfs = docs.fetch_text(r["link"])
            text = docs.normalize(raw)
            if r["announce_type"] in ("W0", "W2"):
                return self.winner(r, text, info, detail)
            budget = docs.parse_budget(text)
            bid, bid_time = docs.parse_bid_date(text)
            source = "regex"
            if not docs.plausible(bid, pub):
                bid, bid_time = None, None
            scanned = len(text) <= 200 and bool(pdfs)
            # Only D0 announces the bid date; drafts (B0) and reference prices (15) need just the budget.
            needs_ai = budget is None or (r["announce_type"] == "D0" and bid is None)
            if (needs_ai and scanned and r["announce_type"] != "D0"
                    and self.db.rpc("later_stage_budget_of", {"p_id": r["id"]})):
                # The project shows the budget of its latest stage, so this scan's number would never show.
                self.stats["scan skipped: later stage has a budget"] += 1
                needs_ai, skipped = False, True
            else:
                skipped = False
            if needs_ai and self.ai_ok and (len(text) > 200 or scanned):
                try:
                    a_budget, a_bid, a_time, items, model = ai_extract(self.gemini, text, pub, pdfs if scanned else ())
                    budget, bid = budget or a_budget, bid or a_bid
                    bid_time = bid_time or a_time
                    detail["item_count"] = items
                    source = f"ai:{model}" + (":scan" if scanned else "")
                except QuotaExhausted as e:
                    if scanned:
                        # Every model used up today; read it next run. Logged: these waited silently for
                        # weeks before (scanned 15 of 69109365521, Nov 2026).
                        self.stats["scan waits: AI used up"] += 1
                        print(f"  scan of {r['announce_type']} {r['link'][-40:]} waits: {str(e)[:200]}", flush=True)
                        return
                    self.ai_ok = False
                    print("::warning::Gemini budget used up; remaining documents use code patterns only")
                except RuntimeError as e:
                    if scanned:
                        self.stats["scan waits: error"] += 1
                        print(f"  scan of {r['announce_type']} {r['link'][-40:]} waits: {str(e)[:200]}", flush=True)
                        return
                    detail["error"] = f"ai: {str(e)[:200]}"
            self.fix_department_name(r, text)
            detail.update(budget_thb=budget, bid_date=bid.isoformat() if bid else None,
                          extracted={**info, "bid_time": bid_time, "source": source, "text_chars": len(text),
                                     "scanned": scanned, "province": provinces.from_dept(r.get("dept_code")) or provinces.detect(r["title"], text)})
            if budget is None and bid is None:
                self.stats["missing"] += 0 if skipped else 1
                detail["error"] = detail["error"] or (
                    "scanned document, not read: a later stage has the budget" if skipped
                    else "scanned document, not read" if scanned
                    else "no text in document" if len(text) <= 200 else "fields not found")
            else:
                self.stats["ai" if source.startswith("ai") else "regex"] += 1
                if source.endswith(":scan"):
                    self.stats["scan read: lite" if "lite" in source else "scan read: flash"] += 1
        except Exception as e:  # recorded on the row so the digest still shows the announcement
            if transient(e):
                self.stats["retry later"] += 1
                time.sleep(BACK_OFF_SECONDS)
                return
            self.stats["error"] += 1
            detail["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        self.save(detail, r.get("dept_code"))

    def winner(self, r, text, info, detail):
        """W0 / W2: who won and at what price."""
        winners, price = docs.parse_winners(text)
        source = "regex"
        if not winners and len(text) > 200 and self.ai_ok:
            try:
                ans, model = self.gemini.json(docs.WINNER_PROMPT.format(text=text[:8000]), docs.WINNER_SCHEMA,
                                              LITE_FIRST, max_output=OUTPUT_TOKENS)
                winners = [w.strip() for w in ans["winners"] if w and w.strip()]
                price, source = ans["price_thb"], f"ai:{model}"
            except QuotaExhausted:
                self.ai_ok = False
                print("::warning::Gemini budget used up; remaining documents use code patterns only")
            except RuntimeError as e:
                detail["error"] = f"ai: {str(e)[:200]}"
        detail["extracted"] = {**info, "source": source, "text_chars": len(text), "winners": winners,
                               "winner_price": price, "province": provinces.from_dept(r.get("dept_code")) or provinces.detect(r["title"], text)}
        if winners:
            self.stats["winner " + source.split(":")[0]] += 1
        else:
            self.stats["missing"] += 1
            detail["error"] = detail["error"] or ("no text in document" if len(text) <= 200 else "winner not found")
        self.save(detail, r.get("dept_code"))

    def name_department(self, code, link):
        """A department with a placeholder name: read one of its pages for the real name."""
        try:
            raw, info, _ = docs.fetch_text(link)
            name = docs.parse_department(docs.normalize(raw))
            self.note_unit(info.get("unit"), code)
        except Exception as e:
            self.stats["name retry later" if transient(e) else "name error"] += 1
            if transient(e):
                time.sleep(BACK_OFF_SECONDS)
            return
        if name:
            self.db.rpc("set_department_name", {"p_code": code, "p_name": name})
        self.stats["named" if name else "name not found"] += 1

    def name_announcement(self, r):
        """A Lead stored from the nationwide feed with no department (before 30 Oct 2026): its document's
        heading names the agency ("ประกาศ<หน่วยงาน> เรื่อง ...")."""
        try:
            name = docs.parse_department(docs.normalize(docs.fetch_text(r["link"])[0]))
        except Exception as e:
            self.stats["lead dept retry later" if transient(e) else "lead dept error"] += 1
            if transient(e):
                time.sleep(BACK_OFF_SECONDS)
            return
        if name:
            self.db.rpc("set_announcement_dept_name", {"p_id": r["id"], "p_project": r.get("project_no"), "p_name": name})
        self.stats["lead dept named" if name else "lead dept not found"] += 1

    def add_province(self, link, rows):
        """Rows read before provinces were added: fetch the document again and add only the province."""
        try:
            page = docs.normalize(docs.fetch_text(link)[0])
        except Exception as e:  # the title may still name the province
            print(f"  {link[:80]}: {type(e).__name__}")
            page = ""
        for r in rows:
            self.db.update("announcement_details",
                           {"extracted": {**(r["extracted"] or {}), "province": provinces.from_dept(r.get("dept_code")) or provinces.detect(r["title"], page)}},
                           announcement_id=f"eq.{r['id']}")
            self.stats["province added"] += 1

    def plan_page(self, link, rows):
        """P0: one plan page, many plan lines."""
        try:
            raw, info, _ = docs.fetch_text(link)
            lines = docs.parse_plan_lines(raw)
            page = docs.normalize(raw)
            self.fix_department_name(rows[0], page)  # all lines on a page share a department
        except Exception as e:
            if transient(e):
                self.stats["retry later"] += len(rows)
                time.sleep(BACK_OFF_SECONDS)
                return
            lines, info, page, err = {}, {}, "", f"{type(e).__name__}: {str(e)[:300]}"
        else:
            err = None
        for r in rows:
            line = lines.get(r.get("plan_no") or "")
            self.save({"announcement_id": r["id"], "budget_thb": line["budget_thb"] if line else None,
                       "bid_date": None, "item_count": None,
                       "extracted": {**info, "source": "plan-page",
                                     "expected_month": line["expected_month"] if line else None,
                                     "province": provinces.from_dept(r.get("dept_code")) or provinces.detect(r["title"], page)},
                       "error": err or (None if line else "plan line not found on the page")}, r.get("dept_code"))
            self.stats["plan" if line else "missing"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=800)  # ~830 relevant W0 a day since local governments were added
    ap.add_argument("--min-score", type=int, default=50)
    ap.add_argument("--max-minutes", type=float, default=140,
                    help="stop reading after this long (daily.yml's process job allows 200 minutes)")
    ap.add_argument("--name-departments", type=int, default=60,
                    help="departments with a placeholder name to read a page for, before the documents")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--add-province-days", type=int, default=0,
                    help="instead: add the province to documents read in the last N days without one")
    args = ap.parse_args()

    db = DB()
    gemini = Gemini(os.environ.get("GEMINI_API_KEY", ""),
                    lambda model, budget: db.rpc("ai_take", {"p_model": model, "p_budget": budget}))
    if args.add_province_days:
        e = Enricher(db, gemini, False)
        pages = defaultdict(list)
        for r in db.rpc("details_without_province", {"p_days": args.add_province_days, "p_limit": args.limit}):
            pages[r["link"]].append(r)
        print(f"{len(pages)} documents to read for the province", flush=True)
        for link, page_rows in pages.items():
            e.add_province(link, page_rows)
            time.sleep(1)
        print("DONE", dict(e.stats))
        return

    deadline = time.monotonic() + args.max_minutes * 60
    e = Enricher(db, gemini, not args.no_ai)
    for d in db.rpc("departments_to_name", {"p_limit": args.name_departments}) if args.name_departments else []:
        e.name_department(d["code"], d["link"])
        time.sleep(1)
    for r in db.rpc("nationwide_to_name", {"p_limit": 30}) if args.name_departments else []:
        e.name_announcement(r)
        time.sleep(1)
    print(f"department names: {dict(e.stats)}", flush=True)

    rows = db.rpc("announcements_to_enrich", {"p_types": TYPES, "p_min_score": args.min_score,
                                              "p_limit": args.limit})
    plans = defaultdict(list)
    for r in rows:
        if r["announce_type"] == "P0":
            plans[r["link"]].append(r)
    others = [r for r in rows if r["announce_type"] != "P0"]  # W0 come last (see the SQL order)
    print(f"{len(others)} documents and {len(plans)} plan pages ({len(rows) - len(others)} plan lines) to read",
          flush=True)

    # Plans go before W0: the SQL order is D0, B0, 15, P0, W0, and W0 are most of the rows.
    work = ([(e.document, (r,)) for r in others if r["announce_type"] != "W0"]
            + [(e.plan_page, item) for item in plans.items()]
            + [(e.document, (r,)) for r in others if r["announce_type"] == "W0"])
    done = 0
    for fn, fn_args in work:
        if time.monotonic() > deadline:
            print(f"time cap of {args.max_minutes} min reached; {len(work) - done} reads wait for the next run")
            break
        fn(*fn_args)
        done += 1
        time.sleep(1)  # be gentle with the e-GP document server
    e.flush_units()
    print("DONE", dict(e.stats))


if __name__ == "__main__":
    main()
