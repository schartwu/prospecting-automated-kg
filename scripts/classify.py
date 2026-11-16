"""Classify every not-yet-classified announcement title and store the verdicts in Supabase.

Clear-cut titles are decided by keyword rules (egp/rules.py); only the ambiguous rest goes to Gemini,
which keeps us inside the free-tier quota. When Gemini is out of quota or overloaded, the rule verdicts
are still saved and the ambiguous titles simply wait for the next run, so the job does not fail.

Only announcement types that lead to a sale or tell us who buys what are classified, plus W2 (winners
changed) so its document is read like a W0. Cancellations and D2/W1 are looked up by project number.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from egp import rules  # noqa: E402
from egp.classify import classify_titles  # noqa: E402
from egp.gemini import Gemini, QuotaExhausted, list_models, try_model  # noqa: E402
from egp.db import DB  # noqa: E402

TYPES = ["P0", "15", "B0", "D0", "W0", "W2"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=100, help="titles per Gemini request")
    ap.add_argument("--max-titles", type=int, default=10000)
    ap.add_argument("--no-ai", action="store_true", help="rules only")
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--try-model", action="append", default=[], help="one test call to this model, then exit")
    args = ap.parse_args()

    key = os.environ.get("GEMINI_API_KEY", "")
    if args.list_models:
        print("\n".join(list_models(key)))
        return
    if args.try_model:
        for model in args.try_model:
            print(try_model(key, model), flush=True)
        return

    db = DB()

    gemini = Gemini(key, lambda model, budget: db.rpc("ai_take", {"p_model": model, "p_budget": budget}))

    # One fetch, most urgent types first (see unclassified_titles). Fetching everything at once keeps
    # rule-decidable titles from getting stuck behind ambiguous ones when Gemini is unavailable.
    # As one JSON array: a plain table response stops at 1,000 rows (PostgREST max rows).
    rows = db.rpc("unclassified_titles_json", {"p_types": TYPES, "p_limit": args.max_titles})

    by_rule, for_ai = [], []
    for r in rows:
        verdict = rules.decide(r["title"])
        (by_rule.append({"title": r["title"], **verdict}) if verdict else for_ai.append(r))
    saved = 0
    for i in range(0, len(by_rule), 500):
        saved += db.rpc("save_title_classifications", {"p_types": TYPES, "p_rows": by_rule[i:i + 500]})
    print(f"rules: {len(by_rule)} of {len(rows)} titles decided "
          f"({sum(v['relevant'] for v in by_rule)} relevant), {saved} announcements saved", flush=True)

    ai_done = ai_relevant = 0
    if not args.no_ai:
        for i in range(0, len(for_ai), args.batch):
            chunk = for_ai[i:i + args.batch]
            try:
                verdicts = classify_titles(chunk, gemini)
            except (QuotaExhausted, RuntimeError) as e:
                # A ::warning:: shows in the run summary without failing the job (no failure email).
                print(f"::warning::Gemini unavailable, {len(for_ai) - ai_done} titles wait for the next run: "
                      f"{str(e)[:300]}", flush=True)
                break
            db.rpc("save_title_classifications", {"p_types": TYPES, "p_rows": verdicts})
            ai_done += len(chunk)
            ai_relevant += sum(v["relevant"] for v in verdicts)
            print(f"  gemini: {ai_done}/{len(for_ai)} titles ({ai_relevant} relevant)", flush=True)

    print(f"DONE titles={len(rows)} rules={len(by_rule)} gemini={ai_done} "
          f"waiting={len(for_ai) - ai_done}")


if __name__ == "__main__":
    main()
