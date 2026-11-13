"""NaviServer HTTP transport used only by the Coordinator."""
import concurrent.futures
import socket

from pathlib import Path
from ipaddress import IPv4Address
from Logging import logger, current_context, interaction_context, interaction_details, timed_stage
import requests


def _get_local_subnet():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
    finally:
        sock.close()

    octets = local_ip.split(".")
    if len(octets) != 4:
        raise RuntimeError(f"Unable to determine local IPv4 address: {local_ip}")
    return ".".join(octets[:3])


def _is_direct_response(response, expected_url):
    if response.history:
        return False
    if response.url != expected_url:
        return False
    return True


def _check_server_at_ip(ip, timeout):
    proxies = {"http": None, "https": None}
    # Prefer explicit health endpoint if available.
    health_url = f"http://{ip}:5000/health"
    try:
        response = requests.get(
            health_url,
            timeout=timeout,
            allow_redirects=False,
            proxies=proxies,
        )
        if _is_direct_response(response, health_url) and response.status_code == 200:
            try:
                data = response.json()
                if data.get("server") == "NaviServer":
                    return ip
            except ValueError:
                pass
    except Exception:
        pass

    # Fallback: check transcribe endpoint if the server exists.
    transcribe_url = f"http://{ip}:5000/transcribe"
    try:
        response = requests.options(
            transcribe_url,
            timeout=timeout,
            allow_redirects=False,
            proxies=proxies,
        )
        if _is_direct_response(response, transcribe_url) and (
            response.status_code in (200, 204, 405)
            and "POST" in response.headers.get("Allow", "")
        ):
            return ip
    except Exception:
        pass

    try:
        response = requests.get(
            transcribe_url,
            timeout=timeout,
            allow_redirects=False,
            proxies=proxies,
        )
        if _is_direct_response(response, transcribe_url) and response.status_code == 405:
            return ip
    except Exception:
        pass

    return None


class ServerClient:
    """Coordinator-owned discovery cache and HTTP client; no shared runtime state."""

    def __init__(self, cache_file=None):
        self.cache_file = Path(cache_file) if cache_file is not None else Path.home() / ".navi_server_ip"
        self.transcribe_url = None
        try:
            ip = str(IPv4Address(self.cache_file.read_text(encoding="utf-8").strip()))
            self.transcribe_url = "http://{}:5000/transcribe".format(ip)
        except (OSError, ValueError):
            pass

    def _save_server_ip(self, ip):
        try:
            self.cache_file.write_text(ip, encoding="utf-8")
        except OSError:
            logger.warning("Could not persist server discovery cache")

    def _discover_server(self, timeout=0.25):
        subnet = _get_local_subnet()
        logger.info("Discovering Navi server on subnet %s.0/24...", subnet)
        ips = [f"{subnet}.{i}" for i in range(1, 255)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
            future_to_ip = {executor.submit(_check_server_at_ip, ip, timeout): ip for ip in ips}
            for future in concurrent.futures.as_completed(future_to_ip):
                ip = future.result()
                if ip:
                    logger.info("Found server at %s", ip)
                    self.transcribe_url = f"http://{ip}:5000/transcribe"
                    self._save_server_ip(ip)
                    return

        raise RuntimeError(f"Could not discover Navi server on subnet {subnet}.0/24")

    def send_audio(self, files, source="microphone", trigger="unknown", context=None):
        with interaction_context(interaction_details(context)):
            with timed_stage("upload_roundtrip"):
                return self._send_audio(files, source, trigger)

    def _send_audio(self, files, source, trigger):
        proxies = {"http": None, "https": None}
        metadata = {"source": source, "trigger": trigger}
        metadata.update(current_context())

        def post_audio(attempt):
            with timed_stage(f"upload_attempt_{attempt}"):
                response = requests.post(self.transcribe_url, files=files, data=metadata, timeout=30, proxies=proxies)
                logger.info("upload_response attempt=%s status=%s server_session=%r",
                            attempt, response.status_code, response.headers.get("X-Navi-Server-Session-ID", "unknown"))
                return response

        if self.transcribe_url is None:
            logger.info("No cached Navi server IP; discovering server...")
            self._discover_server()

        try:
            return post_audio(1)
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning("Network error to %s: %s. Attempting discovery...", self.transcribe_url, e)
            self._discover_server()
            if isinstance(files, dict):
                for value in files.values():
                    if hasattr(value, "seek"):
                        try:
                            value.seek(0)
                        except Exception:
                            pass
            return post_audio(2)
