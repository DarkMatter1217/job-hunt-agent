"""Helper for the web-scout agent: what to visit, and a validated way to save what it finds.

    python scripts/scout_io.py start                       # new run: clears data/web_scout.jsonl + status
    python scripts/scout_io.py targets [--tier 2] [--limit N]
    python scripts/scout_io.py add '<json row>'            # one posting
    python scripts/scout_io.py status "<source>" ok|fail [--n N] [--error TEXT]
    python scripts/scout_io.py new-board <company> <ats> <token> --evidence <posting url> [--host H --site S]
    python scripts/scout_io.py finish                      # prints the run summary

Rows: {company, title, location, url, posted?, description?, term?}. The agent never writes files directly.
"""
import argparse
import hashlib
import os
import json
import re
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "web_scout.jsonl"
STATUS = OUT.with_suffix(".status.json")
REGISTRY = Path(os.environ.get("JOB_AGENT_REGISTRY", ROOT / "registry" / "companies.yaml"))
sys.path.insert(0, str(Path(__file__).parent))

STUDENT = re.compile(r"intern|co-?op|student|stage|stagiaire|work term|placement|trainee|apprentice|summer analyst", re.I)


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _liveness(url):
    """'live' (2xx/3xx), 'dead' (404/410), or 'unverified' (blocked, 5xx, timeout)."""
    import requests
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/128"}, timeout=15,
                            allow_redirects=True, stream=True)
        code = resp.status_code
        resp.close()
    except Exception:
        return "unverified"
    if code in (404, 410):
        return "dead"
    return "live" if code < 400 else "unverified"


def cmd_start(_):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("")
    STATUS.write_text("[]")
    print("web-scout run started; output cleared")


def cmd_targets(a):
    reg = yaml.safe_load(open(REGISTRY))
    targets = [{"kind": "company", "source": f"web:{_slug(c['name'])}", "name": c["name"], "tier": c["tier"],
                "city": c.get("city"), "url": c.get("url"), "search_hint": c.get("search_hint"), "why": c.get("why")}
               for c in reg["companies"] if c.get("ats") == "web" and c["tier"] <= a.tier]
    targets.sort(key=lambda t: t["tier"])
    targets += [{"kind": "board", "source": f"web:{_slug(g['name'])}", "name": g["name"], "url": g["url"]}
                for g in reg.get("aggregators", []) if g.get("fetcher") == "web-scout"
                and "githubusercontent" not in g.get("url", "")]  # GitHub lists are parsed by lists_fetch.py
    if a.limit:
        targets = targets[: a.limit]
    print(json.dumps(targets, indent=1))


def cmd_add(a):
    try:
        r = json.loads(a.row)
    except json.JSONDecodeError as e:
        sys.exit(f"REJECTED: not valid JSON ({e})")
    missing = [k for k in ("company", "title", "location", "url") if not r.get(k)]
    if missing:
        sys.exit(f"REJECTED: missing {missing}")
    if not re.match(r"https?://", r["url"]):
        sys.exit("REJECTED: url must be http(s)")
    if not STUDENT.search(r["title"]):
        sys.exit("REJECTED: title is not an intern/co-op/student role; do not save full-time roles")
    live = _liveness(r["url"])
    if live == "dead":
        sys.exit("REJECTED: posting URL is dead (404/410). Search results often point to old postings; skip it.")
    row = {"company": r["company"].strip(), "ats": "web", "token": _slug(r.get("source_name") or r["company"]),
           "job_id": hashlib.sha1(r["url"].encode()).hexdigest()[:16], "title": r["title"].strip(),
           "location": r["location"].strip(), "url": r["url"], "posted": r.get("posted"),
           "description": (r.get("description") or "")[:20000], "extra": {"term": r.get("term"), "url_check": live}}
    with open(OUT, "a") as f:
        f.write(json.dumps(row) + "\n")
    print(f"saved: {row['company']} | {row['title']}")


def cmd_status(a):
    st = json.loads(STATUS.read_text()) if STATUS.exists() else []
    st = [s for s in st if s["source"] != a.source]
    st.append({"source": a.source, "company": None, "ok": a.result == "ok", "n_jobs": a.n, "error": a.error})
    STATUS.write_text(json.dumps(st, indent=1))
    print(f"status recorded: {a.source} {a.result} n={a.n}")


BOARD_URL = {  # a posting URL on each ATS must look like this for the given token
    "greenhouse": r"https?://(job-boards|boards)(\.eu)?\.greenhouse\.io/{t}/jobs/(\d+)",
    "lever": r"https?://jobs\.lever\.co/{t}/([0-9a-f-]{{36}})",
    "ashby": r"https?://jobs\.ashbyhq\.com/{t}/([0-9a-f-]{{36}})",
    "smartrecruiters": r"https?://(jobs|careers)\.smartrecruiters\.com/{t}/(\d+)",
    "workable": r"https?://apply\.workable\.com/{t}/j/([0-9A-F]+)",
    "recruitee": r"https?://{t}\.recruitee\.com/o/([\w-]+)",
    "workday": r"https?://{h}/(?:[a-z]{{2}}-[A-Z]{{2}}/)?{s}/job/.*_([A-Z0-9-]+)$",
}


def cmd_new_board(a):
    """Add an ATS board found on a company's official careers page.

    Same-name collisions are common (e.g. 'vector', 'karbon', 'sanctuary' belong to unrelated companies), so the
    evidence must be a posting URL on this exact board, and that posting must appear in the board's live feed."""
    import ats_fetch
    pat = BOARD_URL.get(a.ats)
    if not pat:
        sys.exit(f"REJECTED: unsupported ats {a.ats}")
    pat = pat.format(t=re.escape(a.token), h=re.escape(a.host or ""), s=re.escape(a.site or ""))
    m = re.match(pat, a.evidence.split("?")[0].rstrip("/"))
    if not m:
        sys.exit("REJECTED: evidence must be a posting URL on this board, e.g. https://jobs.lever.co/<token>/<id>")
    posting_id = m.groups()[-1].lower()
    entry = {"name": a.company, "ats": a.ats, "token": a.token, "host": a.host, "site": a.site}
    if a.ats == "workday":
        entry["queries"] = ["intern", "co-op", "student"]
    try:
        jobs = ats_fetch.fetch(entry)
    except Exception as e:
        sys.exit(f"REJECTED: board does not respond ({e})")
    if not any(posting_id in (j["url"] or "").lower() or posting_id == str(j["job_id"]).lower() for j in jobs):
        sys.exit("REJECTED: the evidence posting is not on this board's live feed, so it may be a different company "
                 "with the same name. Do not register it.")
    reg_text = REGISTRY.read_text()
    reg = yaml.safe_load(reg_text)
    if any(c.get("token") == a.token and c.get("ats") == a.ats for c in reg["companies"]):
        sys.exit("SKIPPED: board already in registry")
    existing = next((c for c in reg["companies"] if c["name"].lower() == a.company.lower()), None)
    if existing and existing.get("ats") == "web":  # upgrade a web entry to a real feed
        existing.update({k: v for k, v in entry.items() if v and k != "name"})
        existing.update({"added_by": "web-scout", "needs_review": True, "evidence": a.evidence, "added": date.today().isoformat()})
    else:
        reg["companies"].append({**{k: v for k, v in entry.items() if v}, "tier": 3, "city": None,
                                 "why": "found by web-scout; review tier", "added_by": "web-scout",
                                 "needs_review": True, "evidence": a.evidence, "added": date.today().isoformat()})
    header = reg_text.split("aggregators:")[0]
    REGISTRY.write_text(header + yaml.safe_dump({"aggregators": reg["aggregators"]}, sort_keys=False, width=150) + "\n"
                        + yaml.safe_dump({"companies": reg["companies"]}, sort_keys=False, allow_unicode=True, width=150))
    print(f"ADDED: {a.company} -> {a.ats}:{a.token} ({len(jobs)} postings, sample: {jobs[0]['title']})")


def cmd_finish(_):
    rows = [json.loads(l) for l in open(OUT)] if OUT.exists() else []
    st = json.loads(STATUS.read_text()) if STATUS.exists() else []
    print(json.dumps({"rows_saved": len(rows), "targets_ok": sum(s["ok"] for s in st),
                      "targets_failed": [s["source"] for s in st if not s["ok"]]}, indent=1))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start")
    t = sub.add_parser("targets"); t.add_argument("--tier", type=int, default=3); t.add_argument("--limit", type=int)
    ad = sub.add_parser("add"); ad.add_argument("row")
    s = sub.add_parser("status"); s.add_argument("source"); s.add_argument("result", choices=["ok", "fail"])
    s.add_argument("--n", type=int, default=0); s.add_argument("--error")
    nb = sub.add_parser("new-board"); nb.add_argument("company"); nb.add_argument("ats"); nb.add_argument("token")
    nb.add_argument("--evidence", required=True); nb.add_argument("--host"); nb.add_argument("--site")
    sub.add_parser("finish")
    a = ap.parse_args()
    {"start": cmd_start, "targets": cmd_targets, "add": cmd_add, "status": cmd_status,
     "new-board": cmd_new_board, "finish": cmd_finish}[a.cmd](a)


if __name__ == "__main__":
    main()
