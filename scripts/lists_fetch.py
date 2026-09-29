"""Fetch community-maintained internship lists (GitHub) fresh on every run. No LLM needed: they are structured.

Rows use the ats_fetch.py shape with ats="list", token=<list name>.

CLI:
    python scripts/lists_fetch.py [--out data/lists.jsonl]
"""
import argparse
import hashlib
import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()
UA = {"User-Agent": "Mozilla/5.0 (job-agent; personal job search)"}

CANADA = re.compile(r"\b(Canada|ON|QC|BC|AB|MB|NS|NB|SK|Toronto|Montr[eé]al|Vancouver|Ottawa|Waterloo|Kitchener|Calgary|Edmonton|"
                    r"Markham|Mississauga|Kanata|Victoria|Burnaby|Oakville|Qu[eé]bec|Halifax|Winnipeg)\b")
INDIA = re.compile(r"\b(India|Bengaluru|Bangalore|Hyderabad|Pune|Gurugram|Gurgaon|Delhi|Noida|Mumbai|Chennai)\b", re.I)
AI = re.compile(r"machine learning|\bML\b|\bAI\b|artificial|NLP|LLM|applied scien|gen ?ai|generative|deep learning|"
                r"computer vision|research|agent", re.I)

SOURCES = {
    "cti2027": "https://raw.githubusercontent.com/negarprh/Canadian-Tech-Internships-2027/main/README.md",
    "simplify": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
    "speedy_ai_intl": "https://raw.githubusercontent.com/speedyapply/2027-AI-College-Jobs/main/INTERN_INTL.md",
    "speedy_ai_usa": "https://raw.githubusercontent.com/speedyapply/2027-AI-College-Jobs/main/README.md",
}


def _row(src, company, title, location, url, posted, extra=None):
    jid = hashlib.sha1(f"{company}|{title}|{url}".encode()).hexdigest()[:16]
    return {"company": company.strip(), "ats": "list", "token": src, "job_id": jid, "title": title.strip(),
            "location": location.strip(), "url": url, "posted": posted, "description": "", "extra": extra or {}}


def _get(url):
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return r


def cti2027():
    out, last = [], None
    for line in _get(SOURCES["cti2027"]).text.splitlines():
        if not line.startswith("| ") or line.startswith("| Company"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < 5:
            continue
        company = last if cells[0] in ("↳", "") else re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cells[0]).strip("* ")
        last = company
        if "Closed" in cells[3] or "🔒" in cells[3]:
            continue
        m = re.search(r"\]\((https?://[^)]+)\)\s*$", cells[3])
        if not m:
            continue
        try:
            posted = datetime.strptime(cells[4], "%b %d, %Y").date().isoformat()
        except ValueError:
            posted = cells[4]
        out.append(_row("cti2027", company, cells[1], cells[2], m.group(1), posted))
    return out


def simplify():
    out = []
    for x in _get(SOURCES["simplify"]).json():
        if not (x.get("active") and x.get("is_visible")):
            continue
        locs = "; ".join(x.get("locations") or [])
        canada, india = bool(CANADA.search(locs)), bool(INDIA.search(locs))
        if not (canada or india or AI.search(x["title"])):  # US/other: AI/ML roles only
            continue
        posted = datetime.fromtimestamp(x["date_posted"], timezone.utc).date().isoformat() if x.get("date_posted") else None
        out.append(_row("simplify", x["company_name"], x["title"], locs, x["url"], posted,
                        {"terms": x.get("terms"), "sponsorship": x.get("sponsorship"), "degrees": x.get("degrees")}))
    return out


def speedy(src, keep):
    out = []
    for line in _get(SOURCES[src]).text.splitlines():
        if not line.startswith("| <a") and not line.startswith("| **"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < 5:
            continue
        company = html.unescape(re.sub(r"<[^>]+>", "", cells[0])).strip("* ")
        location = html.unescape(re.sub(r"<[^>]+>", " ", cells[2])).strip()
        if not keep(location):
            continue
        m = re.search(r'href="([^"]+)"', cells[3])
        if not m:
            continue
        age = re.match(r"(\d+)d", cells[-1])
        posted = (date.today() - timedelta(days=int(age.group(1)))).isoformat() if age else None
        title = html.unescape(re.sub(r"<[^>]+>", "", cells[1]))
        out.append(_row(src, company, title, location, m.group(1), posted))
    return out


FETCH = {
    "cti2027": cti2027,
    "simplify": simplify,
    "speedy_ai_intl": lambda: speedy("speedy_ai_intl", lambda loc: bool(CANADA.search(loc) or INDIA.search(loc))),
    "speedy_ai_usa": lambda: speedy("speedy_ai_usa", lambda loc: True),
}


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/lists.jsonl")
    a = ap.parse_args()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    counts, status = {}, []
    with open(a.out, "w") as f:
        for name, fn in FETCH.items():
            try:
                rows = fn()
            except Exception as e:  # a moved/renamed list must not break the daily run
                counts[name] = f"FAIL: {e}"
                status.append({"source": f"list:{name}", "company": None, "ok": False, "n_jobs": 0, "error": str(e)[:300]})
                continue
            for r in rows:
                f.write(json.dumps(r) + "\n")
            counts[name] = len(rows)
            # 0 rows from a list that normally has hundreds = format changed; do not let it close jobs
            status.append({"source": f"list:{name}", "company": None, "ok": len(rows) > 0, "n_jobs": len(rows),
                           "error": None if rows else "0 rows parsed (format change?)"})
    Path(a.out).with_suffix(".status.json").write_text(json.dumps(status, indent=1))
    print(json.dumps({"lists": counts, "out": a.out}))


if __name__ == "__main__":
    _main()
