# agent-bridge

A skill that lets **Claude Code** talk to **Codex** or **Google Antigravity**:
send a prompt, read replies, and **listen** to a conversation live. Select
`--peer codex` or `--peer antigravity` in the same skill.
Pure Python 3.8+ standard library. Codex supports Windows, macOS and Linux;
Antigravity desktop is tested on Windows.

[![Watch the demo on YouTube: Claude Code talks to Codex](https://img.youtube.com/vi/W3lJTrcBMj0/maxresdefault.jpg)](https://www.youtube.com/watch?v=W3lJTrcBMj0)

**Watch the demo (about 4 minutes):** [I Made Claude Code Talk to Codex (Free Plugin)](https://www.youtube.com/watch?v=W3lJTrcBMj0). It shows why I connect the two agents and the full setup, with the setup starting at 1:04.

**Direction: Claude Code -> Codex / Antigravity.** Install it on the Claude Code side only; peers need no extra skill.
Codex -> Claude Code is not supported (see the end).

## Install (new user, two minutes)

You need **Python 3.8+**, **Claude Code**, and your chosen peer (signed in,
on the same computer and user account). The installer checks both peers;
only the one you use needs to be ready.

```bash
git clone https://github.com/DoctrNikhil/claude-codex-bridge.git   # or download the ZIP and unzip it
cd claude-codex-bridge
python3 install.py                         # Windows: py -3 install.py
```

This copies the skill to `~/.claude/skills/agent-bridge/` (or `$CLAUDE_CONFIG_DIR/skills/`) and runs the
prerequisite check. Re-running updates it; `--uninstall` removes it; `--dest DIR` copies it into any other skills folder.

Then:

1. **Start a new Claude Code session** so it loads the skill.
2. Fix anything marked `FAIL` in the check output; each line says how.
3. Get the Codex thread id (ask Codex "what is your thread id?", or `bridge.py threads --peer codex`).
4. Tell Claude Code in plain words, e.g. *"talk to codex thread 01a1…, ask it to summarise its last turn"*
   or *"listen to codex thread 01a1… and tell me when it finishes"*. The skill does the rest.

## Install from the Claude plugin marketplace (no script)

In Claude Code (terminal or the Claude desktop app's Code tab), run:

```
/plugin marketplace add DoctrNikhil/claude-codex-bridge
/plugin install agent-bridge@claude-codex-bridge
```

On Claude Code 2.1.275 or later one step also works: `/plugin install agent-bridge --marketplace DoctrNikhil/claude-codex-bridge`.
Then start a new session. The same two
commands work in a terminal as `claude plugin marketplace add ...` and `claude plugin install ...`.
Marketplace installs get the skill; to run the prerequisite check, use the skill's `scripts/bridge.py doctor --peer codex`.

To refresh an installed plugin after an update:

```text
claude plugin marketplace update claude-codex-bridge
claude plugin update agent-bridge@claude-codex-bridge
```

Restart Claude Code afterward. Plugin installations load a versioned cache;
editing or reinstalling a separate `~/.claude/skills/agent-bridge` copy does not
update that cache. Use the official commands above, not manual cache edits.
`claude plugin list --json` reports the enabled version and install path.

## Prerequisites at a glance

| Need | Check / fix |
|---|---|
| Python 3.8+ | `python3 --version` (Windows: `py -3 --version`) |
| Codex installed and signed in | `codex login status`; else `codex login` |
| Codex with the `queue` command | `codex queue --help`; else `codex update` |
| Codex app (or its background server) running | open the Codex app |
| Codex session logs in `~/.codex/sessions` | start Codex once |
| Claude Code allowed to run Python and read `~/.codex` | allow the permission prompt |

`bridge.py doctor --peer codex [--thread ID]` checks all of this and prints a fix for each failure.
The full table, delivery details and safety rules are in `skills/agent-bridge/SKILL.md`.

## Commands

```
bridge.py doctor  --peer codex [--thread ID]            # prerequisite checklist
bridge.py threads --peer codex                          # find a thread id
bridge.py send    --peer codex --thread ID "question"   # message + wait for the reply
bridge.py listen  --peer codex --thread ID              # stream new messages (listen mode)
bridge.py read    --peer codex --thread ID              # recent messages
```

`bridge.py` is at `scripts/bridge.py` inside the installed skill folder. Use `python3`, `python` or `py -3`.

## How it works

- Sending uses `codex queue`, which hands the message to the running Codex app, so it works even while
  the Codex desktop app has the thread open. `codex exec resume` is the fallback for idle threads.
- Listen/read only tail Codex's session log on disk; they never call the Codex CLI or touch credentials.
- Codex asks its own user for approval before acting on a bridged request that changes files.

## Antigravity peer

Use the same installed entry point:

```text
bridge.py doctor --peer antigravity
bridge.py threads --peer antigravity
bridge.py send --peer antigravity --thread new "Reply with the single word PONG."
bridge.py send --peer antigravity --thread ID "Your task"
bridge.py read --peer antigravity --thread ID --last 10
bridge.py listen --peer antigravity --thread ID --only assistant --timeout 900
```

On Windows without a real `python` on PATH, prefix commands with
`uv run --python 3.12 python` and the quoted full path to `scripts/bridge.py`.
Send waits by default; `--no-wait` returns after delivery confirmation.
Capture the `sent` event's `thread` and `index`; a later listener can use
`--after INDEX --until-reply` to recover a reply that already finished.
The final `turn_complete` event contains `reply`.

Antigravity must be running and signed in, with a localhost debugging port
for sending. Auto-discovery uses `DevToolsActivePort` under its app profile
(`~/AppData/Roaming/Antigravity` on Windows). If unavailable, quit the app
normally and restart its executable with `--remote-debugging-port=0`.
Conversation databases live under `~/.gemini/antigravity` and are opened
read-only. Read/listen can work with the app closed. No API keys, auth files,
cookies, process command lines, or CSRF token values are read.

Sending uses the existing chat controls through localhost CDP. It refuses
busy chats and unsent drafts, never approves tools, and may change the selected
conversation. A fresh chat uses current defaults outside a project; choose an
existing project chat for project work. Avoid editing the window during a
submission. After a timeout, read/listen instead of resending automatically.
UI labels and protobuf fields can change in future app versions. Legacy IDE
builds and the separate cloud SDK are not validated.

Options, recovery, and safety details:
[`references/antigravity.md`](skills/agent-bridge/references/antigravity.md).

## Tests

```bash
python3 -B -m unittest discover -s tests -v
```

Tests use synthetic messages and temporary databases, never real conversations
or credentials. No application or network access is needed for offline tests.

## Codex -> Claude Code (not supported)

In a normal Codex sandbox, Codex can't reach Claude Code: the sandbox blocks the network that `claude -p`
needs and blocks writes to the mailbox folder, so every call would need manual elevated approval. The
code is still in `bridge.py` and the findings are in `skills/agent-bridge/references/codex-to-claude-experimental.md`.
