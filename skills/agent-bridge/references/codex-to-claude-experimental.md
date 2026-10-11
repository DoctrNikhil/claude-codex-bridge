# Codex -> Claude Code (experimental, not supported)

Status: **does not work in a normal Codex sandbox.** Kept for reference only. The main skill
(`SKILL.md`) covers Claude Code -> Codex.

## What was found

Tested on Windows with the Codex desktop app (sandboxed commands):

- `claude -p --resume <id>` from the Codex sandbox failed with `ENOTFOUND`: the sandbox has no network,
  so the Claude CLI can't reach its API.
- The mailbox fallback (`~/.agent-bridge/inbox/...`) was denied: the sandbox can't write outside the
  workspace.
- With elevated access (approved by the Codex user per command) both routes worked.
- Codex also asks its user for approval before acting on a bridged request that changes things.

So a Codex agent cannot message Claude Code on its own; every send needs a human approval.

## What exists in `bridge.py` (unsupported)

- `send --peer claude --thread ID "msg"`: runs `claude -p --resume ID` from the session's original
  folder if the session is idle. Needs a signed-in `claude` CLI. If a Claude session is live and running
  `inbox --listen` (it refreshes a heartbeat file), the message goes to its mailbox instead, because a
  running Claude session does not see turns appended by `claude -p --resume`.
- `inbox --peer claude --thread MY_ID [--listen]`: reads and waits on that mailbox. Under Claude Code's
  Monitor tool each message arrives as a notification. Replies are the session's next finished turn.
- `doctor --peer claude [--thread ID]`: checks the Claude CLI, sign-in, session logs, and whether a
  live mailbox listener exists.

## If you still want to try it

Install the skill folder into Codex (`python3 install.py --codex`) and run Codex with a sandbox that allows
network access and writes to `~/.agent-bridge` (or approve each call). Use at your own risk; the safety
rules in `SKILL.md` apply in the other direction too.
