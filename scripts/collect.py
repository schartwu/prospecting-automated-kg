"""Collect announcements from the e-GP RSS feed into data/runs/<mode>/<date>/<HHMM>[-s<k>]/.

Modes:
  daily   - nationwide + every active department (Supabase `departments`) x DAILY_TYPES.
            --shard k --shards n splits the departments so n machines (each with its own IP)
            can share the work; the server answers HTTP 429 above ~2 parallel requests per IP.
  capped  - only the (department, type) feeds that hit the 20-item cap in the last 7 days
            (Supabase `capped_feeds`), re-read during the afternoon so busy departments lose less.
  sweep   - deptId codes not known yet, with W0 (the most frequent type, ~1,300 a week), to discover
            departments that exist. By default the next --codes untried codes (Supabase
            `next_sweep_codes`); --start/--end sweeps a fixed range instead. Runs at 21:00 nightly.

Each run writes requests.jsonl (one line per feed request) and items.jsonl (one line per RSS item);
scripts/load_runs.py loads them into Supabase.
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from egp.db import DB  # noqa: E402
from egp.feed import fetch  # noqa: E402

BKK = ZoneInfo("Asia/Bangkok")
ROOT = os.path.join(os.path.dirname(__file__), "..")

# The types the digest and the web app use.
DAILY_TYPES = ["P0", "15", "B0", "D0", "W0", "D1"]
# Winners withdrawn (W1) or changed (W2) are rare, so they are fetched once a week, on Friday evening
# (the RSS keeps about a week), and the web app shows the new result on Monday. D2 (invitation changed)
# returned no item at all from 2 to 29 Oct 2026, nationwide included, while costing 1 request in 7, so it
# moved here too: kept weekly in case the feed starts serving it.
WEEKLY_TYPES = ["W1", "W2", "D2"]
WEEKLY_DAY = 4  # Friday
NATIONWIDE = "(ไม่ระบุหน่วยงาน)"


def build_requests(db: DB, args) -> list[tuple[dict, dict]]:
    """(params, extra) pairs. extra is stored next to each item (dept_name)."""
    if args.mode == "daily":
        # deptId: ministries' departments (4 digits) and local governments (7 digits, e.g. 3100001
        # กรุงเทพมหานคร); deptsubId: purchasing units (15 digits) of departments that fill 20 items a day.
        # The API returns at most 1,000 rows per call, so read in pages.
        depts = []
        while True:
            page = db.select("departments", param="in.(deptId,deptsubId)", active="is.true",
                             select="code,name,param", order="code", limit=1000, offset=len(depts))
            depts += page
            if len(page) < 1000:
                break
        mine = [d for i, d in enumerate(depts) if i % args.shards == args.shard]
        types = DAILY_TYPES + (WEEKLY_TYPES if datetime.now(BKK).weekday() == WEEKLY_DAY else [])
        reqs = [({"anounceType": t}, {"dept_name": NATIONWIDE}) for t in types] if args.shard == 0 else []
        reqs += [({d["param"]: d["code"], "anounceType": t}, {"dept_name": d["name"]})
                 for d in mine for t in types]
        return reqs
    if args.mode == "capped":
        feeds = db.rpc("recent_capped_feeds", {"p_days": 7})
        return [({"deptId": f["dept_code"], "anounceType": f["announce_type"]}, {"dept_name": f["dept_name"]})
                for f in feeds]
    if args.mode == "sweep":
        if args.start is None:
            codes = [r["code"] for r in db.rpc("next_sweep_codes", {"p_limit": args.codes})]
        else:
            known = {d["code"] for d in db.select("departments", select="code")}
            codes = [f"{n:04d}" for n in range(args.start, args.end + 1) if f"{n:04d}" not in known]
        return [({"deptId": code, "anounceType": "W0"}, {"dept_name": ""}) for code in codes]
    raise ValueError(args.mode)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "capped", "sweep"], required=True)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--start", type=int, default=None, help="sweep: first deptId number (default: next untried)")
    ap.add_argument("--end", type=int, default=9999, help="sweep: last deptId number")
    ap.add_argument("--codes", type=int, default=2500, help="sweep: how many untried codes per run")
    ap.add_argument("--max-minutes", type=float, default=170,
                    help="stop sending new requests after this long and keep what was collected")
    args = ap.parse_args()

    now = datetime.now(BKK)
    suffix = f"-s{args.shard}" if args.shards > 1 else ""
    out_dir = os.path.join(ROOT, "data", "runs", args.mode, now.strftime("%Y-%m-%d"), now.strftime("%H%M") + suffix)
    os.makedirs(out_dir, exist_ok=True)

    reqs = build_requests(DB(), args)
    print(f"{now:%Y-%m-%d %H:%M} BKK | mode={args.mode} shard={args.shard}/{args.shards} | "
          f"{len(reqs)} requests | workers={args.workers}", flush=True)

    deadline = time.monotonic() + args.max_minutes * 60
    ok = failed = n_items = capped = 0
    with open(os.path.join(out_dir, "requests.jsonl"), "w", encoding="utf-8") as freq, \
         open(os.path.join(out_dir, "items.jsonl"), "w", encoding="utf-8") as fitem:

        def record(r, extra):
            nonlocal ok, failed, n_items, capped
            ok += r.ok
            failed += not r.ok
            n_items += len(r.items)
            capped += r.capped
            freq.write(json.dumps({
                **r.params, **extra, "ok": r.ok, "http": r.http_status, "error": r.error,
                "n_items": len(r.items), "capped": r.capped, "elapsed": round(r.elapsed, 2),
            }, ensure_ascii=False) + "\n")
            for it in r.items:
                fitem.write(json.dumps({**r.params, **extra, "fetched_at": now.isoformat(), **it},
                                       ensure_ascii=False) + "\n")

        def run(batch, retries, set_aside, label):
            """Fetch a batch. With set_aside, failures are returned (params, extra, result) instead of
            recorded. Stops at the time budget."""
            later = []
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(fetch, p, retries=retries): (p, extra) for p, extra in batch}
                for i, fut in enumerate(as_completed(futures), 1):
                    r, (p, extra) = fut.result(), futures[fut]
                    if r.ok or not set_aside:
                        record(r, extra)
                    else:
                        later.append((p, extra, r))
                    if i % 200 == 0:
                        print(f"  {label} {i}/{len(batch)} ok={ok} failed={failed} items={n_items} "
                              f"capped={capped} set aside={len(later)}", flush=True)
                        freq.flush()
                        fitem.flush()
                    if time.monotonic() > deadline:
                        pool.shutdown(wait=False, cancel_futures=True)
                        print(f"  time budget of {args.max_minutes} min reached after {i}/{len(batch)} "
                              f"requests", flush=True)
                        break
            return later

        # One try each first: an IP the server is limiting (HTTP 429) used to spend ~3 minutes per request on
        # back-off and retries (collect shard 3 ran 2-2.7 h on 30 Oct and 1 Nov 2026 while the others took
        # ~45 min). Failed requests are set aside and tried again, with retries, once the rest is done.
        later = run(reqs, 0, True, "pass 1")
        if later and time.monotonic() < deadline:
            print(f"  retrying {len(later)} failed requests", flush=True)
            run([(p, extra) for p, extra, _ in later], 2, False, "retry")
        elif later:
            for _, extra, r in later:
                record(r, extra)

    print(f"DONE ok={ok} failed={failed} items={n_items} capped_feeds={capped} -> {out_dir}", flush=True)


if __name__ == "__main__":
    main()
