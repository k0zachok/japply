"""Auto-apply: tailor documents, open the application form in a real browser, fill it, submit.

Anything that can't be done safely (site needs a login, CAPTCHA, a question only you can answer, a form the
script doesn't understand) is moved to "Needs you" with the reason and the documents ready. Nothing is guessed.
"""
from __future__ import annotations

import html
import json
import os
import re
import smtplib
import urllib.parse
from email.message import EmailMessage
from pathlib import Path

import time

from . import mailbox, pipeline
from .core import HttpError, load_cv_text, norm, now_iso, resolve
from .llm import LLM
from .tailor import app_folder, detect_lang, tailor

APPLY_WORDS = re.compile(r"apply|bewerb|application|postuler|solliciteer", re.I)
BOARD_HOSTS = ("jobicy.com", "himalayas.app", "remoteok.com", "remotive.com", "weworkremotely.com", "arbeitnow.")
APPLY_START = re.compile(r"^\W*(apply|jetzt bewerben|bewerben|zur bewerbung|online bewerben|i'?m interested|"
                         r"ich bin interessiert|schnellbewerbung|einfach bewerben|weiter zur bewerbung|"
                         r"start (your )?application|submit (an? )?application|bewerbungsformular|zum bewerbungsformular|"
                         r"direkt bewerben|hier bewerben|online[- ]bewerbung|application form|fill (in|out) |"
                         r"(continue|apply|bewerben|weiter) (as (a )?guest|als gast|without (an? )?(account|registration)|"
                         r"ohne (konto|registrierung|account|anmeldung))|manuell bewerben|apply manually)", re.I)
NEXT_BTN = re.compile(r"^\W*(continue|next|weiter|fortfahren|nächster schritt|save and continue|speichern und weiter|"
                      r"review( your)? application|bewerbung prüfen)", re.I)
SUBMIT_BTN = re.compile(r"submit|absenden|abschicken|send application|bewerbung (ab)?senden|^\W*(apply|jetzt bewerben|"
                        r"bewerben|senden|send)\b", re.I)
BLOCKED = re.compile(r"request blocked|performing security verification|you have been blocked|access denied|verify (that )?you are (a )?human|"
                     r"unusual traffic|are you a robot", re.I)
SUCCESS = re.compile(r"thank you|thanks for|danke|vielen dank|(you'?ve|you have) (successfully )?(submitted|applied)|submitted your application|application (has been |was )?(received|submitted|sent)|"
                     r"successfully (submitted|applied|sent)|bewerbung (ist |wurde )?(eingegangen|erhalten|gesendet|"
                     r"übermittelt|abgeschickt)|erfolgreich", re.I)
CAPTCHA = ("iframe[src*='hcaptcha.com']", "iframe[src*='challenges.cloudflare.com']",
           "iframe[src*='recaptcha/api2/bframe']", "iframe[src*='recaptcha/enterprise/bframe']", "#px-captcha",
           "iframe[src*='recaptcha/api2/anchor']:not([src*='size=invisible'])", "iframe[src*='captcha-delivery.com']")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*[a-z]{2,}", re.I)

PERSONAL = re.compile(r"anrede|salutation|gender|geschlecht|birth|geburt|geboren|nationalit|staatsangeh|staatsbürger|"
                      r"citizenship|religio|konfession|disab|behinder|marital|familienstand|ethnic|race\b|veteran|"
                      r"sozialversicherung|social security|passport|reisepass|iban|pronoun", re.I)

# label/name pattern -> key in [apply.answers] (or a special value). First match wins.
RULES = [
    (r"first and last name|full name|legal name|vor- und nachname", "full_name"),
    (r"first.?name|vorname|given.?name|prénom", "first_name"),
    (r"last.?name|nachname|surname|family.?name|familienname", "last_name"),
    (r"e-?mail", "email"),
    (r"birth|geburt|\bdob\b|geboren", "birth_date"),
    (r"anrede|salutation|\bgender\b|geschlecht|\btitle \(mr", "salutation"),
    (r"phone|telefon|mobile|handy|mobil", "phone"),
    (r"linkedin", "linkedin"),
    (r"github|gitlab", "github"),
    (r"salary|gehalt|compensation|vergütung", "salary_expectation"),
    (r"start date(?! ?(month|year))|earliest|eintritt|frühest|available from|verfügbar ab|notice period|kündigungsfrist|when can you",
     "earliest_start"),
    (r"work permit|work authori[sz]ation|right to work|arbeitserlaubnis|arbeitsgenehmigung|visa|sponsorship",
     "work_permit"),
    (r"post ?code|postal|zip|\bplz\b|postleitzahl", "postcode"),
    (r"address|adresse|straße|street", "address"),
    (r"\bcity\b|stadt|wohnort|\bort\b|current location|where are you (based|located)|^location", "city"),
    (r"country|\bland\b", "country"),
    (r"^(full |your |vor- und nach)?name\b|vollständiger name", "full_name"),
    (r"cover letter|anschreiben|motivation|message to|nachricht|why (do you|are you)|additional information", "letter"),
]

# Runs inside the page: tags every form control with data-jp and describes it.
FIELDS_JS = r"""
() => {
  const txt = e => (e ? (e.innerText || e.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                     return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const own = e => {          // the control's own label element
    let l = e.id ? document.querySelector('label[for="' + CSS.escape(e.id) + '"]') : null;
    if (!l && e.getAttribute('aria-labelledby')) l = document.getElementById(e.getAttribute('aria-labelledby').split(/\s+/)[0]);
    return l || e.closest('label');
  };
  const group = (e, skip) => { // the question a control (or a set of radio options) belongs to
    const fs = e.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && txt(lg)) return lg; }
    for (let p = e.parentElement, i = 0; p && i < 6; p = p.parentElement, i++) {
      for (const l of p.querySelectorAll(':scope > label, :scope > legend, :scope > p, :scope > span, :scope > div > label'))
        if (l !== skip && !l.contains(e) && txt(l)) return l;
    }
    return null;
  };
  const isReq = l => !!l && (/required/i.test(l.className || '') || /\*\s*$|\(required\)|pflichtfeld/i.test(txt(l)));
  document.querySelectorAll('[data-jp],[data-jp-yn]').forEach(e => { e.removeAttribute('data-jp'); e.removeAttribute('data-jp-yn'); });
  const out = [];
  let n = 0;
  for (const e of document.querySelectorAll('input, textarea, select')) {
    const type = (e.getAttribute('type') || e.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'search', 'image', 'reset', 'password'].includes(type)) continue;
    if (e.closest('header, nav, footer, [role=search], [id*=cookie], [class*=cookie], [id*=consent-banner], .fc-consent-root, [class*="fc-"], ' +
                  '#onetrust-consent-sdk, #usercentrics-root, #CybotCookiebotDialog, [class*=newsletter], [id*=newsletter]')) continue;
    // a native <select> hidden behind a styled widget (select2, chosen, tom-select…) is still the real field
    const widget = e.tagName === 'SELECT' && e.name && e.options.length > 1 &&
                   /select2-hidden|chosen|selectized|tomselected|ts-hidden|visually-hidden|sr-only/.test(e.className);
    if (!widget && type !== 'file' && !vis(e) && !['radio', 'checkbox'].includes(type)) continue;
    if (e.disabled || e.readOnly && e.getAttribute('role') !== 'combobox') continue;
    if (!widget && e.getAttribute('aria-hidden') === 'true' || (e.tabIndex === -1 && !e.name && !['file', 'radio', 'checkbox'].includes(type))) continue;
    const choice = type === 'radio' || type === 'checkbox';
    const o = own(e), g = group(e, o);
    const q = choice ? (g || o) : (o || g);
    const label = (txt(q) || e.getAttribute('aria-label') || e.getAttribute('placeholder') || '').slice(0, 300);
    // yes/no button pairs backed by a hidden checkbox (Ashby)
    let yn = false;
    if (type === 'checkbox' && e.parentElement) {
      const bs = [...e.parentElement.querySelectorAll('button')].filter(b => /^(yes|no|ja|nein)$/i.test(txt(b)));
      if (bs.length === 2) { yn = true; bs.forEach(b => b.setAttribute('data-jp-yn', n + ':' + (/^(yes|ja)$/i.test(txt(b)) ? 'yes' : 'no'))); }
    }
    const combo = e.getAttribute('role') === 'combobox' || e.getAttribute('aria-autocomplete') === 'list';
    let shown = '', cc = '';
    for (let p = e.parentElement, i = 0; p && i < 4; p = p.parentElement, i++) {
      if (combo && !shown) { const sv = p.querySelector('[class*=singleValue], [class*=single-value], [class*=SingleValue]'); if (sv) shown = txt(sv); }
      if (type === 'tel' && !cc) { const m = [...p.querySelectorAll('button, select, [role=combobox], [class*=singleValue]')].map(x => txt(x)).find(t => /^\+\d{1,4}$/.test(t)); if (m) cc = m; }
    }
    e.setAttribute('data-jp', String(n));
    out.push({
      cc,
      i: n++, type, name: e.name || e.id || '', label, option: choice && !yn ? txt(o).slice(0, 200) : '', yn,
      required: e.required || e.getAttribute('aria-required') === 'true' || isReq(q),
      combo: e.getAttribute('role') === 'combobox' || e.getAttribute('aria-autocomplete') === 'list',
      options: e.tagName === 'SELECT' ? [...e.options].map(x => txt(x)).filter(Boolean) : [],
      value: choice ? (e.checked || (yn && !!e.parentElement.querySelector('button[aria-pressed=true]')) ? '1' : '')
                    : (type === 'file' ? '' : e.value || shown || ''),
    });
  }
  // choice cards built from ARIA roles instead of real inputs (JOIN and other wizard-style forms)
  for (const e of document.querySelectorAll('[role=radio]:not(input), [role=checkbox]:not(input)')) {
    if (!vis(e) || e.querySelector('input') || e.closest('header, nav, footer')) continue;
    let q = null;
    for (let p = e.parentElement, i = 0; p && i < 7 && !q; p = p.parentElement, i++)
      q = p.querySelector(':scope > h1, :scope > h2, :scope > h3, :scope > legend, :scope > label');
    const title = [...e.querySelectorAll('p, span, label')].map(x => txt(x)).find(t => t.length > 2) || txt(e);
    e.setAttribute('data-jp', String(n));
    out.push({i: n++, type: e.getAttribute('role'), name: 'aria:' + txt(q).slice(0, 80), label: txt(q).slice(0, 300),
              option: title.slice(0, 200), yn: false, required: true, combo: false, options: [], cc: '',
              value: e.getAttribute('aria-checked') === 'true' ? '1' : ''});
  }
  return out;
}
"""


LOGIN_PAGES = {"join.com": "https://join.com/auth/login/candidate"}


ALT_NAMES = {"herr": ["Mr", "Mr.", "Male", "Männlich", "Mann"], "frau": ["Ms", "Ms.", "Mrs", "Female", "Weiblich"],
             "mr": ["Herr", "Male", "Männlich"], "ms": ["Frau", "Female", "Weiblich"], "austria": ["Österreich"], "vienna": ["Wien"], "germany": ["Deutschland"], "english": ["Englisch"],
             "german": ["Deutsch"], "ukrainian": ["Ukrainisch"], "russian": ["Russisch"], "ukraine": ["Ukraine"]}
LOGIN_CHECK = {"join.com": ("https://join.com/talent/home", "/auth/login")}   # host -> (page, login-URL marker)


class NeedsYou(Exception):
    """This job can't be auto-applied; the message is shown in the dashboard."""


# ------------------------------------------------------------------ documents
def _md_html(md: str) -> str:
    out, in_ul = [], False
    for line in md.splitlines():
        s = line.rstrip()
        b = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(s))
        m = re.match(r"^\s*[-*•] (.*)", b)
        if m:
            out.append(("" if in_ul else "<ul>") + f"<li>{m.group(1)}</li>")
            in_ul = True
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        h = re.match(r"^(#{1,3}) (.*)", b)
        if h:
            out.append(f"<h{len(h.group(1))}>{h.group(2)}</h{len(h.group(1))}>")
        elif b:
            out.append(f"<p>{b}</p>")
    return "\n".join(out) + ("</ul>" if in_ul else "")


CSS = ("<style>@page{size:A4;margin:14mm 16mm}body{font:10pt/1.38 Helvetica,Arial,sans-serif;color:#111}"
       "h1{font-size:20pt;margin:0 0 2pt}h2{font-size:11pt;margin:10pt 0 4pt;border-bottom:1px solid #999;"
       "text-transform:uppercase;letter-spacing:.04em}h3{font-size:10pt;margin:7pt 0 2pt}p{margin:3pt 0}"
       "ul{margin:2pt 0;padding-left:15pt}li{margin:1.2pt 0}"
       ".l{font-size:11pt;line-height:1.5;margin:6mm 4mm}.l p{margin:0 0 10pt}.l .from{font-size:10pt;color:#333;"
       "border-bottom:1px solid #999;padding-bottom:6pt;margin-bottom:18pt}.l .from b{font-size:14pt;color:#111;"
       "display:block;margin-bottom:2pt}.l .to{margin-bottom:16pt}.l .date{text-align:right;margin-bottom:14pt}"
       ".l .subj{font-weight:bold;margin-bottom:14pt}</style>")
MONTHS_DE = ["Jänner", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
             "November", "Dezember"]


def _pdf_fit(page, path: Path, max_pages: int = 1) -> None:
    """Print to PDF, shrinking slightly until it fits on max_pages (a junior CV should be one page)."""
    from pypdf import PdfReader
    for scale in (1.0, 0.95, 0.9, 0.86, 0.82):
        page.pdf(path=str(path), format="A4", scale=scale)
        try:
            if len(PdfReader(str(path)).pages) <= max_pages:
                return
        except Exception:
            return


def make_documents(cfg: dict, job: dict, folder: Path, browser) -> tuple[Path, Path, str]:
    """PDF versions of the tailored CV and letter (rendered by the browser). Returns (cv, letter, letter text)."""
    import datetime
    a, name = cfg.get("apply", {}), cfg["profile"]["name"].replace(" ", "_")
    ans = a.get("answers", {})
    letter = (folder / "cover_letter.txt").read_text(encoding="utf-8")
    page = browser.new_page()
    letter_pdf = folder / f"{name}_Cover_Letter.pdf"
    de = bool(re.match(r"\s*(sehr geehrte|liebe|guten tag|hallo)", letter, re.I))
    today = datetime.date.today()
    date = f"{today.day}. {MONTHS_DE[today.month - 1]} {today.year}" if de else today.strftime("%-d %B %Y")
    city = {"Vienna": "Wien"}.get(ans.get("city", ""), ans.get("city", "")) if de else ans.get("city", "")
    contact = " · ".join(filter(None, [" ".join(filter(None, [ans.get("postcode"), city])), ans.get("phone"), ans.get("email")]))
    head = (f"<div class='from'><b>{html.escape(cfg['profile']['name'])}</b>{html.escape(contact)}</div>"
            f"<div class='to'>{html.escape(job.get('company') or '')}</div>"
            f"<div class='date'>{html.escape(', '.join(filter(None, [city, date])))}</div>"
            f"<div class='subj'>{'Bewerbung als' if de else 'Application for'} {html.escape(job['title'])}</div>")
    paras = "".join(f"<p>{html.escape(p.strip()).replace(chr(10), '<br>')}</p>" for p in letter.split("\n\n") if p.strip())
    page.set_content(f"<html><head><meta charset='utf-8'>{CSS}</head><body class='l'>{head}{paras}</body></html>")
    _pdf_fit(page, letter_pdf)
    own = resolve(cfg, a["cv_pdf"]) if a.get("cv_pdf") else None
    if own and own.exists() and not a.get("tailor_cv", True):
        cv_pdf = own
    else:
        cv_pdf = folder / f"{name}_CV.pdf"
        md = (folder / "cv_tailored.md").read_text(encoding="utf-8")
        page.set_content(f"<html><head><meta charset='utf-8'>{CSS}</head><body>{_md_html(md)}</body></html>")
        _pdf_fit(page, cv_pdf)
    page.close()
    return cv_pdf, letter_pdf, letter


def _known(cfg: dict, con, label: str, name: str, choice: bool = False) -> str | None:
    """A question you answered earlier in the dashboard, else the matching [apply.answers] value."""
    r = con.execute("SELECT answer FROM answers WHERE key=? AND answer IS NOT NULL AND answer != ''",
                    (norm(label),)).fetchone()
    if r:
        return r["answer"]
    ans = dict(cfg.get("apply", {}).get("answers", {}))
    ans["full_name"] = f"{ans.get('first_name', '')} {ans.get('last_name', '')}".strip()
    hay = f"{label} | {name}".lower()
    for pat, key in RULES:
        if re.search(pat, hay):
            if choice and key not in ("salutation", "country"):
                return None         # yes/no and multiple-choice questions are never answered by keyword rules,
            val = ans.get(key) or None      # except plain personal facts (salutation, country)
            if key == "birth_date" and val:
                m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", val.strip())
                if m:
                    val = f"{m.group(3)}.{m.group(2)}.{m.group(1)}"      # Austrian forms: TT.MM.JJJJ
            return "@letter" if key == "letter" else val
    return None


ASK_PROMPT = """You are filling in a job application form for the candidate below. Answer each question.

CANDIDATE CV:
{cv}

CANDIDATE DETAILS: {details}
JOB: {title} at {company}

QUESTIONS (JSON): {qs}

RULES:
- Use ONLY facts stated above. Never invent experience, permits, salary numbers, dates or anything else.
- If a question has "options", answer with one option copied exactly.
- Demographic / diversity questions (gender, ethnicity, disability, veteran…): choose the "prefer not to say" /
  "decline" option if there is one.
- "How did you hear about us": answer "Job board", or the closest option such as "Company website / careers
  page" or "Other". Never name a specific site (LinkedIn, Indeed…) unless it is the only fitting option.
- Questions about the candidate's preferences or willingness (days in the office, relocation, travel, shifts,
  contract type, start date not given above): answer "UNKNOWN" unless the details above state it.
- Consent to the employer's data-privacy notice / processing of this application: {consent}
- If the facts above do not clearly answer a question, answer exactly "UNKNOWN". Do not guess.
Return ONLY JSON: {{"answers": {{"<i>": "<answer>", ...}}}}"""


def _ask_llm(cfg, llm, job, questions: list[dict]) -> dict:
    if not (questions and llm.enabled):
        return {}
    details = {k: v for k, v in cfg.get("apply", {}).get("answers", {}).items() if v}
    details.update(german=cfg["profile"].get("german_level"), english=cfg["profile"].get("english_level"))
    try:
        res = llm.json(ASK_PROMPT.format(
            cv=load_cv_text(cfg)[:6000], details=json.dumps(details, ensure_ascii=False), title=job["title"],
            company=job.get("company", ""), qs=json.dumps(questions, ensure_ascii=False),
            consent="choose the agreeing option (the candidate accepts it)." if cfg.get("apply", {}).get(
                "accept_privacy", True) else 'answer "UNKNOWN".'), best=True)
        return {str(k): str(v).strip() for k, v in (res.get("answers") or {}).items()}
    except HttpError as e:
        if e.status in (429, 503):
            raise                    # out of quota: the job stays queued instead of being marked "needs you"
        return {}
    except Exception:
        return {}


# ------------------------------------------------------------------ browser steps
def _dismiss_cookies(page):
    for pat in (r"^(reject|decline|deny|ablehnen|alle ablehnen|optionale cookies ablehnen|nur notwendige|"
                r"only (necessary|essential)|necessary only|nichts akzeptieren|nur (erforderliche|essenzielle|notwendige)|"
                r"notwendige cookies|continue without accepting|ohne (zustimmung|einwilligung)|refuse|"
                r"alle optionalen ablehnen|use necessary cookies only)", ):
        try:
            b = page.get_by_role("button", name=re.compile(pat, re.I)).or_(
                page.get_by_role("link", name=re.compile(pat, re.I))).first
            if b.is_visible(timeout=800):
                b.click(timeout=1500)
                return
        except Exception:
            pass


def _button(page, pat):
    """First visible button (or button-like link) whose text matches, in any frame."""
    for fr in page.frames:
        for el in fr.locator("button:visible, input[type=submit]:visible, [role=button]:visible, a.button:visible, a.btn:visible").all()[:120]:
            try:
                name = " ".join((el.inner_text(timeout=400) or el.get_attribute("value") or "").split())
            except Exception:
                continue
            if name and len(name) <= 45 and pat.search(name):
                return el
    return None


def _form_frame(page, relaxed: bool = False):
    """The frame (page or iframe) that holds the application form, with its fields; None if there is no form.
    relaxed: later wizard steps and sites you are signed in to don't ask for your e-mail again."""
    best = None
    for fr in page.frames:
        try:
            fields = fr.evaluate(FIELDS_JS)
        except Exception:
            continue
        has_mail = any(re.search(r"e-?mail", f["label"] + f["name"], re.I) or f["type"] == "email" for f in fields)
        ok = (has_mail and len(fields) >= 3) or (relaxed and fields and (
            any(f["type"] == "file" for f in fields) or len(fields) >= 2))
        if ok and (best is None or len(fields) > len(best[1])):
            best = (fr, fields)
    return best


def _open_form(ctx, page, url: str, skip_hosts: list[str], mine: list[str] = ()):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    clicked: set[str] = set()
    for _ in range(5):
        page.wait_for_timeout(2500)
        host = urllib.parse.urlparse(page.url).netloc.lower()
        if any(h in host for h in skip_hosts):
            raise NeedsYou(f"{host} needs an account — sign in once with:  python -m jobpilot login {host.removeprefix('www.')}")
        _dismiss_cookies(page)
        on_board = any(h in host for h in BOARD_HOSTS) and not any(m in host for m in mine)     # a job board's own sign-up box is not the application
        try:
            if BLOCKED.search(page.inner_text("body", timeout=3000)[:1500]) or \
                    page.locator("iframe[src*='captcha-delivery.com']").count():
                raise NeedsYou(f"{host} blocks automated browsers — open the posting and apply by hand (documents are ready)")
        except NeedsYou:
            raise
        except Exception:
            pass
        signed_in = any(m in host for m in mine)
        found = None if on_board else _form_frame(page, relaxed=signed_in and bool(clicked))
        if found:
            return page, found
        # no form yet: follow the "Apply" button or link (may open a new tab); never click the same one twice
        target = None
        for el in page.locator("a:visible, button:visible, [role=button]:visible").all()[:400]:
            try:
                name = " ".join((el.inner_text(timeout=500) or "").split())
                key = name + "|" + (el.get_attribute("href") or "")
            except Exception:
                continue
            if APPLY_START.search(name) and len(name) <= 45 and key not in clicked:
                clicked.add(key)
                target = el
                break
        if not target:
            break
        before = len(ctx.pages)
        try:
            target.click(timeout=5000)
        except Exception:
            break
        page.wait_for_timeout(2500)
        if len(ctx.pages) > before:
            page = ctx.pages[-1]
            page.wait_for_load_state("domcontentloaded", timeout=30000)
    if any(h in urllib.parse.urlparse(page.url).netloc.lower() for h in BOARD_HOSTS):
        raise NeedsYou("this job board only lets you apply with an account on it — open the posting and apply there")
    host = urllib.parse.urlparse(page.url).netloc.lower()
    try:
        head = (page.title() + " " + page.url).lower()
        pw_box = page.locator("input[type=password]:visible").count()
    except Exception:
        head, pw_box = "", 0
    if pw_box or re.search(r"login|log-in|signin|sign-in|anmeld|authenti|register|registrier", head):
        raise NeedsYou(f"{host} wants you to create an account / sign in before applying — sign in once with:  "
                       f"python -m jobpilot login {host.removeprefix('www.')}")
    raise NeedsYou(f"couldn't find an application form on {host} — apply by hand (documents are ready)")


def _pick_option(options: list[str], answer: str) -> str | None:
    a = norm(answer)
    for o in options:
        if norm(o) == a:
            return o
    for o in options:
        if a and (a in norm(o) or norm(o) in a) and norm(o):
            return o
    for alt in ALT_NAMES.get(a, []):                      # the list may be in German (Austria -> Österreich)
        for o in options:
            if norm(alt) == norm(o) or norm(alt) in norm(o):
                return o
    return None


def _fill(cfg, con, llm, job, fr, fields, cv_pdf: Path, letter_pdf: Path, letter: str, log) -> int:
    a = cfg.get("apply", {})
    loc = lambda f: fr.locator(f'[data-jp="{f["i"]}"]')
    filled, unknown, groups, files = 0, [], {}, [f for f in fields if f["type"] == "file"]

    # ---- uploads
    for f in files:      # upload widgets (Greenhouse) label a wrapping group, not the hidden <input>
        try:
            g = loc(f).evaluate("""e => { const w = e.closest('[role=group][aria-labelledby], .file-upload, fieldset');
                if (!w) return null; const id = w.getAttribute('aria-labelledby');
                const l = (id && document.getElementById(id)) || w.querySelector('.label, legend, label');
                return {label: l ? l.innerText.trim() : '', req: w.getAttribute('aria-required') === 'true' || !!w.querySelector('.required')}; }""")
        except Exception:
            g = None
        if g and g["label"] and len(g["label"]) > len(f["label"]):
            f["label"], f["required"] = g["label"], f["required"] or g["req"]
    hay = lambda f: f"{f['label'][:120]} {f['name']}".lower()
    cv_field = next((f for f in files if re.search(r"resume|\bcv\b|lebenslauf|curriculum", hay(f))), None)
    letter_field = None
    if cv_field is None and files and not any(f["label"].strip() for f in files):
        # unlabeled drop zone (JOIN): the page heading says what it wants
        try:
            head = (fr.evaluate("() => [...document.querySelectorAll('h1,h2')].map(e => e.innerText).join(' ')") or "").lower()
        except Exception:
            head = ""
        if re.search(r"anschreiben|cover letter|motivation", head):
            letter_field = files[0]
        else:
            cv_field = files[0]
        files = files[:1]
    if cv_field is None and len(files) == 1 and letter_field is None:
        cv_field = files[0]
    if files and cv_field is None and letter_field is None:
        raise NeedsYou("couldn't tell which upload field is for the CV")
    for f in files:
        if f is cv_field:
            loc(f).set_input_files(str(cv_pdf))
        elif f is letter_field or re.search(r"cover|anschreiben|motivation|letter", hay(f)):
            loc(f).set_input_files(str(letter_pdf))
        elif re.search(r"transcript|zeugnis|records|grades|notenspiegel|studienerfolg", hay(f)):
            tp = Path(str(a.get("transcript_pdf") or "")).expanduser()
            if a.get("transcript_pdf") and tp.is_file():
                loc(f).set_input_files(str(tp))
            elif f["required"]:
                raise NeedsYou("this form requires your transcript of records — set transcript_pdf under [apply] "
                               "in config.toml to the PDF's path, then retry")
            else:
                continue
        elif f["required"]:
            raise NeedsYou(f"this form requires a document I don't have: “{f['label'][:60]}” — apply by hand "
                           f"(python -m jobpilot finish)")
        else:
            continue
        filled += 1
        fr.page.wait_for_timeout(1500)

    for f in fields:
        t = f["type"]
        if t == "file":
            continue
        if t in ("radio", "checkbox"):
            groups.setdefault((t, f["name"] or f["label"]), []).append(f)
            continue
        if f["value"]:
            continue                                   # pre-filled (e.g. from your account on the site)
        if f["combo"] and (f["required"] or con.execute(
                "SELECT 1 FROM answers WHERE key=? AND answer != ''", (norm(f["label"]),)).fetchone()):
            opts = _combo_options(fr, loc(f))
            if 0 < len(opts) <= 30:                    # a short list = a multiple-choice question, not free text
                f["options"] = opts
                ans = _known(cfg, con, f["label"], f["name"], True)
                pick = _pick_option(opts, ans) if ans else None
                if pick:
                    _set(fr, f, loc(f), pick)
                    filled += 1
                elif f["required"]:
                    unknown.append(f)
                continue
        ans = _known(cfg, con, f["label"], f["name"])
        if ans == "@letter":
            ans = letter if t == "textarea" else None
        if ans is None:
            if f["required"]:
                unknown.append(f)
            continue
        _set(fr, f, loc(f), ans)
        filled += 1

    # ---- radio groups / checkboxes
    for (t, _), opts in groups.items():
        label = opts[0]["label"] or opts[0]["option"]
        if any(o["value"] for o in opts):
            continue
        if t == "checkbox" and len(opts) == 1:
            text = f"{label} {opts[0]['option']}".lower()
            if opts[0]["yn"]:
                ans = _known(cfg, con, label, "", True)
                if ans and norm(ans).split()[:1] in (["yes"], ["no"], ["ja"], ["nein"]):
                    _check(fr, opts[0], "yes" if norm(ans).split()[0] in ("yes", "ja") else "no")
                    filled += 1
                elif opts[0]["required"]:
                    unknown.append({**opts[0], "options": ["Yes", "No"], "_group": opts})
            elif re.search(r"privacy|datenschutz|data protection|einwillig|terms|agree|zustimm|gdpr|dsgvo|"
                           r"consent to (the )?(processing|storage)", text):
                if not a.get("accept_privacy", True):
                    raise NeedsYou("the form needs you to accept its privacy terms")
                _check(fr, opts[0])
                filled += 1
            elif opts[0]["required"]:
                unknown.append({**opts[0], "label": text.strip(), "options": ["yes", "no"], "_group": opts})
            continue
        ans = _known(cfg, con, label, opts[0]["name"], True)
        names = [o["option"] for o in opts]
        pick = _pick_option(names, ans) if ans and ans != "@letter" else None
        if pick:
            _check(fr, opts[names.index(pick)])
            filled += 1
        elif any(o["required"] for o in opts):
            unknown.append({**opts[0], "label": label, "options": names, "_group": opts})

    # ---- everything the rules couldn't answer: ask the model, never guess
    if unknown:
        # Personal facts only the candidate knows are never sent to the model: it would have to guess.
        qs = [{"i": str(f["i"]), "question": f["label"] or f["name"], **({"options": f["options"]} if f["options"] else {})}
              for f in unknown if not PERSONAL.search(f"{f['label']} {f['name']}")]
        got = _ask_llm(cfg, llm, job, qs)
        missing = []
        for f in unknown:
            ans = got.get(str(f["i"]), "UNKNOWN")
            opts = f.get("options") or []
            if opts and ans.upper() != "UNKNOWN":
                ans = _pick_option(opts, ans) or "UNKNOWN"
            if not ans or ans.upper() == "UNKNOWN":
                missing.append(f)
                continue
            if f.get("_group"):
                if len(f["_group"]) == 1:
                    if f.get("yn"):
                        _check(fr, f["_group"][0], "yes" if norm(ans) in ("yes", "ja") else "no")
                    elif norm(ans) in ("yes", "ja"):
                        _check(fr, f["_group"][0])
                else:
                    _check(fr, f["_group"][[o["option"] for o in f["_group"]].index(ans)])
            else:
                _set(fr, f, loc(f), ans)
            filled += 1
        if missing:
            for f in missing:
                q = f["label"] or f["name"]
                con.execute("INSERT OR IGNORE INTO answers (key, question, options, job_id) VALUES (?,?,?,?)",
                            (norm(q), q, json.dumps(f.get("options") or [], ensure_ascii=False), job["id"]))
            con.commit()
            raise NeedsYou("needs your answer: " + " · ".join((f["label"] or f["name"])[:80] for f in missing[:4])
                           + " — answer under “Questions”, then retry")
    return filled


def _combo_options(fr, el) -> list[str]:
    """Open a drop-down, read its options, close it again."""
    try:
        el.click(timeout=3000)
        fr.page.wait_for_timeout(500)
        opts = [" ".join(t.split()) for t in fr.locator("[role=option]:visible").all_inner_texts()[:60]]
        el.press("Escape")
        return [o for o in opts if o]
    except Exception:
        return []


def _check(fr, f, yn: str = ""):
    if yn:
        fr.locator(f'[data-jp-yn="{f["i"]}:{yn}"]').click(timeout=4000)
        return
    el = fr.locator(f'[data-jp="{f["i"]}"]')
    try:
        el.check(timeout=2500)
    except Exception:
        el.evaluate("e => (e.closest('label') || document.querySelector('label[for=\"' + e.id + '\"]') || e).click()")


def _set(fr, f, el, ans: str):
    if f["type"] == "select":
        pick = _pick_option(f["options"], ans)
        if not pick:
            raise NeedsYou(f"no option matching “{ans[:40]}” for “{f['label'][:60]}”")
        try:
            el.select_option(label=pick, timeout=3000)
        except Exception:                                  # hidden behind a widget: set it directly
            el.evaluate("""(e, label) => { const o = [...e.options].find(o => o.text.trim() === label.trim());
                if (o) { e.value = o.value; e.dispatchEvent(new Event('input', {bubbles: true}));
                         e.dispatchEvent(new Event('change', {bubbles: true})); } }""", pick)
    elif f["combo"] and f.get("options"):
        el.click()
        fr.page.wait_for_timeout(400)
        fr.locator("[role=option]:visible").filter(has_text=re.compile(r"^\s*" + re.escape(ans) + r"\s*$")).first.click(timeout=4000)
    elif f["combo"]:
        el.click()
        for cand in [ans] + ALT_NAMES.get(norm(ans), []):     # the list may be in German
            el.fill(cand[:60])
            fr.page.wait_for_timeout(1300)
            opt = fr.locator("[role=option]:visible").first
            if opt.count():
                opt.click(timeout=2500)
                return
        el.fill("")
        el.press("Escape")
        if f["required"]:
            raise NeedsYou(f"no option matching “{ans[:40]}” for “{f['label'][:60]}”")
    elif f["type"] == "date":
        m = re.match(r"(\d{1,2})[./](\d{1,2})[./](\d{4})$", ans.strip())
        el.fill(f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else ans)
    elif f["type"] == "number":
        m = re.search(r"\d[\d.,' ]*", ans)
        digits = re.sub(r"\D", "", m.group(0)) if m else ""
        if not digits:
            raise NeedsYou(f"“{f['label'][:60]}” needs a number, but your saved answer is “{ans[:40]}”")
        el.fill(digits)
    elif f["type"] == "tel" and f.get("cc") and ans.replace(" ", "").startswith(f["cc"]):
        el.fill(ans.replace(" ", "")[len(f["cc"]):])           # the country code has its own box
    elif f["type"] == "tel" and ans.strip().startswith("+"):
        el.fill(re.sub(r"[^\d+]", "", ans))                    # strict validators reject spaces: +43665…
    else:
        el.fill(ans)


def _verify_by_mail(cfg, llm, page, fr, since: float, log) -> bool:
    """If the site asks for an e-mailed code / confirmation link, fetch it from your mailbox and continue."""
    body = ""
    for f in page.frames:
        try:
            body += f.inner_text("body", timeout=2000)[:5000]
        except Exception:
            pass
    if not re.search(r"verif|bestätig|confirm your e-?mail|check your (e-?mail|inbox)|enter the code|sent (you )?(a|an) (code|e-?mail)|"
                     r"code (gesendet|geschickt)|one-time", body, re.I) or not mailbox.enabled(cfg):
        return False
    host = urllib.parse.urlparse(page.url).netloc.lower()
    log(f"    waiting for the verification e-mail from {host}…")
    got = mailbox.wait_for_verification(cfg, since, host.removeprefix("www."), llm=llm)
    if not got:
        raise NeedsYou("the site sent a verification e-mail but JobPilot couldn't find it — finish it yourself")
    if got.get("link"):
        page.goto(got["link"], wait_until="domcontentloaded", timeout=45000)
        return True
    box = None
    for f in page.frames:
        cand = f.locator("input[autocomplete='one-time-code']:visible, input[name*=code i]:visible, "
                         "input[inputmode=numeric]:visible, input[maxlength='1']:visible")
        if cand.count():
            box = cand
            break
    if box is None:
        raise NeedsYou(f"got verification code {got['code']} but found no field for it — finish it yourself")
    if box.count() > 1:                       # one box per digit
        for i, ch in enumerate(got["code"][: box.count()]):
            box.nth(i).fill(ch)
    else:
        box.first.fill(got["code"])
        box.first.press("Enter")
    return True


FORM_ERROR = re.compile(r"enthält fehler|contains? errors?|pflichtfeld|required field|field is required|"
                        r"bitte (füllen|korrigieren|überprüfen|wählen)|please (fill|correct|check|select|complete)|"
                        r"ungültig|invalid|fehlerhaft|muss ausgefüllt|darf nicht leer", re.I)


def _texts(page) -> list[str]:
    out = []
    for f in page.frames:
        try:
            out.append(f.inner_text("body", timeout=2000)[:8000])
        except Exception:
            pass
    return out


def _confirmed(page, before: set[str], fr) -> bool:
    """A real confirmation: success wording that was NOT on the page before the click (form intros often say
    "thank you for your interest"), with no error message and no field marked invalid."""
    texts = _texts(page)
    new = {m.group(0).lower() for t in texts for m in SUCCESS.finditer(t)} - before
    if not new:
        return False
    if any(FORM_ERROR.search(t) for t in texts):
        return False
    try:
        if sum(f.locator("[aria-invalid=true]:visible, .has-error:visible, .is-invalid:visible").count() for f in page.frames):
            return False
    except Exception:
        pass
    return True


def _submit(cfg, llm, page, fr, log) -> str:
    for sel in CAPTCHA:
        if any(page.locator(sel).nth(i).is_visible() for i in range(page.locator(sel).count())):
            raise NeedsYou("the form shows a CAPTCHA — submit it yourself ")
    btn = _button(page, SUBMIT_BTN)
    if btn is None:
        cand = fr.locator("form button[type=submit]:visible, form input[type=submit]:visible")
        btn = cand.last if cand.count() else None
    if btn is None:
        raise NeedsYou("couldn't find the submit button")
    before = {m.group(0).lower() for t in _texts(page) for m in SUCCESS.finditer(t)}
    had_error = any(FORM_ERROR.search(t) for t in _texts(page))
    t0 = time.time()
    btn.click(timeout=8000)
    verified = False
    for _ in range(12):
        page.wait_for_timeout(1500)
        if _confirmed(page, before, fr):
            return "confirmed"
        if not verified and _ >= 1:
            verified = _verify_by_mail(cfg, llm, page, fr, t0, log)
    for sel in CAPTCHA:
        if page.locator(sel).count() and page.locator(sel).first.is_visible():
            raise NeedsYou("a CAPTCHA appeared on submit — finish it yourself")
    bad = sum(f.locator("[aria-invalid=true]:visible, .has-error:visible, .is-invalid:visible").count() for f in page.frames)
    errs = [m.group(0) for t in _texts(page) for m in FORM_ERROR.finditer(t)]
    if bad or (errs and not had_error):
        raise NeedsYou(f"the form rejected the submission ({bad or len(errs)} field(s) missing or invalid — see form_problem.png)")
    raise NeedsYou("submitted, but no confirmation appeared — check the screenshot / your e-mail")


def _wait_user(page, log) -> None:
    """Visible-window mode: everything is filled in; the user checks it and clicks Submit (and solves any CAPTCHA)."""
    def confirmed():
        for f in page.frames:
            try:
                if SUCCESS.search(f.inner_text("body", timeout=2000)[:6000]):
                    return True
            except Exception:
                pass
        return False
    url, base = page.url, confirmed()
    wait = int(os.environ.get("JOBPILOT_HAND_WAIT", "900"))
    log(f"      form is filled — check it in the browser window and click Submit (waiting up to {wait // 60} min)")
    t0 = time.time()
    while time.time() - t0 < wait:
        if page.is_closed():
            raise NeedsYou("window closed before a confirmation appeared — not counted as applied")
        try:
            page.wait_for_timeout(1500)
            if confirmed() and (not base or page.url != url):
                return
        except Exception:
            raise NeedsYou("window closed before a confirmation appeared — not counted as applied")
    raise NeedsYou("filled in the visible window, but nobody submitted it in time")


# ------------------------------------------------------------------ e-mail applications
def apply_email_address(desc: str) -> str | None:
    for m in EMAIL.finditer(desc or ""):
        ctx = desc[max(0, m.start() - 200): m.end() + 80].lower()
        if re.search(r"bewerb|apply|application|send (us )?your|cv|lebenslauf|unterlagen|resume", ctx) \
                and not re.search(r"datenschutz|privacy|dpo@|noreply|no-reply", m.group(0).lower()):
            return m.group(0).rstrip(".")
    return None


def _mail_pw(e: dict) -> str:
    return os.environ.get("JOBPILOT_EMAIL_PASSWORD") or e.get("app_password", "")


def _send_email(cfg, job, to: str, letter: str, attachments: list[Path], folder: Path, dry: bool) -> str:
    e = cfg.get("apply", {}).get("email", {})
    lang = detect_lang(job.get("description", ""))
    msg = EmailMessage()
    msg["From"], msg["To"] = f"{cfg['profile']['name']} <{e.get('user', '')}>", to
    msg["Subject"] = ("Bewerbung: " if lang == "de" else "Application: ") + job["title"]
    msg.set_content(letter)
    for p in attachments:
        msg.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename=p.name)
    (folder / "application_email.eml").write_bytes(bytes(msg))
    if dry:
        return f"dry run: e-mail to {to} written to application_email.eml, not sent"
    with smtplib.SMTP(e.get("smtp_host", "smtp.gmail.com"), int(e.get("smtp_port", 587)), timeout=30) as s:
        s.starttls()
        s.login(e["user"], _mail_pw(e))
        s.send_message(msg)
    return f"e-mailed to {to}"


# ------------------------------------------------------------------ main loop
def _target_url(job: dict) -> str:
    u = job["url"]
    if job["source"] == "dvinci":
        return re.sub(r"(/jobs/\d+)(/.*)?$", r"\1/form", u)     # skips the "form or CV upload?" choice page
    if job["source"] == "karriere":
        # Most karriere.at postings link out to the employer's own application page; go straight there.
        try:
            from .core import http
            m = re.search(r'<a[^>]*href="([^"]+)"[^>]*m-applyButton__button--external', http(u, headers={"Accept": "text/html"}))
            if m:
                return html.unescape(m.group(1))
        except Exception:
            pass
    if job["source"] == "greenhouse" and "greenhouse.io" not in u:
        # The company's own careers page only embeds this Greenhouse form (and is often behind a bot wall).
        slug, _, jid = job["id"].split(":", 1)[1].rpartition("-")
        return f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={jid}"
    return u.rstrip("/") + "/apply" if job["source"] == "arbeitnow" and "arbeitnow." in u else u


def profile_dir(cfg: dict) -> Path:
    return resolve(cfg, cfg.get("apply", {}).get("profile_dir", "browser_profile"))


def open_profile(cfg: dict, pw, headless: bool):
    """The saved browser profile, with the sign-in cookies captured by `login` restored."""
    ctx = pw.chromium.launch_persistent_context(str(profile_dir(cfg)), headless=headless, locale="en-GB",
                                                viewport={"width": 1280, "height": 1600})
    sf = profile_dir(cfg) / "jobpilot_state.json"
    if sf.exists():
        try:
            ctx.add_cookies(json.loads(sf.read_text())["cookies"])
        except Exception:
            pass
    return ctx


def logged_in_hosts(cfg: dict) -> list[str]:
    f = profile_dir(cfg) / "jobpilot_logins.json"
    return json.loads(f.read_text()) if f.exists() else []


def login(cfg: dict, url: str, log=print) -> None:
    """Open a visible browser on `url`; you sign in (password, 2FA, CAPTCHA) and close the window.
    The session is saved in the browser profile and reused for every later application. No password is stored."""
    from playwright.sync_api import sync_playwright
    if "://" not in url:
        url = "https://" + url
    host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    url = LOGIN_PAGES.get(host, url)        # candidate sign-in page, not the employer one
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile_dir(cfg)), headless=False,
                                                    viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url)
        log(f"Sign in to {host} in the browser window, then close the window.")
        # Sign-in cookies are often "session" cookies that the browser forgets when it closes, so keep
        # snapshotting them while the window is open and restore them on every later run.
        state = None
        while True:
            try:
                state = ctx.storage_state()
                time.sleep(1.5)
            except Exception:
                break
    sf = profile_dir(cfg) / "jobpilot_state.json"
    old = json.loads(sf.read_text()) if sf.exists() else {"cookies": []}
    new = (state or {}).get("cookies", [])
    keys = {(c["name"], c["domain"], c["path"]) for c in new}
    cookies = new + [c for c in old["cookies"] if (c["name"], c["domain"], c["path"]) not in keys]
    sf.write_text(json.dumps({"cookies": cookies}))
    hosts = set(logged_in_hosts(cfg)) | {host}
    ok = None
    if host in LOGIN_CHECK:                   # make sure the sign-in really stuck
        url, marker = LOGIN_CHECK[host]
        with sync_playwright() as pw:
            ctx = open_profile(cfg, pw, headless=True)
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(5000)
                ok = marker not in page.url
            except Exception:
                pass
            ctx.close()
    if ok is False:
        hosts.discard(host)
        log(f"✗ Not signed in to {host}: the site still shows its login page. Run the command again and close "
            f"the window only after you see your account page.")
    else:
        log(f"Saved{' and verified' if ok else ''}. JobPilot will now apply on: {', '.join(sorted(hosts))}")
    (profile_dir(cfg) / "jobpilot_logins.json").write_text(json.dumps(sorted(hosts)))


def apply_one(cfg, con, llm, job: dict, ctx, pdf_browser, log=print, hand: bool = False) -> tuple[str, str]:
    """Returns (new status, note). hand=True: fill in a visible window and let the user click Submit."""
    a = cfg.get("apply", {})
    dry = bool(a.get("dry_run", True)) and not hand
    job = pipeline.ensure_description(cfg, con, job, log=log)
    folder = app_folder(cfg, job)
    con.execute("UPDATE jobs SET app_dir=? WHERE id=?", (str(folder), job["id"]))
    con.commit()
    job["app_dir"] = str(folder)

    def documents():
        """Written only once the form (or an e-mail address) is reachable, so no AI quota is spent on dead ends."""
        if not (folder / "cover_letter.txt").exists():
            tailor(cfg, job, llm, log=log)
        return make_documents(cfg, job, folder, pdf_browser)

    mail = apply_email_address(job.get("description", ""))
    em = a.get("email", {})
    page = ctx.new_page()
    mine = logged_in_hosts(cfg)
    skip = [h.lower() for h in a.get("skip_hosts", []) if not any(h.lower() in m or m in h.lower() for m in mine)]
    try:
        try:
            page, (fr, fields) = _open_form(ctx, page, _target_url(job), skip, mine)
        except NeedsYou:
            if mail and em.get("enabled") and _mail_pw(em):
                cv_pdf, letter_pdf, letter = documents()
                return ("manual" if dry else "applied"), _send_email(cfg, job, mail, letter, [cv_pdf, letter_pdf], folder, dry)
            if mail:
                raise NeedsYou(f"this job wants an e-mail to {mail} — enable [apply.email] in config.toml or send it yourself")
            raise
        cv_pdf, letter_pdf, letter = documents()
        n, steps = 0, 0
        while True:                               # one pass per page of a multi-step application
            n += _fill(cfg, con, llm, job, fr, fields, cv_pdf, letter_pdf, letter, log)
            page.wait_for_timeout(800)
            steps += 1
            page.screenshot(path=str(folder / ("form_filled.png" if steps == 1 else f"form_filled_{steps}.png")), full_page=True)
            nxt = None if _button(page, SUBMIT_BTN) else _button(page, NEXT_BTN)
            if nxt is None or steps >= 8:
                break
            try:
                nxt.click(timeout=8000)
            except Exception:
                page.screenshot(path=str(folder / "form_problem.png"), full_page=True)
                raise NeedsYou(f"step {steps} of the form won't continue — a required field is missing or invalid "
                               f"(see form_problem.png)")
            page.wait_for_timeout(3000)
            bad = sum(f.locator("[aria-invalid=true]:visible").count() for f in page.frames)
            if bad:
                raise NeedsYou(f"step {steps} of the form was rejected ({bad} field(s) marked invalid)")
            found = _form_frame(page, relaxed=True)
            fr, fields = found if found else (fr, [])
        if dry:
            return "manual", (f"dry run: filled {n} fields" + (f" over {steps} steps" if steps > 1 else "") +
                              ", not submitted (see form_filled.png; set dry_run = false to apply)")
        if hand:
            _wait_user(page, log)
            page.screenshot(path=str(folder / "form_submitted.png"), full_page=True)
            return "applied", f"filled automatically ({n} fields), submitted by you"
        job["_host"] = urllib.parse.urlparse(page.url).netloc.lower()
        _submit(cfg, llm, page, fr, log)
        page.screenshot(path=str(folder / "form_submitted.png"), full_page=True)
        return "applied", f"applied automatically ({n} fields)"
    except NeedsYou:
        try:
            job["_host"] = urllib.parse.urlparse(page.url).netloc.lower()
            page.screenshot(path=str(folder / "form_problem.png"), full_page=True)
            (folder / "form_problem.html").write_text(page.content(), encoding="utf-8")   # for debugging the form
        except Exception:
            pass
        raise
    finally:
        for pg in ctx.pages:
            try:
                pg.close()
            except Exception:
                pass


WALL_NOTE = re.compile(r"needs an account|create an account|sign in before|blocks automated|CAPTCHA|"
                       r"only lets you apply with an account", re.I)
SOFT_NOTE = re.compile(r"no confirmation appeared|couldn't find an application form", re.I)


def _host_stats(con) -> dict:
    con.execute("CREATE TABLE IF NOT EXISTS hosts (host TEXT PRIMARY KEY, ok INTEGER DEFAULT 0, wall INTEGER DEFAULT 0, "
                "soft INTEGER DEFAULT 0, reason TEXT, last TEXT)")
    return {r["host"]: dict(r) for r in con.execute("SELECT * FROM hosts")}


def _host_record(con, host: str, status: str, note: str) -> None:
    if not host:
        return
    col = "ok" if status == "applied" else "wall" if WALL_NOTE.search(note) else "soft" if SOFT_NOTE.search(note) else None
    if col is None:
        return
    con.execute("INSERT OR IGNORE INTO hosts (host) VALUES (?)", (host,))
    con.execute(f"UPDATE hosts SET {col} = {col} + 1, reason = ?, last = ? WHERE host = ?",
                (None if col == "ok" else note[:200], now_iso(), host))
    con.commit()


def _known_wall(stats: dict, host: str) -> str | None:
    """A site that has never worked and has stopped us before (account wall, bot check): don't spend a slot on it."""
    h = stats.get(host)
    if h and not h["ok"] and (h["wall"] >= 1 or h["soft"] >= 2):
        return h["reason"] or "this site stopped earlier attempts"
    return None


def apply_queued(cfg: dict, con, log=print, ids: list[str] | None = None, hand: bool = False) -> dict:
    a = cfg.get("apply", {})
    q = "SELECT id FROM jobs WHERE status='queued'"
    if hand:        # `finish`: jobs that were filled but couldn't be submitted by the script
        q = ("SELECT id FROM jobs WHERE (status='queued' OR (status='manual' AND (apply_note LIKE '%CAPTCHA%' "
             "OR apply_note LIKE '%no confirmation%' OR apply_note LIKE '%submit%')))")
    rows = [r["id"] for r in con.execute(q + " ORDER BY COALESCE(ai_score, kw_score) DESC").fetchall()]
    if ids:
        rows = [i for i in rows if i in ids]
    stats = {"applied": 0, "manual": 0}
    hosts = _host_stats(con)
    if not ids and not hand:
        # Fill the run's slots with jobs that can actually go through: a site already known to need an account or
        # to block automation goes straight to "Needs you" without costing a slot (or any AI quota).
        keep, limit = [], int(a.get("max_per_run", 15))
        for jid in rows:
            if len(keep) >= limit:
                break
            job = pipeline.job_row(con, jid)
            try:
                host = urllib.parse.urlparse(_target_url(job)).netloc.lower()
            except Exception:
                host = ""
            why = _known_wall(hosts, host)
            if why:
                con.execute("UPDATE jobs SET status='manual', apply_note=? WHERE id=?",
                            (f"{host}: {why} (known from earlier attempts — not retried)", jid))
                con.commit()
                stats["manual"] += 1
                continue
            keep.append(jid)
        rows = keep
    else:
        rows = rows[: int(a.get("max_per_run", 15))]
    if not rows:
        return stats
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("  ⚠ auto-apply needs Playwright:  pip install playwright && playwright install chromium")
        return stats
    llm = LLM(cfg)
    log(f"Applying to {len(rows)} job(s)" + (" — you click Submit in the window" if hand else
        " — DRY RUN, nothing is submitted" if a.get("dry_run", True) else "") + "…")
    with sync_playwright() as pw:
        pdf_browser = pw.chromium.launch(headless=True)
        browser = open_profile(cfg, pw, headless=False if hand else bool(a.get("headless", True)))
        for i, jid in enumerate(rows, 1):
            job = pipeline.job_row(con, jid)
            try:
                status, note = apply_one(cfg, con, llm, job, browser, pdf_browser, log, hand)
            except NeedsYou as e:
                status, note = "manual", str(e)
            except HttpError as e:
                if e.status in (429, 503) and e.url == "llm":
                    log(f"  ⚠ AI quota used up for today — {len(rows) - i + 1} job(s) stay queued for the next run")
                    break
                status, note = "manual", f"auto-apply failed: {e}"
            except (ConnectionError, OSError) as e:
                if "11434" in str(e) or "refused" in str(e).lower():
                    log(f"  ⚠ AI model not reachable ({str(e)[:80]}) — {len(rows) - i + 1} job(s) stay queued")
                    break
                status, note = "manual", f"auto-apply failed: {str(e)[:200]}"
            except Exception as e:
                status, note = "manual", f"auto-apply failed: {str(e).splitlines()[0][:200]}"
            con.execute("UPDATE jobs SET status=?, apply_note=?, applied_at=? WHERE id=?",
                        (status, note, now_iso() if status == "applied" else None, jid))
            con.commit()
            _host_record(con, job.get("_host") or "", status, note)
            stats[status] += 1
            log(f"  [{i}/{len(rows)}] {'✓' if status == 'applied' else '→ needs you'}  {job['title'][:50]} — "
                f"{(job.get('company') or '')[:25]}: {note}")
        browser.close()
        pdf_browser.close()
    log(f"  {stats['applied']} applied, {stats['manual']} need you")
    return stats
