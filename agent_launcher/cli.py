"""agent-launcher add <github url> [...]   download, find what each agent needs, ask for it, install
agent-launcher run <name>              start an agent with the values you gave"""
from __future__ import annotations

import argparse
import sys

from .launcher import Launcher
from .store import load_config


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent-launcher", description="Install and run GitHub agents with the values they need.")
    ap.add_argument("--no-llm", dest="top_no_llm", action="store_true", help="guided mode without Claude reading the files")
    sub = ap.add_subparsers(dest="cmd")
    w = sub.add_parser("wizard", help="guided mode (this is also what runs when you give no command)")
    w.add_argument("--no-llm", action="store_true")
    a = sub.add_parser("add", help="download one or more agents from GitHub, set them up")
    a.add_argument("urls", nargs="+", help="https://github.com/<owner>/<repo>")
    a.add_argument("--no-llm", action="store_true", help="do not use Claude to read the README (plain detection only)")
    a.add_argument("--yes", action="store_true", help="skip the 'install this?' question (the plan is still printed)")
    a.add_argument("--run", help="run command to use instead of the detected one, e.g. \"python -m pkg --input {INPUT}\"")
    a.add_argument("--launch", action="store_true", help="start the agent(s) when set up (one agent: in front; several: in background)")
    sub.add_parser("list", help="installed agents")
    sub.add_parser("check", help="re-check the programs an agent needs").add_argument("name")
    for n, h in (("show", "plan and which values are set"), ("start", "start in the background"), ("stop", "stop a background agent"),
                 ("update", "download the latest version and show the plan again"), ("remove", "delete an agent and its stored secrets")):
        sub.add_parser(n, help=h).add_argument("name")
    r = sub.add_parser("run", help="start an agent in front")
    r.add_argument("name")
    r.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="value for this run only")
    r.add_argument("extra", nargs=argparse.REMAINDER, help="after --: extra arguments passed to the agent")
    s = sub.add_parser("set", help="change a stored value")
    s.add_argument("name")
    s.add_argument("var")
    s.add_argument("value", nargs="?", help="omit to be asked (hidden input for secrets)")
    ns = ap.parse_args(argv)

    lz = Launcher(use_llm=not (getattr(ns, "no_llm", False) or ns.top_no_llm))
    if ns.cmd in (None, "wizard"):
        from .wizard import wizard
        return wizard(lz)
    if ns.cmd == "check":
        return 0 if lz.check(ns.name) else 1
    if ns.cmd == "add":
        slugs = [lz.add(u, ns.yes, ns.run) for u in ns.urls]
        ready = [s for s in slugs if s]
        if ns.launch and ready:
            if len(ready) == 1:
                return lz.run(ready[0])
            for s in ready:
                lz.run(s, background=True)
        return 0 if ready or not ns.urls else 1
    if ns.cmd == "list":
        lz.list()
    elif ns.cmd == "show":
        lz.show(ns.name)
    elif ns.cmd == "run":
        ov = dict(kv.split("=", 1) for kv in ns.set if "=" in kv)
        extra = [x for x in ns.extra if x != "--"] if ns.extra else []
        return lz.run(ns.name, ov, extra)
    elif ns.cmd == "start":
        return lz.run(ns.name, background=True)
    elif ns.cmd == "stop":
        from . import runner
        slug = lz.resolve(ns.name, load_config())
        print("stopped." if runner.stop(slug) else "not running.")
    elif ns.cmd == "set":
        lz.set_value(ns.name, ns.var, ns.value)
    elif ns.cmd == "update":
        cfg = load_config()
        slug = lz.resolve(ns.name, cfg)
        lz.add(cfg["agents"][slug]["url"])
    elif ns.cmd == "remove":
        lz.remove(ns.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
