"""Deterministic checks on a built application (stage 8a). The LLM fact-checker (stage 8b) runs after this.

Resume: exactly 1 page; plan valid (all text comes from facts.yaml by construction).
Cover letter: length, no dashes, no banned phrases, no do-not-claim strings, and every number / email / URL
must already exist in facts.yaml or in the job posting.

    python scripts/verify_docs.py <output_dir>
"""
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
ROOT = Path(__file__).resolve().parent.parent
FACTS_TEXT = (ROOT / "profile" / "facts.yaml").read_text()
FACTS = yaml.safe_load(FACTS_TEXT)
STYLE = (ROOT / "profile" / "style.md").read_text()

BANNED = [w.strip().strip('"\'. ').lower() for w in
          re.search(r"Banned words and phrases:(.*?)\n- ", STYLE, re.S).group(1).replace("\n", " ").split(",") if w.strip()]
# Regexes that must never appear in a letter. Personal ones (old emails, retracted metrics, unproven tools) live in
# facts.yaml under `do_not_claim_patterns`, so they stay private.
HARD_NO = [r"\bnew grad"] + FACTS.get("do_not_claim_patterns", [])


def _own_link_keys():
    """Hosts/paths of the candidate's own links (contact, publication, projects); letters may cite only these."""
    urls = [v for v in FACTS.get("contact", {}).values() if isinstance(v, str) and v.startswith("http")]
    urls += [(FACTS.get("publication") or {}).get("link") or ""]
    for p in FACTS.get("projects", []):
        urls += [p.get("link") or ""] + list(p.get("links") or [])
    return [re.sub(r"^https?://(www\.)?", "", u).split("?")[0].rstrip("/") for u in urls if u]


OWN_LINK_KEYS = _own_link_keys()


def numbers(text):
    return set(re.findall(r"\d+(?:[.,]\d+)?%?", text))


def check_letter(letter_md, job):
    errs = []
    body = letter_md
    words = len(re.findall(r"[A-Za-z0-9'’]+", body))
    if not 230 <= words <= 370:
        errs.append(f"length {words} words (target 250-350)")
    if re.search(r"[–—]", body):
        errs.append("contains an em or en dash")
    if re.search(r"\s-\s", body):
        errs.append("uses a spaced hyphen as a dash")
    low = body.lower()
    hits = [b for b in BANNED if b and b in low]
    if hits:
        errs.append(f"banned phrases: {hits}")
    for pat in HARD_NO:
        m = re.search(pat, low)
        if m:
            errs.append(f"do-not-claim text: '{m.group(0)}'")
    allowed = numbers(FACTS_TEXT) | numbers(job.get("title", "") + " " + job.get("description", ""))
    bad_nums = sorted(n for n in numbers(body) if n not in allowed and n.rstrip("%") not in allowed)
    if bad_nums:
        errs.append(f"numbers not in facts.yaml or the posting: {bad_nums}")
    c = FACTS["contact"]
    for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", body):
        if e != c["email"]:
            errs.append(f"unknown email {e}")
    for u in re.findall(r"https?://\S+|(?:www\.)?[\w-]+\.(?:com|ai|io|ca|org)/\S*", body):
        if not any(k.lower() in u.lower() for k in OWN_LINK_KEYS + [job.get("company_domain") or "@@"]):
            errs.append(f"unknown link {u}")
    if job.get("company") and job["company"].split()[0].lower() not in low:
        errs.append("letter never names the company")
    return errs, words


def check(out_dir):
    out = Path(out_dir)
    job = json.loads((out / "job.json").read_text())
    res = {"resume": [], "letter": [], "ok": False}
    import render
    pdfs = list(out.glob(f"{render.doc_prefix()}_Resume_*.pdf"))
    if not pdfs:
        res["resume"].append("resume pdf missing")
    else:
        from pypdf import PdfReader
        pages = len(PdfReader(str(pdfs[0])).pages)
        if pages != 1:
            res["resume"].append(f"resume is {pages} pages")
    letter = out / "cover_letter.md"
    if not letter.exists():
        res["letter"].append("cover_letter.md missing")
    else:
        errs, words = check_letter(letter.read_text(), job)
        res["letter"], res["letter_words"] = errs, words
    res["ok"] = not res["resume"] and not res["letter"]
    (out / "verify.json").write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    print(json.dumps(check(sys.argv[1]), indent=1))
