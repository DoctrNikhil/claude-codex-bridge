#!/usr/bin/env python3
"""agent-bridge: let Claude Code message, read and listen to a Codex thread.

Supported direction: Claude Code -> Codex (--peer codex). The --peer claude / inbox paths exist but
are experimental and unsupported (Codex's sandbox blocks them; see references/).

Pure Python 3.8+ standard library. Works on Windows, macOS and Linux.

    bridge.py doctor   --peer P
    bridge.py threads  --peer P [--limit N]
    bridge.py read     --peer P --thread ID [--last N] [--all]
    bridge.py send     --peer P --thread ID  "message" | -   [--timeout S] [--dry-run]
    bridge.py listen   --peer P --thread ID  [--from-start] [--until-reply]
                                             [--only user|assistant] [--timeout S]

--peer names the agent you are TALKING TO:
    codex   -> sessions in $CODEX_HOME/sessions        (default ~/.codex)
    claude  -> sessions in $CLAUDE_CONFIG_DIR/projects (default ~/.claude)

--thread accepts a full id, a unique id prefix, or "latest".

send   runs the peer's own CLI non-interactively against that session
       (`codex exec resume <id> -` / `claude -p --resume <id>`), feeding the
       message on stdin, and prints the peer's reply as JSON.
listen tails the peer's on-disk session log and prints each new message as one
       JSON line. It never calls the peer's CLI, so it is safe at any time.

Output is always JSON lines on stdout; errors go to stderr with a non-zero exit.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

TAG = "[agent-bridge]"          # prefix on messages we send, so echoes can be told apart
HOME = Path.home()
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")


# ----------------------------------------------------------------- helpers
def die(msg: str, code: int = 2):
    print(f"agent-bridge: {msg}", file=sys.stderr)
    sys.exit(code)


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def other(peer: str) -> str:
    return "claude" if peer == "codex" else "codex"


# ----------------------------------------------------------------- locations
def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or HOME / ".codex")


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")


def session_root(peer: str) -> Path:
    return codex_home() if peer == "codex" else claude_home()


def session_files(peer: str):
    if peer == "codex":
        root = codex_home()
        pats = [root / "sessions" / "**" / "rollout-*.jsonl",
                root / "archived_sessions" / "**" / "rollout-*.jsonl"]
    else:
        pats = [claude_home() / "projects" / "**" / "*.jsonl"]
    out = []
    for p in pats:
        out += glob.glob(str(p), recursive=True)
    if peer == "claude":   # sub-agent logs live in <session>/subagents/; they are not threads
        out = [f for f in out if UUID_RE.search(Path(f).stem)
               and "subagents" not in Path(f).parts]
    return sorted(out, key=os.path.getmtime, reverse=True)


def session_id_of(path: str) -> str:
    stem = Path(path).stem
    m = UUID_RE.search(stem)
    return m.group(1) if m else stem


def resolve_thread(peer: str, thread: str):
    files = session_files(peer)
    if not files:
        die(f"no {peer} session logs found under {session_root(peer)}. "
            "Set CODEX_HOME / CLAUDE_CONFIG_DIR if they live elsewhere.")
    if thread == "latest":
        return session_id_of(files[0]), files[0]
    hits = [f for f in files if session_id_of(f).startswith(thread)]
    if not hits:
        die(f"{peer} thread '{thread}' not found (try: threads --peer {peer}).")
    ids = {session_id_of(f) for f in hits}
    if len(ids) > 1:
        die(f"'{thread}' is ambiguous: {sorted(ids)[:5]}")
    return session_id_of(hits[0]), hits[0]


def session_cwd(peer: str, path: str):
    """Working directory the session was started in. Claude Code looks sessions up
    per project directory, so `claude --resume` only works when run from there."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 200:
                    break
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                cwd = (o.get("payload") or {}).get("cwd") if peer == "codex" else o.get("cwd")
                if cwd and Path(cwd).is_dir():
                    return cwd
    except OSError:
        pass
    return None


# ------------------------------------------------------------- log parsing
SKIP_PREFIXES = ("<environment_context", "<user_instructions", "<permissions",
                 "<system-reminder", "<turn_aborted", "# AGENTS.md",
                 "<collaboration_mode", "<skills_instructions", "<external_",
                 "<local-command", "<command-name", "<command-message")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text"):
            parts.append(c.get("text", ""))
    return "\n".join(parts)


def parse_line(peer: str, line: str):
    """Return dict(role, text, phase, ts) for a visible chat message, an
    {'event': ...} marker, or None.  phase == 'final_answer' marks the end-of-turn reply."""
    try:
        o = json.loads(line)
    except ValueError:
        return None
    ts = o.get("timestamp")
    if peer == "codex":
        p = o.get("payload") or {}
        kind = p.get("type")
        if o.get("type") == "response_item" and kind == "message":
            role = p.get("role")
            if role not in ("user", "assistant"):
                return None
            text = _text_of(p.get("content")).strip()
            if not text or text.startswith(SKIP_PREFIXES):
                return None
            return {"role": role, "text": text, "phase": p.get("phase") or "", "ts": ts}
        if o.get("type") == "event_msg" and kind == "task_started":
            return {"event": "turn_started", "ts": ts}
        if o.get("type") == "event_msg" and kind in ("task_complete", "turn_aborted"):
            return {"event": "turn_complete" if kind == "task_complete" else "turn_aborted",
                    "ts": ts, "text": p.get("last_agent_message") or ""}
        return None
    # claude
    if o.get("type") in ("user", "assistant") and isinstance(o.get("message"), dict):
        if o.get("isMeta") or o.get("isSidechain"):
            return None
        msg = o["message"]
        role = msg.get("role")
        text = _text_of(msg.get("content")).strip()
        if not text or text.startswith(SKIP_PREFIXES):
            return None
        final = role == "assistant" and msg.get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens")
        return {"role": role, "text": text, "phase": "final_answer" if final else "", "ts": ts}
    return None


def read_messages(peer: str, path: str):
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = parse_line(peer, line)
            if m and "role" in m:
                out.append(m)
    return out


def is_final(m) -> bool:
    return m.get("role") == "assistant" and m.get("phase") in ("final_answer",)


def clip(m, limit):
    if limit and len(m.get("text", "")) > limit:
        m = dict(m, text=m["text"][:limit] + f"... [+{len(m['text']) - limit} chars; use `read`]")
    return m


# ------------------------------------------------------------ CLI discovery
def _newest(globs):
    hits = []
    for g in globs:
        hits += glob.glob(g)
    hits = [h for h in hits if os.path.isfile(h)]
    return sorted(hits, key=os.path.getmtime, reverse=True)


def find_cli(peer: str, fatal: bool = True):
    name = "codex" if peer == "codex" else "claude"
    var = "CODEX_CLI_PATH" if peer == "codex" else "CLAUDE_CLI_PATH"
    env = os.environ.get(var)
    if env and Path(env).exists():
        return env
    w = shutil.which(name)
    if w:
        return w
    exe = ".exe" if os.name == "nt" else ""
    g = []
    if os.name == "nt":      # sandboxes often hide these variables; derive them from the home dir
        os.environ.setdefault("APPDATA", str(HOME / "AppData" / "Roaming"))
        os.environ.setdefault("LOCALAPPDATA", str(HOME / "AppData" / "Local"))
    if peer == "codex":
        la, ad = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
        if la:   # Codex desktop app (Windows)
            g.append(str(Path(la) / "OpenAI" / "Codex" / "bin" / "*" / f"codex{exe}"))
        if ad:
            g.append(str(Path(ad) / "OpenAI" / "Codex" / "bin" / "*" / f"codex{exe}"))
        g += [str(codex_home() / ".sandbox-bin" / f"codex{exe}"),
              "/Applications/Codex.app/Contents/Resources/codex",
              str(HOME / ".local" / "bin" / "codex"),
              str(HOME / ".npm-global" / "bin" / "codex"),
              "/opt/homebrew/bin/codex", "/usr/local/bin/codex"]
    else:
        ad, la = os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")
        if ad:   # Claude desktop app bundles Claude Code here (Windows)
            g.append(str(Path(ad) / "Claude" / "claude-code" / "*" / "*" / f"claude{exe}"))
        if la:
            g.append(str(Path(la) / "Programs" / "claude" / f"claude{exe}"))
        g += [str(HOME / "Library" / "Application Support" / "Claude" / "claude-code" / "*" / "claude"),
              str(HOME / ".config" / "Claude" / "claude-code" / "*" / "claude"),
              str(HOME / ".claude" / "local" / f"claude{exe}"),
              str(HOME / ".claude" / "local" / "node_modules" / ".bin" / "claude*"),
              str(HOME / ".local" / "bin" / f"claude{exe}"),
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    hits = _newest(g)
    if hits:
        return hits[0]
    if fatal:
        die(f"could not find the '{name}' CLI. Put it on PATH or set {var}.")
    return None


# ----------------------------------------------------------------- commands
def cmd_threads(a):
    for f in session_files(a.peer)[: a.limit]:
        first = next((m for m in read_messages(a.peer, f) if m["role"] == "user"), None)
        emit({"thread": session_id_of(f),
              "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(f))),
              "cwd": session_cwd(a.peer, f),
              "first_user_message": (first["text"][:100] if first else "")})


def cmd_read(a):
    tid, path = resolve_thread(a.peer, a.thread)
    msgs = read_messages(a.peer, path)
    if not a.all:
        msgs = [m for m in msgs if m["role"] == "user" or is_final(m)]
    for m in msgs[-a.last:]:
        emit({"thread": tid, **clip(m, a.max_chars)})


def tail(peer, path, pos, interval, deadline):
    """Yield parsed log entries appended after byte offset `pos`; yield
    {'event': 'timeout'} and stop at `deadline`."""
    buf = b""
    while True:
        size = os.path.getsize(path)
        if size < pos:               # file rotated/truncated
            pos, buf = 0, b""
        if size > pos:
            with open(path, "rb") as fh:
                fh.seek(pos)
                buf += fh.read()
                pos = fh.tell()
            *lines, buf = buf.split(b"\n")   # keep trailing partial line
            for raw in lines:
                m = parse_line(peer, raw.decode("utf-8", errors="replace"))
                if m:
                    yield m
        if deadline and time.time() > deadline:
            yield {"event": "timeout"}
            return
        time.sleep(interval)


def run(cmd, body, cwd, timeout, peer, tid):
    try:
        return subprocess.run(cmd, input=body, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        die(f"{peer} CLI did not finish within {timeout}s; it may still be working. "
            f"Check with: listen --peer {peer} --thread {tid} --until-reply", 3)


def send_codex(a, tid, path, cli, body, nonce, cwd):
    """Codex: `codex queue` talks to the running app-server, so it works even while the
    Codex desktop app has the thread open (exec resume fails with 'active writer').
    `exec resume` is the fallback for idle threads. Returns (via, reply)."""
    pos = os.path.getsize(path)
    deadline = time.time() + a.timeout
    tried = []
    if a.via in ("auto", "queue"):
        r = run([cli, "queue", "--thread", tid, "--message", body] + a.extra, None, cwd, 60, a.peer, tid)
        if r.returncode == 0:
            if a.no_wait:
                return "queue", None
            mine = False
            reply = None
            for m in tail(a.peer, path, pos, a.interval, deadline):
                if m.get("event") == "timeout":
                    die(f"queued, but no finished reply within {a.timeout}s. "
                        f"Keep watching: listen --peer codex --thread {tid} --until-reply", 3)
                if m.get("role") == "user" and nonce in m["text"]:
                    mine = True
                elif mine and is_final(m):
                    reply = m["text"]
                elif mine and m.get("event") == "turn_complete" and reply is not None:
                    return "queue", reply
                elif mine and m.get("event") == "turn_aborted":
                    return "queue", reply or "(turn aborted)"
        tried.append("queue: " + ((r.stderr or r.stdout or "").strip()[-300:] or f"exit {r.returncode}"))
        if a.via == "queue":
            die("codex queue failed: " + tried[-1], 4)
    out_file = os.path.join(tempfile.gettempdir(), f"agent-bridge-{os.getpid()}.txt")
    cmd = [cli, "exec", "resume", tid, "--skip-git-repo-check", "-o", out_file] + a.extra + ["-"]
    r = run(cmd, body, cwd, a.timeout, a.peer, tid)
    reply = ""
    if os.path.exists(out_file):
        reply = Path(out_file).read_text(encoding="utf-8", errors="replace").strip()
        os.unlink(out_file)
    reply = reply or (r.stdout or "").strip()
    if r.returncode != 0 and not reply:
        tried.append("exec resume: " + (r.stderr or "").strip()[-400:])
        die("could not deliver message to codex. " + " | ".join(tried), 4)
    return "exec", reply


def send_claude(a, tid, path, cli, body, nonce, cwd):
    """Claude Code. A session that is open and running does not see turns appended by
    `claude -p --resume` (that edits the log behind its back), so when a live listener is
    watching the mailbox we deliver there. Otherwise `claude -p --resume` runs the turn directly.
    Returns (via, reply)."""
    live = listener_alive("claude", tid)
    via = a.via if a.via in ("print", "inbox") else ("inbox" if live or not cli else "print")
    if via == "print":
        if not cli:
            die("claude CLI not found; set CLAUDE_CLI_PATH or use --via inbox.")
        r = run([cli, "-p", "--resume", tid] + a.extra, body, cwd, a.timeout, a.peer, tid)
        reply = (r.stdout or "").strip()
        if r.returncode == 0:
            return "print", reply
        why = (r.stderr or reply or f"exit {r.returncode}").strip()[-300:]
        if a.via == "print":
            die(f"claude CLI failed: {why}", 4)
        emit({"warning": "claude -p could not run (" + why + "); using the mailbox instead"})
    pos = os.path.getsize(path)
    where = send_inbox("claude", tid, body, nonce)
    if a.no_wait or not live:
        out = {"thread": tid, "sent": True, "via": "inbox", "msg": nonce, "file": where,
               "note": "left in the mailbox; no live listener right now. The session sees it when it runs "
                       "`inbox --peer claude --listen` (or `inbox`). Read its answer with `listen --peer claude`."}
        emit(out)
        sys.exit(0)
    deadline = time.time() + a.timeout     # live listener: wait for its next finished answer
    for m in tail("claude", path, pos, a.interval, deadline):
        if m.get("event") == "timeout":
            emit({"thread": tid, "sent": True, "via": "inbox", "msg": nonce, "file": where,
                  "note": f"delivered to a live listener but no finished answer within {a.timeout}s; "
                          "keep watching with `listen --peer claude --until-reply`"})
            sys.exit(0)
        if is_final(m):
            return "inbox", m["text"]


def cmd_send(a):
    tid, path = resolve_thread(a.peer, a.thread)
    cli = find_cli(a.peer, fatal=a.peer == "codex")
    body = a.message if a.message != "-" else sys.stdin.read()
    if not body.strip():
        die("empty message.")
    nonce = uuid.uuid4().hex[:8]
    body = f"{TAG} from {other(a.peer)} (msg {nonce}): {body}"
    before = len(read_messages(a.peer, path))
    cwd = session_cwd(a.peer, path)

    if a.dry_run:
        emit({"dry_run": True, "peer": a.peer, "via": a.via if a.peer == "codex" else ("inbox (live listener)" if listener_alive("claude", tid) else "claude -p --resume"),
              "cli": cli, "cwd": cwd, "message_chars": len(body)})
        return
    if a.peer == "codex":
        via, reply = send_codex(a, tid, path, cli, body, nonce, cwd)
    else:
        via, reply = send_claude(a, tid, path, cli, body, nonce, cwd)
    out = {"thread": tid, "sent": True, "via": via, "msg": nonce}
    if reply is not None:
        out["reply"] = reply
    else:
        out["note"] = "queued; read the answer with: listen --until-reply"
    emit(out)


def cmd_listen(a):
    tid, path = resolve_thread(a.peer, a.thread)
    pos = 0 if a.from_start else os.path.getsize(path)
    deadline = time.time() + a.timeout if a.timeout else None
    seen_final = False
    emit({"event": "listening", "thread": tid, "peer": a.peer, "file": path})
    for m in tail(a.peer, path, pos, a.interval, deadline):
        if "event" in m:
            emit({"thread": tid, **clip(m, a.max_chars)})
            if a.until_reply and seen_final and m["event"] in ("turn_complete", "turn_aborted"):
                return
            if m["event"] == "timeout":
                return
            continue
        if m["role"] == "user" and m["text"].startswith(TAG) and not a.include_bridge:
            continue                 # our own outgoing message echoed back
        if a.only and m["role"] != a.only:
            continue
        emit({"thread": tid, **clip(m, a.max_chars)})
        if a.until_reply and is_final(m):
            seen_final = True
            if a.peer == "claude":   # claude logs have no turn_complete event
                return


def inbox_dir(peer: str, tid: str) -> Path:
    root = Path(os.environ.get("AGENT_BRIDGE_HOME") or HOME / ".agent-bridge")
    return root / "inbox" / peer / tid


def send_inbox(peer, tid, body, nonce):
    """Drop a message in the recipient's mailbox. Used when the peer's CLI cannot be driven
    headlessly (e.g. `claude` is not logged in). The recipient reads it with `inbox`."""
    d = inbox_dir(peer, tid)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{time.strftime('%Y%m%dT%H%M%S')}-{nonce}.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"from": other(peer), "to": peer, "thread": tid, "msg": nonce,
                               "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "text": body},
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)       # atomic, so a reader never sees a half-written file
    return str(f)


HEARTBEAT = ".listening"
HEARTBEAT_FRESH = 15     # seconds: a listener that touched its heartbeat this recently is "live"


def listener_alive(peer: str, tid: str) -> bool:
    try:
        return time.time() - (inbox_dir(peer, tid) / HEARTBEAT).stat().st_mtime < HEARTBEAT_FRESH
    except OSError:
        return False


def cmd_inbox(a):
    """Receive messages other agents left for THIS session: --peer is the agent whose mailbox you
    read, i.e. yourself (a Claude Code session reads `--peer claude`, Codex reads `--peer codex`)."""
    tid, _ = resolve_thread(a.peer, a.thread)
    d = inbox_dir(a.peer, tid)
    done = d / ".read"
    deadline = time.time() + a.timeout if a.timeout else None
    emit({"event": "inbox_listening" if a.listen else "inbox_check", "thread": tid, "dir": str(d)})
    hb = d / HEARTBEAT
    try:
        while True:
            if a.listen:             # tells senders a live listener is here, so they use the mailbox
                d.mkdir(parents=True, exist_ok=True)
                hb.write_text(str(os.getpid()))
            if _inbox_pass(a, tid, d, done, deadline):
                return
            time.sleep(a.interval)
    finally:
        if a.listen:
            try:
                hb.unlink()
            except OSError:
                pass


def _inbox_pass(a, tid, d, done, deadline):
    """One sweep of the mailbox. Returns True when the caller should stop."""
    files = sorted(d.glob("*.json")) if d.is_dir() else []
    for f in files:
        try:
            msg = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        emit({"event": "inbox_message", **clip(msg, a.max_chars)})
        if not a.keep:
            done.mkdir(exist_ok=True)
            f.replace(done / f.name)
    if not a.listen:
        return True
    if deadline and time.time() > deadline:
        emit({"event": "timeout", "thread": tid})
        return True
    return False


def _probe(cmd, timeout=25):
    """Run a short CLI probe; return (returncode, combined output) or (None, error text)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)


def cmd_doctor(a):
    """Checklist of everything needed to talk to the peer. level: required (blocks the
    bridge) | optional (a fallback exists). Exit 0 when no required check failed."""
    checks = []

    def add(name, ok, detail="", fix="", level="required"):
        checks.append({"check": name, "ok": bool(ok), "level": level, "detail": detail,
                       **({"fix": fix} if (fix and not ok) else {})})

    peer = a.peer
    pname = "Codex" if peer == "codex" else "Claude Code"
    var = "CODEX_CLI_PATH" if peer == "codex" else "CLAUDE_CLI_PATH"
    add("python >= 3.8", sys.version_info >= (3, 8), sys.version.split()[0],
        "Install Python 3.8+ (python.org, your package manager, or `uv python install 3.12`).")

    cli = find_cli(peer, fatal=False)
    if peer == "codex":
        add("codex CLI found", cli, cli or "not found",
            f"Install Codex (app or CLI: https://developers.openai.com/codex) or set {var}=<path to codex>.")
    else:
        add("claude CLI found", cli, cli or "not found (sends will use the mailbox)",
            f"Install Claude Code (https://docs.claude.com/claude-code) or set {var}=<path to claude>. "
            "Without it the bridge still works through the mailbox.", level="optional")
    if cli:
        rc, out = _probe([cli, "--version"])
        add(f"{pname} CLI runs", rc == 0, out.splitlines()[0] if out else "", f"Reinstall {pname}.",
            level="required" if peer == "codex" else "optional")
        if peer == "codex":
            rc, out = _probe([cli, "login", "status"])
            add("codex logged in", rc == 0, out.splitlines()[0] if out else "",
                "Run `codex login` (or sign in inside the Codex app).")
            rc, _ = _probe([cli, "queue", "--help"])
            add("codex `queue` command (reaches threads open in the Codex app)", rc == 0,
                "available" if rc == 0 else "missing in this Codex version",
                "Update Codex (`codex update`). Without `queue` only idle threads can be reached, via exec resume.",
                level="optional")
        else:
            rc, out = _probe([cli, "auth", "status"])
            logged = False
            try:
                logged = bool(json.loads(out).get("loggedIn"))
            except ValueError:
                logged = rc == 0 and "not logged" not in out.lower()
            add("claude logged in (needed for live replies)", logged,
                "logged in" if logged else "not logged in -> only live sessions with a mailbox listener are reachable",
                "Run `claude auth login` once to reach idle/closed Claude sessions. A running Claude "
                "session is reachable without it if it runs `inbox --listen`.", level="optional")

    root = session_root(peer)
    n = len(session_files(peer))
    add(f"{pname} session logs found", n > 0, f"{n} sessions under {root}",
        f"Open {pname} and start a session once, or set {'CODEX_HOME' if peer == 'codex' else 'CLAUDE_CONFIG_DIR'} "
        "if it keeps data elsewhere.")

    if a.thread:
        try:
            tid, path = resolve_thread(peer, a.thread)
            add("thread exists", True, f"{tid}")
            if peer == "claude":
                live = listener_alive("claude", tid)
                add("session has a live mailbox listener", live,
                    "yes: sends go to its mailbox" if live else "no: sends use `claude -p --resume`",
                    "Only needed for a RUNNING session: have it run `inbox --peer claude --thread <id> --listen`.",
                    level="optional")
            cwd = session_cwd(peer, path)
            add("thread's original folder still exists", cwd is not None, cwd or "missing",
                "Claude resumes sessions from their original folder; recreate it or use the mailbox.",
                level="optional" if peer == "codex" else "required")
        except SystemExit:
            add("thread exists", False, f"'{a.thread}' not found",
                f"Run `threads --peer {peer}` and copy a valid id.")

    if peer == "claude":     # mailbox is only used by the unsupported Codex->Claude path
        mb = Path(os.environ.get("AGENT_BRIDGE_HOME") or HOME / ".agent-bridge")
        try:
            mb.mkdir(parents=True, exist_ok=True)
            probe = mb / ".write-test"
            probe.write_text("ok")
            probe.unlink()
            add("mailbox folder writable", True, str(mb), level="optional")
        except OSError as e:
            add("mailbox folder writable", False, f"{mb}: {e}", "Set AGENT_BRIDGE_HOME to a writable folder.",
                level="optional")

    ok = all(c["ok"] for c in checks if c["level"] == "required")
    emit({"peer": peer, "platform": sys.platform, "ok": ok, "checks": checks,
          "summary": "READY" if ok else "NOT READY: fix the failed required checks"})
    for c in checks:
        mark = "OK  " if c["ok"] else ("FAIL" if c["level"] == "required" else "warn")
        line = f"  [{mark}] {c['check']}: {c['detail']}"
        if "fix" in c:
            line += "\n         fix: " + c["fix"]
        print(line, file=sys.stderr)
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(name, thread=True, **kw):
        p = sub.add_parser(name, **kw)
        p.add_argument("--peer", choices=["codex", "claude"], required=True,
                       help="the agent you are talking to")
        if thread:
            p.add_argument("--thread", required=True, help="session id, unique prefix, or 'latest'")
        return p

    p = common("doctor", False)
    p.add_argument("--thread", help="optionally also verify this thread id is reachable")
    p.set_defaults(f=cmd_doctor)
    p = common("threads", False); p.add_argument("--limit", type=int, default=10); p.set_defaults(f=cmd_threads)
    p = common("read"); p.add_argument("--last", type=int, default=10)
    p.add_argument("--all", action="store_true", help="include commentary, not just final answers")
    p.add_argument("--max-chars", type=int, default=0, help="truncate each message (0 = no limit)")
    p.set_defaults(f=cmd_read)
    p = common("send"); p.add_argument("message", help="text, or '-' to read stdin")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--dry-run", action="store_true", help="show what would run, send nothing")
    p.add_argument("--via", choices=["auto", "queue", "exec", "print", "inbox"], default="auto",
                   help="codex: 'queue' (works while the Codex app has the thread open), 'exec' "
                        "(codex exec resume, idle threads only). claude: 'print' (claude -p --resume, "
                        "needs a logged-in claude CLI), 'inbox' (mailbox file). auto = best available")
    p.add_argument("--no-wait", action="store_true", help="codex queue only: return without waiting for the reply")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--extra", nargs=argparse.REMAINDER, default=[],
                   help="extra args passed to the peer CLI (must be last), e.g. --extra --permission-mode acceptEdits")
    p.set_defaults(f=cmd_send)
    p = common("listen")
    p.add_argument("--from-start", action="store_true", help="replay history first")
    p.add_argument("--until-reply", action="store_true", help="exit after the peer's next final answer")
    p.add_argument("--only", choices=["user", "assistant"], help="filter by role")
    p.add_argument("--include-bridge", action="store_true", help="also show messages sent via this bridge")
    p.add_argument("--timeout", type=int, default=0, help="seconds, 0 = forever")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--max-chars", type=int, default=4000, help="truncate each message (0 = no limit)")
    p.set_defaults(f=cmd_listen)

    p = common("inbox")
    p.add_argument("--listen", action="store_true", help="keep watching for new messages")
    p.add_argument("--keep", action="store_true", help="do not mark messages as read")
    p.add_argument("--timeout", type=int, default=0, help="seconds, 0 = forever (listen only)")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--max-chars", type=int, default=4000)
    p.set_defaults(f=cmd_inbox)

    a = ap.parse_args()
    try:
        a.f(a)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    main()
