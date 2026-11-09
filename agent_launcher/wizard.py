"""The guided mode: `agent-launcher` with no arguments.

  Step 1  give me the GitHub link(s)
  Step 2  I download and read each agent, check what it needs on this PC, and set it up
  Step 3  you give the inputs each agent needs (keys, folders...)
  Step 4  I start the agent(s)"""
from __future__ import annotations

import os
import re

from . import fetch
from .launcher import Launcher

BANNER = """
  agent-launcher
  Give me the GitHub link of an agent. I will find out what it needs, ask you for it, and start it.
"""


def read_links(lz: Launcher) -> list[str]:
    io = lz.io
    io.out("Step 1 of 4 - Which agent(s) do you want to run?")
    io.out("  Paste one GitHub link per line (several are fine). Press Enter on an empty line when you are done.")
    links: list[str] = []
    empty = 0
    while True:
        line = io.ask("  link> " if not links else "  more (or Enter to continue)> ", False)
        if not line.strip():
            empty += 1
            if links or not io.interactive or empty >= 5:        # 5 empty answers in a row: input is closed, stop asking
                break
            io.out("  I need at least one link, for example https://github.com/owner/repo")
            continue
        for part in ([line.strip()] if (lz.local_ok and os.path.isdir(line.strip())) else re.split(r"[\s,;]+", line.strip())):
            if not part:
                continue
            if lz.local_ok and os.path.isdir(part):              # tests only: a local folder instead of a GitHub link
                links.append(part)
                continue
            url = fetch.normalize_url(part)
            try:
                fetch.parse_github(url)
            except ValueError:
                io.out(f"  '{part}' is not a GitHub repository link (only github.com is supported). Example: https://github.com/owner/repo")
                continue
            if url not in links:
                links.append(url)
                io.out(f"  added: {url}")
    return links


def offer_claude(lz: Launcher) -> None:
    """Optional: a key lets Claude read the project's files to find the exact inputs. Kept in memory for this session only."""
    io = lz.io
    if not lz.use_llm or lz.client is not None or lz.parent_env.get("ANTHROPIC_API_KEY") or not io.interactive:
        return
    io.out("\nOptional: Claude can read each project's files and find the exact inputs for you (better than my simple detection).")
    key = io.ask("  Paste your Anthropic API key, or press Enter to skip (it is not saved): ", True).strip()
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key           # this process only; agents get it only if they declare they need it
        lz.parent_env = os.environ
    else:
        lz.use_llm = False


def wizard(lz: Launcher) -> int:
    io = lz.io
    io.out(BANNER)
    links = read_links(lz)
    if not links:
        io.out("No links given. Nothing to do.")
        return 1
    offer_claude(lz)

    io.out("\nStep 2 of 4 - Checking the agent(s)")
    ready = []
    for url in links:
        try:
            prep = lz.analyze(url)
        except Exception as e:
            io.out(f"  could not get {url}: {e}")
            continue
        if prep is None:
            continue
        if not lz.ensure_prerequisites(prep.plan, f"{prep.owner}/{prep.repo}"):
            io.out(f"  {prep.owner}/{prep.repo} is not ready. Install what is missing and start me again with the same link: I will re-check and continue.")
            continue
        ready.append(prep)
    if not ready:
        io.out("\nNo agent is ready yet.")
        return 1
    if not io.confirm(f"\n  Install {', '.join(p.repo for p in ready)} now? (this installs their Python/Node packages)", True):
        io.out("  Stopped. Nothing was installed.")
        return 0
    done = []
    for prep in ready:
        try:
            lz.install_and_save(prep)
            done.append(prep)
        except Exception as e:
            io.out(f"  installing {prep.repo} failed: {e}")
    if not done:
        return 1

    io.out("\nStep 3 of 4 - Inputs")
    for prep in done:
        io.out(f"\n  For {prep.owner}/{prep.repo}:")
        lz.collect(prep.slug)

    io.out("\nStep 4 of 4 - Start")
    if not io.confirm("  Everything is ready. Start now?", True):
        io.out("  OK. Start later with:  " + "  |  ".join(f"agent-launcher run {p.slug}" for p in done))
        return 0
    code = 0
    for prep in done:
        code |= lz.run(prep.slug, background=prep.plan.long_running)   # servers go to the background, jobs run in front
    io.out("\nDone. Useful commands:  agent-launcher list | show <name> | run <name> | set <name> <VAR> | stop <name> | remove <name>")
    return code
