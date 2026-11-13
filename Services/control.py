"""Stable SSH-friendly component controls; no shell or daemon spawning."""
import argparse
import concurrent.futures
import json
import subprocess

from .ipc import COMPONENTS, IPCError, call


def health(component):
    try:
        return call(component, "health", timeout=2)
    except IPCError as exc:
        return {"component": component, "state": "unavailable", "error": str(exc)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "start", "stop", "restart", "command"))
    parser.add_argument("component", nargs="?", choices=COMPONENTS)
    parser.add_argument("--text")
    args = parser.parse_args(argv)
    try:
        if args.action == "command":
            if not args.text or args.component not in (None, "coordinator"):
                parser.error("command requires --text and targets coordinator")
            print(json.dumps(call("coordinator", "command", {"text": args.text})))
            return 0
        if args.action == "status":
            names = (args.component,) if args.component else COMPONENTS
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                states = list(pool.map(health, names))
            print(json.dumps({"v": 1, "components": states}))
            return 0 if all(s["state"] in ("ready", "busy") for s in states) else 1
        if not args.component:
            parser.error("Lifecycle actions require a component")
        result = subprocess.run(["systemctl", "--user", args.action, "navi-" + args.component + ".service"],
                                timeout=35)
        return result.returncode
    except (IPCError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"v": 1, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
