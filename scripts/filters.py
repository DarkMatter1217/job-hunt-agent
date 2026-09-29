"""Deterministic filters (stage 5). No LLM. Runs over every open job on each run; cheap and repeatable.

Each open job gets:
  filter_result  'pass' or 'drop:<reason>'
  flags          JSON list of {"flag": name, "evidence": "<snippet>"}; flags never drop a job, the scorer and the user see them

CLI:
  python scripts/filters.py run          # (re)filter all open jobs
  python scripts/filters.py explain KEY  # show why one job passed/dropped
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PREFS = yaml.safe_load(open(ROOT / "profile" / "preferences.yaml"))
DB = ROOT / "data" / "jobs.db"
TODAY = date.today()
def _grad(text):
    m = re.search(r"([A-Za-z]+)\W+(20\d\d)", str(text or ""))
    months = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
    return (int(m.group(2)), months.index(m.group(1).lower()) + 1) if m and m.group(1).lower() in months else (2100, 1)


GRAD = _grad(PREFS["candidate"].get("graduation"))  # e.g. "May 2028" in preferences.yaml

I = re.IGNORECASE


def _words(ws):
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in ws) + r")\b", I)


# ---------- level / role ----------
STUDENT = re.compile(r"\b(intern(ship)?s?|co-?\s?op|co op|student|stage|stagiaire|work term|placement|pey|trainee|"
                     r"summer analyst|summer associate|apprentice)\b", I)
EXCLUDE_TITLE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|architect|vp|"
                           r"new grad(uate)?|full[- ]time|front[- ]?end|ios|android|mobile|embedded|firmware|hardware|"
                           r"asic|fpga|pcb|rf|analog|mechanical|electrical|civil|chemical|qa|quality assurance|"
                           r"test engineer|sales|marketing|recruit(er|ing)|hr|human resources|finance|financial|"
                           r"accounting|tax|audit|legal|paralegal|compliance|facilities|retail|store|"
                           r"graphic|ux|ui designer|product design|content|communications|"
                           r"actuarial|investment banking|trading|capital markets analyst|business analyst|"
                           r"data analyst|operations coordinator|sales operations|revenue)\b", I)
AI_T = re.compile(r"machine learning|\bml\b|\bai\b|artificial intelligence|\bnlp\b|\bllms?\b|language model|gen ?ai|"
                  r"generative|deep learning|computer vision|applied scien|research|agent|\brag\b|intelligence", I)
RESEARCH_T = re.compile(r"research|applied scien|scientist", I)
NON_TECH = re.compile(r"business development|customer (management|success|experience|support)|account management|"
                      r"product design|product manage\w*|product marketing|\bsales\b|marketing|recruit\w*|"
                      r"human resources|\bhr\b|people analytics|talent|communications|content (writer|creator|strategy)|"
                      r"finance|financial analyst|accounting|\btax\b|audit|legal|paralegal|procurement|supply chain analyst|"
                      r"investment (banking|operations)|equity research|strategy analyst|consult\w*|project coordinator", I)
SWE_T = re.compile(r"software|developer|software development|backend|back[- ]end|engineer|platform|infrastructure|devops|"
                   r"site reliability|data engineer|programmer|python|api", I)
DATA_SCI_ONLY = re.compile(r"data scien|analytics|data analys", I)
SWE_DESC_REQ = re.compile(r"\bpython\b|\bapi\b|backend|back-end|fastapi|flask|machine learning|\bai\b|\bllm", I)

# ---------- markets ----------
CANADA = re.compile(r"\b(canada|can|ontario|quebec|québec|british columbia|alberta|manitoba|nova scotia|"
                    r"on|qc|bc|ab|mb|ns|nb|sk|toronto|montr[eé]al|vancouver|ottawa|kanata|gatineau|waterloo|"
                    r"kitchener|calgary|edmonton|markham|mississauga|victoria|burnaby|oakville|brampton|"
                    r"winnipeg|halifax|laval|longueuil|scarborough|aurora|oshawa|guelph|hamilton|london, on)\b", I)
HOME = re.compile(r"\b(" + "|".join(re.escape(m) for m in PREFS["markets"][0]["match"]) + r")\b", I)  # first market = home city
USA = re.compile(r"\b(united states|usa|u\.s\.|us|new york|nyc|san francisco|sf|seattle|austin|boston|chicago|"
                 r"california|ca, us|texas|washington|remote us|remote - us|palo alto|mountain view|menlo park|"
                 r"sunnyvale|san jose|los angeles|denver|atlanta|pittsburgh|[a-z]+, (ny|ca|wa|tx|ma|il|pa|nj|co|ga|va|nc|mi|md|or|az|ut|mn|oh|fl))\b", I)
INDIA = re.compile(r"\b(india|bengaluru|bangalore|hyderabad|pune|gurugram|gurgaon|delhi|noida|mumbai|chennai|kolkata)\b", I)
REMOTE = re.compile(r"\bremote\b", I)

# ---------- terms ----------
SEASON_MONTH = {"winter": 1, "spring": 5, "summer": 5, "fall": 9, "autumn": 9}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
TERM_RE = re.compile(r"\b(winter|spring|summer|fall|autumn)\W{0,3}(20\d\d)\b", I)
MONTH_YEAR_RE = re.compile(r"\b(" + "|".join(MONTHS) + r")\W{0,3}(20\d\d)\b", I)

# ---------- eligibility (strict: EEO boilerplate like "without regard to ... citizenship" must not match) ----------
FLAG_PATTERNS = {
    "citizenship_or_pr": [
        r"must (be|hold) (a )?(canadian|u\.?s\.?) (citizen|permanent resident)",
        r"(canadian )?citizens? (and|or) permanent residents? only",
        r"(open|available) (only )?to (canadian )?citizens",
        r"citizenship (is )?required", r"requires? (canadian )?citizenship",
        r"must be (a )?citizen",
    ],
    "security_clearance": [
        r"(obtain|maintain|hold|eligib\w+ for|able to get)\W+(a |an )?(\w+ )?(security clearance|reliability status|secret clearance|top secret)",
        r"(security clearance|reliability status|secret clearance) (is |will be )?(required|mandatory|a condition)",
        r"subject to (a )?(security|government) clearance",
    ],
    "french_required": [
        r"bilingual(ism)? \(?(english|french)\W+(and|/|&)\W*(french|english)\)? (is )?(required|mandatory|essential)",
        r"(fluen\w+|proficien\w+) in (both )?(english and french|french and english|french)\W+(is )?(required|mandatory|essential)",
        r"french (is|language skills are) (required|mandatory|essential)",
        r"must (be able to )?(speak|write|communicate) (fluently )?in french",
        r"ma[iî]trise du fran[cç]ais", r"bilinguisme (est )?(requis|obligatoire|exig[ée])",
    ],
    "french_asset": [r"french (is|would be|considered) (an )?(asset|plus|nice)", r"bilingual\w* (is )?(an )?(asset|plus)"],
    "us_work_auth": [
        r"authori[sz]ed to work in the (united states|u\.s\.|us)\b",
        r"(will not|won't|does not|do not|cannot|unable to|not able to) (provide |offer |sponsor)\w*\s?(visa |work )?(sponsorship)?",
        r"\bu\.?s\.? person\b", r"export control", r"itar|ear99",
    ],
    "phd_preferred": [r"\bph\.?d\.? (student|candidate|degree|preferred|in progress)", r"pursuing a ph\.?d", r"doctoral (student|candidate)"],
    "masters_welcome": [r"master'?s (or|and|/) ph\.?d", r"(msc|m\.sc|master'?s) (student|candidate|degree)", r"graduate students?"],
    "final_year_only": [r"(final|last)[- ]year (student|of (your |their )?(study|program|degree))", r"returning to school.{0,40}(not|no) required"],
}
FLAG_RE = {k: [re.compile(p, I) for p in ps] for k, ps in FLAG_PATTERNS.items()}
GRAD_RE = re.compile(r"graduat\w*[^.;\n]{0,25}?(in|by|between|before|from|no later than|date of)[^.;\n]{0,60}", I)
HARD_DROP_DESC = [re.compile(p, I) for p in [
    r"\b[5-9]\+? years (of )?(professional|industry|relevant|full[- ]time)", r"ph\.?d\.? (is )?required",
    r"must be (currently )?(enrolled in|pursuing) a ph\.?d\.?(?!\W{0,3}(or|/|and)\W{0,3}(a )?(master|msc|m\.sc))",
    r"(only|exclusively) (for|open to) ph\.?d", r"must be a ph\.?d\.? student",
    r"currently pursuing a ph\.?d\.?(?! or| /|/|, master| and master)( degree)?(?!\W{0,3}(or|/|and)\W{0,3}(a )?(master|msc|m\.sc))",
]]


def snippet(text, m, pad=70):
    s = text[max(0, m.start() - pad): m.end() + pad]
    return re.sub(r"\s+", " ", s).strip()


def parse_posted(p):
    if not p:
        return None
    p = str(p).strip()
    if p.isdigit() and len(p) >= 12:  # epoch ms (Lever)
        return datetime.utcfromtimestamp(int(p) / 1000).date()
    if p.isdigit() and len(p) == 10:  # epoch seconds
        return datetime.utcfromtimestamp(int(p)).date()
    m = re.match(r"Posted (Today|Yesterday|(\d+)\+? Days? Ago)", p, I)  # Workday
    if m:
        return TODAY if m.group(1).lower() == "today" else TODAY - timedelta(days=1 if m.group(1).lower() == "yesterday" else int(m.group(2)))
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(p[:10] if fmt == "%Y-%m-%d" else p.split()[0], fmt).date()
        except ValueError:
            continue
    return None


def terms_in(text):
    found = [(int(y), SEASON_MONTH[s.lower()]) for s, y in TERM_RE.findall(text)]
    found += [(int(y), MONTHS[mn.lower()]) for mn, y in MONTH_YEAR_RE.findall(text)]
    return found


def classify(job, live_on_ats=False):
    title = job["title"] or ""
    desc = job["description"] or ""
    loc = job["location"] or ""
    extra = json.loads(job["extra"] or "{}")
    text = f"{title}\n{desc}"
    flags = []

    def flag(name, evidence):
        if not any(f["flag"] == name for f in flags):
            flags.append({"flag": name, "evidence": evidence[:220]})

    # level
    if not STUDENT.search(title):
        return "drop:not_student", flags
    nt = NON_TECH.search(title)
    if nt and not AI_T.search(title):
        return f"drop:non_technical({nt.group(0).lower()})", flags
    if nt:  # AI-adjacent non-technical role ("Product Design Intern for AI"): the user rates these "maybe"
        flag("non_technical", nt.group(0))
    m = EXCLUDE_TITLE.search(title)
    if m and m.group(0).lower() in ("manager", "lead", "architect", "head of"):
        m = None  # an intern title that mentions a manager/lead is about the work, not the level
    if m and not AI_T.search(title):
        return f"drop:excluded_title({m.group(0).lower()})", flags
    if m and m.group(0).lower() in ("senior", "sr", "sr.", "staff", "principal", "manager", "director", "head of", "new grad", "new graduate", "full-time", "full time"):
        return f"drop:excluded_title({m.group(0).lower()})", flags
    # role family
    if AI_T.search(title):
        family = "research" if RESEARCH_T.search(title) else "ml_ai"
    elif SWE_T.search(title):
        if desc and not SWE_DESC_REQ.search(desc):
            return "drop:swe_without_python_api_ml", flags
        family = "swe"
    elif nt:
        family = "ai_adjacent"
    elif DATA_SCI_ONLY.search(title):
        return "drop:data_science_not_selected", flags
    else:
        return "drop:role_not_targeted", flags
    if DATA_SCI_ONLY.search(title) and not re.search(r"machine learning|\bml\b|\bai\b|llm|nlp", title, I):
        return "drop:data_science_not_selected", flags
    for rx in HARD_DROP_DESC:
        mm = rx.search(desc)
        if mm:
            return "drop:hard_requirement", [{"flag": "hard_requirement", "evidence": snippet(desc, mm)}]
    # market
    if HOME.search(loc):
        market = "home"
    elif CANADA.search(loc):
        market = "canada"
    elif INDIA.search(loc):
        market = "india"; flag("outside_canada", loc)
    elif USA.search(loc):
        market = "us"; flag("us_location", loc)
    elif REMOTE.search(loc) or not loc.strip():
        market = "remote_unspecified"; flag("location_unclear", loc or "(no location)")
    else:
        return "drop:market", flags
    # term
    terms = terms_in(title) or terms_in(desc[:3000])
    ht = extra.get("hire_time") or ""
    m2 = re.match(r"(20\d\d)-(\w+)", ht)
    if not terms and m2 and m2.group(2).lower() in MONTHS:
        terms = [(int(m2.group(1)), MONTHS[m2.group(2).lower()])]
    if terms:
        starts = sorted(set(terms))
        if all((y, mo) < (2027, 1) for y, mo in starts):
            return f"drop:term_passed({starts[0][0]}-{starts[0][1]:02d})", flags
        if all((y, mo) > (2027, 9) for y, mo in starts):
            flag("term_outside_prefs", f"{starts[0][0]}-{starts[0][1]:02d}")
    # age
    posted = parse_posted(job["posted"])
    if posted:
        age = (TODAY - posted).days
        if age > 90 and not live_on_ats:
            return f"drop:stale_{age}d", flags  # only unverifiable list/aggregator rows; live ATS postings stay
        if age > PREFS["caps"]["max_posting_age_days"]:
            flag("older_than_30d", f"posted {posted.isoformat()}")
    # eligibility flags
    for name, rxs in FLAG_RE.items():
        for rx in rxs:
            mm = rx.search(text)
            if mm:
                if name == "us_work_auth" and "sponsor" in mm.group(0).lower() and market in ("canada", "home"):
                    continue  # "no sponsorship" on a Canadian co-op is normal; co-op permit covers it
                flag(name, snippet(text, mm)); break
    for mm in GRAD_RE.finditer(desc):
        phrase = mm.group(0).lower()
        dates = [(int(y), MONTHS.get((mn or "").lower(), 6)) for mn, y in
                 re.findall(r"(?:(" + "|".join(MONTHS) + r")\W{0,3})?(20\d\d)", phrase)]
        if not dates:
            continue
        if re.search(r"or later|or after|\bafter\b|no earlier than|and later", phrase):
            bad = min(dates) > GRAD      # lower bound later than the candidate graduates
        elif re.search(r"between|from", phrase) and len(dates) >= 2:
            bad = not (min(dates) <= GRAD <= max(dates))
        else:                            # "by", "before", "in", "no later than": upper bound / exact
            bad = max(dates) < GRAD
        if bad:
            flag("graduation_mismatch", snippet(desc, mm)); break
    if re.search(r"\b(stage|stagiaire)\b", title, I) or (re.search(r"\b(québec|quebec|qc|montr[eé]al|laval)\b", loc, I)
                                                       and not any(f["flag"] == "french_required" for f in flags)):
        flag("french_likely", "Quebec location or French title")
    if not desc.strip():
        flag("no_description", "")
    flags.append({"flag": f"family:{family}", "evidence": ""})
    flags.append({"flag": f"market:{market}", "evidence": ""})
    return "pass", flags


def run(con):
    rows = con.execute("SELECT key, title, description, location, posted, extra FROM jobs WHERE status='open'").fetchall()
    run_id = con.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    live = {r[0] for r in con.execute("""SELECT key FROM sightings WHERE last_seen_run=? AND source NOT LIKE 'list:%'
                                          AND source NOT LIKE 'intern-list:%' AND source NOT LIKE 'web:%'""", (run_id,))}
    stats = {}
    for r in rows:
        result, flags = classify(r, live_on_ats=r["key"] in live)
        con.execute("UPDATE jobs SET filter_result=?, flags=? WHERE key=?", (result, json.dumps(flags), r["key"]))
        k = result.split("(")[0] if not result.startswith("drop:stale") else "drop:stale"
        stats[k] = stats.get(k, 0) + 1
    con.commit()
    return dict(sorted(stats.items(), key=lambda kv: -kv[1]))


def _main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    e = sub.add_parser("explain"); e.add_argument("key")
    a = ap.parse_args()
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    if a.cmd == "run":
        print(json.dumps(run(con), indent=1))
    else:
        r = con.execute("SELECT * FROM jobs WHERE key=?", (a.key,)).fetchone()
        print(json.dumps({"title": r["title"], "company": r["company"], "result": classify(r)[0],
                          "flags": classify(r)[1]}, indent=1))


if __name__ == "__main__":
    _main()
