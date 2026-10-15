"""Small dependency-free localhost Chrome DevTools Protocol client."""
import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request


class BridgeError(RuntimeError):
    pass


def local_url(url, schemes=("http", "ws")):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in schemes or p.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise BridgeError("Only localhost Antigravity endpoints are allowed")
    if p.username or p.password:
        raise BridgeError("Credentials in endpoint URLs are not allowed")
    return p


def get_json(url, timeout=5):
    local_url(url, ("http",))
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise BridgeError("Unexpected redirect from localhost debugging endpoint")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=timeout) as r:
        return json.load(r)


class CDP:
    def __init__(self, url, timeout=15):
        p = local_url(url)
        self.timeout = timeout
        self.sock = socket.create_connection((p.hostname, p.port or 80), timeout)
        self.buffer = bytearray()
        self.seq = 0
        key = base64.b64encode(os.urandom(16)).decode()
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {p.netloc}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                self.close()
                raise BridgeError("Antigravity closed the WebSocket handshake")
            self.buffer.extend(chunk)
            if len(self.buffer) > 65536:
                raise BridgeError("Invalid WebSocket handshake")
        head, rest = bytes(self.buffer).split(b"\r\n\r\n", 1)
        self.buffer = bytearray(rest)
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if b" 101 " not in head.split(b"\r\n")[0] or accept.lower() not in head.lower():
            self.close()
            raise BridgeError("Antigravity rejected the WebSocket handshake")

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _read(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buffer)))
            if not chunk:
                raise BridgeError("Antigravity closed its debugging connection")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:n])
        del self.buffer[:n]
        return result

    def _send(self, payload, opcode=1):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _message(self):
        chunks = []
        while True:
            first, second = self._read(2)
            n = second & 127
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            if n > 64 * 1024 * 1024:
                raise BridgeError("Oversized WebSocket frame")
            mask = self._read(4) if second & 128 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            op = first & 15
            if op == 8:
                raise BridgeError("Antigravity closed its debugging connection")
            if op == 9:
                self._send(data, 10)
                continue
            if op == 10:
                continue
            chunks.append(data)
            if first & 128:
                return json.loads(b"".join(chunks))

    def call(self, method, params=None):
        self.seq += 1
        ident = self.seq
        self._send(json.dumps({"id": ident, "method": method, "params": params or {}}).encode())
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            msg = self._message()
            if msg.get("id") != ident:
                continue
            if "error" in msg:
                raise BridgeError(str(msg["error"]))
            return msg.get("result", {})
        raise BridgeError("Antigravity debugging request timed out")

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if result.get("exceptionDetails"):
            raise BridgeError(str(result["exceptionDetails"].get("text", "JavaScript failed")))
        return result.get("result", {}).get("value")
