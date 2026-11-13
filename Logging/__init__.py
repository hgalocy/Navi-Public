import logging
import os
import json
import re
import socket
import time
import uuid
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler

LOG_DIR = os.environ.get("NAVI_LOG_DIR") or os.path.join(os.path.dirname(__file__), "logs")
COMPONENT = os.environ.get("NAVI_COMPONENT", "tools")
# RotatingFileHandler must never have multiple component process writers.
LOG_DIR = os.path.join(LOG_DIR, COMPONENT)
LOG_FILE = os.path.join(LOG_DIR, "navi.log")
CONVERSATION_LOG_FILE = os.path.join(LOG_DIR, "navi_conversation.log")

os.makedirs(LOG_DIR, exist_ok=True)

SESSION_ID = uuid.uuid4().hex


def _label(value, fallback="unknown"):
    return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", value) else fallback


CLIENT_NAME = _label(os.environ.get("NAVI_CLIENT_NAME") or socket.gethostname())
_context = ContextVar("navi_interaction", default=None)


def current_context():
    return dict(_context.get() or {
        "interaction_id": "-", "client_name": CLIENT_NAME, "client_session_id": SESSION_ID,
    })


def interaction_details(context=None):
    details = current_context()
    if context is not None:
        details.update({key: _label(context[key]) for key in details if key in context})
    if details["interaction_id"] in ("-", "unknown"):
        details["interaction_id"] = uuid.uuid4().hex
    return details


@contextmanager
def interaction_context(context):
    details = current_context()
    details.update({key: _label(context[key]) for key in details if key in context})
    token = _context.set(details)
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def timed_stage(stage):
    started = time.monotonic()
    status = "completed"
    try:
        yield
    except Exception:
        status = "failed"
        raise
    finally:
        logger.info("stage=%s status=%s elapsed_ms=%.1f", stage, status, (time.monotonic() - started) * 1000)


class ContextFilter(logging.Filter):
    def filter(self, record):
        for key, value in current_context().items():
            setattr(record, key, _label(value))
        record.session_id = SESSION_ID
        return True


IDENTITY_FORMAT = "[session=%(session_id)s client=%(client_name)s client_session=%(client_session_id)s interaction=%(interaction_id)s]"


class TimestampFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")


class JournalFormatter(logging.Formatter):
    def format(self, record):
        fields = {"timestamp": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
                  "level": record.levelname, "component": COMPONENT, "pid": os.getpid(),
                  "session_id": SESSION_ID, "message": record.getMessage()}
        fields.update(current_context())
        if record.exc_info:
            fields["exception"] = self.formatException(record.exc_info)
        return json.dumps(fields, ensure_ascii=False)


def _add_file_handler(target, path, formatter):
    if not target.handlers:
        handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        handler.addFilter(ContextFilter())
        target.addHandler(handler)
    target.propagate = False


logger = logging.getLogger("Navi")
_level_name = os.environ.get("NAVI_LOG_LEVEL", "INFO").upper()
_levels = {name: getattr(logging, name) for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")}
logger.setLevel(_levels.get(_level_name, logging.INFO))
_add_file_handler(logger, LOG_FILE, TimestampFormatter("%(asctime)s [%(levelname)s] %(message)s " + IDENTITY_FORMAT))
if not any(getattr(h, "navi_journal", False) for h in logger.handlers):
    journal = logging.StreamHandler(sys.stdout)
    journal.navi_journal = True
    journal.setFormatter(JournalFormatter())
    logger.addHandler(journal)

conversation_logger = logging.getLogger("NaviConversation")
conversation_logger.setLevel(logging.INFO)
_add_file_handler(
    conversation_logger,
    CONVERSATION_LOG_FILE,
    TimestampFormatter("%(asctime)s " + IDENTITY_FORMAT + " [source=%(source)s trigger=%(trigger)s] %(speaker)s: %(message)s"),
)

if _level_name not in _levels:
    logger.warning("Unknown NAVI_LOG_LEVEL %r; using INFO", _level_name)


def log_conversation(speaker, text, source, trigger):
    """Write one utterance, preserving Unicode and escaping embedded newlines."""
    if not text or not text.strip():
        return
    conversation_logger.info(
        json.dumps(text, ensure_ascii=False),
        extra={"speaker": speaker, "source": _label(source), "trigger": _label(trigger)},
    )


def terminal(message, level=logging.INFO):
    print(message, flush=True)
    logger.log(level, message)


def stream_subprocess_output(pipe, level=logging.DEBUG, prefix="", context=None):
    with interaction_context(context or current_context()):
        _stream_subprocess_output(pipe, level, prefix)


def _stream_subprocess_output(pipe, level, prefix):
    if pipe is None:
        return
    for raw_line in iter(pipe.readline, b""):
        if not raw_line:
            break
        try:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
        except Exception:
            line = repr(raw_line)
        if line:
            # stderr also carries routine progress; honor errors embedded in it.
            if re.search(r"\berror\b|\bfatal\b|\bfailed\b|\[E:", line, re.IGNORECASE):
                line_level = logging.ERROR
            elif re.search(r"\bwarn(?:ing)?\b|\bunderrun\b|\boverrun\b|\[W:", line, re.IGNORECASE):
                line_level = logging.WARNING
            else:
                line_level = level
            logger.log(line_level, "%s%s", prefix, line)

    try:
        pipe.close()
    except Exception:
        pass
