"""Small service host; systemd alone supervises and restarts processes."""
import asyncio
import os
import signal
import socket
import threading
import time

from .ipc import IPCError, MAX_MESSAGE, decode, encode, runtime_dir, socket_path


class Lease:
    """Kernel-held advisory lock. Never unlink: crashes release it automatically."""
    def __init__(self, name):
        self.path = runtime_dir() / (name + ".lock")
        self.file = None

    def __enter__(self):
        import fcntl
        self.file = self.path.open("a")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise IPCError("{} already owned".format(self.path.stem)) from exc
        return self

    def __exit__(self, *unused):
        self.file.close()


def notify(message):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.sendto(message.encode("utf-8"), address)


class Service:
    def __init__(self, component, handler, deadline=120, tick=None, close=None,
                 start=None, control=None, diagnostics=None):
        self.component = component
        self.handler = handler
        self.deadline = deadline
        self.tick = tick
        self.close = close
        self.start = start
        self.control = control
        self.diagnostics = diagnostics
        self.started = time.monotonic()
        # Python 3.8 synchronization primitives bind to their creation loop.
        self.stop = None
        self.busy_since = None
        self.job = None
        self.last_job = None
        self.last_error = None
        self.worker = None
        self.processes = []
        self.process_lock = threading.Lock()
        self.closing = False
        self.connections = 0

    def track_process(self, process):
        with self.process_lock:
            if self.closing:
                process.terminate()
                raise RuntimeError("Service stopping")
            self.processes.append(process)

    def status(self):
        result = {"component": self.component, "pid": os.getpid(),
                "state": "busy" if self.busy_since is not None else "ready",
                "uptime_s": round(time.monotonic() - self.started, 3),
                "job_id": self.job, "last_job": self.last_job, "last_error": self.last_error}
        if self.diagnostics:
            result["audio"] = self.diagnostics()
        return result

    async def request(self, reader, writer):
        self.connections += 1
        request_id = None
        try:
            if self.connections > 16:
                raise IPCError("Too many connections")
            message = decode(await asyncio.wait_for(reader.readline(), 2))
            request_id = message.get("id")
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
                raise IPCError("Invalid request ID")
            op, args, context = message.get("op"), message.get("args"), message.get("context")
            if not isinstance(op, str) or not isinstance(args, dict) or not isinstance(context, dict):
                raise IPCError("Invalid request schema")
            if op == "health":
                result = self.status()
            elif self.control and (result := self.control(op, args, context)) is not None:
                pass  # Small Listener controls remain responsive during a recording job.
            else:
                if self.busy_since is not None:
                    raise IPCError("Component busy; request rejected")
                # Handler validates synchronously, before acceptance.
                work, wait = self.handler(op, args, context)
                self.job = request_id
                self.busy_since = time.monotonic()
                self.last_error = None
                future = asyncio.get_running_loop().create_future()
                self.worker = threading.Thread(target=self._work, args=(work, future), daemon=True)
                self.worker.start()
                result = await asyncio.shield(future) if wait else {"accepted": True, "job_id": request_id}
            reply = {"v": 1, "id": request_id, "ok": True, "result": result}
        except Exception as exc:
            reply = {"v": 1, "id": request_id, "ok": False, "error": str(exc)[:512]}
        try:
            writer.write(encode(reply))
            await asyncio.wait_for(writer.drain(), 2)
        except (OSError, IPCError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            finally:
                self.connections -= 1

    def _work(self, work, future):
        result, error = None, None
        try:
            result = work()
        except Exception as exc:
            from Logging import logger
            logger.exception("job_failed component=%s job_id=%s", self.component, self.job)
            # Empty exception messages (e.g. AssertionError) are still failures.
            error = (str(exc) or type(exc).__name__)[:512]
        try:
            self.loop.call_soon_threadsafe(self._completed, future, result, error)
        except RuntimeError:
            pass  # Shutdown already closed the loop; this daemon worker exits.

    def _completed(self, future, result, error):
        from Logging import logger
        self.last_job = {"id": self.job, "state": "failed" if error else "completed"}
        self.last_error = error
        logger.info("job_finished component=%s job_id=%s status=%s", self.component, self.job, self.last_job["state"])
        self.busy_since = None
        self.job = None
        with self.process_lock:
            self.processes = [p for p in self.processes if p.poll() is None]
        if error:
            future.set_exception(IPCError(error))
            # Async commands expose errors in health and logs, not a dangling future.
            future.exception()
        else:
            future.set_result(result)

    async def heartbeat(self):
        # Event-loop liveness AND bounded job execution; never a blind watchdog thread.
        interval = min(1, int(os.environ.get("WATCHDOG_USEC", "10000000")) / 3000000)
        while not self.stop.is_set():
            if self.busy_since is not None and time.monotonic() - self.busy_since > self.deadline:
                raise RuntimeError("Worker exceeded {}s deadline".format(self.deadline))
            if self.tick:
                self.tick()  # Face tick checks the actual draw loop.
            notify("WATCHDOG=1")
            try:
                await asyncio.wait_for(self.stop.wait(), interval)
            except asyncio.TimeoutError:
                pass

    async def run(self):
        from Logging import logger
        self.loop = asyncio.get_running_loop()
        self.stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.loop.add_signal_handler(sig, self.stop.set)
        path = socket_path(self.component)
        with Lease(self.component):
            # Lease establishes no live owner before stale socket removal.
            if os.path.lexists(path):
                os.unlink(path)
            server = await asyncio.start_unix_server(self.request, path, limit=MAX_MESSAGE)
            os.chmod(path, 0o600)
            try:
                if self.start:
                    self.start()
                notify("READY=1\nSTATUS=Ready")
                logger.info("service_ready component=%s pid=%s", self.component, os.getpid())
                async with server:
                    await self.heartbeat()
            finally:
                self.closing = True
                notify("STOPPING=1")
                server.close()
                await server.wait_closed()
                with self.process_lock:
                    processes = list(self.processes)
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                if self.worker:
                    await self.loop.run_in_executor(None, self.worker.join, 3)
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)
                if self.close:
                    self.close()
                os.unlink(path)
                logger.info("service_stopped component=%s", self.component)


def run(service):
    from Logging import logger
    try:
        asyncio.run(service.run())
    except Exception:
        logger.exception("service_failed component=%s", service.component)
        raise
