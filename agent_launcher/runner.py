"""Install and start an agent. No shell is ever used; every command is an argv list.

Each agent gets its own virtual environment, and a minimal environment: only the variables it declared plus what a
program needs to start. Your other keys and tokens are not passed on."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .plan import PLACEHOLDER, Plan
from .scan import Evidence
from .store import agent_dir, home

BASE_ENV = ["PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
            "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "HTTP_PROXY",
            "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "ProgramW6432", "windir"]


def repo_dir(slug: str) -> str:
    return os.path.join(agent_dir(slug), "repo")


def venv_python(slug: str) -> str:
    v = os.path.join(agent_dir(slug), "venv")
    return os.path.join(v, "Scripts", "python.exe") if os.name == "nt" else os.path.join(v, "bin", "python")


def install_commands(plan: Plan, ev: Evidence, slug: str) -> list[list[str]]:
    """What install will run (shown to the user before anything is executed)."""
    cmds: list[list[str]] = []
    if plan.kind == "python":
        cmds.append([sys.executable, "-m", "venv", os.path.join(agent_dir(slug), "venv")])
        if ev.has_requirements:
            cmds.append([venv_python(slug), "-m", "pip", "install", "-r", "requirements.txt"])
    elif plan.kind == "node" and ev.has_package_json:
        cmds.append([shutil.which("npm") or "npm", "install", "--ignore-scripts"])
    return cmds


def install(plan: Plan, ev: Evidence, slug: str, run=subprocess.run, out=print) -> None:
    for cmd in install_commands(plan, ev, slug):
        out("  $ " + " ".join(cmd))
        r = run(cmd, cwd=repo_dir(slug), env=build_env({}))
        if r.returncode:
            raise RuntimeError(f"install step failed ({r.returncode}): {' '.join(cmd)}")


def build_env(declared: dict[str, str]) -> dict[str, str]:
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update({k: v for k, v in declared.items() if v is not None})
    return env


def resolve_argv(plan: Plan, slug: str, arg_values: dict[str, str], extra: list[str] | None = None) -> list[str]:
    argv: list[str] = []
    for i, t in enumerate(plan.run):
        names = PLACEHOLDER.findall(t)
        if any(n not in arg_values for n in names):           # optional argument left empty: drop it and its flag
            if argv and argv[-1].startswith("-") and not PLACEHOLDER.findall(plan.run[i - 1]):
                argv.pop()
            continue
        argv.append(PLACEHOLDER.sub(lambda m: arg_values[m.group(1)], t))
    if argv[0] == "python":
        argv[0] = venv_python(slug)
    elif argv[0] == "npm":
        argv[0] = shutil.which("npm") or "npm"
    elif argv[0] == "node":
        argv[0] = shutil.which("node") or "node"
    return argv + list(extra or [])


def run_foreground(argv: list[str], env: dict[str, str], cwd: str, run=subprocess.run) -> int:
    return run(argv, cwd=cwd, env=env).returncode


def _pidfile(slug: str) -> str:
    return os.path.join(agent_dir(slug), "run.pid")


def start_background(slug: str, argv: list[str], env: dict[str, str], cwd: str) -> tuple[int, str]:
    os.makedirs(os.path.join(home(), "logs"), exist_ok=True)
    log = os.path.join(home(), "logs", f"{slug}.log")
    fh = open(log, "ab")
    kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}   # detached
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
    with open(_pidfile(slug), "w") as f:
        f.write(str(p.pid))
    return p.pid, log


def stop(slug: str) -> bool:
    try:
        pid = int(open(_pidfile(slug)).read().strip())
    except (OSError, ValueError):
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        os.remove(_pidfile(slug))
    except OSError:
        pass
    return True
