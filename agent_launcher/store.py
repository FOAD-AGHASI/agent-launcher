"""Where things live, and where values are kept.

~/.agent-launcher/ (override with AGENT_LAUNCHER_HOME)
    config.json              non-secret values (paths, options) and metadata, never a secret
    agents/<owner__repo>/    repo/  venv/  plan.json
    logs/                    output of background runs
Secrets go to the OS credential store (Windows Credential Manager, macOS Keychain) through `keyring` when it is
installed. Without it they are never written anywhere: they are asked for again, or read from your environment."""
from __future__ import annotations

import json
import os
import re


def home() -> str:
    return os.environ.get("AGENT_LAUNCHER_HOME") or os.path.join(os.path.expanduser("~"), ".agent-launcher")


def slugify(owner: str, repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{owner}__{repo}")


def agent_dir(slug: str) -> str:
    return os.path.join(home(), "agents", slug)


def load_config() -> dict:
    p = os.path.join(home(), "config.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"agents": {}}


def save_config(cfg: dict) -> None:
    os.makedirs(home(), exist_ok=True)
    with open(os.path.join(home(), "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


class Secrets:
    """Secret values. `persistent` tells whether they survive this process."""

    def __init__(self):
        try:
            import keyring  # noqa: F401
            self._kr = keyring
        except Exception:
            self._kr = None
        self._mem: dict[tuple[str, str], str] = {}

    @property
    def persistent(self) -> bool:
        return self._kr is not None

    def get(self, slug: str, name: str) -> str | None:
        if (slug, name) in self._mem:
            return self._mem[(slug, name)]
        if self._kr:
            try:
                return self._kr.get_password(f"agent-launcher:{slug}", name)
            except Exception:
                return None
        return None

    def set(self, slug: str, name: str, value: str) -> None:
        self._mem[(slug, name)] = value
        if self._kr:
            try:
                self._kr.set_password(f"agent-launcher:{slug}", name, value)
            except Exception:
                pass

    def delete(self, slug: str, name: str) -> None:
        self._mem.pop((slug, name), None)
        if self._kr:
            try:
                self._kr.delete_password(f"agent-launcher:{slug}", name)
            except Exception:
                pass
