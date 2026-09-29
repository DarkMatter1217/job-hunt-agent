"""Fetch full descriptions for jobs that passed the filters but have none (stage 5b). No LLM.

Uses each posting's own ATS detail endpoint, parsed from its URL. A 404/410 from the ATS means the posting is gone,
so the job is marked closed. home-city, Canada and India jobs go first; the run is capped.

CLI:
  python scripts/enrich.py [--limit 400]
"""
import argparse
import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import ats_fetch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "jobs.db"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/128"}
_ashby_cache = {}


class Gone(Exception):
    pass


def _json(url):
    r = requests.get(url, headers=UA, timeout=20)
    if r.status_code in (404, 410):
        raise Gone(url)
    r.raise_for_status()
    return r.json()


def detail(url):
    """Return description text for a posting URL, or None if the ATS is not supported."""
    u = url.split("?")[0]
    m = re.match(r"https?://(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([^/]+)/jobs/(\d+)", u)
    if m:
        return ats_fetch._strip(_json(f"https://boards-api.greenhouse.io/v1/boards/{m[1]}/jobs/{m[2]}").get("content"))
    m = re.match(r"https?://jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})", u)
    if m:
        j = _json(f"https://api.lever.co/v0/postings/{m[1]}/{m[2]}")
        return ats_fetch._strip(j.get("descriptionPlain") or j.get("description")) + "\n" + "\n".join(
            f"{l.get('text')}: " + ats_fetch._strip(l.get("content")) for l in j.get("lists", []))
    m = re.match(r"https?://jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", u)
    if m:
        if m[1] not in _ashby_cache:
            _ashby_cache[m[1]] = {j["id"]: j for j in _json(f"https://api.ashbyhq.com/posting-api/job-board/{m[1]}").get("jobs", [])}
        j = _ashby_cache[m[1]].get(m[2])
        if j is None:
            raise Gone(url)
        return ats_fetch._strip(j.get("descriptionHtml") or j.get("descriptionPlain"))
    m = re.match(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([^/]+)/(\d+)", u)
    if m:
        j = _json(f"https://api.smartrecruiters.com/v1/companies/{m[1]}/postings/{m[2]}")
        sec = (j.get("jobAd") or {}).get("sections") or {}
        return "\n".join(ats_fetch._strip((sec.get(k) or {}).get("text")) for k in ("jobDescription", "qualifications", "additionalInformation"))
    m = re.match(r"https?://(([^.]+)\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)(/job/.+)", u)
    if m:
        d = _json(f"https://{m[1]}/wday/cxs/{m[2]}/{m[3]}{m[4]}")
        return ats_fetch._strip((d.get("jobPostingInfo") or {}).get("jobDescription"))
    m = re.match(r"https?://([^.]+)\.icims\.com/jobs/(\d+)/", u)
    if m:
        r = requests.get(u, params={"in_iframe": 1}, headers=UA, timeout=20)
        if r.status_code in (404, 410) or "no longer available" in r.text.lower():
            raise Gone(url)
        return jsonld_description(r.text)
    # any other career site: many embed schema.org JobPosting JSON-LD for search engines
    r = requests.get(url, headers=UA, timeout=20)
    if r.status_code in (404, 410):
        raise Gone(url)
    if r.ok:
        return jsonld_description(r.text)
    return None


def jsonld_description(page):
    for block in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else data.get("@graph", [data]):
            if isinstance(item, dict) and "JobPosting" in str(item.get("@type")) and item.get("description"):
                return ats_fetch._strip(item["description"])
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=400)
    a = ap.parse_args()
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT j.key, j.flags, j.url, GROUP_CONCAT(s.url, ' ') AS urls FROM jobs j
                          JOIN sightings s ON s.key = j.key
                          WHERE j.status='open' AND j.filter_result='pass' AND (j.description IS NULL OR length(j.description) < 600)
                          GROUP BY j.key""").fetchall()
    prio = lambda r: 0 if '"market:home"' in r["flags"] else 1 if '"market:canada"' in r["flags"] or '"market:india"' in r["flags"] else 2
    rows = sorted(rows, key=prio)[: a.limit]

    def work(r):
        urls = list(dict.fromkeys([r["url"], *re.findall(r"https?://\S+", r["urls"] or "")]))
        urls.sort(key=lambda u: "jobright.ai" in (u or ""))  # company/ATS pages first, jobright page last
        for url in urls:  # try every source's link
            if not url:
                continue
            try:
                text = detail(url)
            except Gone:
                return r["key"], "gone", None
            except Exception:
                continue
            if text and len(text) > 600:
                return r["key"], "ok", text
        return r["key"], "unsupported", None

    stats = {"ok": 0, "gone": 0, "unsupported": 0}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with ThreadPoolExecutor(8) as pool:
        for key, res, text in pool.map(work, rows):
            stats[res] += 1
            if res == "ok":
                con.execute("UPDATE jobs SET description=? WHERE key=?", (text[:20000], key))
            elif res == "gone":
                con.execute("UPDATE jobs SET status='closed', closed_at=? WHERE key=?", (now, key))
    con.commit()
    print(json.dumps({"checked": len(rows), **stats}))


if __name__ == "__main__":
    main()
