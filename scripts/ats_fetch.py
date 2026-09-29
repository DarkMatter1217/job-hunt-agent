"""Fetch job postings from public ATS (applicant tracking system) APIs.

Every fetcher returns a list of normalized dicts:
    {company, ats, token, job_id, title, location, url, posted, description}

CLI:
    python scripts/ats_fetch.py probe <ats> <token> [--site SITE] [--q intern]
    python scripts/ats_fetch.py harvest [--registry registry/companies.yaml] [--out data/raw.jsonl]
"""
import argparse
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import urllib3
import yaml

urllib3.disable_warnings()  # system Python uses LibreSSL; certs still verified by requests
UA = {"User-Agent": "Mozilla/5.0 (job-agent; personal job search)", "Accept": "application/json"}
TIMEOUT = 20
PAUSE = 0.4  # seconds between requests to the same ATS


def _request(method, url, retries=1, **kw):
    """One retry on timeouts, connection errors and 5xx; a flaky board should not count as failed."""
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url, timeout=TIMEOUT, **kw)
            if r.status_code >= 500 and attempt < retries:
                time.sleep(3)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.ConnectionError):
            if attempt == retries:
                raise
            time.sleep(3)


def _get(url, **kw):
    return _request("GET", url, headers=UA, **kw)


def _post(url, body, **kw):
    return _request("POST", url, headers={**UA, "Content-Type": "application/json"}, json=body, **kw)


def _strip(text):
    text = html.unescape(text or "")
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _job(company, ats, token, job_id, title, location, url, posted=None, description=""):
    return {"company": company, "ats": ats, "token": token, "job_id": str(job_id), "title": (title or "").strip(),
            "location": (location or "").strip(), "url": url, "posted": posted, "description": description}


# ---------- fetchers ----------

def greenhouse(token, company=None, full=True, **_):
    d = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true" if full else "false"})
    return [_job(company or token, "greenhouse", token, j["id"], j["title"], (j.get("location") or {}).get("name"),
                 j["absolute_url"], j.get("updated_at"), _strip(j.get("content"))) for j in d.get("jobs", [])]


def lever(token, company=None, **_):
    d = _get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
    out = []
    for j in d:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location")]
        desc = _strip(j.get("descriptionPlain") or j.get("description")) + "\n" + "\n".join(
            f"{l.get('text')}: " + _strip(l.get("content")) for l in j.get("lists", []))
        out.append(_job(company or token, "lever", token, j["id"], j["text"], "; ".join(filter(None, locs)),
                        j["hostedUrl"], j.get("createdAt"), desc))
    return out


def ashby(token, company=None, **_):
    d = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}", params={"includeCompensation": "true"})
    out = []
    for j in d.get("jobs", []):
        locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations", []) if isinstance(s, dict)]
        if j.get("isRemote"):
            locs.append("Remote")
        out.append(_job(company or token, "ashby", token, j["id"], j["title"], "; ".join(filter(None, locs)),
                        j["jobUrl"], j.get("publishedAt"), _strip(j.get("descriptionHtml") or j.get("descriptionPlain"))))
    return out


def smartrecruiters(token, company=None, **_):
    out, offset = [], 0
    while True:
        d = _get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings", params={"limit": 100, "offset": offset})
        for j in d.get("content", []):
            loc = j.get("location") or {}
            out.append(_job(company or token, "smartrecruiters", token, j["id"], j["name"],
                            ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")])) + (" (Remote)" if loc.get("remote") else ""),
                            f"https://jobs.smartrecruiters.com/{token}/{j['id']}", j.get("releasedDate")))
        offset += 100
        if offset >= d.get("totalFound", 0) or offset > 2000:
            return out
        time.sleep(PAUSE)


def workable(token, company=None, **_):
    d = _get(f"https://apply.workable.com/api/v1/widget/accounts/{token}", params={"details": "true"})
    return [_job(company or token, "workable", token, j.get("shortcode"), j["title"],
                 ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")])) + (" (Remote)" if j.get("telecommuting") else ""),
                 j.get("url") or j.get("application_url"), j.get("published_on") or j.get("created_at"),
                 _strip(j.get("description"))) for j in d.get("jobs", [])]


def recruitee(token, company=None, **_):
    d = _get(f"https://{token}.recruitee.com/api/offers/")
    return [_job(company or token, "recruitee", token, j["id"], j["title"],
                 ", ".join(filter(None, [j.get("city"), j.get("state_name") or j.get("state_code"), j.get("country")])) + (" (Remote)" if j.get("remote") else ""),
                 j.get("careers_url"), j.get("published_at") or j.get("created_at"),
                 _strip((j.get("description") or "") + "\n" + (j.get("requirements") or ""))) for j in d.get("offers", [])]


def icims(token, company=None, q="", max_pages=10, **_):
    """token = iCIMS subdomain, e.g. 'careers-kinaxis'. The ?in_iframe=1 listing is server-rendered HTML."""
    out, seen = [], set()
    for page in range(max_pages):
        r = requests.get(f"https://{token}.icims.com/jobs/search",
                         params={"ss": 1, "searchKeyword": q, "in_iframe": 1, "pr": page}, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        cards = r.text.split('<li class="iCIMS_JobCardItem">')[1:]
        new = 0
        for c in cards:
            m = re.search(r'href="(https://[^"]+/jobs/(\d+)/[^"]+?/job)(?:\?[^"]*)?"[^>]*>.*?<h3[^>]*>\s*(.*?)\s*</h3>', c, re.S)
            if not m or m.group(2) in seen:
                continue
            seen.add(m.group(2)); new += 1
            locs = re.findall(r'Location</span>\s*<span\s*>\s*([^<]+?)\s*</span>', c)
            locs += re.findall(r'Additional Locations</span>.*?<dd[^>]*><span\s*>\s*([^<]+?)\s*</span>', c, re.S)
            posted = re.search(r'Posted Date</dt>.*?title="([^"]+)"', c, re.S)
            out.append(_job(company or token, "icims", token, m.group(2), html.unescape(m.group(3)),
                            "; ".join(l.replace("CA-", "Canada-").replace("-", ", ") for l in locs), m.group(1),
                            posted.group(1) if posted else None))
        if not new or f"pr={page + 1}" not in r.text:
            break
        time.sleep(PAUSE)
    return out


def workday(token, company=None, site=None, host=None, q="", max_jobs=400, **_):
    """token = tenant, e.g. 'rbc'; host = 'rbc.wd3.myworkdayjobs.com'; site = 'RBCEARLYTALENT1'.
    Workday lists 20 per page; `q` narrows server-side (use 'intern' / 'co-op' to stay polite)."""
    base = f"https://{host}/wday/cxs/{token}/{site}"
    out, offset, total = [], 0, None
    while total is None or offset < min(total, max_jobs):
        d = _post(f"{base}/jobs", {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q})
        total = d.get("total", 0) if total is None else total
        for j in d.get("jobPostings", []):
            out.append(_job(company or token, "workday", token, j.get("bulletFields", [j.get("externalPath")])[0],
                            j.get("title"), j.get("locationsText"),
                            f"https://{host}/en-US/{site}{j.get('externalPath', '')}", j.get("postedOn")))
        if not d.get("jobPostings"):
            break
        offset += 20
        time.sleep(PAUSE)
    return out


def workday_detail(job):
    """Fetch the full description for one Workday job (list endpoint has none)."""
    m = re.match(r"https://([^/]+)/en-US/([^/]+)(/.*)", job["url"])
    host, site, path = m.groups()
    d = _get(f"https://{host}/wday/cxs/{job['token']}/{site}{path}")
    info = d.get("jobPostingInfo", {})
    job["description"] = _strip(info.get("jobDescription"))
    job["posted"] = info.get("startDate") or job["posted"]
    return job


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
            "workable": workable, "workday": workday, "recruitee": recruitee, "icims": icims}


def fetch(entry, q=""):
    """entry = one company from registry/companies.yaml."""
    fn = FETCHERS[entry["ats"]]
    if entry["ats"] in ("workday", "icims"):  # search-based boards: only pull student-level postings
        seen = {}
        for query in entry.get("queries") or [q]:
            for j in fn(entry["token"], company=entry.get("name"), site=entry.get("site"), host=entry.get("host"), q=query):
                seen[j["url"]] = j
            time.sleep(PAUSE)
        return list(seen.values())
    return fn(entry["token"], company=entry.get("name"))


# ---------- CLI ----------

def _main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("ats"); p.add_argument("token")
    p.add_argument("--site"); p.add_argument("--host"); p.add_argument("--q", default="")
    h = sub.add_parser("harvest")
    h.add_argument("--registry", default="registry/companies.yaml")
    h.add_argument("--out", default="data/raw.jsonl")
    a = ap.parse_args()

    if a.cmd == "probe":
        jobs = fetch({"ats": a.ats, "token": a.token, "site": a.site, "host": a.host}, q=a.q)
        print(json.dumps({"count": len(jobs), "sample": [{k: j[k] for k in ("title", "location", "url")} for j in jobs[:5]]}, indent=1))
        return

    reg = yaml.safe_load(open(a.registry))
    entries = [e for e in reg["companies"] if e.get("ats") in FETCHERS and not e.get("disabled")]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)

    def safe_fetch(entry):
        try:
            return entry, fetch(entry), None
        except Exception as e:  # one bad board must not stop the run
            return entry, [], e

    n_ok = n_fail = total = 0
    started = time.time()
    status = []  # per-board result; db.py only closes postings from boards that fetched OK
    with open(a.out, "w") as f, ThreadPoolExecutor(max_workers=8) as pool:
        for entry, jobs, err in pool.map(safe_fetch, entries):
            source = f"{entry['ats']}:{entry['token']}"
            if err:
                n_fail += 1
                status.append({"source": source, "company": entry["name"], "ok": False, "n_jobs": 0, "error": str(err)[:300]})
                print(f"FAIL {entry['name']} ({source}): {err}", file=sys.stderr)
                continue
            for j in jobs:
                f.write(json.dumps(j) + "\n")
            status.append({"source": source, "company": entry["name"], "ok": True, "n_jobs": len(jobs), "error": None})
            total += len(jobs); n_ok += 1
    Path(a.out).with_suffix(".status.json").write_text(json.dumps(status, indent=1))
    print(json.dumps({"boards_ok": n_ok, "boards_failed": n_fail, "jobs": total,
                      "seconds": round(time.time() - started), "out": a.out}))


if __name__ == "__main__":
    _main()
