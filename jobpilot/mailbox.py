"""Read verification codes / confirmation links from the mailbox you apply with (IMAP, read-only)."""
from __future__ import annotations

import email
import imaplib
import os
import re
import time
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from .core import secret, strip_html

CODE = re.compile(r"(?<![\w/=.-])(\d{4,8})(?![\w/=-])")
LINK = re.compile(r"https?://[^\s\"'<>)]+", re.I)
VERIFY_WORDS = re.compile(r"verif|confirm|bestätig|activate|aktivier|code|one-time|magic|sign.?in|log.?in|anmeld", re.I)


def password(m: dict) -> str:
    return secret("JOBPILOT_MAILBOX_PASSWORD") or m.get("app_password", "")


def enabled(cfg: dict) -> bool:
    m = cfg.get("apply", {}).get("mailbox", {})
    return bool(m.get("enabled") and m.get("user") and password(m))


def wait_for_verification(cfg: dict, since: float, site: str = "", timeout: int = 120, llm=None) -> dict | None:
    """Poll for a verification mail that arrived after `since` (unix time). Returns {"code": ..} or {"link": ..}."""
    m = cfg["apply"]["mailbox"]
    site_word = site.split(".")[-2] if site.count(".") else site      # "join" from "join.com"
    end = time.time() + timeout
    while time.time() < end:
        try:
            box = imaplib.IMAP4_SSL(m.get("imap_host", "imap.gmail.com"))
            box.login(m["user"], password(m))
            box.select(m.get("folder", "INBOX"), readonly=True)
            typ, data = box.search(None, "SINCE", time.strftime("%d-%b-%Y", time.gmtime(since - 86400)))
            for i in reversed(data[0].split()[-15:] if typ == "OK" else []):
                typ, d = box.fetch(i, "(RFC822)")
                if typ != "OK":
                    continue
                msg = email.message_from_bytes(d[0][1])
                try:
                    if parsedate_to_datetime(msg.get("Date")).timestamp() < since - 30:
                        continue
                except Exception:
                    continue
                subj = str(make_header(decode_header(msg.get("Subject", ""))))
                sender = msg.get("From", "")
                body = ""
                for part in msg.walk():
                    if part.get_content_type() in ("text/plain", "text/html"):
                        raw = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                        body += "\n" + raw
                text = strip_html(body)
                if not VERIFY_WORDS.search(subj + " " + text[:600]):
                    continue
                if site_word and site_word.lower() not in (sender + subj + body).lower():
                    continue
                box.logout()
                code = CODE.search(subj) or CODE.search(text[:1500])
                links = [u.rstrip(".,") for u in LINK.findall(body)
                         if VERIFY_WORDS.search(u) and not re.search(r"unsubscribe|privacy|\.(png|jpg|gif|css)", u, re.I)]
                if code:
                    return {"code": code.group(1)}
                if links:
                    return {"link": links[0]}
                if llm is not None and llm.enabled:
                    ans = llm.chat("Extract the verification code from this e-mail. Reply with the code only, "
                                   f"or NONE.\n\nSubject: {subj}\n\n{text[:3000]}", max_tokens=30).strip()
                    if ans and ans.upper() != "NONE" and len(ans) <= 12:
                        return {"code": ans}
                return None
            box.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
        time.sleep(6)
    return None
