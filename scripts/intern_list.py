"""Pull internship lists from intern-list.com (run by Jobright.ai; data lives in public Airtable shared views).

Output rows use the same shape as ats_fetch.py, with ats="intern-list".
The Apply URL points to a jobright.ai page; the dedupe step matches these to ATS postings by company + title.

CLI:
    python scripts/intern_list.py [--lists ca_ml_ai ca_swe us_ml_ai] [--out data/intern_list.jsonl]
"""
import argparse
import json
import re
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()

# path on intern-list.com -> Airtable embed (app/share id). Mapped 2026-09-28 from the page's data-job-path attributes.
LISTS = {
    "ca_ml_ai": "app742LMLO7tQP9dO/shrUtHuyzXodJCjTv",
    "ca_swe": "app742LMLO7tQP9dO/shrnuGuK0LFqso8vt",
    "ca_data_analysis": "app742LMLO7tQP9dO/shrxLJiBa4dfQZwx8",
    "ca_engineering_development": "app742LMLO7tQP9dO/shrayOx4h0UMsWgfs",
    "us_ml_ai": "appjSXAWiVF4d1HoZ/shrf04yGbrK3IebAl",
    "us_swe": "app17F0kkWQZhC6HB/shrOTtndhc6HSgnYb",
}
DEFAULT_LISTS = ["ca_ml_ai", "ca_swe", "us_ml_ai"]
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36"}


def read_shared_view(embed):
    """Return (columns_by_id, rows) for an Airtable shared view embed like 'appXXX/shrYYY'."""
    s = requests.Session()
    page = s.get(f"https://airtable.com/embed/{embed}", headers=UA, timeout=30).text
    m = re.search(r'urlWithParams:\s*"([^"]+)"', page)
    if not m:
        raise RuntimeError(f"no shared-view data URL found for {embed} (Airtable page format changed?)")
    url = "https://airtable.com" + m.group(1).encode().decode("unicode_escape")
    hdr = {**UA, "x-requested-with": "XMLHttpRequest", "x-time-zone": "America/Toronto", "x-user-locale": "en",
           "x-airtable-application-id": embed.split("/")[0]}
    d = s.get(url, headers=hdr, timeout=60).json()
    table = d["data"]["table"]
    return {c["id"]: c for c in table["columns"]}, table["rows"]


def _cell(col, v):
    opts = (col.get("typeOptions") or {}).get("choices") or {}
    if col["type"] == "select":
        return opts.get(v, {}).get("name", v)
    if col["type"] == "multiSelect":
        return [opts.get(x, {}).get("name", x) for x in v or []]
    if col["type"] == "button":
        return (v or {}).get("url")
    return v


def fetch_list(name):
    cols, rows = read_shared_view(LISTS[name])
    out = []
    for r in rows:
        rec = {cols[k]["name"]: _cell(cols[k], v) for k, v in r["cellValuesByColumnId"].items() if k in cols}
        out.append({
            "company": (rec.get("Company") or "").strip(), "ats": "intern-list", "token": name,
            "job_id": r["id"], "title": (rec.get("Position Title") or "").strip(),
            "location": (rec.get("Location") or "").strip(), "url": rec.get("Apply"), "posted": rec.get("Date"),
            "description": rec.get("Qualifications") or "",
            "extra": {"work_model": rec.get("Work Model"), "hire_time": rec.get("Hire Time"),
                      "graduate_time": rec.get("Graduate Time"), "salary": rec.get("Salary"),
                      "industry": rec.get("Company Industry"), "company_size": rec.get("Company Size")},
        })
    return out


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lists", nargs="+", default=DEFAULT_LISTS, choices=list(LISTS))
    ap.add_argument("--out", default="data/intern_list.jsonl")
    a = ap.parse_args()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    counts, status = {}, []
    with open(a.out, "w") as f:
        for name in a.lists:
            source = f"intern-list:{name}"
            try:
                jobs = fetch_list(name)
            except Exception as e:  # a changed page must not break the daily run
                counts[name] = f"FAIL: {e}"
                status.append({"source": source, "company": None, "ok": False, "n_jobs": 0, "error": str(e)[:300]})
                continue
            for j in jobs:
                f.write(json.dumps(j) + "\n")
            counts[name] = len(jobs)
            status.append({"source": source, "company": None, "ok": True, "n_jobs": len(jobs), "error": None})
            time.sleep(1)
    Path(a.out).with_suffix(".status.json").write_text(json.dumps(status, indent=1))
    print(json.dumps({"lists": counts, "out": a.out}))


if __name__ == "__main__":
    _main()
