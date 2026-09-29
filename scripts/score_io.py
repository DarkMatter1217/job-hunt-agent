"""Helper for the fit-scorer agent: hands out unscored jobs in priority order and validates saved scores.

  python scripts/score_io.py pending [--limit 8]      # next batch (JSON)
  python scripts/score_io.py save '<json>'             # one score; validated against the rubric ranges
  python scripts/score_io.py summary                   # counts
  python scripts/score_io.py prescore KEY              # debug: deterministic priority of one job

Priority (prescore) is deterministic and cheap, so the daily LLM cap is spent on the most promising jobs first.
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "jobs.db"
FACTS = yaml.safe_load(open(ROOT / "profile" / "facts.yaml"))
RANGES = {"skills": (0, 25), "ai_relevance": (0, 25), "level_term": (0, 10), "eligibility": (0, 25), "location": (0, 15)}
ROLE_TYPES = ("llm", "ml_ai", "ai_adjacent_tech", "software", "non_technical")

def _ids(node):
    if isinstance(node, dict):
        if isinstance(node.get("id"), str):
            yield node["id"]
        for v in node.values():
            yield from _ids(v)
    elif isinstance(node, list):
        for v in node:
            yield from _ids(v)


FACT_IDS = set(_ids(FACTS))  # every `id` anywhere in facts.yaml

KEYWORDS = re.compile(r"\b(llms?|large language|rag|retrieval|langchain|langgraph|agents?|agentic|embeddings?|vector|"
                      r"faiss|pinecone|nlp|natural language|prompt|generative|genai|fastapi|flask|python|hallucination|"
                      r"evaluation|semantic search|transformers|hugging ?face|ocr|document)\b", re.I)


def prescore(j):
    f = j["flags"] or ""
    s = {"market:home": 30, "market:canada": 22, "market:remote_unspecified": 12, "market:india": 8, "market:us": 4}
    p = next((v for k, v in s.items() if f'"{k}"' in f), 0)
    p += {"family:ml_ai": 20, "family:research": 14, "family:swe": 8}.get(next((k for k in ("family:ml_ai", "family:research", "family:swe") if f'"{k}"' in f), ""), 0)
    p += {1: 12, 2: 7, 3: 3}.get(j["tier"], 0)
    p += min(20, 2 * len(set(m.lower() for m in KEYWORDS.findall((j["title"] or "") + " " + (j["description"] or "")))))
    for bad, pen in (("citizenship_or_pr", 40), ("security_clearance", 40), ("graduation_mismatch", 25),
                     ("french_required", 12), ("phd_preferred", 8), ("us_work_auth", 10), ("no_description", 5)):
        if f'"{bad}"' in f:
            p -= pen
    return p


def con_():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    return c


def cmd_pending(a):
    con = con_()
    rows = con.execute("""SELECT key, company, tier, title, location, posted, url, flags, description FROM jobs
                          WHERE status='open' AND filter_result='pass' AND score IS NULL""").fetchall()
    if a.keys:
        wanted = a.keys.split(",")
        rows = [r for r in rows if r["key"] in wanted]
    elif not a.include_us:  # US roles are capped at 40 and never tailored; don't spend LLM usage on them
        rows = [r for r in rows if '"market:us"' not in (r["flags"] or "")]
    rows = sorted(rows, key=prescore, reverse=True)[: a.limit]
    out = [{"key": r["key"], "company": r["company"], "tier": r["tier"], "title": r["title"], "location": r["location"],
            "posted": r["posted"], "url": r["url"],
            "flags": [f for f in json.loads(r["flags"] or "[]") if not f["flag"].startswith(("family:", "market:"))],
            "description": (r["description"] or "")[:3500]} for r in rows]
    print(json.dumps(out, indent=1))


def cmd_save(a):
    try:
        s = json.loads(a.row)
    except json.JSONDecodeError as e:
        sys.exit(f"REJECTED: invalid JSON ({e})")
    need = ["key", "subscores", "why", "gaps", "matched_facts", "confidence", "deal_breaker", "role_type"]
    miss = [k for k in need if k not in s]
    if miss:
        sys.exit(f"REJECTED: missing {miss}")
    sub = s["subscores"]
    for k, (lo, hi) in RANGES.items():
        if not isinstance(sub.get(k), int) or not lo <= sub[k] <= hi:
            sys.exit(f"REJECTED: subscores.{k} must be an int in [{lo},{hi}]")
    total = sum(sub[k] for k in RANGES)
    bad_ids = [f for f in s["matched_facts"] if f not in FACT_IDS]
    if bad_ids:
        sys.exit(f"REJECTED: unknown fact ids {bad_ids}; use ids from profile/facts.yaml")
    if s["confidence"] not in ("high", "medium", "low"):
        sys.exit("REJECTED: confidence must be high|medium|low")
    if len(s["why"]) > 400:
        sys.exit("REJECTED: why is too long (max ~2 sentences)")
    con = con_()
    if not con.execute("SELECT 1 FROM jobs WHERE key=?", (s["key"],)).fetchone():
        sys.exit("REJECTED: unknown job key")
    if s["role_type"] not in ROLE_TYPES:
        sys.exit(f"REJECTED: role_type must be one of {ROLE_TYPES}")
    job = con.execute("SELECT flags FROM jobs WHERE key=?", (s["key"],)).fetchone()
    caps = []
    if '"market:us"' in (job["flags"] or ""):
        caps.append(("us_role", 40))           # no US work authorization
    if s["role_type"] == "non_technical":
        caps.append(("non_technical", 40))     # a "maybe", never tailored
    if s["deal_breaker"]:
        caps.append(("deal_breaker", 25))      # can never reach the tailoring threshold
    raw_total = total
    for _, c in caps:
        total = min(total, c)
    detail = {"subscores": sub, "raw_total": raw_total, "caps": [c[0] for c in caps], "role_type": s["role_type"],
              "matched_facts": s["matched_facts"], "confidence": s["confidence"], "deal_breaker": s["deal_breaker"],
              "rubric": "v2"}
    con.execute("UPDATE jobs SET score=?, score_reason=?, gaps=? WHERE key=?",
                (total, json.dumps({"why": s["why"], **detail}), json.dumps(s["gaps"]), s["key"]))
    con.commit()
    print(f"saved {s['key']}: {total}")


def cmd_summary(_):
    con = con_()
    print(json.dumps({
        "scored": con.execute("SELECT COUNT(*) FROM jobs WHERE score IS NOT NULL AND status='open'").fetchone()[0],
        "unscored_passing_non_us": con.execute("SELECT COUNT(*) FROM jobs WHERE score IS NULL AND filter_result='pass' AND status='open' AND flags NOT LIKE '%market:us%'").fetchone()[0],
        "unscored_us_skipped": con.execute("SELECT COUNT(*) FROM jobs WHERE score IS NULL AND filter_result='pass' AND status='open' AND flags LIKE '%market:us%'").fetchone()[0],
        "score_70_plus": con.execute("SELECT COUNT(*) FROM jobs WHERE score >= 70 AND status='open'").fetchone()[0],
    }))


def cmd_prescore(a):
    r = con_().execute("SELECT * FROM jobs WHERE key=?", (a.key,)).fetchone()
    print(prescore(r))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pending"); p.add_argument("--limit", type=int, default=8); p.add_argument("--keys"); p.add_argument("--include-us", action="store_true")
    s = sub.add_parser("save"); s.add_argument("row")
    sub.add_parser("summary")
    q = sub.add_parser("prescore"); q.add_argument("key")
    a = ap.parse_args()
    {"pending": cmd_pending, "save": cmd_save, "summary": cmd_summary, "prescore": cmd_prescore}[a.cmd](a)


if __name__ == "__main__":
    main()
