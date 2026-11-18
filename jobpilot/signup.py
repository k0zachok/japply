"""Create a candidate account on an employer's career portal, then let the normal form filler continue.

Off unless you switch it on: [apply.signup] enabled = true AND the JOBPILOT_PORTAL_PASSWORD environment variable
is set (you choose that password; JobMan never invents one). Accounts are registered with the application mailbox
(the application mailbox), so the confirmation e-mail can be read automatically. Every account created is listed in
portal_accounts.json next to the database.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from pathlib import Path

from . import mailbox
from .core import secret, now_iso, resolve

REGISTER = re.compile(r"^\W*(registrieren|jetzt registrieren|konto (erstellen|anlegen)|account (erstellen|anlegen)|"
                      r"create (an? )?account|sign ?up|register( now)?|neu hier|noch kein (konto|account)|"
                      r"neues (konto|profil)|profil (erstellen|anlegen)|als neue[rn]? (bewerber|kandidat)|"
                      r"create (a )?(new )?(applicant |candidate )?profile|bewerberprofil (erstellen|anlegen)|"
                      r"sign in / sign up|anmelden / registrieren)", re.I)
DONE = re.compile(r"(konto|account|profil|registrierung).{0,40}(erstellt|angelegt|created|erfolgreich|successful)|"
                  r"(bestätig|verify|confirm|aktivier).{0,40}(e-?mail|link|konto|account)|check your (e-?mail|inbox)", re.I)


def enabled(cfg: dict) -> bool:
    s = cfg.get("apply", {}).get("signup", {})
    return bool(s.get("enabled") and password() and mailbox.enabled(cfg))


def password() -> str:
    return secret("JOBPILOT_PORTAL_PASSWORD")


def _accounts_file(cfg: dict) -> Path:
    return resolve(cfg, "portal_accounts.json")


def known(cfg: dict) -> dict:
    try:
        return json.loads(_accounts_file(cfg).read_text())
    except Exception:
        return {}


def _remember(cfg: dict, host: str, email: str, state: str) -> None:
    d = known(cfg)
    d[host] = {"email": email, "state": state, "at": now_iso()}
    _accounts_file(cfg).write_text(json.dumps(d, indent=2))


def _click(page, pat) -> bool:
    for fr in page.frames:
        for el in fr.locator("a:visible, button:visible, [role=button]:visible, input[type=submit]:visible").all()[:300]:
            try:
                t = " ".join((el.inner_text(timeout=300) or el.get_attribute("value") or "").split())
            except Exception:
                continue
            if t and len(t) <= 50 and pat.search(t):
                el.click(timeout=6000)
                page.wait_for_timeout(3000)
                return True
    return False


def _fill_registration(cfg: dict, page, email: str, dry: bool) -> int:
    """Fill a sign-up form: e-mail, password (+ repeat), names; tick required consent boxes. Returns fields filled."""
    ans = cfg.get("apply", {}).get("answers", {})
    pw, n = password(), 0
    for fr in page.frames:
        for el in fr.locator("input:visible").all()[:80]:
            try:
                t = (el.get_attribute("type") or "text").lower()
                hay = " ".join(filter(None, [el.get_attribute("name"), el.get_attribute("id"), el.get_attribute("placeholder"),
                                             el.get_attribute("aria-label"), el.get_attribute("autocomplete")])).lower()
                if el.evaluate("e => e.labels && e.labels.length") :
                    hay += " " + el.evaluate("e => [...e.labels].map(l => l.innerText).join(' ')").lower()
                if t == "password":
                    el.fill(pw); n += 1
                elif t == "email" or re.search(r"e-?mail|username|benutzer", hay):
                    el.fill(email); n += 1
                elif re.search(r"first|vorname|given", hay) and ans.get("first_name"):
                    el.fill(ans["first_name"]); n += 1
                elif re.search(r"last|nachname|surname|family", hay) and ans.get("last_name"):
                    el.fill(ans["last_name"]); n += 1
                elif t == "checkbox" and (el.get_attribute("required") is not None or
                                          re.search(r"agb|terms|datenschutz|privacy|nutzungsbeding|einverstanden|accept|zustimm", hay)) \
                        and cfg.get("apply", {}).get("accept_privacy", True) and not re.search(r"newsletter|marketing|werbung", hay):
                    if not el.is_checked():
                        el.check(timeout=3000); n += 1
            except Exception:
                continue
    return n


def try_signup(cfg: dict, page, log=print, dry: bool = False) -> bool:
    """On a portal's login page: register, confirm the e-mail, and leave the browser signed in.
    Returns True when the account is ready (the caller then reloads the job and applies)."""
    if not enabled(cfg):
        return False
    host = urllib.parse.urlparse(page.url).netloc.lower()
    email = cfg["apply"]["mailbox"]["user"]
    if known(cfg).get(host, {}).get("state") == "failed":
        return False
    if not _click(page, REGISTER):
        if not re.search(r"pre-?regist|registrier|register|signup|sign-up", page.url, re.I):
            return False                     # some portals already show the sign-up form (OeNB "PreRegister")
    page.wait_for_timeout(1500)
    # a sign-up form has an e-mail field and either a password or name fields (some portals send the password
    # step by e-mail: "pre-registration")
    has = lambda sel: page.locator(sel).count()
    if not (has("input[type=email]:visible, input[name*=mail i]:visible") and
            (has("input[type=password]:visible") or has("input[name*=name i]:visible, input[id*=name i]:visible"))):
        return False
    n = _fill_registration(cfg, page, email, dry)
    log(f"    creating an account on {host} with {email} ({n} fields)" + (" — dry run, not submitted" if dry else ""))
    if dry or n < 2:
        return False
    since = time.time()
    if not _click(page, re.compile(r"^\W*(registrieren|konto erstellen|account erstellen|create account|sign ?up|"
                                   r"register|weiter|continue|absenden|submit|speichern)", re.I)):
        _remember(cfg, host, email, "failed")
        return False
    page.wait_for_timeout(3000)
    text = page.inner_text("body", timeout=5000)[:4000]
    if page.locator("input[type=password]:visible").count() and not DONE.search(text):
        _remember(cfg, host, email, "failed")             # still on the form: something was rejected
        return False
    got = mailbox.wait_for_verification(cfg, since, host.removeprefix("www."), timeout=150)
    if got and got.get("link"):
        page.goto(got["link"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        if page.locator("input[type=password]:visible").count():       # "choose your password" step
            _fill_registration(cfg, page, email, dry)
            _click(page, re.compile(r"^\W*(speichern|save|weiter|continue|absenden|submit|passwort (setzen|festlegen)|"
                                    r"set password|bestätigen|confirm)", re.I))
    elif got and got.get("code"):
        box = page.locator("input[autocomplete='one-time-code']:visible, input[name*=code i]:visible").first
        if box.count():
            box.fill(got["code"]); box.press("Enter"); page.wait_for_timeout(3000)
    _remember(cfg, host, email, "created")
    try:                                    # keep the session, like `jobpilot login` does
        from .apply import profile_dir
        page.context.storage_state(path=str(profile_dir(cfg) / "jobpilot_state.json"))
        f = profile_dir(cfg) / "jobpilot_logins.json"
        hosts = set(json.loads(f.read_text())) if f.exists() else set()
        hosts.add(host.removeprefix("www."))
        f.write_text(json.dumps(sorted(hosts)))
    except Exception:
        pass
    log(f"    ✓ account created on {host}")
    return True
