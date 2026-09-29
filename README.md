# Job Hunt Agent

An agentic pipeline, built on Claude Code, that finds internship and co-op postings across ~100 company job boards and
several community lists, ranks them against a candidate's real background, and writes a tailored resume and cover letter
for the best matches. The output is a spreadsheet plus one folder per application.

It never applies, messages anyone, or logs in anywhere. It prepares drafts, and a person reviews and sends them.

## What it does

| Stage | How | LLM? |
|---|---|---|
| 1. Web scout | Reads career pages that have no public API and runs capped open web searches | Yes (Sonnet subagent) |
| 2. Discover | Pulls live postings from Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Workday, Recruitee and iCIMS boards, intern-list.com (Airtable) and GitHub internship lists | No |
| 3. Filter + enrich | Keeps student-level roles in the target markets and flags eligibility issues (citizenship, clearance, French, PhD-only, graduation window); fetches full descriptions from each ATS or schema.org JobPosting data | No |
| 4. Score | Scores each job 0 to 100 on a rubric (skills with evidence, AI relevance, level/term, eligibility, location), calibrated against the candidate's own ratings | Yes (Sonnet subagent) |
| 5. Tailor | Builds a 1-page resume and a cover letter for the top jobs, after re-checking that the posting is still live | Yes (Sonnet subagent) |
| 6. Fact-check | A second, independent agent checks every sentence of each letter against the candidate's facts and the posting | Yes (Sonnet subagent) |
| 7. Export | `output/Job_Tracker.xlsx`, where your Status and Notes columns survive every run | No |

About 3 minutes of each run is plain Python (fetching ~19k postings and deduplicating them). The LLM stages have daily caps
so a normal run fits within a Claude subscription plan. If a usage limit stops a run, the next run resumes from `data/jobs.db`.

## Honesty by design

- **The resume can't contain invented claims.** The tailor agent can't write resume text. It returns a *plan* that
  selects and orders pre-approved bullets from `profile/facts.yaml`, and `scripts/render.py` builds the document from
  that file.
- **Cover letters get two checks.** `scripts/verify_docs.py` requires every number to appear in the facts file or
  the posting, and blocks banned phrases and a personal do-not-claim list. Then the fact-checker agent checks each
  sentence for inflated scope ("built" → "led", team → solo), wrong tools on the wrong project, and company claims that
  aren't in the posting.
- **Tested with planted faults.** An invented metric was caught by the script. An invented "I led the team and
  deployed to production" and a tool credited to the wrong project were caught by the fact-checker, which wasn't told
  what was planted.

## Engineering notes

- **Real-time tracking:** every run refetches all sources. A posting is marked closed only when every source that listed
  it fetched successfully and none of them still has it, so a failed fetch never looks like a closed job.
- **Cross-source dedupe:** jobs are keyed on company plus the set of meaningful title words (term, year and level words
  removed), so reworded copies of one posting from different sources merge.
- **Board verification:** new ATS boards found by the scout are accepted only if a posting the scout saw on the
  company's own site appears in that board's live feed. This guards against same-name companies ("vector", "karbon").
- **Prompt-injection hygiene:** all page content is treated as data, and agents write only through validating helper
  scripts (`scout_io.py`, `score_io.py`, `tailor_io.py`).

## Setup

Requirements: macOS or Linux, Python 3.9+, [LibreOffice](https://www.libreoffice.org/) (`soffice`, for PDFs), and
[Claude Code](https://code.claude.com/) signed in.

```bash
git clone https://github.com/DarkMatter1217/job-hunt-agent.git
cd job-hunt-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p profile            # then create the files below
claude                      # accept the "trust this folder" prompt once
```

Inside Claude Code:

```
/job-hunt daily      # everything
/job-hunt discover   # fetch + filter only
/job-hunt score      # score waiting jobs
/job-hunt tailor     # write applications for top jobs
/job-hunt export     # rebuild the spreadsheet
/job-hunt status     # summary, no fetching
```

## The profile (not in this repo)

The candidate's data lives in `profile/`, which is gitignored. The pipeline expects four files.

**`profile/facts.yaml`** is the single source of truth. Only `status: approved` items are ever used.

```yaml
contact: {name: Jane Doe, location: "Toronto, ON", phone: "...", email: jane@example.com,
          linkedin: https://www.linkedin.com/in/..., github: https://github.com/...}
education:
  - {id: edu_msc, school: Example University, degree: MSc Computer Science, dates: Sep 2026 - May 2028 (expected)}
  - {id: edu_bsc, school: ..., degree: ..., dates: ..., coursework_default: [...], coursework_optional: [...]}
experience:
  - id: exp_acme
    org: Acme Corp
    title: ML Intern
    location: Remote
    dates: May 2025 - Aug 2025
    bullets:
      - id: acme_rag
        text: Built a retrieval pipeline over internal docs with FAISS and FastAPI.
        variants: [{text: "...", status: approved}]
        status: approved
projects:
  - {id: proj_x, name: ..., link: ..., note: team project, stack: [...], bullets: [...]}
publication: {id: pub_x, text: ..., link: ...}
skills: {languages: [{name: Python}], ml_llm: [...], data: [...], backend_tools: [...]}
certifications: [{id: ..., text: ...}]
achievements: [{id: ..., text: ...}]
languages: {text: "English (fluent)"}
volunteer: [{id: ..., title: ..., org: ..., dates: ..., bullets: [{id: ..., text: ..., status: approved}]}]
optional: [{id: ..., text: ..., use_when: ...}]
do_not_claim: ["things the candidate must never claim"]
do_not_claim_patterns: ['\btensorflow\b']      # regexes blocked in letters
```

**`profile/preferences.yaml`** holds the candidate's graduation date and work authorization, target terms, role
keywords, markets (the first one is the home city), eligibility flag patterns, and daily caps (`daily_tailor_cap`,
`min_score_to_tailor`, `daily_score_cap`, ...). It can also set `outputs.resume_template`, a `.docx` whose styles are reused.

**`profile/rubric.md`** holds the scoring rubric with five parts: skills 25, AI relevance 25, level/term 10,
eligibility 25, location 15. Calibrate it by rating ~20 postings yourself and comparing.

**`profile/style.md`** holds the letter voice, length and a `Banned words and phrases:` list.

`registry/companies.yaml` (included) lists the companies checked directly, with their ATS type and board token. It was
researched for AI/ML co-ops in Canada, but any company can be added.

## Limitations

- JavaScript-only career sites are covered by the web scout on a best-effort basis.
- US roles are capped by default for candidates without US work authorization.
- Always read an application before sending it. The checks stop invented facts, but you're the judge of tone.
