"""Helper for the tailor and fact-checker agents (stages 7-8).

  python scripts/tailor_io.py next [--limit 5]           # pick jobs to tailor: live re-check first, creates output folders
  python scripts/tailor_io.py save-plan KEY '<json>'     # validate + store the resume plan
  python scripts/tailor_io.py build KEY                  # render resume (1-page fit) + letter, run verify_docs
  python scripts/tailor_io.py to-check                   # built jobs waiting for the fact-checker
  python scripts/tailor_io.py checker-save KEY '<json>'  # store the fact-check verdict
  python scripts/tailor_io.py to-revise                  # jobs whose letter failed a check once (one retry allowed)
  python scripts/tailor_io.py status                     # all tailored jobs and where they stand

tailor_status: in_progress -> built | verify_failed -> ready | needs_revision -> ready | needs_review
The tailor writes cover_letter.md itself (Write tool) into the job's output folder; everything else goes through here.
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import enrich  # noqa: E402
import render  # noqa: E402
import verify_docs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "jobs.db"
PREFS = yaml.safe_load(open(ROOT / "profile" / "preferences.yaml"))


def con_():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    return c


def slug(s, n=40):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:n]


def job_dir(j):
    return ROOT / "output" / date.today().isoformat() / f"{slug(j['company'], 25)}_{slug(j['title'])}"


def still_live(con, j):
    """Re-check the posting right before tailoring. Returns 'live', 'gone' or 'unverified'."""
    urls = [j["url"]] + [r["url"] for r in con.execute("SELECT url FROM sightings WHERE key=?", (j["key"],))]
    urls = [u for u in dict.fromkeys(urls) if u and "jobright.ai" not in u]
    for u in urls:
        try:
            if enrich.detail(u):
                return "live"
        except enrich.Gone:
            return "gone"
        except Exception:
            continue
    return "unverified"


def cmd_next(a):
    con = con_()
    min_score = PREFS["caps"]["min_score_to_tailor"]
    limit = a.limit or PREFS["caps"]["daily_tailor_cap"]
    rows = con.execute("""SELECT * FROM jobs WHERE status='open' AND filter_result='pass' AND score >= ?
                          AND tailor_status IS NULL ORDER BY score DESC""", (min_score,)).fetchall()
    picked = []
    for j in rows:
        if len(picked) >= limit:
            break
        reason = json.loads(j["score_reason"] or "{}")
        if reason.get("deal_breaker"):
            continue
        live = still_live(con, j)
        if live == "gone":
            con.execute("UPDATE jobs SET status='closed', closed_at=date('now') WHERE key=?", (j["key"],)); con.commit()
            print(f"skipped (closed since fetch): {j['company']} | {j['title']}", file=sys.stderr)
            continue
        out = job_dir(j); out.mkdir(parents=True, exist_ok=True)
        meta = {"key": j["key"], "company": j["company"], "title": j["title"], "location": j["location"], "url": j["url"],
                "score": j["score"], "why": reason.get("why"), "gaps": json.loads(j["gaps"] or "[]"),
                "matched_facts": reason.get("matched_facts", []), "role_type": reason.get("role_type"),
                "liveness": live, "description": j["description"] or ""}
        (out / "job.json").write_text(json.dumps(meta, indent=1))
        (out / "jd.md").write_text(f"# {j['title']} | {j['company']}\n\n{j['location']}\n\n{j['url']}\n\n{j['description'] or '(no description)'}\n")
        (out / "fit.md").write_text(f"Score {j['score']}\n\n{reason.get('why')}\n\nGaps: {', '.join(meta['gaps']) or 'none'}\n")
        con.execute("UPDATE jobs SET tailor_status='in_progress', output_dir=? WHERE key=?", (str(out), j["key"])); con.commit()
        picked.append({**{k: v for k, v in meta.items() if k != "description"},
                       "description": meta["description"][:6000], "output_dir": str(out)})
    print(json.dumps(picked, indent=1))


def _job(con, key):
    j = con.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
    if not j or not j["output_dir"]:
        sys.exit("REJECTED: unknown key or job not started with `next`")
    return j, Path(j["output_dir"])


def cmd_save_plan(a):
    con = con_(); j, out = _job(con, a.key)
    try:
        plan = json.loads(a.plan)
    except json.JSONDecodeError as e:
        sys.exit(f"REJECTED: invalid JSON ({e})")
    errs = render.validate_plan(plan)
    if errs:
        sys.exit("REJECTED:\n- " + "\n- ".join(errs))
    (out / "plan.json").write_text(json.dumps(plan, indent=1))
    print(f"plan saved -> {out / 'plan.json'}. Now write {out / 'cover_letter.md'} and run build.")


def cmd_build(a):
    con = con_(); j, out = _job(con, a.key)
    if not (out / "plan.json").exists() or not (out / "cover_letter.md").exists():
        sys.exit("REJECTED: needs plan.json (save-plan) and cover_letter.md first")
    plan = json.loads((out / "plan.json").read_text())
    comp = slug(j["company"], 25)
    rdocx = out / f"{render.doc_prefix()}_Resume_{comp}.docx"
    pdf, pages, trimmed, final = render.fit_one_page(plan, rdocx)
    (out / "plan_final.json").write_text(json.dumps({"plan": final, "trimmed_to_fit": trimmed}, indent=1))
    paras = render.letter_paragraphs((out / "cover_letter.md").read_text())
    ldocx = out / f"{render.doc_prefix()}_Cover_Letter_{comp}.docx"
    render.build_letter_docx(j["company"], j["title"], paras, ldocx, date.today().strftime("%B %-d, %Y"))
    render.to_pdf(ldocx)
    res = verify_docs.check(out)
    status = "built" if res["ok"] else "verify_failed"
    con.execute("UPDATE jobs SET tailor_status=? WHERE key=?", (status, a.key)); con.commit()
    print(json.dumps({"status": status, "pages": pages, "trimmed_to_fit": trimmed, **res}, indent=1))


def cmd_to_check(_):
    con = con_()
    rows = con.execute("SELECT key, company, title, output_dir FROM jobs WHERE tailor_status='built'").fetchall()
    print(json.dumps([dict(r) for r in rows], indent=1))


def cmd_checker_save(a):
    con = con_(); j, out = _job(con, a.key)
    try:
        v = json.loads(a.verdict)
    except json.JSONDecodeError as e:
        sys.exit(f"REJECTED: invalid JSON ({e})")
    if v.get("verdict") not in ("pass", "fail") or not isinstance(v.get("unsupported", []), list):
        sys.exit('REJECTED: need {"verdict": "pass"|"fail", "unsupported": [{"sentence":..., "reason":...}], "notes": "..."}')
    if v["verdict"] == "pass" and v.get("unsupported"):
        sys.exit("REJECTED: verdict pass but unsupported claims listed")
    hist_path = out / "factcheck.json"
    hist = json.loads(hist_path.read_text()) if hist_path.exists() else []
    hist.append(v); hist_path.write_text(json.dumps(hist, indent=1))
    if v["verdict"] == "pass":
        status = "ready"
    else:
        status = "needs_revision" if len(hist) < 2 else "needs_review"  # one retry, then a human looks
    con.execute("UPDATE jobs SET tailor_status=? WHERE key=?", (status, a.key)); con.commit()
    print(f"{a.key}: {status}")


def cmd_to_revise(_):
    con = con_()
    rows = con.execute("SELECT key, company, title, output_dir FROM jobs WHERE tailor_status IN ('needs_revision','verify_failed')").fetchall()
    out = []
    for r in rows:
        d = Path(r["output_dir"])
        fc = json.loads((d / "factcheck.json").read_text())[-1] if (d / "factcheck.json").exists() else None
        vf = json.loads((d / "verify.json").read_text()) if (d / "verify.json").exists() else None
        out.append({**dict(r), "factcheck": fc, "verify": vf})
    print(json.dumps(out, indent=1))


def cmd_status(_):
    con = con_()
    rows = con.execute("SELECT company, title, score, tailor_status, output_dir FROM jobs WHERE tailor_status IS NOT NULL ORDER BY score DESC").fetchall()
    print(json.dumps([dict(r) for r in rows], indent=1))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("next"); n.add_argument("--limit", type=int)
    p = sub.add_parser("save-plan"); p.add_argument("key"); p.add_argument("plan")
    b = sub.add_parser("build"); b.add_argument("key")
    sub.add_parser("to-check")
    c = sub.add_parser("checker-save"); c.add_argument("key"); c.add_argument("verdict")
    sub.add_parser("to-revise")
    sub.add_parser("status")
    a = ap.parse_args()
    {"next": cmd_next, "save-plan": cmd_save_plan, "build": cmd_build, "to-check": cmd_to_check,
     "checker-save": cmd_checker_save, "to-revise": cmd_to_revise, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    main()
