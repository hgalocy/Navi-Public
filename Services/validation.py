"""Validation shared by command endpoints, before work is accepted."""
from .ipc import IPCError


def require_text(args):
    text = args.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise IPCError("text must contain 1..4000 characters")
    return text.strip()
