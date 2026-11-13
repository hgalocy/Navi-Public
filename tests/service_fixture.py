"""Subprocess fixture for real IPC/lifecycle tests; never opens audio hardware."""
import os
import subprocess
import sys
import time

from Services.runtime import Lease, Service, run

service = None


def handler(op, args, context):
    if op == "echo":
        return lambda: {"args": args, "context": context}, True
    if op == "hold":
        def work():
            time.sleep(args.get("seconds", 0.3))
            return {"finished": True}
        return work, False
    if op == "child":
        def work():
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            service.track_process(process)
            return {"child_pid": process.pid}
        return work, True
    if op == "fail":
        def work():
            raise RuntimeError("fixture failure")
        return work, False
    raise ValueError("Unknown operation")


service = Service(sys.argv[1], handler, deadline=float(os.environ.get("FIXTURE_DEADLINE", "5")))
if sys.argv[1] == "listener":
    with Lease("microphone"):
        run(service)
else:
    run(service)
