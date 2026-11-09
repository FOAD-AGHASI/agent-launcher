"""Download a repository. Only github.com over https is accepted (tests may pass a local path)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

def normalize_url(text: str) -> str:
    """Accept https://github.com/o/r, github.com/o/r, www.github.com/o/r and the short form o/r."""
    t = text.strip().strip("<>\"'").rstrip("/")
    t = re.sub(r"^(https?://)?(www\.)?github\.com/", "https://github.com/", t, flags=re.I)
    t = re.sub(r"/(tree|blob)/.*$", "", t)                 # a link to a branch or file of the repo
    if re.match(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$", t):
        t = "https://github.com/" + t
    return t


def parse_github(url: str) -> tuple[str, str]:
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    m = re.match(r"^https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})$", u)
    if not m:
        raise ValueError(f"not a GitHub repository URL (https://github.com/<owner>/<repo>): {url!r}")
    return m.group(1), m.group(2)


def _git(args: list[str], cwd: str | None = None) -> str:
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def clone(url: str, dest: str, local_ok: bool = False) -> str:
    """Shallow clone into `dest` (replacing it). Returns the commit hash. Never runs anything from the repo."""
    if local_ok and os.path.isdir(url):
        src = os.path.abspath(url)
    else:
        owner, repo = parse_github(url)
        src = f"https://github.com/{owner}/{repo}.git"
    if os.path.exists(dest):
        shutil.rmtree(dest, onerror=lambda f, p, e: (os.chmod(p, 0o700), f(p)))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    _git(["clone", "--depth", "1", "--no-tags", "-c", "core.hooksPath=/dev/null", src, dest])
    return _git(["rev-parse", "HEAD"], cwd=dest)
