"""Tiny LLM client: Ollama (local, free), any OpenAI-compatible API, or Anthropic."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .core import HttpError, get_json


class LLM:
    spent: set[str] = set()      # models whose quota ran out during this run

    def __init__(self, cfg: dict):
        c = cfg.get("llm", {})
        self.provider = c.get("provider", "none")
        self.model = c.get("model", "")
        # strongest first; one entry is fine
        self.models = [m for m in (c.get("models") or [self.model]) if m]
        self.last_model = ""
        # models whose daily quota is used up are remembered on disk until the quota resets, so later runs
        # the same day don't waste time on them
        self._state = Path(cfg.get("_path", ".")).parent / "llm_quota.json"
        try:
            saved = json.loads(self._state.read_text())
        except Exception:
            saved = {}
        self._until = {m: t for m, t in saved.items() if t > time.time()}
        LLM.spent |= set(self._until)
        self.base = (c.get("base_url") or "").rstrip("/")
        # Prefer an environment variable so the key never sits in config.toml.
        self.key = (os.environ.get("JOBPILOT_API_KEY") or os.environ.get(
            {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(self.provider, "-"))
            or c.get("api_key", ""))

    @property
    def enabled(self) -> bool:
        return self.provider not in ("", "none", None)

    def chat(self, prompt: str, *, json_mode: bool = False, max_tokens: int = 1500, best: bool = False) -> str:
        """Ask the model. With several `models` configured, moves on to the next one when a model's quota is
        used up, it is overloaded or it was retired. best=True starts with the strongest model (letters, CVs,
        form answers); otherwise the cheapest goes first so bulk scoring doesn't eat the good models' quota."""
        order = [m for m in (self.models if best else self.models[::-1]) if m not in LLM.spent]
        if not order:
            raise HttpError(429, "llm", "every configured model has used up its quota for today")
        last: Exception | None = None
        for m in order:
            for attempt in (1, 2):
                try:
                    out = self._call(m, prompt, json_mode, max_tokens)
                    if out.strip():
                        self.last_model = m
                        return out
                    last = RuntimeError(f"{m} returned an empty reply")
                    break
                except HttpError as e:
                    last = e
                    wait = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", e.body or "")
                    secs = (int(wait.group(1) or 0) * 3600 + int(wait.group(2) or 0) * 60 + float(wait.group(3))) if wait else None
                    if e.status == 429 and attempt == 1 and secs is not None and secs <= 70 and "per day" not in e.body:
                        time.sleep(secs + 1)          # per-minute limit: wait and try this model once more
                        continue
                    if e.status in (429, 404):        # daily quota gone / model retired: skip it until it resets
                        LLM.spent.add(m)
                        self._until[m] = time.time() + (secs + 60 if secs else 6 * 3600)
                        try:
                            self._state.write_text(json.dumps(self._until))
                        except OSError:
                            pass
                    elif e.status in (500, 502, 503, 504) and len(self.models) > 1:
                        LLM.spent.add(m)              # overloaded: don't wait on it again this run
                    elif e.status not in (500, 502, 503, 504):
                        raise
                    break
                except (TimeoutError, OSError) as e:  # hung or unreachable: next model
                    last = e
                    if len(self.models) > 1:
                        LLM.spent.add(m)
                    break
        out_of_quota = (isinstance(last, HttpError) and last.status == 429) or not [
            m for m in self.models if m not in LLM.spent]
        raise HttpError(429 if out_of_quota else 503, "llm", str(last))

    def _call(self, model: str, prompt: str, json_mode: bool, max_tokens: int) -> str:
        msgs = [{"role": "user", "content": prompt}]
        if self.provider == "ollama":
            body = {"model": model, "messages": msgs, "stream": False,
                    "options": {"temperature": 0.3, "num_ctx": 8192}}
            if json_mode:
                body["format"] = "json"
            d = get_json(f"{self.base or 'http://localhost:11434'}/api/chat", data=body, timeout=300, retries=0)
            return d["message"]["content"]
        if self.provider == "openai":
            body = {"model": model, "messages": msgs, "temperature": 0.3,
                    "max_tokens": max_tokens + 4000}   # headroom: "thinking" models spend tokens before answering
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            d = get_json(f"{self.base or 'https://api.openai.com/v1'}/chat/completions", data=body, timeout=90,
                         retries=0, headers={"Authorization": f"Bearer {self.key}"})
            return d["choices"][0]["message"].get("content") or ""
        if self.provider == "anthropic":
            d = get_json(f"{self.base or 'https://api.anthropic.com'}/v1/messages", timeout=180, data={
                "model": model, "max_tokens": max_tokens, "messages": msgs},
                headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"})
            return "".join(b.get("text", "") for b in d.get("content", []))
        raise RuntimeError("LLM disabled (set [llm] provider in config.toml)")

    def json(self, prompt: str, *, best: bool = False, max_tokens: int = 600) -> dict:
        txt = self.chat(prompt, json_mode=True, max_tokens=max_tokens, best=best)
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            raise ValueError(f"no JSON in reply: {txt[:120]}")
        return json.loads(m.group(0))
