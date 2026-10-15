#!/usr/bin/env python3
"""Install agent-bridge for Claude Code (Codex and Antigravity need no extra skill).

    python3 install.py                 # install for Claude Code (default)
    python3 install.py --uninstall     # remove it again
    python3 install.py --dest DIR      # copy into DIR/agent-bridge instead (another skills folder)
    python3 install.py --codex         # install into Codex instead/as well (experimental, unsupported)

Pure Python 3.8+ standard library; Windows, macOS and Linux. Safe to re-run: it replaces the
installed copy with this one (that is also how you update). Skills directories used:
    Claude Code  $CLAUDE_CONFIG_DIR/skills  (default ~/.claude/skills)
    Codex        ~/.agents/skills           (only with --codex)
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "agent-bridge"
HERE = Path(__file__).resolve().parent
SRC = HERE / "skills" / NAME
HOME = Path.home()

NEXT_STEPS = """
Next steps
  1. Start a NEW Claude Code session (restart or /clear) so it loads the skill.
  2. Get the Codex thread id: ask Codex "what is your thread id?", or run
       bridge.py threads --peer codex --limit 3
     For Antigravity, run bridge.py threads --peer antigravity, or use --thread new.
  3. Smoke test:
       bridge.py send --peer codex --thread <CODEX_ID> "Reply with the single word PONG. No tools."
     Or just tell Claude Code: "talk to codex thread <id>" and it will use the skill.
     Antigravity: bridge.py send --peer antigravity --thread new "Reply with the single word PONG."
  Codex may ask its own user to approve requests that change files; that is expected."""


def targets(a):
    if a.dest:
        return [("custom", Path(a.dest).expanduser() / NAME)]
    out = []
    if a.claude or not a.codex:
        out.append(("Claude Code", Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude") / "skills" / NAME))
    if a.codex:
        out.append(("Codex (experimental)", HOME / ".agents" / "skills" / NAME))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--claude", action="store_true", help="install for Claude Code (the default)")
    ap.add_argument("--codex", action="store_true", help="install into Codex instead/as well (experimental)")
    ap.add_argument("--dest", help="install into this skills folder instead")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-check", action="store_true", help="skip the doctor check after installing")
    a = ap.parse_args()

    if not (SRC / "SKILL.md").is_file() or not (SRC / "scripts" / "bridge.py").is_file():
        sys.exit(f"install.py: skill sources not found under {SRC}. Run it from a full copy of the repo.")

    done = []
    for label, dest in targets(a):
        if a.uninstall:
            if dest.exists():
                shutil.rmtree(dest)
                print(f"removed   {label}: {dest}")
            else:
                print(f"absent    {label}: {dest}")
            continue
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        print(f"installed {label}: {dest}")
        done.append((label, dest))

    if a.uninstall or not done:
        return
    if not a.no_check:
        bridge = done[0][1] / "scripts" / "bridge.py"
        for peer in ("codex", "antigravity"):
            print(f"\nPrerequisite check (doctor --peer {peer}); only your chosen peer needs to be ready:", flush=True)
            r = subprocess.run([sys.executable, "-B", str(bridge), "doctor", "--peer", peer])
            print(f"    => {'READY' if r.returncode == 0 else 'NOT READY (see check output above)'}")
    print(NEXT_STEPS)


if __name__ == "__main__":
    main()
