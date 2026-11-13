"""Version 1: one bounded JSON request/reply per local Unix stream connection."""
import json
import os
from pathlib import Path
import socket
import time
import uuid

COMPONENTS = ("face", "listener", "speaker", "coordinator")
MAX_MESSAGE = 256 * 1024


class IPCError(RuntimeError):
    pass


def runtime_dir():
    root = os.environ.get("NAVI_RUNTIME_DIR")
    if not root:
        root = str(Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/{}".format(os.getuid()))) / "navi")
    path = Path(root)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise IPCError("Runtime directory must be owned by this user and mode 0700")
    return path


def socket_path(component):
    if component not in COMPONENTS:
        raise IPCError("Unknown component")
    return str(runtime_dir() / (component + ".sock"))


def encode(message):
    data = json.dumps(message, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(data) > MAX_MESSAGE:
        raise IPCError("IPC message too large")
    return data


def decode(data):
    if len(data) > MAX_MESSAGE or not data.endswith(b"\n"):
        raise IPCError("Invalid IPC frame")
    try:
        message = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise IPCError("Invalid JSON") from exc
    if not isinstance(message, dict) or type(message.get("v")) is not int or message["v"] != 1:
        raise IPCError("Expected protocol version 1")
    return message


def call(component, op, args=None, context=None, timeout=3):
    request_id = uuid.uuid4().hex
    request = {"v": 1, "id": request_id, "op": op, "args": args or {}, "context": context or {}}
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path(component))
            sock.sendall(encode(request))
            data = bytearray()
            while not data.endswith(b"\n"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise IPCError("IPC deadline expired")
                sock.settimeout(remaining)
                chunk = sock.recv(min(65536, MAX_MESSAGE + 1 - len(data)))
                if not chunk:
                    raise IPCError("Peer closed before replying")
                data.extend(chunk)
                if len(data) > MAX_MESSAGE:
                    raise IPCError("IPC message too large")
            reply = decode(data)
    except OSError as exc:
        raise IPCError("{} unavailable: {}".format(component, exc)) from exc
    if reply.get("id") != request_id or type(reply.get("ok")) is not bool:
        raise IPCError("Invalid IPC reply")
    if not reply["ok"]:
        raise IPCError(reply.get("error", "Remote operation failed"))
    return reply.get("result")
