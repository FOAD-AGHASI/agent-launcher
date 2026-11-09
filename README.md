# agent-launcher

Paste the GitHub link of an agent. The launcher downloads it, reads **every text file** in it, finds what it needs
(API keys, folders, programs such as Tesseract or AutoCAD), tells you what to install, asks you for the values,
and starts it. Run it again later and it re-checks what is still missing.

```
python -m agent_launcher            # guided, 4 steps
python -m agent_launcher --no-llm   # same, without Claude reading the files
```

Requirements: Python 3.10+ and `git`. Nothing else is needed to start.

## The 4 steps

1. **Links** - paste one GitHub link per line (several agents are fine).
2. **Check** - the agent is downloaded and read; nothing is run. You see the plan: what will be installed, the exact
   command that will run, which variables it will receive, and which programs it needs on this PC
   (`[OK]` / `[MISSING]`). Missing programs are split into **free** (the launcher offers to install them, after your
   yes) and **yours to install** (paid/licensed or unknown). Everything is re-checked afterwards.
3. **Inputs** - only required values are asked (secrets are hidden). Optional ones are listed, not asked.
4. **Start** - you confirm, it runs. Long-running agents start in the background.

## How the plan is found (most trusted first)

1. `agent.json` in the repo (the author's own declaration) - see `examples/manifests/`.
2. Claude reads all text files and proposes a plan (optional, needs an Anthropic key; the text is treated as untrusted).
3. Plain detection: Python `ast`, `process.env`, `.env.example`, requirements, `package.json` scripts, SDK defaults.

Every plan goes through `validate()`: only `python` / `node` / `npm` commands, the script must exist in the repo,
no shell characters, placeholders must be declared, and an environment variable name must appear in the repo's files.
A proposal that fails this check is dropped.

## Safety model

- Each agent gets its own virtual environment; `npm install --ignore-scripts`.
- The agent receives a minimal environment plus **only** the variables it declared. No shell is used.
- Secrets go to the OS credential store if `keyring` is installed; otherwise they live for this session only
  and you are asked again. `config.json` never holds secrets.
- Installing requirements still runs third-party package code. **Only add repositories you trust.**

## Other commands

`add <url>...`, `list`, `show <name>`, `check <name>`, `run <name>`, `start <name>`, `stop <name>`,
`set <name> <VAR> [value]`, `update <name>`, `remove <name>`.
State is kept in `~/.agent-launcher` (override with `AGENT_LAUNCHER_HOME`).

## Limits (honest)

- Tested on Linux with unit tests (`pip install pytest && pytest`) and against real repos. Not yet tested on Windows,
  with a real Windows Credential Manager, or with a real AutoCAD install.
- Prerequisite checks are passive (PATH lookup, known install folders, version probes).
- Free programs (Tesseract, Node.js, Git, Python, Ollama, FFmpeg, LibreOffice) can be installed for you via `winget`
  (Windows) or `brew` (macOS), **only after you say yes**. The list is fixed in the launcher, never taken from a
  repository's text. Paid/licensed software (AutoCAD, Docker Desktop) and anything not on the list (e.g. Poppler)
  are only listed for you to install yourself. Installers may ask for administrator permission. This part is
  tested with mocks only, not on a real Windows machine.
- Program names found only in prose are shown as optional, not blocking.
