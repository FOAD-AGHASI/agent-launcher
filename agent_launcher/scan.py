"""Static scan of a downloaded repo. Nothing from the repo is imported or executed.

Finds: which environment variables the code reads, how it could be started, what to install."""
from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", "tests", "test", ".pytest_cache"}
# Variables every program may read; never asked for.
SYSTEM_ENV = {"PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "PROGRAMFILES",
              "PROGRAMFILES(X86)", "PYTHONPATH", "PYTHONIOENCODING", "LANG", "LC_ALL", "USER", "USERNAME", "SHELL", "TERM",
              "NODE_ENV", "CI", "COMPUTERNAME", "OS", "PWD", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
# SDKs that read their key from the environment without the code naming it.
SDK_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "@anthropic-ai/sdk": "ANTHROPIC_API_KEY"}
SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|CREDENTIAL|PWD)", re.I)
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


@dataclass
class EnvUse:
    files: set[str] = field(default_factory=set)
    has_default: bool = False
    hard: bool = False        # read as os.environ["X"] / SDK implicit: fails without it
    implicit: bool = False


@dataclass
class Evidence:
    root: str
    env: dict[str, EnvUse] = field(default_factory=dict)
    kind: str = "unknown"                       # python | node | unknown
    py_modules: list[str] = field(default_factory=list)      # packages with __main__.py
    py_scripts: list[str] = field(default_factory=list)      # top-level scripts with a __main__ guard
    npm_scripts: dict[str, str] = field(default_factory=dict)
    has_requirements: bool = False
    has_package_json: bool = False
    readme: str = ""
    docs: dict[str, str] = field(default_factory=dict)     # every text file worth reading: relative path -> text
    py_deps: set[str] = field(default_factory=set)         # package names from requirements*.txt / pyproject.toml
    manifest: dict | None = None
    notes: list[str] = field(default_factory=list)

    def add_env(self, name: str, file: str, default=False, hard=False, implicit=False):
        if not ENV_NAME_RE.match(name) or name in SYSTEM_ENV:
            return
        u = self.env.setdefault(name, EnvUse())
        u.files.add(file)
        u.has_default |= default
        u.hard |= hard
        u.implicit |= implicit


def _walk(root: str):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            yield os.path.join(dp, fn)


def _read(p: str, limit: int = 2_000_000) -> str:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _scan_python(ev: Evidence, path: str, rel: str):
    try:
        tree = ast.parse(_read(path))
    except SyntaxError:
        return
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
            for m in mods:
                top = m.split(".")[0]
                if top in SDK_KEYS:
                    ev.add_env(SDK_KEYS[top], rel, hard=True, implicit=True)
        elif isinstance(n, ast.Call) and isinstance(n.func, (ast.Attribute, ast.Name)):
            fn = ast.unparse(n.func)
            if fn in ("os.environ.get", "os.getenv", "os.environ.setdefault") and n.args and \
                    isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                has_default = len(n.args) > 1 or any(k.arg == "default" for k in n.keywords)
                ev.add_env(n.args[0].value, rel, default=has_default)
        elif isinstance(n, ast.Subscript) and ast.unparse(n.value) == "os.environ":
            sl = n.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                ev.add_env(sl.value, rel, hard=True)
    src = _read(path)
    if "__main__" in src and os.sep not in rel and rel.endswith(".py") and re.search(r"__name__\s*==\s*['\"]__main__['\"]", src):
        ev.py_scripts.append(rel)


JS_ENV = [re.compile(r"process\.env\.([A-Z][A-Z0-9_]+)(\s*(\?\?|\|\|))?"), re.compile(r"process\.env\[['\"]([A-Z][A-Z0-9_]+)['\"]\](\s*(\?\?|\|\|))?")]


def _scan_js(ev: Evidence, path: str, rel: str):
    src = _read(path)
    for rx in JS_ENV:
        for m in rx.finditer(src):
            ev.add_env(m.group(1), rel, default=bool(m.group(2)))
    for pkg, key in SDK_KEYS.items():
        if pkg.startswith("@") and re.search(r"from\s+['\"]" + re.escape(pkg), src):
            ev.add_env(key, rel, hard=True, implicit=True)


def _scan_text_envs(ev: Evidence, text: str, rel: str):
    for m in re.finditer(r"^\s*(?:export\s+|set\s+|\$env:)?([A-Z][A-Z0-9_]{2,})\s*=\s*\S", text, re.M):
        ev.add_env(m.group(1), rel)


DOC_EXT = {".md", ".txt", ".rst", ".toml", ".cfg", ".ini", ".yml", ".yaml", ".markdown"}
DOC_NAMES = {"dockerfile", "makefile", ".env.example", ".env.sample", ".env.template", "env.example", "package.json"}
DATA_DIRS = {"data", "examples", "example", "fixtures", "samples", "sample", "datasets", "assets", "out", "output"}
MAX_DOC, MAX_TOTAL, MAX_FILES = 60_000, 400_000, 60


def _doc_rank(rel: str) -> tuple:
    b = os.path.basename(rel).lower()
    depth = rel.count(os.sep)
    first = 0 if b.startswith("readme") else 1 if b.startswith("requirements") else 2 if b in ("claude.md", "skill.md", "install.md", "setup.md", "agent.json") \
        else 3 if b in (".env.example", ".env.sample", "pyproject.toml", "package.json", "dockerfile") else 4
    return (first, depth, rel)


def collect_docs(root: str) -> dict[str, str]:
    """Every text file that can say how to run the project: README, requirements (comments too), docs, config, Dockerfile.
    Capped in size and count; sample-data folders are skipped."""
    found = []
    for p in _walk(root):
        rel = os.path.relpath(p, root)
        parts = {x.lower() for x in rel.split(os.sep)[:-1]}
        b = os.path.basename(rel).lower()
        ext = os.path.splitext(b)[1]
        if parts & DATA_DIRS and not b.startswith("readme"):
            continue
        if ext in DOC_EXT or b in DOC_NAMES or (b.startswith("requirements") and ext == ".txt"):
            if b in ("package-lock.json", "yarn.lock", "poetry.lock") or os.path.getsize(p) > 2_000_000:
                continue
            found.append(rel)
    docs, total = {}, 0
    for rel in sorted(found, key=_doc_rank)[:MAX_FILES]:
        t = _read(os.path.join(root, rel), MAX_DOC)
        if "\x00" in t:                      # binary
            continue
        if total + len(t) > MAX_TOTAL:
            break
        docs[rel] = t
        total += len(t)
    return docs


def scan(root: str) -> Evidence:
    ev = Evidence(root=root)
    for p in _walk(root):
        rel = os.path.relpath(p, root)
        low = rel.lower()
        base = os.path.basename(low)
        if low.endswith(".py"):
            _scan_python(ev, p, rel)
            if base == "__main__.py" and os.sep in rel:
                ev.py_modules.append(os.path.dirname(rel).replace(os.sep, "."))
        elif low.endswith((".ts", ".js", ".mjs", ".tsx")):
            _scan_js(ev, p, rel)
        elif base in (".env.example", ".env.sample", "env.example", ".env.template"):
            _scan_text_envs(ev, _read(p), rel)
        elif base == "requirements.txt":
            ev.has_requirements = True
        elif base == "package.json" and os.sep not in rel:
            ev.has_package_json = True
            try:
                pj = json.loads(_read(p))
                ev.npm_scripts = pj.get("scripts") or {}
                deps = {**(pj.get("dependencies") or {}), **(pj.get("devDependencies") or {})}
                for pkg, key in SDK_KEYS.items():
                    if pkg in deps:
                        ev.add_env(key, rel, hard=True, implicit=True)
                if any(k in ev.npm_scripts for k in ("preinstall", "install", "postinstall", "prepare")):
                    ev.notes.append("package.json has install-time scripts; they are NOT run (npm --ignore-scripts)")
            except ValueError:
                pass
        elif base in ("agent.json", "agent-launcher.json") and os.sep not in rel:
            try:
                ev.manifest = json.loads(_read(p))
            except ValueError:
                ev.notes.append(f"{rel} is not valid JSON; ignored")
    ev.docs = collect_docs(root)
    for rel, text in ev.docs.items():
        _scan_text_envs(ev, text, rel)
        base = os.path.basename(rel).lower()
        if base.startswith("requirements") and base.endswith(".txt"):
            for ln in text.splitlines():
                m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", ln)
                if m and not ln.lstrip().startswith(("#", "-")):
                    ev.py_deps.add(m.group(1).lower().replace("_", "-"))
        elif base == "pyproject.toml":
            for m in re.finditer(r"['\"]([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:[<>=!~\[;].*)?['\"]", text):
                ev.py_deps.add(m.group(1).lower().replace("_", "-"))
    ev.readme = next((t for r, t in ev.docs.items() if os.path.basename(r).lower().startswith("readme")), "")
    ev.kind = "python" if (ev.py_modules or ev.py_scripts or ev.has_requirements) else ("node" if ev.has_package_json else "unknown")
    if ev.kind == "python" and ev.has_package_json:
        ev.notes.append("both Python and Node files found; check the plan")
    if ev.kind == "python" and not ev.has_requirements:
        ev.notes.append("no requirements.txt: nothing will be installed, the script must run on the standard library")
    # README-only names (found only in prose) are weaker evidence than names read in code
    return ev


def is_secret(name: str) -> bool:
    return bool(SECRET_RE.search(name))
