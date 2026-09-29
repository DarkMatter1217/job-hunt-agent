"""SQLite store for postings. Every run re-fetches all sources; this file tracks what is new, still open, or closed.

Tables
  jobs        one row per unique posting (dedupe key = company + title + first city)
  sightings   which source saw which job, and in which run (a job can come from an ATS and from intern-list)
  runs        one row per discovery run
  board_runs  per-source health for each run (ok, job count, error)

A job is marked CLOSED only when every source that ever listed it fetched OK in this run and none of them saw it.
If a source failed, its jobs stay open (a broken fetch must never look like a closed posting).

CLI:
  python scripts/db.py ingest data/raw.jsonl data/intern_list.jsonl [--web data/web_scout.jsonl]
  python scripts/db.py stats
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "jobs.db"
REGISTRY = ROOT / "registry" / "companies.yaml"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  key TEXT PRIMARY KEY,
  company TEXT, title TEXT, location TEXT, url TEXT,
  posted TEXT, description TEXT, extra TEXT,
  tier INTEGER,
  first_seen_run INTEGER, last_seen_run INTEGER,
  first_seen_at TEXT, last_seen_at TEXT,
  status TEXT DEFAULT 'open', closed_at TEXT,
  -- filled by later stages
  filter_result TEXT, flags TEXT, score INTEGER, score_reason TEXT, gaps TEXT,
  tailor_status TEXT, output_dir TEXT
);
CREATE TABLE IF NOT EXISTS sightings (
  key TEXT, source TEXT, source_job_id TEXT, url TEXT, last_seen_run INTEGER,
  PRIMARY KEY (key, source, source_job_id)
);
CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
  n_fetched INTEGER, n_new INTEGER, n_open INTEGER, n_closed INTEGER, n_sources_failed INTEGER
);
CREATE TABLE IF NOT EXISTS board_runs (
  run_id INTEGER, source TEXT, company TEXT, ok INTEGER, n_jobs INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
"""


def connect(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SUFFIXES = r"\b(inc|incorporated|ltd|limited|llc|corp|corporation|co|company|group|technologies|technology|canada|the)\b"


def norm(s):
    s = (s or "").lower().replace("&", "and")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(_SUFFIXES, " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def load_company_index():
    """normalized name or alias -> (canonical name, tier)."""
    reg = yaml.safe_load(open(REGISTRY))["companies"]
    idx = {}
    for c in reg:
        for n in [c["name"], *c.get("aliases", [])]:
            idx[norm(n)] = (c["name"], c["tier"])
    return idx


_GENERIC = set("""intern interns internship internships co op coop coops student students stage stagiaire work term
placement pey winter summer fall autumn spring month months program programme the and of for a an in at to with
with or new position role opportunity job hybrid remote onsite on site full time part time paid 2025 2026 2027 2028
fy25 fy26 fy27 fy28 i ii iii jan january may september sept sep 4 8 12 16 x""".split())


def title_key(title):
    """Order-free set of meaningful title words. Sources reword the same posting ("AI Quality Co-op Intern,
    Evaluation & Security" vs "Co-op/Intern AI Quality, Evaluation & Security"); term, year and level words are
    dropped so these merge. A re-post for a new term merges too; the newest ATS title wins."""
    t = (title or "").lower().replace("&", " ").replace("/", " ")
    t = re.sub(r"\[[^\]]*\]", " ", t)  # tags like "[I]"
    words = re.findall(r"[a-z0-9+#]+", t)
    words = [w for w in words if w not in _GENERIC and not re.fullmatch(r"\d+", w)]
    return " ".join(sorted(set(words))) or (title or "").lower()


def job_key(company, title, location=None):
    # one job per company + title word set; all locations it is offered in are kept in jobs.location
    raw = f"{norm(company)}|{title_key(title)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def merge_locations(old, new):
    parts = [p.strip() for p in re.split(r";|\n", f"{old or ''};{new or ''}") if p.strip()]
    seen, out = set(), []
    for p in parts:
        k = re.sub(r"[^a-z]", "", p.lower())
        if k and k not in seen and k != "multilocation":
            seen.add(k); out.append(p)
    return "; ".join(out)


def source_of(job):
    return f"{job['ats']}:{job['token']}"


def ingest(con, files):
    idx = load_company_index()
    now = _now()
    cur = con.execute("INSERT INTO runs (started_at) VALUES (?)", (now,))
    run_id = cur.lastrowid

    # 1. source health for this run
    ok_sources, n_failed = set(), 0
    for f in files:
        st = Path(f).with_suffix(".status.json")
        if not st.exists():
            print(f"warning: no status file for {f}; its jobs will not be closed this run", file=sys.stderr)
            continue
        for s in json.loads(st.read_text()):
            con.execute("INSERT INTO board_runs VALUES (?,?,?,?,?,?)",
                        (run_id, s["source"], s.get("company"), int(s["ok"]), s["n_jobs"], s.get("error")))
            if s["ok"]:
                ok_sources.add(s["source"])
            else:
                n_failed += 1

    # 2. upsert every fetched posting
    n_fetched = n_new = 0
    ats_first = sorted(files, key=lambda f: ("intern_list" in f) or ("lists" in f) or ("web_scout" in f))  # ATS rows first: direct url + description
    for f in ats_first:
        for line in open(f):
            j = json.loads(line)
            n_fetched += 1
            canon, tier = idx.get(norm(j["company"]), (j["company"].strip(), None))
            key = job_key(canon, j["title"], j["location"])
            row = con.execute("SELECT key, description, url, location, last_seen_run FROM jobs WHERE key=?", (key,)).fetchone()
            if row is None:
                con.execute("""INSERT INTO jobs (key, company, title, location, url, posted, description, extra, tier,
                               first_seen_run, last_seen_run, first_seen_at, last_seen_at, status)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')""",
                            (key, canon, j["title"], j["location"], j["url"], str(j.get("posted") or ""),
                             j.get("description") or "", json.dumps(j.get("extra") or {}), tier,
                             run_id, run_id, now, now))
                n_new += 1
            else:
                # refresh; keep the richer description and a direct (non-aggregator) url
                desc = j.get("description") or ""
                better_desc = desc if len(desc) > len(row["description"] or "") else row["description"]
                url = row["url"] if "jobright.ai" not in (row["url"] or "") else j["url"]
                # same run: another location/source of the same job -> add location; new run: start fresh
                loc = merge_locations(row["location"], j["location"]) if row["last_seen_run"] == run_id else j["location"]
                is_ats = j["ats"] not in ("list", "intern-list", "web")
                title = j["title"] if (is_ats and row["last_seen_run"] != run_id) else None
                con.execute("""UPDATE jobs SET last_seen_run=?, last_seen_at=?, status='open', closed_at=NULL,
                               description=?, url=?, location=?, title=COALESCE(?, title) WHERE key=?""",
                            (run_id, now, better_desc, url, loc, title, key))
            con.execute("""INSERT INTO sightings VALUES (?,?,?,?,?)
                           ON CONFLICT(key, source, source_job_id) DO UPDATE SET last_seen_run=excluded.last_seen_run, url=excluded.url""",
                        (key, source_of(j), j["job_id"], j["url"], run_id))

    # 3. close postings that every (healthy) source stopped listing
    n_closed = 0
    for r in con.execute("SELECT key FROM jobs WHERE status='open' AND last_seen_run < ?", (run_id,)).fetchall():
        srcs = [s["source"] for s in con.execute("SELECT source FROM sightings WHERE key=?", (r["key"],))]
        if srcs and all(s in ok_sources for s in srcs):
            con.execute("UPDATE jobs SET status='closed', closed_at=? WHERE key=?", (now, r["key"]))
            n_closed += 1

    n_open = con.execute("SELECT COUNT(*) FROM jobs WHERE status='open'").fetchone()[0]
    con.execute("""UPDATE runs SET finished_at=?, n_fetched=?, n_new=?, n_open=?, n_closed=?, n_sources_failed=?
                   WHERE run_id=?""", (_now(), n_fetched, n_new, n_open, n_closed, n_failed, run_id))
    con.commit()
    return {"run_id": run_id, "fetched": n_fetched, "new": n_new, "open": n_open, "closed": n_closed,
            "sources_ok": len(ok_sources), "sources_failed": n_failed}


def stats(con):
    last = con.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    by_status = dict(con.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
    failing = [dict(r) for r in con.execute(
        "SELECT source, company, error FROM board_runs WHERE run_id=? AND ok=0", (last["run_id"],))] if last else []
    quiet = [r[0] for r in con.execute(  # boards that returned 0 jobs 3 runs in a row: probably moved ATS
        """SELECT source FROM board_runs WHERE run_id > (SELECT MAX(run_id) - 3 FROM runs)
           GROUP BY source HAVING COUNT(*) >= 3 AND MAX(n_jobs) = 0""")]
    return {"last_run": dict(last) if last else None, "jobs_by_status": by_status,
            "failing_sources": failing, "empty_3_runs": quiet}


def _main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest"); i.add_argument("files", nargs="+")
    sub.add_parser("stats")
    a = ap.parse_args()
    con = connect()
    if a.cmd == "ingest":
        print(json.dumps(ingest(con, [f for f in a.files if Path(f).exists()])))
    else:
        print(json.dumps(stats(con), indent=1))


if __name__ == "__main__":
    _main()
