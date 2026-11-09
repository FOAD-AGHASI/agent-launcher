"""The run plan of one agent: how to install it, how to start it, which values it needs.

Sources, best first: the repo's own agent.json  >  Claude reading the README and the scan  >  plain heuristics.
Whatever the source, validate() decides if the plan may be used. The launcher never runs a free-form shell command."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field

from .prereq import Requirement, fill_known, from_deps, from_texts, implied, sanitize
from .scan import Evidence, is_secret

ALLOWED_EXE = {"python", "node", "npm"}
SHELL_CHARS = re.compile(r"[;&|<>`$\n\r]")
PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


@dataclass
class EnvVar:
    name: str
    secret: bool = False
    required: bool = True
    description: str = ""


@dataclass
class Arg:
    name: str                 # used as {NAME} in `run`
    description: str = ""
    kind: str = "text"        # text | path
    required: bool = True


@dataclass
class Plan:
    name: str = ""
    description: str = ""
    kind: str = "python"      # python | node
    run: list[str] = field(default_factory=list)
    env: list[EnvVar] = field(default_factory=list)
    args: list[Arg] = field(default_factory=list)
    source: str = "heuristic"  # manifest | llm | heuristic
    long_running: bool = False
    requires: list[Requirement] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict, source: str = "") -> "Plan":
        return Plan(name=str(d.get("name", "")), description=str(d.get("description", "")), kind=str(d.get("kind", "python")),
                    run=[str(x) for x in d.get("run", [])],
                    env=[EnvVar(str(e["name"]), bool(e.get("secret", is_secret(str(e["name"])))), bool(e.get("required", True)),
                                str(e.get("description", ""))) for e in d.get("env", [])],
                    args=[Arg(str(a["name"]), str(a.get("description", "")), str(a.get("kind", "text")), bool(a.get("required", True)))
                          for a in d.get("args", [])],
                    source=source or str(d.get("source", "heuristic")), long_running=bool(d.get("long_running", False)),
                    requires=[r for r in (_req(x, allow_paths=(source in ("manifest", "stored", "user"))) for x in d.get("requires", [])) if r])


def _req(x: dict, allow_paths: bool) -> Requirement | None:
    if not isinstance(x, dict) or not x.get("name"):
        return None
    return sanitize(Requirement(name=str(x["name"]), description=str(x.get("description", "")),
                                commands=[str(c) for c in x.get("commands", [])], paths=[str(c) for c in x.get("paths", [])],
                                url=str(x.get("url", "")), hint=str(x.get("hint", "")), optional=bool(x.get("optional", False)),
                                min_version=str(x.get("min_version", ""))), allow_paths)


def _merge_requires(*lists: list[Requirement]) -> list[Requirement]:
    out: dict[str, Requirement] = {}
    for lst in lists:
        for r in lst:
            out.setdefault(r.name.lower(), r)
    return list(out.values())


def from_manifest(ev: Evidence, repo_name: str) -> Plan | None:
    if not ev.manifest:
        return None
    m = dict(ev.manifest)
    m.setdefault("name", repo_name)
    m.setdefault("kind", ev.kind if ev.kind != "unknown" else "python")
    try:
        p = Plan.from_dict(m, "manifest")
    except (KeyError, TypeError, ValueError):
        return None
    p.requires = _merge_requires(p.requires, implied(p.kind))
    return p


def heuristic(ev: Evidence, repo_name: str) -> Plan:
    """No LLM: pick the most likely entry point and list every environment variable the code reads."""
    p = Plan(name=repo_name, kind=ev.kind if ev.kind != "unknown" else "python", source="heuristic")
    if ev.kind == "python":
        if ev.py_modules:
            p.run = ["python", "-m", sorted(ev.py_modules, key=len)[0]]
        elif ev.py_scripts:
            pick = next((s for s in ("main.py", "cli.py", "app.py") if s in ev.py_scripts), sorted(ev.py_scripts)[0])
            p.run = ["python", pick]
    elif ev.kind == "node":
        skip = {"test", "typecheck", "lint", "build", "eval", "eval:baseline"}
        pick = "start" if "start" in ev.npm_scripts else next((s for s in ev.npm_scripts if s not in skip), "")
        if pick:
            p.run = ["npm", "run", pick]
    p.requires = _merge_requires(implied(p.kind), from_deps(ev.py_deps), from_texts(ev.docs))
    for name, u in sorted(ev.env.items()):
        p.env.append(EnvVar(name, is_secret(name), required=u.hard,
                            description="read by " + ", ".join(sorted(u.files)[:3])))
    return p


def validate(p: Plan, ev: Evidence) -> list[str]:
    """Errors that make the plan unusable. Run commands are checked against the real repo files."""
    errs: list[str] = []
    if not p.run:
        return ["no run command could be determined; give one with --run"]
    if p.kind not in ("python", "node"):
        errs.append(f"kind must be python or node, got {p.kind!r}")
    exe = p.run[0]
    if exe not in ALLOWED_EXE:
        errs.append(f"run command must start with one of {sorted(ALLOWED_EXE)}, got {exe!r}")
    for tok in p.run:
        if SHELL_CHARS.search(PLACEHOLDER.sub("", tok)):
            errs.append(f"shell characters are not allowed in the run command: {tok!r}")
    if exe == "python" and len(p.run) >= 2:
        if p.run[1] == "-m":
            mod = p.run[2] if len(p.run) > 2 else ""
            if mod.split(".")[0] and not os.path.exists(os.path.join(ev.root, *mod.split("."))) \
                    and not os.path.exists(os.path.join(ev.root, *mod.split(".")) + ".py"):
                errs.append(f"module {mod!r} is not in the repository")
        elif not p.run[1].startswith("-") and not os.path.isfile(os.path.join(ev.root, p.run[1])):
            errs.append(f"script {p.run[1]!r} is not in the repository")
    if exe == "npm":
        if p.run[1:2] != ["run"] and p.run[1:2] != ["start"]:
            errs.append("npm commands must be `npm run <script>` or `npm start`")
        elif p.run[1] == "run" and (len(p.run) < 3 or p.run[2] not in ev.npm_scripts):
            errs.append(f"npm script {p.run[2] if len(p.run) > 2 else ''!r} is not in package.json")
    if exe == "node" and len(p.run) > 1 and not p.run[1].startswith("-") and not os.path.isfile(os.path.join(ev.root, p.run[1])):
        errs.append(f"file {p.run[1]!r} is not in the repository")
    declared_args = {a.name for a in p.args}
    used = {m for tok in p.run for m in PLACEHOLDER.findall(tok)}
    if used - declared_args:
        errs.append(f"placeholders without an argument definition: {sorted(used - declared_args)}")
    for a in p.args:
        if not NAME_RE.match(a.name) or a.kind not in ("text", "path"):
            errs.append(f"bad argument definition {a.name!r}")
    for e in p.env:
        if not NAME_RE.match(e.name):
            errs.append(f"bad environment variable name {e.name!r}")
        elif p.source != "manifest" and e.name not in ev.env:
            errs.append(f"{e.name} is not read anywhere in the code or docs (not asked for)")
    return errs
