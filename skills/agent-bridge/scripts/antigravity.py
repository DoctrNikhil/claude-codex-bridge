#!/usr/bin/env python3
"""Antigravity peer for agent-bridge: localhost chat UI and read-only SQLite."""
from contextlib import closing
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import urllib.parse

from cdp import BridgeError, CDP, get_json

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def emit(value, stream=None):
    value = dict(value, peer="antigravity")
    if "conversation_id" in value:
        value["thread"] = value["conversation_id"]
    if value.get("event") == "sent":
        value.update(sent=True, via="cdp-ui")
    elif value.get("event") == "turn_complete" and "text" in value:
        value.update(sent=True, via="cdp-ui", reply=value["text"])
    print(json.dumps(value, ensure_ascii=False), file=stream or sys.stdout, flush=True)


def readonly(path):
    if not path.is_file():
        raise BridgeError(f"Conversation database not found: {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def varint(blob, pos):
    value = shift = 0
    while pos < len(blob) and shift < 70:
        byte = blob[pos]
        pos += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, pos
        shift += 7
    raise BridgeError("Invalid protobuf varint in conversation database")


def fields(blob):
    """Parse wire fields without generated protobuf classes or third-party packages."""
    pos = 0
    while pos < len(blob):
        tag, pos = varint(blob, pos)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise BridgeError("Invalid protobuf field in conversation database")
        if wire == 0:
            value, pos = varint(blob, pos)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, pos = varint(blob, pos)
            else:
                size = 8 if wire == 1 else 4
            if pos + size > len(blob):
                raise BridgeError("Truncated protobuf field in conversation database")
            value = blob[pos:pos + size]
            pos += size
        else:
            raise BridgeError(f"Unsupported protobuf wire type: {wire}")
        yield number, value


def timestamp(metadata):
    try:
        stamp = dict(fields(dict(fields(metadata))[1]))
        seconds = stamp.get(1, 0) + stamp.get(2, 0) / 1e9
        return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError, BridgeError):
        return None


class Store:
    def __init__(self, root):
        self.root = Path(root)

    def summaries(self):
        with closing(readonly(self.root / "conversation_summaries.db")) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT conversation_id,title,preview,step_count,last_modified_time,"
                "status,not_fully_idle,killed,workspace_uris FROM conversation_summaries "
                "ORDER BY last_modified_time DESC")]

    def resolve(self, name):
        rows = self.summaries()
        if name == "latest":
            if not rows:
                raise BridgeError("No conversations found. Use send --thread new PROMPT")
            return rows[0]["conversation_id"]
        if not re.fullmatch(r"[0-9a-f-]{4,36}", name):
            raise BridgeError("--thread must be a UUID, unique UUID prefix, latest, or new (send only)")
        hits = {r["conversation_id"] for r in rows if r["conversation_id"].startswith(name)}
        if UUID.fullmatch(name) and (self.root / "conversations" / f"{name}.db").is_file():
            hits.add(name)
        if len(hits) != 1:
            raise BridgeError(f"Conversation prefix matched {len(hits)} conversations; use list and an explicit ID")
        return hits.pop()

    def summary(self, ident):
        return next((r for r in self.summaries() if r["conversation_id"] == ident), None)

    def idle(self, ident):
        row = self.summary(ident)
        return bool(row and row["status"] == "CASCADE_RUN_STATUS_IDLE" and not row["not_fully_idle"])

    def path(self, ident):
        if not UUID.fullmatch(ident):
            raise BridgeError("Invalid conversation ID")
        return self.root / "conversations" / (ident + ".db")

    def max_index(self, ident):
        with closing(readonly(self.path(ident))) as conn:
            row = conn.execute("SELECT MAX(idx) FROM steps").fetchone()
            return row[0] if row[0] is not None else -1

    def messages(self, ident, after=-1):
        with closing(readonly(self.path(ident))) as conn:
            rows = conn.execute(
                "SELECT idx,step_type,status,metadata,step_payload,step_format "
                "FROM steps WHERE step_type IN (14,15) AND idx>? ORDER BY idx", (after,)).fetchall()
        out = []
        for row in rows:
            if row["step_format"] != 0:
                raise BridgeError(f"Unsupported step format {row['step_format']}; bridge needs updating")
            user = row["step_type"] == 14
            outer = dict(fields(row["step_payload"] or b""))
            body = dict(fields(outer.get(19 if user else 20, b"")))
            text = body.get(2 if user else 1, b"").decode("utf-8", errors="replace")
            # Assistant field 3 is reasoning; only field 1 is the visible reply.
            if text:
                out.append({"event": "message", "conversation_id": ident, "index": row["idx"],
                            "role": "user" if user else "assistant", "text": text,
                            "complete": row["status"] == 3, "ts": timestamp(row["metadata"] or b"")})
        return out


def discover(profile, port=None):
    if port is None:
        try:
            port = int((Path(profile) / "DevToolsActivePort").read_text().splitlines()[0])
        except (OSError, ValueError, IndexError):
            raise BridgeError("No usable DevToolsActivePort. Start Antigravity with --remote-debugging-port=0, or pass --port")
    if not 1 <= port <= 65535:
        raise BridgeError("Invalid debugging port")
    try:
        targets = get_json(f"http://127.0.0.1:{port}/json/list")
    except (OSError, ValueError) as exc:
        raise BridgeError("Antigravity debugging endpoint is unavailable. Open Antigravity with --remote-debugging-port=0") from exc
    pages = [t for t in targets if t.get("type") == "page" and
             "Antigravity" in t.get("title", "") and
             urllib.parse.urlsplit(t.get("url", "")).hostname in ("127.0.0.1", "localhost", "::1")]
    return port, pages


class SendLock:
    """OS lock shared by send processes; released even if a process crashes."""
    def __init__(self, profile):
        key = hashlib.sha256(str(Path(profile).resolve()).casefold().encode()).hexdigest()[:20]
        self.path = Path(tempfile.gettempdir()) / ("antigravity-bridge-" + key + ".lock")

    def __enter__(self):
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise BridgeError("Another bridge send is using this Antigravity profile; retry after it finishes submitting")
        return self

    def __exit__(self, *args):
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


class App:
    def __init__(self, profile, port=None, target=None):
        self.port, pages = discover(profile, port)
        if target:
            pages = [t for t in pages if t["id"] == target]
        if len(pages) != 1:
            raise BridgeError(f"Found {len(pages)} Antigravity chat pages. Use --target with a page ID from doctor")
        self.target = pages[0]
        self.cdp = CDP(self.target["webSocketDebuggerUrl"])

    def close(self):
        self.cdp.close()

    def state(self):
        return self.cdp.evaluate("""(()=>{
          const e=document.querySelector('[contenteditable="true"][aria-label="Message input"]');
          const b=document.querySelector('button[aria-label="Send message"]');
          return {url:location.href,title:document.title,input:!!e,
            draft:e?(e.innerText||'').replace(/\\r\\n/g,'\\n'):'',
            send:!!b&&!b.disabled,stop:!!document.querySelector('[data-tooltip-id="input-send-button-cancel-tooltip"]')};
        })()""")

    def prepare(self, ident, deadline):
        # Lexical's UI update callbacks stall when an Electron window is hidden.
        # This emulates page focus only for this debugging connection; no OS focus stealing.
        self.cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        state = self.state()
        if state["draft"].strip():
            raise BridgeError("The visible chat has an unsent draft; it was left untouched")
        parsed = urllib.parse.urlsplit(state["url"])
        if ident == "new":
            clicked = self.cdp.evaluate("""(()=>{const b=[...document.querySelectorAll('button[aria-label="New Conversation"]')].find(e=>e.getBoundingClientRect().width>0);if(!b)return false;b.click();return true})()""")
            if not clicked:
                raise BridgeError("New Conversation button not found; app UI may have changed")
        elif parsed.path.rstrip("/") != "/c/" + ident:
            url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/c/" + ident, parsed.query, ""))
            self.cdp.call("Page.navigate", {"url": url})
        while time.monotonic() < deadline:
            state = self.state()
            path = urllib.parse.urlsplit(state["url"]).path.rstrip("/")
            if state["input"] and (ident == "new" and not re.search(r"/c/[0-9a-f-]{36}$", path)
                                   or path == "/c/" + ident):
                if state["draft"].strip():
                    raise BridgeError("Target chat contains an unsent draft; it was left untouched")
                if state["stop"]:
                    raise BridgeError("Target chat is generating a response; wait until it is idle")
                return
            time.sleep(0.2)
        raise BridgeError("Timed out opening target chat; no prompt was sent")

    def submit(self, text):
        # Navigation recreates the renderer document; reapply focus after it.
        self.cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        state = self.state()
        if state["draft"].strip() or not state["input"] or state["stop"]:
            raise BridgeError("Chat is not ready or has an unsent draft; no prompt was sent")
        self.cdp.evaluate("document.querySelector('[contenteditable=\"true\"][aria-label=\"Message input\"]').focus()")
        self.cdp.call("Input.insertText", {"text": text})
        # A normal key cycle also wakes editor change listeners in hidden windows.
        self.cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "End", "code": "End", "windowsVirtualKeyCode": 35})
        self.cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "End", "code": "End", "windowsVirtualKeyCode": 35})
        for _ in range(80):
            if self.state()["send"]:
                break
            time.sleep(0.1)
        state = self.state()
        if state["draft"].strip() != text.strip():
            raise BridgeError("Inserted prompt does not match the draft. Draft left in place; no prompt was sent")
        clicked = self.cdp.evaluate("""(()=>{const b=document.querySelector('button[aria-label="Send message"]');if(!b||b.disabled)return false;b.click();return true})()""")
        if not clicked:
            raise BridgeError("Send button unavailable. Draft left in place; no prompt was sent")

    def conversation_id(self):
        path = urllib.parse.urlsplit(self.state()["url"]).path
        match = re.fullmatch(r"/c/([0-9a-f-]{36})/?", path)
        return match.group(1) if match and UUID.fullmatch(match.group(1)) else None


def wait_reply(store, ident, after, deadline, poll):
    emitted = set()
    idle_since = None
    last_index = None
    while time.monotonic() < deadline:
        messages = store.messages(ident, after)
        for message in messages:
            if message["role"] == "assistant" and message["complete"] and message["index"] not in emitted:
                emit(message)
                emitted.add(message["index"])
        index = store.max_index(ident)
        if emitted and store.idle(ident):
            if idle_since is None or last_index != index:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= 0.5:
                reply = next(m for m in reversed(messages) if m["role"] == "assistant" and m["complete"])
                emit({"event": "turn_complete", "conversation_id": ident,
                      "index": reply["index"], "text": reply["text"]})
                return
        else:
            idle_since = None
        last_index = index
        time.sleep(poll)
    raise BridgeError(f"Timed out waiting for reply. Prompt was submitted to {ident}; use read/listen, do not resend automatically")


def send(args, store):
    with SendLock(args.profile_dir):
        ident, index, deadline = send_locked(args, store)
    if args.wait:
        wait_reply(store, ident, index, deadline, args.poll)


def send_locked(args, store):
    text = sys.stdin.read() if args.message == "-" else args.message
    if not text or not text.strip():
        raise BridgeError("Prompt must not be empty")
    if "\x00" in text:
        raise BridgeError("Prompt must not contain NUL characters")
    ident = "new" if args.thread == "new" else store.resolve(args.thread)
    if ident != "new" and not store.idle(ident):
        raise BridgeError("Target conversation is busy or has unknown status; no prompt was sent")
    baseline = -1 if ident == "new" else store.max_index(ident)
    deadline = time.monotonic() + args.timeout
    app = App(args.profile_dir, args.port, args.target)
    submitted = False
    try:
        app.prepare(ident, min(deadline, time.monotonic() + 20))
        app.submit(text)
        submitted = True
        # Confirm the actual committed user message instead of assuming a click succeeded.
        while time.monotonic() < deadline:
            if ident == "new":
                ident = app.conversation_id() or "new"
            if ident != "new" and store.path(ident).is_file():
                matches = [m for m in store.messages(ident, baseline)
                           if m["role"] == "user" and m["text"].strip() == text.strip()]
                if matches:
                    user = matches[-1]
                    emit({"event": "sent", "conversation_id": ident, "index": user["index"],
                          "route": "cdp-ui", "confirmed": True})
                    break
            time.sleep(args.poll)
        else:
            raise BridgeError("Send was clicked, but database confirmation timed out; check the app before retrying")
    except Exception:
        if submitted:
            emit({"event": "submission_uncertain", "conversation_id": ident,
                  "detail": "Send was clicked. Check read/listen or the app before retrying"}, sys.stderr)
        raise
    finally:
        app.close()
    return ident, user["index"], deadline


def listen(args, store):
    ident = store.resolve(args.thread)
    baseline = args.after if args.after is not None else (-1 if args.from_start else store.max_index(ident))
    seen = {}
    assistant_index = None
    idle_since = None
    last_index = None
    deadline = time.monotonic() + args.timeout if args.timeout else float("inf")
    emit({"event": "listening", "conversation_id": ident, "after": baseline})
    while time.monotonic() < deadline:
        for message in store.messages(ident, baseline):
            key = message["index"]
            signature = (hashlib.sha256(message["text"].encode()).hexdigest(), message["complete"])
            if signature == seen.get(key):
                continue
            seen[key] = signature
            if (not args.only or args.only == message["role"]) and not (
                    message["role"] == "user" and message["text"].startswith("[agent-bridge]")
                    and not args.include_bridge):
                emit(clipped(message, args.max_chars))
            if message["role"] == "assistant" and message["complete"]:
                assistant_index = key
        index = store.max_index(ident)
        if args.until_reply and assistant_index is not None and store.idle(ident):
            if idle_since is None or last_index != index:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= 0.5:
                emit({"event": "turn_complete", "conversation_id": ident, "index": assistant_index})
                return
        else:
            idle_since = None
        last_index = index
        time.sleep(args.poll)
    emit({"event": "timeout", "conversation_id": ident})
    if args.until_reply:
        raise BridgeError("No completed reply before listen timeout")


def doctor(args, store):
    report = {"event": "doctor", "route": "cdp-ui+sqlite", "data_dir": str(store.root),
              "profile_dir": str(args.profile_dir), "python": sys.version.split()[0],
              "credentials_accessed": False, "ready": False,
              "native_cli": "unused: bundled agentapi requires CSRF authentication on the tested build"}
    try:
        rows = store.summaries()
        report["conversation_count"] = len(rows)
        if rows:
            report["readable_messages_in_latest"] = len(store.messages(rows[0]["conversation_id"]))
        report["database_ready"] = True
        if args.thread:
            ident = store.resolve(args.thread)
            report["thread"] = ident
            report["thread_messages"] = len(store.messages(ident))
    except (OSError, sqlite3.Error, BridgeError) as exc:
        report["database_error"] = str(exc)
    app = None
    try:
        port, pages = discover(args.profile_dir, args.port)
        report["pages"] = [{"id": t["id"], "title": t["title"]} for t in pages]
        app = App(args.profile_dir, args.port, args.target)
        report.update({"debug_port": app.port, "page_id": app.target["id"], "page_title": app.target["title"],
                       "input_available": app.state()["input"], "ui_ready": True})
    except (OSError, ValueError, BridgeError) as exc:
        report["ui_error"] = str(exc)
    finally:
        if app:
            app.close()
    report["ready"] = bool(report.get("database_ready") and not report.get("database_error") and report.get("ui_ready") and report.get("input_available"))
    report["ok"] = report["ready"]
    report["summary"] = "READY" if report["ready"] else "NOT READY"
    emit(report)
    return 0 if report["ready"] else 1


def clipped(message, limit):
    if limit and len(message.get("text", "")) > limit:
        return dict(message, text=message["text"][:limit] + "... [use read for full text]")
    return message


def default_profile():
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Roaming" / "Antigravity"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Antigravity"
    return Path.home() / ".config" / "Antigravity"


def dispatch(args):
    """Adapt the shared CLI without changing the Codex/experimental Claude paths."""
    args.data_dir = args.data_dir or Path.home() / ".gemini" / "antigravity"
    args.profile_dir = args.profile_dir or default_profile()
    store = Store(args.data_dir)
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8-sig")
    try:
        if getattr(args, "interval", 1) <= 0 or not getattr(args, "interval", 1) < float("inf"):
            raise BridgeError("--interval must be finite and positive")
        if getattr(args, "timeout", 0) < 0:
            raise BridgeError("--timeout must be nonnegative")
        if getattr(args, "max_chars", 0) < 0:
            raise BridgeError("--max-chars must be nonnegative")
        args.poll = getattr(args, "interval", 0.5)
        if args.cmd == "doctor":
            return doctor(args, store)
        if args.cmd == "threads":
            if args.limit < 0:
                raise BridgeError("--limit must be nonnegative")
            for row in store.summaries()[:args.limit]:
                emit({"event": "conversation", **row})
        elif args.cmd == "read":
            if args.last < 0:
                raise BridgeError("--last must be nonnegative")
            ident = store.resolve(args.thread)
            messages = store.messages(ident, args.after if args.after is not None else -1)
            if not args.all:
                messages = [m for m in messages if m["complete"]]
            if args.only:
                messages = [m for m in messages if m["role"] == args.only]
            for message in messages[-args.last:] if args.last else []:
                emit(clipped(message, args.max_chars))
        elif args.cmd == "send":
            if args.via != "auto" or args.extra:
                raise BridgeError("Antigravity uses its chat UI; --via must be auto and --extra is unsupported")
            if args.timeout <= 0:
                raise BridgeError("--timeout must be positive for send")
            args.message = sys.stdin.read() if args.message == "-" else args.message
            if not args.message.strip() or "\x00" in args.message:
                raise BridgeError("Prompt must be nonempty and contain no NUL characters")
            if args.dry_run:
                ident = "new" if args.thread == "new" else store.resolve(args.thread)
                emit({"dry_run": True, "thread": ident, "via": "cdp-ui", "message_chars": len(args.message)})
            else:
                args.wait = not args.no_wait
                send(args, store)
        elif args.cmd == "listen":
            listen(args, store)
        else:
            raise BridgeError("Antigravity supports doctor, threads, send, read, and listen")
        return 0
    except (OSError, ValueError, sqlite3.Error, BridgeError) as exc:
        emit({"event": "error", "error": str(exc)}, sys.stderr)
        return 2
    except KeyboardInterrupt:
        emit({"event": "interrupted"}, sys.stderr)
        return 130
