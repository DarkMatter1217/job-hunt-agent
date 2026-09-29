"""Stage 1 of /job-hunt: fetch every source live, then update data/jobs.db.

    python scripts/discover.py            # ATS boards + intern-list + GitHub lists, then ingest
    python scripts/discover.py --skip-intern-list --skip-lists

web-scout (an agent, not this script) writes data/web_scout.jsonl + .status.json; if present and fresh
(written today), it is ingested too.
"""
import argparse
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run(cmd):
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stderr[-2000:], file=sys.stderr)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-intern-list", action="store_true")
    ap.add_argument("--skip-lists", action="store_true")
    a = ap.parse_args()

    procs = [subprocess.Popen([PY, "-W", "ignore", "scripts/ats_fetch.py", "harvest",
                               "--registry", "registry/companies.yaml", "--out", "data/raw.jsonl"],
                              cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)]
    if not a.skip_intern_list:
        procs.append(subprocess.Popen([PY, "-W", "ignore", "scripts/intern_list.py", "--out", "data/intern_list.jsonl"],
                                      cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    names, outs = ["ats"], ["data/raw.jsonl"]
    if not a.skip_intern_list:
        names.append("intern_list"); outs.append("data/intern_list.jsonl")
    if not a.skip_lists:
        procs.append(subprocess.Popen([PY, "-W", "ignore", "scripts/lists_fetch.py", "--out", "data/lists.jsonl"],
                                      cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        names.append("lists"); outs.append("data/lists.jsonl")
    summary, files = {}, []
    for p, name, out in zip(procs, names, outs):
        stdout, stderr = p.communicate()
        if p.returncode != 0:
            summary[name] = f"FAILED: {stderr[-500:]}"
            continue
        summary[name] = json.loads(stdout.strip().splitlines()[-1])
        files.append(out)

    web = ROOT / "data" / "web_scout.jsonl"
    if web.exists() and datetime.fromtimestamp(web.stat().st_mtime).date() == date.today():
        files.append("data/web_scout.jsonl")

    ing = run([PY, "-W", "ignore", "scripts/db.py", "ingest", *files])
    summary["ingest"] = json.loads(ing.stdout.strip().splitlines()[-1]) if ing.returncode == 0 else "FAILED"
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
