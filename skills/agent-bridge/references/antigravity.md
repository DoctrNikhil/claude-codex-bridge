# Antigravity peer

Use the same `scripts/bridge.py` entry point with `--peer antigravity`.
Sending drives the running Electron desktop chat through localhost Chrome
DevTools Protocol (CDP); reading and listening open SQLite databases read-only.
No extra Python packages, API keys, auth files, cookies, or CSRF values are read.
The native `agentapi` command requires CSRF authentication and is unused.

## Prerequisites

- Python 3.8+; tested on Windows with Python 3.12. Use
  `uv run --python 3.12 python "<SKILL_DIR>/scripts/bridge.py" ...` if PATH points
  at Microsoft Store Python stubs.
- Antigravity desktop installed, running, and signed in for sending.
- A local debugging port. The bridge reads `DevToolsActivePort` under the app
  profile, normally `~/AppData/Roaming/Antigravity` on Windows. If absent,
  quit Antigravity normally and restart its executable with
  `--remote-debugging-port=0`. Do not force-kill unsaved work. Keep it local.
- Conversation databases under `~/.gemini/antigravity`. Read/listen can work
  while the app is closed. Claude needs permission to read these files and
  connect to localhost for sending/doctor.

`doctor --peer antigravity` returns `ready`/`ok` and an exit status. Use
`--thread ID` to also verify that conversation. Each command accepts
`--data-dir`, `--profile-dir`, `--port`, and `--target`; use a page ID printed by
doctor if multiple Antigravity windows are present. macOS/Linux profile defaults
are provided, but the Antigravity integration is only validated on Windows.
Legacy IDE versions and the separate cloud SDK are not validated.

## Commands

```text
bridge.py doctor --peer antigravity [--thread ID]
bridge.py threads --peer antigravity [--limit 10]
bridge.py send --peer antigravity --thread ID "prompt" [--timeout 600]
bridge.py send --peer antigravity --thread new "prompt"
bridge.py send --peer antigravity --thread ID - [--no-wait]
bridge.py read --peer antigravity --thread ID [--last 10] [--all] [--only assistant]
bridge.py listen --peer antigravity --thread ID [--until-reply] [--timeout 900]
bridge.py listen --peer antigravity --thread ID --after INDEX --until-reply
```

`ID` can be a UUID, a unique prefix, or `latest`. Prefer an explicit ID.
`new` is send-only: it opens an outside-of-project conversation using current
model/permission defaults. For project tasks, use an existing project chat.
`-` reads a UTF-8 stdin prompt, including multiple lines; `--dry-run` validates
the request without connecting to or changing the UI. Antigravity supports
`--via auto` only, and does not accept CLI `--extra` arguments.

Send waits by default. A `sent` event confirms the exact prompt was committed
to SQLite; capture its `thread` and step `index`. The final `turn_complete`
event includes `reply` and `text`. `--no-wait` returns after delivery; `--wait`
explicitly requests the default behavior. Unlike Codex's tagged messages,
Antigravity receives the prompt unchanged. `conversation_id` aliases `thread`.

Read returns the last N persisted user/assistant messages, excluding reasoning
and tool data. `--all` also includes partial visible replies; `--after INDEX`
filters by database step index. `--max-chars` truncates displayed text only.

Listen polls every `--interval` seconds (default 1), emitting new/changed
messages with `complete: false` while streaming. It starts after the current
last step unless `--after INDEX` or `--from-start` is supplied. After sending
without waiting, use `--after <sent.index>` to avoid missing a fast reply.
`--until-reply` ends after a completed assistant reply and idle status, not an
intermediate comment. `--from-start` can satisfy it with a historical reply.
Timeout 0 listens indefinitely. Ctrl+C stops it.

## Recovery and boundaries

- Sending can change the selected chat. CDP focus emulation supports hidden
  windows without stealing OS focus; avoid manually editing during submission.
- Busy/unknown-status targets and unsent drafts are refused. One submission
  per app profile is allowed at a time. Tool permission prompts are never
  approved automatically, and permission presets are unchanged.
- After `sent`, a wait timeout means read/listen to the existing turn instead
  of resending. `submission_uncertain` means Send was clicked but persistence
  was not confirmed: inspect the app before retrying. A tool approval, agent
  error, or turn without a text reply can time out.
- Reply updates are limited to what SQLite has persisted, not every token.
  UI labels and protobuf fields may change with application versions. Report
  failures rather than guessing controls or extracting authentication material.
