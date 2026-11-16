"""Write a tailored cover letter + CV for one job. Never invents facts."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from .core import load_cv_text, resolve

RULES = """STRICT RULES:
- Use ONLY facts that appear in the CV. Never invent employers, dates, numbers, degrees, tools or achievements.
- If the job asks for something the CV doesn't show, don't claim it; you may say the candidate is keen to learn it.
- No clichés ("I am writing to express my interest", "dynamic team", "passionate"). Concrete, confident, short.
- Plain text, no markdown symbols, no placeholders like [Company]."""

LETTER_PROMPT = """Write a cover letter for this job application in {lang_name}.

CV:
{cv}

JOB: {title} at {company} ({location})
{desc}

{rules}
- {length}
- Austrian conventions. Greeting: use a named contact if the ad names one, otherwise "{greeting}".
- Open with why this specific role/company, then 2–3 concrete matches between the job's main requirements and
  the CV (name the project or experience), then availability ({availability}), then a short closing.
- End with "{closing}" and the name {name}.
Return only the letter text."""

CV_PROMPT = """Tailor this CV (Markdown) for the job below. Return the full CV in the same Markdown structure.

CV:
{cv}

JOB: {title} at {company}
{desc}

{rules}
- Allowed changes: rewrite the Summary (2–3 sentences) aimed at this job; reorder bullets and skills so the most
  relevant come first; rephrase bullets to use the job's terminology where it is truthful; drop clearly irrelevant
  minor items. Keep contact line, dates, employers, degrees exactly as they are.
- Write it in {lang_name}.
Return only the Markdown CV."""


def detect_lang(text: str) -> str:
    words = re.findall(r"[a-zäöüß]+", (text or "").lower())
    de = sum(w in {"und", "die", "der", "wir", "sie", "mit", "für", "deine", "ihre", "bei", "du", "unser", "kenntnisse"}
             for w in words)
    en = sum(w in {"and", "the", "we", "you", "with", "for", "your", "our", "experience", "skills"} for w in words)
    return "de" if de > en else "en"


def availability(types: str, lang: str) -> str:
    student = any(t in (types or "") for t in ("working_student", "internship", "thesis", "part_time"))
    if lang == "de":
        return ("neben meinem berufsbegleitenden MSc AI & Data Science  flexibel verfügbar"
                if student else "ab sofort in Vollzeit verfügbar – mein MSc  ist ein Abendstudium")
    return ("available part-time alongside my evening MSc in AI & Data Science "
            if student else "available full-time immediately — my MSc  is an evening programme")


BOTH_PROMPT = """Prepare a job application in {lang_name}: a cover letter and a new summary paragraph for the CV.

CV:
{cv}

JOB: {title} at {company} ({location})
{desc}

{rules}
COVER LETTER:
- {length}
- Austrian conventions. Greeting: use a named contact if the ad names one, otherwise "{greeting}".
- Open with why this specific role/company, then 2–3 concrete matches between the job's main requirements and
  the CV (name the project or experience), then availability ({availability}), then a short closing.
- End with "{closing}" and the name {name}. Separate paragraphs with a blank line.
CV SUMMARY:
- 2–3 sentences replacing the CV's summary, aimed at this job, using only facts from the CV.
Return ONLY JSON: {{"letter": "<the full letter>", "summary": "<the new summary>"}}"""

TRANSLATE_PROMPT = """Translate this CV (Markdown) into {lang_name}. Keep the exact Markdown structure, every fact,
number, date, name, employer, tool and link unchanged. Do not add, drop or embellish anything. Keep proper nouns
and technology names as they are. Return only the Markdown.

{cv}"""


def app_folder(cfg: dict, job: dict) -> Path:
    """The job's folder under applications/ (reused if it already exists from an earlier day)."""
    root = resolve(cfg, cfg["app"]["applications_dir"])
    if job.get("app_dir") and Path(job["app_dir"]).is_dir():
        return Path(job["app_dir"])
    folder = root / f"{date.today():%Y-%m-%d}_{_slug(job.get('company') or 'company')}_{_slug(job['title'])[:40]}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def base_cv(cfg: dict, lang: str, llm, log=print) -> str:
    """Your CV in the job's language. A translation is made once and saved next to cv.md (edit it freely)."""
    cv = load_cv_text(cfg)
    if lang == detect_lang(cv):
        return cv
    src = resolve(cfg, cfg["profile"]["cv_file"])
    cached = src.with_name(f"{src.stem}.{lang}.md")
    if cached.exists() and cached.stat().st_mtime >= src.stat().st_mtime:
        return cached.read_text(encoding="utf-8")
    out = llm.chat(TRANSLATE_PROMPT.format(lang_name="German" if lang == "de" else "English", cv=cv),
                   max_tokens=2500, best=True).strip()
    out = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", out)
    if len(out) < len(cv) * 0.6:
        return cv
    cached.write_text(out, encoding="utf-8")
    log(f"  translated your CV once -> {cached.name} (check it; it is reused for every {lang} application)")
    return out


def _with_summary(cv_md: str, summary: str) -> str:
    """Swap the body of the CV's first '## ' section (the summary) for the tailored one."""
    if not summary:
        return cv_md
    m = re.search(r"(^## [^\n]*\n)(.*?)(?=^## )", cv_md, re.S | re.M)
    return cv_md[:m.start(2)] + summary + "\n\n" + cv_md[m.end(2):] if m else cv_md


def tailor(cfg: dict, job: dict, llm, log=print) -> Path:
    cv = load_cv_text(cfg)
    desc = (job.get("description") or "")[:7000]
    lang = (json.loads(job["ai_json"]).get("language") if job.get("ai_json") else None) or detect_lang(desc + job["title"])
    lang_name = "German" if lang == "de" else "English"
    name = cfg["profile"]["name"]
    avail = availability(job.get("types", ""), lang)
    folder = app_folder(cfg, job)

    letter = cv_md = None
    if llm.enabled:
        base = base_cv(cfg, lang, llm, log)            # your CV in the job's language (translated once, cached)
        try:                                           # one call: letter + a new CV summary. The rest of the CV
            res = llm.json(BOTH_PROMPT.format(         # stays word for word, so nothing can be invented there.
                cv=base, title=job["title"], company=job.get("company") or "the company",
                location=job.get("location", ""), desc=desc, rules=RULES, lang_name=lang_name, name=name,
                availability=avail, length="250–330 words" if lang == "en" else "250–330 Wörter",
                greeting="Sehr geehrte Damen und Herren," if lang == "de" else "Dear Hiring Team,",
                closing="Mit freundlichen Grüßen" if lang == "de" else "Kind regards"), best=True, max_tokens=2200)
            letter = str(res.get("letter") or "").strip()
            if len(letter.split()) < 120:
                raise ValueError("letter too short")
            cv_md = _with_summary(base, str(res.get("summary") or "").strip())
        except Exception as e:
            if getattr(e, "status", 0) == 429:
                raise
            letter = None
    if letter is None:
        letter = _template_letter(cfg, job, lang, avail)
        cv_md = cv

    (folder / "cover_letter.txt").write_text(letter, encoding="utf-8")
    (folder / "cv_tailored.md").write_text(cv_md, encoding="utf-8")
    (folder / "job.md").write_text(
        f"# {job['title']}\n**{job.get('company', '')}** — {job.get('location', '')}\n\n{job.get('url', '')}\n\n"
        f"{job.get('description', '')}", encoding="utf-8")
    try:
        _docx_letter(letter, name, cfg, folder / "cover_letter.docx")
        _docx_from_md(cv_md, folder / "cv_tailored.docx")
    except ImportError:
        log("  (pip install python-docx to also get .docx files)")
    (folder / "CHECK_BEFORE_SENDING.txt").write_text(
        "Before you send:\n"
        "1. Read the letter out loud once. Fix anything that doesn't sound like you.\n"
        "2. Check that every claim is true (the AI was told not to invent, but verify).\n"
        "3. Add the contact person's name if the ad has one.\n"
        "4. Attach your designed CV PDF if you prefer it over cv_tailored.docx.\n"
        f"5. Apply here: {job.get('url', '')}\n", encoding="utf-8")
    return folder


def _template_letter(cfg, job, lang, avail) -> str:
    nice = {"sql": "SQL", "aws": "AWS", "etl": "ETL", "llm": "LLMs", "nlp": "NLP", "gcp": "GCP", "api": "APIs",
            "fastapi": "FastAPI", "pytorch": "PyTorch", "postgresql": "PostgreSQL", "mongodb": "MongoDB",
            "numpy": "NumPy", "rabbitmq": "RabbitMQ", "github actions": "GitHub Actions", "rest api": "REST APIs"}
    d = (job.get("description") or "").lower()
    skills = [nice.get(s, s.capitalize() if s.islower() and " " not in s else s)
              for s in cfg["profile"]["skills"] if re.search(rf"(?<![a-z]){re.escape(s)}(?![a-z])", d)][:5]
    name = cfg["profile"]["name"]
    if lang == "de":
        return (f"Sehr geehrte Damen und Herren,\n\nmit großem Interesse habe ich Ihre Ausschreibung „{job['title']}“ "
                f"bei {job.get('company') or 'Ihrem Unternehmen'} gelesen.\n\nIn meiner letzten Position habe ich Produktions-Scraper für "
                f"über 20 Bewerbermanagementsysteme gebaut und Python-Pipelines entwickelt, die Hunderttausende "
                f"Stellenanzeigen in ein einheitliches MongoDB-Schema überführen. "
                f"{('Besonders passen meine Erfahrungen mit ' + ', '.join(skills) + '. ') if skills else ''}"
                f"\n\nIch bin {avail}.\n\nIch freue mich auf ein Gespräch.\n\nMit freundlichen Grüßen\n{name}")
    return (f"Dear Hiring Team,\n\nI'd like to apply for the {job['title']} role at {job.get('company') or 'your company'}.\n\n"
            f"In my last role I built production scrapers for 20+ applicant tracking systems and Python pipelines "
            f"that turn hundreds of thousands of job postings into one standard MongoDB schema. "
            f"{('This role asks for ' + ', '.join(skills) + ', which I use day to day. ') if skills else ''}"
            f"\n\nI am {avail}.\n\nI'd welcome the chance to talk.\n\nKind regards\n{name}")


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:50] or "x"


def _docx_letter(text: str, name: str, cfg: dict, out: Path):
    from docx import Document
    from docx.shared import Pt
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(11)
    doc.add_paragraph(f"{name}\nVienna, Austria\n{date.today():%d.%m.%Y}")
    for para in text.split("\n\n"):
        doc.add_paragraph(para.strip())
    doc.save(out)


def _docx_from_md(md: str, out: Path):
    from docx import Document
    from docx.shared import Pt
    doc = Document()
    doc.styles["Normal"].font.name, doc.styles["Normal"].font.size = "Calibri", Pt(10.5)

    def add_runs(p, text):
        for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
            if part:
                p.add_run(part).bold = bool(i % 2)

    for line in md.splitlines():
        s = line.rstrip()
        if not s:
            continue
        if s.startswith("### "):
            doc.add_heading(s[4:], level=3)
        elif s.startswith("## "):
            doc.add_heading(s[3:], level=2)
        elif s.startswith("# "):
            doc.add_heading(s[2:], level=1)
        elif re.match(r"^\s*[-*•] ", s):
            add_runs(doc.add_paragraph(style="List Bullet"), re.sub(r"^\s*[-*•] ", "", s))
        else:
            add_runs(doc.add_paragraph(), s)
    doc.save(out)
