"""System software an agent needs (Tesseract, AutoCAD, Node, Ollama...). Declared by the agent's author, or found in its README.

Checking is passive: a program is looked up on PATH / at known install paths. Nothing declared by a repo is executed
(only `python` and `node` are asked for their own version)."""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

CMD_RE = re.compile(r"^[A-Za-z0-9._+-]{1,40}$")

# name -> (commands to look for, common install paths, download page, plain-words hint)
KNOWN: dict[str, dict] = {
    "tesseract": {"label": "Tesseract OCR", "commands": ["tesseract"],
                  "paths": ["%ProgramFiles%/Tesseract-OCR/tesseract.exe", "%ProgramFiles(x86)%/Tesseract-OCR/tesseract.exe",
                            "%LOCALAPPDATA%/Programs/Tesseract-OCR/tesseract.exe"],
                  "url": "https://github.com/UB-Mannheim/tesseract/wiki",
                  "hint": "Install it and tick the extra languages you need (for example Persian = 'fas'). Then add its folder to PATH or leave the default location."},
    "autocad": {"label": "AutoCAD (accoreconsole.exe)", "commands": ["accoreconsole"],
                "paths": ["%ProgramFiles%/Autodesk/AutoCAD */accoreconsole.exe"],
                "url": "https://www.autodesk.com/products/autocad/overview",
                "hint": "AutoCAD is commercial software; it must be installed and licensed on this PC."},
    "node": {"label": "Node.js", "commands": ["node", "npm"], "paths": ["%ProgramFiles%/nodejs/node.exe"],
             "url": "https://nodejs.org/en/download", "hint": "Install the LTS version."},
    "git": {"label": "Git", "commands": ["git"], "paths": ["%ProgramFiles%/Git/cmd/git.exe"],
            "url": "https://git-scm.com/downloads", "hint": "Install with the default options."},
    "ollama": {"label": "Ollama", "commands": ["ollama"], "paths": ["%LOCALAPPDATA%/Programs/Ollama/ollama.exe"],
               "url": "https://ollama.com/download", "hint": "Install it, then pull the model the agent names."},
    "ffmpeg": {"label": "FFmpeg", "commands": ["ffmpeg"], "paths": [], "url": "https://ffmpeg.org/download.html",
               "hint": "Download a build and add its bin folder to PATH."},
    "poppler": {"label": "Poppler (pdftoppm)", "commands": ["pdftoppm"], "paths": [], "url": "https://github.com/oschwartz10612/poppler-windows/releases",
                "hint": "Unzip and add the bin folder to PATH."},
    "libreoffice": {"label": "LibreOffice", "commands": ["soffice"], "paths": ["%ProgramFiles%/LibreOffice/program/soffice.exe"],
                    "url": "https://www.libreoffice.org/download/", "hint": "Install with the default options."},
    "docker": {"label": "Docker", "commands": ["docker"], "paths": ["%ProgramFiles%/Docker/Docker/resources/bin/docker.exe"],
               "url": "https://docs.docker.com/get-docker/", "hint": "Install Docker Desktop and start it."},
    "powershell": {"label": "PowerShell", "commands": ["powershell", "pwsh"], "paths": [],
                   "url": "https://learn.microsoft.com/powershell/", "hint": "Built into Windows."},
    "python": {"label": "Python", "commands": ["python", "python3"], "paths": [], "url": "https://www.python.org/downloads/",
               "hint": "Install Python and tick 'Add to PATH'."},
}
KEYWORDS = {"tesseract": r"tesseract", "autocad": r"autocad|accoreconsole", "ollama": r"ollama", "ffmpeg": r"ffmpeg",
            "poppler": r"poppler|pdftoppm", "libreoffice": r"libreoffice|soffice", "docker": r"\bdocker\b", "node": r"node\.?js"}
NEED_WORDS = re.compile(r"require|prerequisite|prereq|needs?\b|must|install|depends", re.I)


@dataclass
class Requirement:
    name: str                                   # a key of KNOWN, or free text from a manifest
    description: str = ""
    commands: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    url: str = ""
    hint: str = ""
    optional: bool = False
    min_version: str = ""                       # python and node only, e.g. "3.10"

    def label(self) -> str:
        return KNOWN.get(self.name, {}).get("label", self.name)


def fill_known(r: Requirement) -> Requirement:
    k = KNOWN.get(r.name.lower())
    if k:
        r.commands = r.commands or list(k["commands"])
        r.paths = r.paths or list(k["paths"])
        r.url = r.url or k["url"]
        r.hint = r.hint or k["hint"]
    return r


def sanitize(r: Requirement, allow_paths: bool) -> Requirement | None:
    """Reject anything that is not a plain program name. Paths only from the author's manifest."""
    r = fill_known(r)
    r.commands = [c for c in r.commands if CMD_RE.match(c)]
    r.paths = [p for p in r.paths if allow_paths and not re.search(r"[;&|<>`$\n\r]", p.replace("%", ""))]
    if not r.commands and not r.paths:
        return None
    return r


def _version_of(cmd: str) -> tuple[int, ...] | None:
    try:
        out = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def check(r: Requirement) -> tuple[bool, str]:
    """(found, where-or-reason)."""
    name = r.name.lower()
    if name == "python":
        if r.min_version:
            want = tuple(int(x) for x in r.min_version.split(".")[:2])
            if sys.version_info[:2] < want:
                return False, f"you have Python {sys.version_info[0]}.{sys.version_info[1]}, {r.min_version} or newer is needed"
        return True, sys.executable
    for c in r.commands:
        w = shutil.which(c)
        if w:
            if name == "node" and r.min_version and c == "node":
                v = _version_of(w)
                if v and v[0] < int(r.min_version.split(".")[0]):
                    return False, f"you have Node {v[0]}.{v[1]}, version {r.min_version} or newer is needed"
            return True, w
    for pat in r.paths:
        hits = glob.glob(os.path.expandvars(pat))
        if hits:
            return True, sorted(hits)[-1]
    return False, "not found"


def from_readme(text: str) -> list[Requirement]:
    """Tools a README says are needed. Weak evidence, so these are only shown as 'mentioned' unless the author declares them."""
    found: dict[str, Requirement] = {}
    for line in text.splitlines():
        if not NEED_WORDS.search(line):
            continue
        for name, rx in KEYWORDS.items():
            if re.search(rx, line, re.I) and name not in found:
                found[name] = fill_known(Requirement(name=name, description="mentioned in the README"))
    return list(found.values())


DEP_IMPLIES = {"pytesseract": "tesseract", "pdf2image": "poppler", "ffmpeg-python": "ffmpeg", "moviepy": "ffmpeg", "pydub": "ffmpeg",
               "ollama": "ollama", "docker": "docker"}


def from_deps(deps: set[str]) -> list[Requirement]:
    """A Python package that is only a wrapper around a program means the program must be installed (strong evidence)."""
    return [fill_known(Requirement(name=DEP_IMPLIES[d], description=f"needed by the Python package {d}")) for d in sorted(deps) if d in DEP_IMPLIES]


def from_texts(docs: dict[str, str]) -> list[Requirement]:
    """Tools mentioned as needed anywhere in the repo's text files. Comments in requirements.txt count too."""
    found: dict[str, Requirement] = {}
    for rel, text in docs.items():
        if rel.lower().endswith(".json") and rel.lower() != "agent.json":
            continue
        for line in text.splitlines():
            if not NEED_WORDS.search(line):
                continue
            for name, rx in KEYWORDS.items():
                if re.search(rx, line, re.I) and name not in found:
                    found[name] = fill_known(Requirement(name=name, description=f"mentioned in {rel}", optional=True))
    return list(found.values())


def implied(kind: str) -> list[Requirement]:
    return [fill_known(Requirement(name="node", description="runs on Node.js"))] if kind == "node" else []


def check_all(reqs: list[Requirement]) -> list[tuple[Requirement, bool, str]]:
    return [(r, *check(r)) for r in reqs]


# ---- installing free programs (only after the user says yes) -------------------------------------------------
# Fixed whitelist: program -> package ids. Never built from repository text. Everything else is "install it yourself".
# Commercial / licensed software is listed in PAID with the reason, and is never installed by the launcher.
AUTO_INSTALL: dict[str, dict[str, str]] = {
    "tesseract": {"winget": "UB-Mannheim.TesseractOCR", "brew": "tesseract"},
    "node": {"winget": "OpenJS.NodeJS.LTS", "brew": "node"},
    "git": {"winget": "Git.Git", "brew": "git"},
    "python": {"winget": "Python.Python.3.12", "brew": "python"},
    "ollama": {"winget": "Ollama.Ollama", "brew": "ollama"},
    "ffmpeg": {"winget": "Gyan.FFmpeg", "brew": "ffmpeg"},
    "libreoffice": {"winget": "TheDocumentFoundation.LibreOffice", "brew": "--cask libreoffice"},
}
PAID = {"autocad": "commercial software that needs a licence",
        "docker": "Docker Desktop has licence terms for companies and needs admin rights and a restart"}


def installer_for(r: Requirement) -> tuple[list[str], str] | None:
    """The exact command that would install this program on this PC, or None (then the user installs it by hand)."""
    ids = AUTO_INSTALL.get(r.name.lower())
    if not ids:
        return None
    if sys.platform == "win32" and shutil.which("winget") and "winget" in ids:
        return (["winget", "install", "--id", ids["winget"], "-e", "--silent",
                 "--accept-package-agreements", "--accept-source-agreements"], "winget")
    if sys.platform == "darwin" and shutil.which("brew") and "brew" in ids:
        return (["brew", "install", *ids["brew"].split()], "brew")
    return None


def classify(missing: list[Requirement]) -> tuple[list[tuple[Requirement, list[str], str]], list[tuple[Requirement, str]]]:
    """-> (free ones the launcher can install, ones the user must install: (req, reason))."""
    auto, manual = [], []
    for r in missing:
        inst = installer_for(r)
        if inst:
            auto.append((r, *inst))
        elif r.name.lower() in PAID:
            manual.append((r, "paid / licensed: " + PAID[r.name.lower()]))
        else:
            manual.append((r, "install it yourself (no safe automatic installer on this PC)"))
    return auto, manual


def refresh_path() -> None:
    """Windows: a program installed a moment ago is on the registry PATH but not in this process. Re-read it."""
    if sys.platform != "win32":
        return
    try:
        import winreg
        parts = []
        for root, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, "Environment")):
            with winreg.OpenKey(root, sub) as k:
                parts.append(os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]))
        os.environ["PATH"] = ";".join(parts + [os.environ.get("PATH", "")])
    except OSError:
        pass


def run_installer(argv: list[str], out=print) -> bool:
    try:
        rc = subprocess.run(argv, timeout=1800).returncode
    except (OSError, subprocess.SubprocessError) as e:
        out(f"    installer failed: {e}")
        return False
    refresh_path()
    return rc == 0
