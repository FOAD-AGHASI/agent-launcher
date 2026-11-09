"""The workflow: download -> scan -> plan -> confirm -> install -> ask for values -> run."""
from __future__ import annotations

import getpass
import os
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from typing import Callable

from . import fetch, llm, plan as planmod, prereq, runner, scan as scanmod
from .plan import Arg, EnvVar, Plan
from .store import Secrets, agent_dir, load_config, save_config, slugify


def _read(prompt: str, secret: bool) -> str:
    try:
        return getpass.getpass(prompt) if secret else input(prompt)
    except EOFError:            # no more input (piped or closed): treat as an empty answer
        return ""


@dataclass
class IO:
    """All user interaction goes through here, so tests can script it."""
    ask: Callable[[str, bool], str] = lambda prompt, secret: _read(prompt, secret)
    out: Callable[[str], None] = print
    interactive: bool = field(default_factory=lambda: sys.stdin.isatty())

    def confirm(self, prompt: str, default: bool = False) -> bool:
        a = self.ask(prompt + (" [Y/n] " if default else " [y/N] "), False).strip().lower()
        return default if not a else a in ("y", "yes")


@dataclass
class Prepared:
    url: str
    owner: str
    repo: str
    slug: str
    commit: str
    plan: Plan
    ev: object


class Launcher:
    def __init__(self, io: IO | None = None, secrets: Secrets | None = None, client=None, use_llm: bool = True,
                 local_ok: bool = False, parent_env: dict | None = None, run_cmd=None):
        self.io, self.secrets, self.client, self.use_llm = io or IO(), secrets or Secrets(), client, use_llm
        self.local_ok = local_ok
        self.parent_env = os.environ if parent_env is None else parent_env
        self.run_cmd = run_cmd           # injectable subprocess.run for tests

    # ---- add ---------------------------------------------------------------------------------
    def analyze(self, url: str, run_override: str | None = None) -> "Prepared | None":
        """Download, scan, plan, print the plan. Nothing from the repo is installed or run."""
        io = self.io
        owner, repo = (os.path.basename(os.path.dirname(os.path.abspath(url))), os.path.basename(url.rstrip("/\\"))) \
            if (self.local_ok and os.path.isdir(url)) else fetch.parse_github(url)
        slug = slugify(owner, repo)
        io.out(f"\n== {owner}/{repo}")
        io.out("  downloading and reading it (nothing is run yet)...")
        commit = fetch.clone(url, runner.repo_dir(slug), local_ok=self.local_ok)
        ev = scanmod.scan(runner.repo_dir(slug))

        p = planmod.from_manifest(ev, repo)
        if p is None and self.use_llm:
            try:
                p = llm.propose(ev, repo, self.client)
            except Exception as e:               # network or API problem: fall back to the heuristic
                io.out(f"  (Claude could not be used: {type(e).__name__}; using plain detection)")
        if p is None:
            p = planmod.heuristic(ev, repo)
        if run_override:
            p.run = shlex.split(run_override, posix=os.name != "nt")
            names = {m for t in p.run for m in planmod.PLACEHOLDER.findall(t)}
            p.args = [a for a in p.args if a.name in names] + [Arg(n, f"value for {{{n}}}") for n in sorted(names - {a.name for a in p.args})]
            p.source = "user"
        errs = planmod.validate(p, ev)
        if errs and io.interactive and not run_override:
            io.out("  I could not work out how to start this agent: " + "; ".join(errs))
            cmd = io.ask("  Type the command that starts it (e.g. python -m pkg --input {INPUT}), or press Enter to skip: ", False).strip()
            return self.analyze(url, cmd) if cmd else None
        if errs:
            io.out("  Cannot use this agent: " + "; ".join(errs))
            return None
        prep = Prepared(url, owner, repo, slug, commit, p, ev)
        self._show(owner, repo, commit, p, ev, slug)
        return prep

    def ensure_prerequisites(self, p: Plan, name: str = "") -> bool:
        """Check the programs this agent needs. If something is missing, say what to install and check again on Enter."""
        io = self.io
        if not p.requires:
            return True
        while True:
            results = prereq.check_all(p.requires)
            io.out(f"  Checking what {name or 'this agent'} needs on this PC:")
            for r, ok, where in results:
                tag = "OK" if ok else ("optional - not found" if r.optional else "MISSING")
                io.out(f"    [{tag}] {r.label()}" + (f"  ->  {where}" if ok else ""))
            missing = [r for r, ok, _ in results if not ok and not r.optional]
            if not missing:
                io.out("  All prerequisites are installed." if results else "")
                return True
            auto, manual = prereq.classify(missing)
            if auto:
                io.out("  Free - I can install these for you if you agree:")
                for r, argv, via in auto:
                    io.out(f"    - {r.label()}   (via {via}: {' '.join(argv)})")
            if manual:
                io.out("  You need to install these yourself:")
                for r, why in manual:
                    io.out(f"    - {r.label()} - {why}" + (f". {r.hint}" if r.hint else ""))
                    if r.url:
                        io.out(f"      download: {r.url}")
            if auto and io.interactive and io.confirm("  Install the free program(s) listed above now? Installers may ask for administrator permission", False):
                for r, argv, via in auto:
                    io.out(f"  installing {r.label()} ...")
                    ok = prereq.run_installer(argv, io.out)
                    io.out(f"    {'done' if ok else 'did not finish - install it by hand: ' + (r.url or '')}")
                continue
            if not io.interactive:
                return False
            io.out("  On Windows a new program is only seen by a NEW window: if it still says MISSING after installing, close this window, open a new one and run the same command again.")
            ans = io.ask("  Install the missing program(s), then press Enter to check again   (s = continue anyway, q = skip this agent): ", False).strip().lower()
            if ans == "s":
                return True
            if ans == "q":
                return False

    def check(self, name: str) -> bool:
        cfg, slug, p, a = self._load(name)
        return self.ensure_prerequisites(p, slug)

    def install_and_save(self, prep: "Prepared") -> None:
        io = self.io
        io.out(f"  installing {prep.owner}/{prep.repo}:")
        runner.install(prep.plan, prep.ev, prep.slug, run=self.run_cmd or __import__("subprocess").run, out=io.out)
        cfg = load_config()
        a = cfg["agents"].setdefault(prep.slug, {})
        a.update({"url": prep.url, "commit": prep.commit, "values": a.get("values", {}), "from_env": a.get("from_env", [])})
        import json
        with open(os.path.join(agent_dir(prep.slug), "plan.json"), "w", encoding="utf-8") as f:
            json.dump(prep.plan.to_dict(), f, indent=2)
        save_config(cfg)

    def collect(self, slug: str) -> None:
        """Ask for every value this agent needs (only the missing ones)."""
        cfg, slug, p, a = self._load(slug)
        self._collect(slug, p, a)
        save_config(cfg)

    def add(self, url: str, yes: bool = False, run_override: str | None = None) -> str | None:
        prep = self.analyze(url, run_override)
        if prep is None:
            return None
        if not (yes or self.io.confirm("  Install and set up this agent?")):
            self.io.out("  skipped.")
            return None
        if not self.ensure_prerequisites(prep.plan, f"{prep.owner}/{prep.repo}"):
            self.io.out("  skipped: install the missing program(s) and run the same command again; I will re-check and carry on.")
            return None
        self.install_and_save(prep)
        self.collect(prep.slug)
        self.io.out(f"  ready. Start it with:  agent-launcher run {prep.slug}")
        return prep.slug

    def _show(self, owner, repo, commit, p: Plan, ev, slug):
        o = self.io.out
        o(f"  commit {commit[:10]}   plan from: {p.source}")
        if p.description:
            o(f"  what it does: {p.description}")
        o("  will install:")
        for c in runner.install_commands(p, ev, slug) or [["(nothing)"]]:
            o("    " + " ".join(c))
        o("  will run:      " + " ".join(p.run))
        envs = ", ".join(f"{e.name}{' (secret)' if e.secret else ''}" for e in p.env) or "none"
        o(f"  will receive:  {envs}  (and nothing else from your environment)")
        if p.args:
            o("  you will give: " + ", ".join(f"{a.name} ({a.description})" for a in p.args))
        if p.requires:
            o("  needs on this PC:")
            for r, ok, where in prereq.check_all(p.requires):
                o(f"    [{'OK' if ok else ('..' if r.optional else 'MISSING')}] {r.label()}" + (f" ({r.description})" if r.description else ""))
        for n in ev.notes:
            o("  note: " + n)
        o("  note: installing the requirements runs code from third-party packages; only add repositories you trust.")

    # ---- values ------------------------------------------------------------------------------
    def _collect(self, slug: str, p: Plan, a: dict, reset: bool = False):
        io = self.io
        optional = [e.name for e in p.env if not e.required and not self._has_env_value(slug, e, a)]
        for e in p.env:
            if not e.required:
                continue
            if not reset and self._has_env_value(slug, e, a):
                continue
            if e.name in self.parent_env and io.confirm(f"  {e.name} is already set in your environment. Use it for this agent?", True):
                if e.name not in a["from_env"]:
                    a["from_env"].append(e.name)
                continue
            self._ask_env(slug, e, a)
        for g in p.args:
            if not reset and g.name in a["values"]:
                continue
            self._ask_arg(g, a)
        if optional:
            io.out("  optional, not asked: " + ", ".join(optional) + f"   (set later: agent-launcher set {slug} NAME)")

    def _has_env_value(self, slug, e: EnvVar, a) -> bool:
        return (e.name in a.get("from_env", []) or e.name in a["values"] or self.secrets.get(slug, e.name) is not None)

    def _ask_env(self, slug, e: EnvVar, a):
        label = f"  {e.name}{' (hidden)' if e.secret else ''}{' - ' + e.description if e.description else ''}{'' if e.required else ' [optional, Enter to skip]'}: "
        while True:
            v = self.io.ask(label, e.secret).strip()
            if v or not e.required or not self.io.interactive:
                break
        if not v:
            return
        if e.secret:
            self.secrets.set(slug, e.name, v)
            if not self.secrets.persistent:
                self.io.out("    (kept for this session only; install 'keyring' to store it in the Windows Credential Manager)")
        else:
            a["values"][e.name] = v

    def _ask_arg(self, g: Arg, a):
        while True:
            v = self.io.ask(f"  {g.name} - {g.description}{' (path)' if g.kind == 'path' else ''}: ", False).strip().strip('"')
            if g.kind == "path" and v:
                v = os.path.abspath(os.path.expanduser(v))
                if not os.path.exists(v):
                    self.io.out(f"    not found: {v}")
                    if self.io.interactive:
                        continue
                    v = ""
            if v or not g.required or not self.io.interactive:
                break
        if v:
            a["values"][g.name] = v

    # ---- run ---------------------------------------------------------------------------------
    def _load(self, name: str):
        cfg = load_config()
        slug = self.resolve(name, cfg)
        with open(os.path.join(agent_dir(slug), "plan.json"), encoding="utf-8") as f:
            import json
            p = Plan.from_dict(json.load(f), "stored")
        return cfg, slug, p, cfg["agents"][slug]

    @staticmethod
    def resolve(name: str, cfg: dict) -> str:
        agents = cfg["agents"]
        if name in agents:
            return name
        hits = [s for s in agents if s.lower().endswith("__" + name.lower()) or s.lower() == name.lower()]
        if len(hits) != 1:
            raise SystemExit(f"unknown or ambiguous agent {name!r}; installed: {', '.join(agents) or 'none'}")
        return hits[0]

    def _env_and_args(self, slug, p: Plan, a, overrides: dict[str, str]):
        declared, args = {}, {}
        for e in p.env:
            v = a["values"].get(e.name) or (self.parent_env.get(e.name) if e.name in a.get("from_env", []) else None) \
                or self.secrets.get(slug, e.name)
            if v is None and e.required:
                if not self.io.interactive:
                    raise SystemExit(f"missing value for {e.name}; run `agent-launcher set {slug} {e.name}`")
                self._ask_env(slug, e, a)
                v = a["values"].get(e.name) or self.secrets.get(slug, e.name)
            if v is not None:
                declared[e.name] = v
        for g in p.args:
            v = overrides.get(g.name) or a["values"].get(g.name)
            if v is None and g.required:
                if not self.io.interactive:
                    raise SystemExit(f"missing value for {g.name}; pass --set {g.name}=...")
                self._ask_arg(g, a)
                v = a["values"].get(g.name)
            if v is not None:
                args[g.name] = v
        return declared, args

    def run(self, name: str, overrides: dict | None = None, extra: list[str] | None = None, background: bool = False) -> int:
        cfg, slug, p, a = self._load(name)
        if not self.ensure_prerequisites(p, slug):
            return 1
        declared, args = self._env_and_args(slug, p, a, overrides or {})
        save_config(cfg)
        argv = runner.resolve_argv(p, slug, args, extra)
        env = runner.build_env(declared)
        cwd = runner.repo_dir(slug)
        self.io.out(f"  running {slug}: " + " ".join(argv))
        if background:
            pid, log = runner.start_background(slug, argv, env, cwd)
            self.io.out(f"  started (pid {pid}). Log: {log}")
            return 0
        return runner.run_foreground(argv, env, cwd, run=self.run_cmd or __import__("subprocess").run)

    def set_value(self, name: str, var: str, value: str | None = None):
        cfg, slug, p, a = self._load(name)
        known = {e.name: e for e in p.env} | {g.name: g for g in p.args}
        if var not in known:
            raise SystemExit(f"{var} is not a variable of this agent; known: {', '.join(known)}")
        tgt = known[var]
        if isinstance(tgt, EnvVar):
            a["values"].pop(var, None)
            if var in a["from_env"]:
                a["from_env"].remove(var)
            self.secrets.delete(slug, var)
            if value is not None:
                (self.secrets.set(slug, var, value) if tgt.secret else a["values"].__setitem__(var, value))
            else:
                self._ask_env(slug, tgt, a)
        else:
            a["values"].pop(var, None)
            if value is not None:
                a["values"][var] = value
            else:
                self._ask_arg(tgt, a)
        save_config(cfg)

    def show(self, name: str):
        cfg, slug, p, a = self._load(name)
        o = self.io.out
        o(f"{slug}  (commit {a.get('commit', '')[:10]})  plan from {p.source}")
        o(f"  run: {' '.join(p.run)}")
        for e in p.env:
            st = "set" if self._has_env_value(slug, e, a) else ("MISSING" if e.required else "not set")
            o(f"  env  {e.name:<28} {st}{'  (secret)' if e.secret else ''}")
        for g in p.args:
            o(f"  arg  {g.name:<28} {a['values'].get(g.name, 'MISSING' if g.required else 'not set')}")

    def list(self):
        cfg = load_config()
        if not cfg["agents"]:
            self.io.out("no agents installed yet. Try: agent-launcher add https://github.com/<owner>/<repo>")
        for s, a in cfg["agents"].items():
            self.io.out(f"{s}   {a.get('url', '')}   {a.get('commit', '')[:10]}")

    def remove(self, name: str):
        cfg, slug, p, a = self._load(name)
        runner.stop(slug)
        for e in p.env:
            self.secrets.delete(slug, e.name)
        shutil.rmtree(agent_dir(slug), ignore_errors=True)
        del cfg["agents"][slug]
        save_config(cfg)
        self.io.out(f"removed {slug} (its stored secrets too).")
