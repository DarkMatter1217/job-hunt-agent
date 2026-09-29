"""Build the tailored resume and cover letter (.docx + .pdf) for one job. No LLM.

The resume text comes ONLY from profile/facts.yaml. The tailor supplies a plan that selects and orders bullets,
picks approved variants, and orders skills; it cannot add words. Layout: Letter, Calibri, 0.55/0.45 in margins,
right tab at 7.4 in. If preferences.yaml sets outputs.resume_template (a .docx), its page setup and styles are reused.

Plan schema (validated by validate_plan):
{
  "experience": [{"id": "exp_acme", "bullets": ["acme_rag:v1", "acme_eval"]}, ...],                   # every role, >=1 bullet each
  "projects":   [{"id": "proj_x", "bullets": ["x_pipeline", "x_result"]}, ...],                       # 2-3 projects, any order
  "skills": {"languages": [...], "ml_llm": [...], "data": [...], "backend_tools": [...]},               # subsets of facts.yaml names
  "coursework_extra": ["Software Engineering"],                                                        # from coursework_optional
  "include_optional": ["opt_test_score"]                                                               # from facts.yaml optional
}
Bullet refs: "<id>" uses the approved text; "<id>:vN" uses approved variant N (1-based).
"""
import copy
import re
import subprocess
from pathlib import Path

import docx
import yaml
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
FACTS = yaml.safe_load(open(ROOT / "profile" / "facts.yaml"))
PREFS = yaml.safe_load(open(ROOT / "profile" / "preferences.yaml"))
_tpl = (PREFS.get("outputs") or {}).get("resume_template")
TEMPLATE = (ROOT / _tpl).resolve() if _tpl else None  # optional .docx whose styles/page setup are reused


def doc_prefix():
    """File-name prefix from the candidate's name, e.g. 'Jane_Doe' -> Jane_Doe_Resume_<Company>.pdf"""
    return re.sub(r"[^A-Za-z0-9]+", "_", FACTS["contact"]["name"]).strip("_")
SKILL_LABELS = {"languages": "Languages", "ml_llm": "ML / LLM", "data": "Data", "backend_tools": "Backend & Tools"}
LINK_BLUE = RGBColor(0x1F, 0x4E, 0x9A)


# ---------------- plan validation ----------------

def _index():
    idx = {}
    for sec in ("experience", "projects"):
        for item in FACTS[sec]:
            idx[item["id"]] = item
    return idx


def bullet_text(item, ref):
    bid, _, var = ref.partition(":v")
    b = next((b for b in item["bullets"] if b["id"] == bid), None)
    if b is None:
        raise ValueError(f"bullet {bid} is not in {item['id']}")
    if b.get("status") != "approved":
        raise ValueError(f"bullet {bid} is not approved")
    if not var:
        return b["text"]
    vs = [v for v in b.get("variants", []) if v.get("status") == "approved"]
    n = int(var)
    if not 1 <= n <= len(vs):
        raise ValueError(f"{bid} has no approved variant {n}")
    return vs[n - 1]["text"]


def _coursework_entry():
    """The education entry that lists coursework (the first one with coursework_default), or {}."""
    return next((e for e in FACTS["education"] if e.get("coursework_default")), {})


def validate_plan(plan):
    errs, idx = [], _index()
    exp_ids = [i["id"] for i in FACTS["experience"]]
    got = [e.get("id") for e in plan.get("experience", [])]
    if sorted(got) != sorted(exp_ids):
        errs.append(f"experience must list all roles {exp_ids} (you can reorder bullets, not remove roles)")
    projs = plan.get("projects", [])
    if not 2 <= len(projs) <= 3:
        errs.append("projects: pick 2 or 3")
    for sec in ("experience", "projects"):
        for e in plan.get(sec, []):
            item = idx.get(e.get("id"))
            if item is None or (sec == "projects" and not e["id"].startswith("proj_")):
                errs.append(f"unknown {sec} id {e.get('id')}")
                continue
            if not e.get("bullets"):
                errs.append(f"{e['id']}: needs at least 1 bullet")
            if len(set(r.partition(':v')[0] for r in e.get("bullets", []))) != len(e.get("bullets", [])):
                errs.append(f"{e['id']}: a bullet appears twice")
            for r in e.get("bullets", []):
                try:
                    bullet_text(item, r)
                except ValueError as x:
                    errs.append(str(x))
    all_skills = {cat: [s["name"] for s in FACTS["skills"][cat]] for cat in SKILL_LABELS}
    for cat, names in (plan.get("skills") or {}).items():
        if cat not in all_skills:
            errs.append(f"unknown skills category {cat}")
            continue
        bad = [n for n in names if n not in all_skills[cat]]
        if bad:
            errs.append(f"skills.{cat}: not in facts.yaml {bad}")
    opt_cw = _coursework_entry().get("coursework_optional", [])
    bad = [c for c in plan.get("coursework_extra", []) if c not in opt_cw]
    if bad:
        errs.append(f"coursework_extra not allowed: {bad}")
    opt_ids = [o["id"] for o in FACTS.get("optional", [])]
    bad = [o for o in plan.get("include_optional", []) if o not in opt_ids]
    if bad:
        errs.append(f"include_optional unknown: {bad}")
    return errs


# ---------------- docx helpers ----------------

def _blank_from_template():
    if TEMPLATE and TEMPLATE.exists():
        d = docx.Document(str(TEMPLATE))
    else:
        d = docx.Document()
        st = d.styles["Normal"]; st.font.name, st.font.size = "Calibri", Pt(10)
        for s in d.sections:
            s.page_width, s.page_height = Inches(8.5), Inches(11)
            s.left_margin = s.right_margin = Inches(0.55); s.top_margin = s.bottom_margin = Inches(0.45)
    body = d.element.body
    for el in list(body):
        if el.tag != qn("w:sectPr"):
            body.remove(el)
    return d


def _fmt(p, before=0, after=1):
    pf = p.paragraph_format
    pf.space_before, pf.space_after = Pt(before), Pt(after)
    return p


def _run(p, text, size=10, bold=False, italic=False, color=None):
    r = p.add_run(text)
    r.font.name, r.font.size, r.bold, r.italic = "Calibri", Pt(size), bold, italic
    if color:
        r.font.color.rgb = color
    return r


def _link(p, text, url, size=10, italic=False):
    part = p.part
    rid = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
    h = OxmlElement("w:hyperlink"); h.set(qn("r:id"), rid)
    r = OxmlElement("w:r"); rpr = OxmlElement("w:rPr")
    for tag, val in (("w:rFonts", None), ("w:color", "1F4E9A"), ("w:u", "single"), ("w:sz", str(int(size * 2)))):
        e = OxmlElement(tag)
        if tag == "w:rFonts":
            e.set(qn("w:ascii"), "Calibri"); e.set(qn("w:hAnsi"), "Calibri")
        else:
            e.set(qn("w:val"), val)
        rpr.append(e)
    if italic:
        rpr.append(OxmlElement("w:i"))
    r.append(rpr)
    t = OxmlElement("w:t"); t.text = text; t.set(qn("xml:space"), "preserve"); r.append(t)
    h.append(r); p._p.append(h)


def _heading(d, text):
    p = _fmt(d.add_paragraph(), before=5, after=2)
    _run(p, text, size=11, bold=True)
    pPr = p._p.get_or_add_pPr(); bdr = OxmlElement("w:pBdr"); b = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "808080")):
        b.set(qn(k), v)
    bdr.append(b); pPr.append(bdr)


def _entry(d, first_gap=False):
    p = _fmt(d.add_paragraph(), before=4 if first_gap else 2, after=1)
    p.paragraph_format.tab_stops.add_tab_stop(Inches(7.4), WD_TAB_ALIGNMENT.RIGHT)
    return p


def _bullet(d, text):
    p = _fmt(d.add_paragraph(), before=0, after=1)
    pf = p.paragraph_format
    pf.left_indent, pf.first_line_indent = Inches(0.2), Inches(-0.13)
    _run(p, "•  " + text)
    return p


def _contact_line(d):
    c = FACTS["contact"]
    p = _fmt(d.add_paragraph(), after=2); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(p, f"{c['location']} | {c['phone']} | ", size=9.5)
    _link(p, c["email"], "mailto:" + c["email"], size=9.5); _run(p, " | ", size=9.5)
    _link(p, c["linkedin"].replace("https://www.", ""), c["linkedin"], size=9.5); _run(p, " | ", size=9.5)
    _link(p, c["github"].replace("https://", ""), c["github"], size=9.5)


def _name(d):
    p = _fmt(d.add_paragraph(), after=2); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(p, FACTS["contact"]["name"], size=16, bold=True)


# ---------------- resume ----------------

def build_resume_docx(plan, path):
    d = _blank_from_template()
    idx = _index()
    _name(d); _contact_line(d)

    _heading(d, "EDUCATION")
    for e in FACTS["education"]:
        p = _entry(d)
        _run(p, e["school"], bold=True)
        rest = f", {e['degree']}" + (f", {e['gpa']}" if e.get("gpa") else "")
        _run(p, rest); _run(p, "\t" + e["dates"])
    cw_src = _coursework_entry()
    cw = cw_src.get("coursework_default", []) + [c for c in plan.get("coursework_extra", []) if c not in cw_src.get("coursework_default", [])]
    if cw:
        p = _fmt(d.add_paragraph(), after=1); _run(p, "Relevant coursework: " + ", ".join(cw), italic=True)

    _heading(d, "EXPERIENCE")
    order = [i["id"] for i in FACTS["experience"]]  # facts.yaml order is reverse-chronological
    by_id = {e["id"]: e for e in plan["experience"]}
    for n, rid in enumerate(order):
        item = idx[rid]
        p = _entry(d, first_gap=n > 0)
        _run(p, item["org"], bold=True); _run(p, f", {item['title']}")
        _run(p, f"\t{item['location']} | {item['dates']}")
        for ref in by_id[rid]["bullets"]:
            _bullet(d, bullet_text(item, ref))

    _heading(d, "PROJECTS")
    for n, pr in enumerate(plan["projects"]):
        item = idx[pr["id"]]
        p = _entry(d, first_gap=n > 0)
        if item.get("link"):
            _link(p, item["name"], item["link"])
        else:
            _run(p, item["name"], bold=True)
        if item.get("links"):
            _run(p, "  (");
            for i, u in enumerate(item["links"]):
                _link(p, (item.get("link_labels") or [f"link {k + 1}" for k in range(len(item["links"]))])[i], u)
                if i < len(item["links"]) - 1:
                    _run(p, ", ")
            _run(p, ")")
        if item.get("note"):
            _run(p, f"  ({item['note']})", italic=True)
        _run(p, "\t" + ", ".join(item.get("stack", [])))
        for ref in pr["bullets"]:
            _bullet(d, bullet_text(item, ref))

    _heading(d, "PUBLICATION")
    pub = FACTS["publication"]
    p = _fmt(d.add_paragraph(), after=1); _run(p, pub["text"] + " "); _link(p, "Read the paper", pub["link"])

    _heading(d, "TECHNICAL SKILLS")
    sk = plan.get("skills") or {}
    for cat, label in SKILL_LABELS.items():
        names = sk.get(cat) or [s["name"] for s in FACTS["skills"][cat]]
        p = _fmt(d.add_paragraph(), after=1); _run(p, f"{label}: ", bold=True); _run(p, ", ".join(names))

    _heading(d, "CERTIFICATIONS & ACHIEVEMENTS")
    p = _fmt(d.add_paragraph(), after=1); _run(p, "Certifications: ", bold=True)
    _run(p, "; ".join(c["text"] for c in FACTS["certifications"]))
    # optional facts included by the plan are appended to the line named by their `append_to` (default achievements)
    opt = {o["id"]: o for o in FACTS.get("optional", [])}
    extra = {"achievements": [], "languages": []}
    for oid in plan.get("include_optional", []):
        extra[opt[oid].get("append_to", "achievements")].append(opt[oid]["text"])
    p = _fmt(d.add_paragraph(), after=1); _run(p, "Achievements: ", bold=True)
    _run(p, "; ".join([a["text"] for a in FACTS["achievements"]] + extra["achievements"]))
    langs = "; ".join([FACTS["languages"]["text"]] + extra["languages"])
    p = _fmt(d.add_paragraph(), after=1); _run(p, "Languages: ", bold=True); _run(p, langs)

    _heading(d, "VOLUNTEER & LEADERSHIP")
    for v in FACTS["volunteer"]:
        p = _entry(d); _run(p, v["title"], bold=True); _run(p, f", {v['org']}"); _run(p, "\t" + v["dates"])
        for b in v["bullets"]:
            _bullet(d, b["text"])
    d.save(path)


def to_pdf(docx_path):
    out = Path(docx_path).parent
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(out), str(docx_path)],
                   capture_output=True, timeout=120, check=True)
    pdf = Path(docx_path).with_suffix(".pdf")
    return pdf, len(PdfReader(str(pdf)).pages)


def fit_one_page(plan, docx_path, max_trims=8):
    """Render; if over 1 page, drop the last (least relevant) bullet from the longest list and retry.
    Returns (pdf_path, pages, trimmed_refs)."""
    plan = copy.deepcopy(plan)
    trimmed = []
    for _ in range(max_trims + 1):
        build_resume_docx(plan, docx_path)
        pdf, pages = to_pdf(docx_path)
        if pages <= 1:
            return pdf, pages, trimmed, plan
        cands = [e for e in plan["projects"] + plan["experience"] if len(e["bullets"]) > 1]
        if not cands:
            break
        victim = max(cands, key=lambda e: (len(e["bullets"]), e in plan["projects"]))
        trimmed.append(f"{victim['id']}:{victim['bullets'].pop()}")
    return pdf, pages, trimmed, plan


# ---------------- cover letter ----------------

def build_letter_docx(company, role, paragraphs, path, date_text):
    d = _blank_from_template()
    for s in d.sections:
        s.left_margin = s.right_margin = Inches(1.0); s.top_margin = s.bottom_margin = Inches(0.9)
    _name(d); _contact_line(d)
    p = _fmt(d.add_paragraph(), before=14, after=10); _run(p, date_text, size=11)
    p = _fmt(d.add_paragraph(), after=10); _run(p, f"Hiring Team, {company}\nRe: {role}", size=11)
    for para in paragraphs:
        p = _fmt(d.add_paragraph(), after=9); p.paragraph_format.line_spacing = 1.1
        _run(p, para, size=11)
    p = _fmt(d.add_paragraph(), before=4, after=0); _run(p, "Sincerely,\n" + FACTS["contact"]["name"], size=11)
    d.save(path)


def letter_paragraphs(md_text):
    """Plain paragraphs from the tailor's markdown: drop headings/greeting/sign-off lines it may add."""
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", md_text) if p.strip()]
    name = re.escape(FACTS["contact"]["name"].lower())
    skip = re.compile(r"^(#|dear\b|hi\b|hello\b|to whom|sincerely|best regards|regards|thank you,$|" + name + r"$)", re.I)
    return [p for p in paras if not skip.match(p)]
