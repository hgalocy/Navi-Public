"""Face owns rendering and receives speech envelopes exclusively through IPC."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

from Services.ipc import IPCError, call
from Services.runtime import Lease, Service


class CommandSubmitter:
    """At most one pending IPC submission; commands are never queued for replay."""

    def __init__(self, logger):
        self.logger = logger
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.pending = None

    def submit(self, command):
        if not command.strip():
            return
        if self.pending is not None and not self.pending.done():
            self.logger.warning("command_rejected: submission in progress")
            return
        self.pending = self.executor.submit(self._send, command)

    def _send(self, command):
        try:
            call("coordinator", "command", {"text": command})
        except IPCError:
            self.logger.exception("command_rejected")

    def close(self):
        self.executor.shutdown(wait=True)


def run():
    from Face.Animation import run as render
    import pyglet
    from Logging import logger
    from Services.animation import EnvelopeReceiver

    def handler(op, args, context):
        raise IPCError("Face only supports health")

    # Health and watchdog progress depend on the render loop making progress.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with Lease("face-renderer"):
        receiver = EnvelopeReceiver()
        service = Service("face", handler, close=pyglet.app.exit)
        commands = CommandSubmitter(logger)
        task = loop.create_task(service.run())

        def tick():
            loop.call_soon(loop.stop)
            loop.run_forever()
            if task.done():
                task.result()

        try:
            render(submit_command=commands.submit, on_tick=tick, playback_sample=receiver.sample)
        finally:
            try:
                if service.stop is None and not task.done():
                    loop.run_until_complete(asyncio.sleep(0))
                if service.stop is not None:
                    service.stop.set()
                loop.run_until_complete(task)
            finally:
                commands.close()
                receiver.close()
                loop.close()
