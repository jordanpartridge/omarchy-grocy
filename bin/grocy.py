#!/usr/bin/python3 -I
"""Grocy stock popup helper. The API key is read from a private config file and sent as a header."""

from __future__ import annotations

import base64
import fcntl
import http.client
import ipaddress
import json
import math
import os
import pwd
import re
import socket
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KROGER_ORIGIN = "https://api.kroger.com"
KROGER_HOST = "api.kroger.com"
CONFIG_DIR = "jordan-grocy"
CONFIG_NAME = "config.env"
DEFAULT_CHAIN = "FRYS"
ZONE = ZoneInfo("UTC")
HOST_RE = re.compile(
    r"(?=.{1,253}\Z)"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
CHAIN_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}$")
ZONE_RE = re.compile(r"[A-Za-z0-9_+-]{1,32}(?:/[A-Za-z0-9_+-]{1,32}){0,2}")
MAX_BODY = 1_048_576
MAX_STOCK = 400
MAX_LISTS = 200
MAX_HITS = 24
MAX_STORES = 8
MAX_PAGE_START = 240
MAX_NAME = 180
MAX_ENV = 4096
MAX_TOKEN = 8192
TIMEOUT = 8
KEY_RE = re.compile(r"^[0-9a-fA-F]{32,128}$")
LIST_RE = re.compile(r"^Order [0-9]{4}-W[0-9]{2}$")
ID_RE = re.compile(r"^[1-9][0-9]{0,9}$")
PRODUCT_RE = re.compile(r"^[0-9]{12,14}$")
LOCATION_RE = re.compile(r"^[0-9]{8}$")
ZIP_RE = re.compile(r"^[0-9]{5}$")
CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/-]{16,4096}$")
TERM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .'%/-]{0,79}$")
AMOUNT_RE = re.compile(r"^\d{1,7}(\.\d{1,3})?$")
MONEY_TEXT_RE = re.compile(r"^\d{1,4}\.\d{2}$")
STORE_NOTE_RE = re.compile(r"^store\|(\d{1,4}\.\d{2})\|(\d{1,4}\.\d{2})\|([^|\x00]{1,120})$")
CODED_NOTE_RE = re.compile(
    r"^store\|(\d{1,4}\.\d{2})\|(\d{1,4}\.\d{2}|-)\|([0-9]{12,14})\|([0-9]{8,14}|-)\|([^|\x00]{1,120})$"
)
UPC_RE = re.compile(r"^[0-9]{8,14}$")
FILE_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
DROP = {ord(ch) for ch in "<>&"} | set(range(32)) | set(range(0x7F, 0xA0)) | {
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069,
}


class GrocyError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GrocyError("redirect refused")


def plain(text: str, limit: int = 240) -> str:
    out: list[str] = []
    for ch in text:
        if ord(ch) in DROP:
            continue
        out.append(ch)
        if len(out) >= limit:
            break
    return "".join(out).strip()


def redact(text: str, key: str) -> str:
    if key and key in text:
        text = text.replace(key, "")
    return plain(text)


def next_order_list_name(now: datetime) -> str:
    if now.tzinfo is None:
        raise GrocyError("clock must have a timezone")
    local = now
    days = (7 - local.weekday()) % 7
    if days == 0:
        days = 7
    # On Sunday the coming Monday's order is already placed.
    if local.weekday() == 6:
        days += 7
    monday = (local + timedelta(days=days)).date()
    iso = monday.isocalendar()
    name = f"Order {iso.year}-W{iso.week:02d}"
    if LIST_RE.fullmatch(name) is None:
        raise GrocyError("list name is not valid")
    return name


def passwd_home() -> str:
    return pwd.getpwuid(os.geteuid()).pw_dir


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def open_home(home: str) -> int:
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise GrocyError("home directory is not trusted") from exc
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        _close(fd)
        raise GrocyError("home directory is not trusted")
    return fd


def open_child_dir(parent: int, name: str, *, create: bool, private: bool) -> int:
    if name in {".", ".."} or "/" in name or "\x00" in name:
        raise GrocyError("refusing directory name")
    fd = -1
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                raise GrocyError("Grocy key file is missing") from None
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise GrocyError("Grocy directory is not trusted")
        if private:
            os.fchmod(fd, 0o700)
        elif st.st_mode & 0o022:
            raise GrocyError("Grocy directory is writable by others")
        owned = fd
        fd = -1
        return owned
    except GrocyError:
        raise
    except OSError as exc:
        raise GrocyError("Grocy directory is not trusted") from exc
    finally:
        if fd >= 0:
            _close(fd)


def read_private_file(dirfd: int, name: str, limit: int) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) or name in {".", ".."}:
        raise GrocyError("refusing file name")
    fd = -1
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dirfd)
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or st.st_nlink != 1
            or st.st_mode & 0o077
            or st.st_size > limit
        ):
            raise GrocyError("Grocy key file must be a private regular file (mode 600)")
        os.set_blocking(fd, True)
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > limit:
            raise GrocyError("Grocy key file is too large")
        return data
    except GrocyError:
        raise
    except FileNotFoundError as exc:
        raise GrocyError("Grocy key file is missing") from exc
    except OSError as exc:
        raise GrocyError("Grocy key file must be a private regular file (mode 600)") from exc
    finally:
        if fd >= 0:
            _close(fd)


def _env_pairs(text: str, label: str) -> dict[str, str]:
    if "\x00" in text or len(text) > MAX_ENV:
        raise GrocyError(f"{label} is not valid")
    found: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if name:
            found[name] = value
    return found


def valid_https_origin(origin: str) -> bool:
    parts = urllib.parse.urlsplit(origin)
    if parts.username or parts.password or parts.query or parts.fragment:
        return False
    if parts.path not in {"", "/"} or parts.scheme != "https":
        return False
    host = parts.hostname or ""
    if HOST_RE.fullmatch(host) is None:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    if parts.port is not None and not 1 <= parts.port <= 65535:
        return False
    return True


def parse_env(text: str) -> tuple[str, str]:
    found = _env_pairs(text, "Grocy key file")
    origin = found.get("GROCY_URL")
    key = found.get("GROCY_API_KEY")
    if origin is None or not valid_https_origin(origin) or key is None or KEY_RE.fullmatch(key) is None:
        raise GrocyError("Grocy key file is not valid")
    return origin, key


def parse_timezone(text: str) -> ZoneInfo:
    found = _env_pairs(text, "Grocy key file")
    name = found.get("GROCY_TIMEZONE", "UTC")
    if ZONE_RE.fullmatch(name) is None:
        raise GrocyError("Grocy key file is not valid")
    try:
        return ZoneInfo(name)
    except (KeyError, OSError, ValueError) as exc:
        raise GrocyError("Grocy key file is not valid") from exc


def load_config_text(home: str) -> str:
    home_fd = open_home(home)
    try:
        config_fd = open_child_dir(home_fd, ".config", create=False, private=False)
    finally:
        _close(home_fd)
    try:
        plugin_fd = open_child_dir(config_fd, CONFIG_DIR, create=False, private=False)
    finally:
        _close(config_fd)
    try:
        raw = read_private_file(plugin_fd, CONFIG_NAME, MAX_ENV)
    finally:
        _close(plugin_fd)
    return raw.decode("utf-8", "strict")


def load_credentials(home: str) -> tuple[str, str]:
    return parse_env(load_config_text(home))


def apply_config_zone(home: str) -> None:
    global ZONE
    try:
        text = load_config_text(home)
    except GrocyError as exc:
        if "missing" in str(exc):
            return
        raise
    ZONE = parse_timezone(text)


def parse_kroger_env(text: str) -> tuple[str, str, str, str]:
    found = _env_pairs(text, "Kroger key file")
    client = found.get("KROGER_CLIENT_ID", "")
    secret = found.get("KROGER_CLIENT_SECRET", "")
    location = found.get("KROGER_LOCATION_ID", "")
    chain = found.get("KROGER_CHAIN", DEFAULT_CHAIN)
    if (
        CLIENT_RE.fullmatch(client) is None
        or SECRET_RE.fullmatch(secret) is None
        or LOCATION_RE.fullmatch(location) is None
        or CHAIN_RE.fullmatch(chain) is None
    ):
        raise GrocyError("Kroger key file is not valid")
    return client, secret, location, chain


def load_kroger(home: str) -> tuple[str, str, str, str]:
    try:
        text = load_config_text(home)
    except GrocyError as exc:
        if "missing" in str(exc):
            raise GrocyError("Kroger is not configured") from exc
        raise
    found = _env_pairs(text, "Kroger key file")
    if "KROGER_CLIENT_ID" not in found and "KROGER_CLIENT_SECRET" not in found:
        raise GrocyError("Kroger is not configured")
    return parse_kroger_env(text)


def config_dir(home: str) -> int:
    home_fd = open_home(home)
    try:
        config_fd = open_child_dir(home_fd, ".config", create=False, private=False)
    finally:
        _close(home_fd)
    try:
        return open_child_dir(config_fd, CONFIG_DIR, create=False, private=False)
    finally:
        _close(config_fd)


def replace_location(text: str, location_id: str) -> str:
    if LOCATION_RE.fullmatch(location_id) is None:
        raise GrocyError("That store could not be saved")
    client, secret, _old, _chain = parse_kroger_env(text)
    kept: list[str] = []
    for line in text.splitlines():
        if "=" in line and line.split("=", 1)[0].strip() == "KROGER_LOCATION_ID":
            continue
        kept.append(line)
    kept.append(f"KROGER_LOCATION_ID={location_id}")
    body = "\n".join(kept).rstrip("\n") + "\n"
    if len(body.encode()) > MAX_ENV:
        raise GrocyError("Kroger key file is not valid")
    same_client, same_secret, saved, _same_chain = parse_kroger_env(body)
    if same_client != client or same_secret != secret or saved != location_id:
        raise GrocyError("Kroger key file is not valid")
    return body


def state_dir(home: str) -> int:
    home_fd = open_home(home)
    try:
        local_fd = open_child_dir(home_fd, ".local", create=True, private=False)
    finally:
        _close(home_fd)
    try:
        state_fd = open_child_dir(local_fd, "state", create=True, private=False)
    finally:
        _close(local_fd)
    try:
        plugin_fd = open_child_dir(state_fd, "jordan-grocy", create=True, private=True)
    finally:
        _close(state_fd)
    scrub_state(plugin_fd)
    return plugin_fd


def scrub_state(dirfd: int) -> None:
    for name in os.listdir(dirfd):
        if name in {".", ".."} or "/" in name or "\x00" in name:
            continue
        try:
            st = os.lstat(name, dir_fd=dirfd)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            fd = -1
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
                info = os.fstat(fd)
                if stat.S_ISREG(info.st_mode) and info.st_mode & 0o077:
                    os.fchmod(fd, 0o600)
            except OSError:
                pass
            finally:
                if fd >= 0:
                    _close(fd)
            continue
        if stat.S_ISLNK(st.st_mode) or stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode):
            try:
                os.unlink(name, dir_fd=dirfd)
            except OSError:
                pass
            continue
        raise GrocyError("unexpected entry in grocy state")


def lock_named(dirfd: int, name: str) -> int:
    if FILE_RE.fullmatch(name) is None or name in {".", ".."}:
        raise GrocyError("refusing file name")
    fd = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dirfd)
    try:
        os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise GrocyError("grocy lock is not a private file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except Exception:
        _close(fd)
        raise


def lock_add(dirfd: int) -> int:
    return lock_named(dirfd, "add.lock")


def write_private(dirfd: int, name: str, data: bytes) -> None:
    if FILE_RE.fullmatch(name) is None or name in {".", ".."}:
        raise GrocyError("refusing file name")
    if len(data) > MAX_TOKEN:
        raise GrocyError("refusing file")
    tmp = f".{name}.{os.urandom(8).hex()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dirfd)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            wrote = os.write(fd, view)
            if wrote <= 0:
                raise GrocyError("could not store the Kroger token")
            view = view[wrote:]
        os.fsync(fd)
        os.rename(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        os.fsync(dirfd)
    except Exception:
        try:
            os.unlink(tmp, dir_fd=dirfd)
        except OSError:
            pass
        raise
    finally:
        _close(fd)


def tailnet_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip in TAILNET_V4 or ip in TAILNET_V6


def allowed_server_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    if tailnet_ip(address):
        return True
    return bool(ip.is_global or ip.is_private)


def resolve_allowed(host: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise GrocyError("Grocy did not respond") from exc
    pins: list[str] = []
    for info in infos:
        ip = info[4][0]
        if not allowed_server_ip(ip):
            raise GrocyError("Grocy address is not allowed")
        if ip not in pins:
            pins.append(ip)
    if not pins:
        raise GrocyError("Grocy address is not allowed")
    return pins[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, context=None, check_hostname=None, *, pin: str, port: int):
        name = host
        if host.startswith("[") and "]" in host:
            name = host[1:host.index("]")]
        elif host.count(":") == 1:
            name = host.split(":", 1)[0]
        super().__init__(name, port=port, timeout=timeout, context=context)
        self._pin = pin

    def connect(self):
        if self._tunnel_host:
            raise GrocyError("refusing proxy tunnel")
        raw = socket.create_connection((self._pin, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _opener(pin: str | None, port: int) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({}), _NoRedirect()]
    if pin is not None:
        context = ssl.create_default_context()

        def connection(host, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, context=None, check_hostname=None):
            return _PinnedHTTPSConnection(
                host,
                timeout=timeout,
                context=context,
                check_hostname=check_hostname,
                pin=pin,
                port=port,
            )

        class _Handler(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(connection, req, context=context)

        handlers.append(_Handler())
    return urllib.request.build_opener(*handlers)


def grocy_endpoint(origin: str, allow_loopback: bool) -> tuple[str, int, str | None]:
    parts = urllib.parse.urlsplit(origin)
    if parts.username or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise GrocyError("refusing origin")
    if (
        allow_loopback
        and parts.scheme == "http"
        and parts.hostname == "127.0.0.1"
        and parts.port is not None
        and 1 <= parts.port <= 65535
    ):
        return "127.0.0.1", parts.port, None
    host = parts.hostname or ""
    if parts.scheme != "https" or not valid_https_origin(origin):
        raise GrocyError("refusing origin")
    port = 443 if parts.port is None else parts.port
    return host, port, resolve_allowed(host, port)


def exchange(origin: str, key: str, method: str, path: str, body: dict | None = None, *, allow_loopback: bool = False) -> tuple[int, bytes]:
    _host, port, pin = grocy_endpoint(origin, allow_loopback)
    if method not in {"GET", "POST", "PUT", "DELETE"}:
        raise GrocyError("refusing method")
    if re.fullmatch(r"/api/[A-Za-z0-9_./-]{1,180}", path) is None or ".." in path:
        raise GrocyError("refusing path")
    if KEY_RE.fullmatch(key) is None:
        raise GrocyError("refusing key")
    data = None
    headers = {
        "GROCY-API-KEY": key,
        "Accept": "application/json",
        "User-Agent": "jordan-grocy",
    }
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("ascii")
        if len(data) > 4096:
            raise GrocyError("request too large")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(origin.rstrip("/") + path, data=data, headers=headers, method=method)
    opener = _opener(pin, port)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            status = int(resp.status)
            blob = resp.read(MAX_BODY + 1)
    except GrocyError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            status = int(exc.code)
            blob = exc.read(MAX_BODY + 1)
        finally:
            exc.close()
    except (TimeoutError, socket.timeout, ssl.SSLError, urllib.error.URLError) as exc:
        raise GrocyError("Grocy did not respond") from exc
    if len(blob) > MAX_BODY:
        raise GrocyError("Grocy response was too large")
    if 300 <= status < 400:
        raise GrocyError("redirect refused")
    return status, blob


def _json_body(blob: bytes, key: str) -> object:
    if not blob:
        return None
    try:
        return json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GrocyError(redact(blob.decode("utf-8", "replace"), key) or "Grocy response was not JSON") from exc


def require_id(value: object) -> int:
    if isinstance(value, bool):
        raise GrocyError("Grocy id is not valid")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and ID_RE.fullmatch(value):
        number = int(value)
    else:
        raise GrocyError("Grocy id is not valid")
    if number < 1 or number > 2_000_000_000:
        raise GrocyError("Grocy id is not valid")
    return number


def parse_amount(value: object) -> str:
    if isinstance(value, bool):
        raise GrocyError("stock amount is not a number")
    if isinstance(value, int):
        number: float | int = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise GrocyError("stock amount is not a number")
        number = value
    elif isinstance(value, str) and AMOUNT_RE.fullmatch(value):
        number = float(value) if "." in value else int(value)
    else:
        raise GrocyError("stock amount is not a number")
    if number < 0 or number > 1_000_000:
        raise GrocyError("stock amount is out of range")
    if float(number).is_integer():
        return str(int(number))
    return f"{float(number):.3f}".rstrip("0").rstrip(".")


def parse_stock(payload: object) -> list[dict[str, str]]:
    if not isinstance(payload, list):
        raise GrocyError("stock response was not a list")
    if len(payload) > MAX_STOCK:
        raise GrocyError("stock response was too large")
    items: list[dict[str, str]] = []
    seen: set[int] = set()
    for row in payload:
        if not isinstance(row, dict):
            raise GrocyError("stock response had an unexpected row")
        product = row.get("product")
        if not isinstance(product, dict):
            raise GrocyError("stock response had an unexpected row")
        product_id = require_id(row.get("product_id", product.get("id")))
        if product_id in seen:
            raise GrocyError("stock response repeated a product")
        seen.add(product_id)
        name = product.get("name")
        if not isinstance(name, str):
            raise GrocyError("stock response had an unexpected row")
        cleaned = plain(name, MAX_NAME)
        if not cleaned or len(name) > MAX_NAME:
            raise GrocyError("stock response had an unexpected row")
        items.append({"id": str(product_id), "name": cleaned, "amount": parse_amount(row.get("amount"))})
    items.sort(key=lambda item: item["name"].casefold())
    return items


def amount_number(value: object) -> float:
    text = parse_amount(value)
    number = float(text)
    if not math.isfinite(number):
        raise GrocyError("stock amount is not a number")
    return number


def format_total(number: float) -> str:
    if not math.isfinite(number) or number < 0 or number > 1_000_000:
        raise GrocyError("stock amount is out of range")
    if float(number).is_integer():
        return str(int(number))
    return f"{float(number):.3f}".rstrip("0").rstrip(".")


def money(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    number: float
    if isinstance(value, str):
        if MONEY_TEXT_RE.fullmatch(value) is None:
            return None
        number = float(value)
    elif isinstance(value, int):
        number = float(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        number = value
    else:
        return None
    if not math.isfinite(number) or number <= 0 or number > 9999.99:
        return None
    cents = int(round(number * 100))
    if cents < 1 or cents > 999999:
        return None
    if abs(number - (cents / 100)) > 0.001:
        return None
    return f"{cents // 100}.{cents % 100:02d}"


def store_note(name: str, price: str, loyalty: str, kroger_id: str, upc: str) -> str:
    if PRODUCT_RE.fullmatch(kroger_id) is None or (upc and UPC_RE.fullmatch(upc) is None):
        raise GrocyError("That item could not be added")
    promo = loyalty if loyalty else "-"
    code = upc if upc else "-"
    note = f"store|{price}|{promo}|{kroger_id}|{code}|{name}"
    if len(note) > 180 or parse_store_note(note) is None:
        raise GrocyError("That item could not be added")
    return note


def _note_name(name: str) -> str | None:
    cleaned = plain(name, 120).replace("|", "").strip()
    if cleaned != name or not cleaned:
        return None
    return name


def parse_store_note(note: str) -> dict[str, str] | None:
    if len(note) > 180:
        return None
    coded = CODED_NOTE_RE.fullmatch(note)
    if coded:
        price, promo, kroger_id, upc, name = coded.group(1), coded.group(2), coded.group(3), coded.group(4), coded.group(5)
        if money(price) != price or _note_name(name) is None:
            return None
        loyalty = "" if promo == "-" else promo
        if loyalty and money(loyalty) != loyalty:
            return None
        return {
            "name": name,
            "price": price,
            "loyalty": loyalty,
            "kroger_id": kroger_id,
            "upc": "" if upc == "-" else upc,
        }
    match = STORE_NOTE_RE.fullmatch(note)
    if match is None:
        return None
    price, loyalty, name = match.group(1), match.group(2), match.group(3)
    if money(price) != price or money(loyalty) != loyalty or _note_name(name) is None:
        return None
    return {"name": name, "price": price, "loyalty": loyalty, "kroger_id": "", "upc": ""}


def parse_lines(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, list):
        raise GrocyError("shopping list response was not a list")
    if len(payload) > MAX_STOCK:
        raise GrocyError("shopping list response was too large")
    lines: list[dict[str, object]] = []
    seen: set[int] = set()
    for row in payload:
        if not isinstance(row, dict):
            raise GrocyError("shopping list response had an unexpected row")
        line_id = require_id(row.get("id"))
        if line_id in seen:
            raise GrocyError("shopping list response repeated a row")
        seen.add(line_id)
        list_id = require_id(row.get("shopping_list_id"))
        raw_product = row.get("product_id")
        if raw_product is None or raw_product == "":
            note = row.get("note")
            store = parse_store_note(note) if isinstance(note, str) else None
            if store is None:
                continue
            raw_amount = row.get("amount")
            lines.append({
                "kind": "store",
                "id": line_id,
                "list_id": list_id,
                "name": store["name"],
                "price": store["price"],
                "loyalty": store["loyalty"],
                "amount": amount_number(1 if raw_amount is None else raw_amount),
                "kroger_id": store["kroger_id"],
                "upc": store["upc"],
            })
            continue
        lines.append({
            "kind": "product",
            "id": line_id,
            "product_id": require_id(raw_product),
            "list_id": list_id,
            "amount": amount_number(row.get("amount")),
        })
    return lines


def parse_products(payload: object) -> dict[int, str]:
    if not isinstance(payload, list):
        raise GrocyError("product response was not a list")
    if len(payload) > MAX_STOCK:
        raise GrocyError("product response was too large")
    names: dict[int, str] = {}
    for row in payload:
        if not isinstance(row, dict):
            raise GrocyError("product response had an unexpected row")
        product_id = require_id(row.get("id"))
        if product_id in names:
            raise GrocyError("product response repeated a product")
        name = row.get("name")
        if not isinstance(name, str) or len(name) > MAX_NAME:
            raise GrocyError("product response had an unexpected row")
        cleaned = plain(name, MAX_NAME)
        if not cleaned:
            raise GrocyError("product response had an unexpected row")
        names[product_id] = cleaned
    return names


def group_order(lines: list[dict[str, object]], names: dict[int, str]) -> list[dict[str, str]]:
    totals: dict[int, float] = {}
    for line in lines:
        product_id = int(line["product_id"])
        totals[product_id] = totals.get(product_id, 0.0) + float(line["amount"])
    if len(totals) > MAX_STOCK:
        raise GrocyError("shopping list response was too large")
    items: list[dict[str, str]] = []
    for product_id, total in totals.items():
        name = names.get(product_id) or f"Product {product_id}"
        cleaned = plain(name, MAX_NAME)
        if not cleaned:
            raise GrocyError("shopping list response had an unexpected row")
        items.append({"id": str(product_id), "name": cleaned, "amount": format_total(total)})
    items.sort(key=lambda item: item["name"].casefold())
    return items


def parse_lists(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, list):
        raise GrocyError("shopping list response was not a list")
    if len(payload) > MAX_LISTS:
        raise GrocyError("too many shopping lists")
    lists: list[dict[str, object]] = []
    seen: set[int] = set()
    for row in payload:
        if not isinstance(row, dict):
            raise GrocyError("shopping list response had an unexpected row")
        list_id = require_id(row.get("id"))
        name = row.get("name")
        if list_id in seen or not isinstance(name, str) or len(name) > MAX_NAME:
            raise GrocyError("shopping list response had an unexpected row")
        seen.add(list_id)
        lists.append({"id": list_id, "name": name})
    return lists


def _detail_text(payload: object, key: str) -> str:
    if not isinstance(payload, dict):
        return ""
    errors = payload.get("errors")
    if isinstance(errors, dict):
        reason = errors.get("reason")
        if isinstance(reason, str):
            return redact(reason, key)
    if isinstance(errors, list):
        reasons: list[str] = []
        for row in errors[:3]:
            if isinstance(row, dict) and isinstance(row.get("reason"), str):
                reasons.append(redact(row["reason"], key))
        if reasons:
            return "; ".join(reasons)
    for field in ("error", "error_message", "message"):
        value = payload.get(field)
        if isinstance(value, str):
            return redact(value, key)
    return ""


def _error_text(status: int, blob: bytes, key: str) -> str:
    detail = ""
    if blob:
        try:
            payload = json.loads(blob.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            payload = None
        detail = _detail_text(payload, key)
        if not detail and payload is None:
            detail = redact(blob.decode("utf-8", "replace"), key)
    message = f"Grocy returned {status}"
    if detail:
        message = f"{message}: {detail}"
    return plain(message)


def _get_lists(origin: str, key: str, allow_loopback: bool) -> list[dict[str, object]]:
    status, blob = exchange(origin, key, "GET", "/api/objects/shopping_lists", allow_loopback=allow_loopback)
    if status != 200:
        raise GrocyError(_error_text(status, blob, key))
    return parse_lists(_json_body(blob, key))


def _matching(lists: list[dict[str, object]], name: str) -> list[int]:
    return sorted(int(row["id"]) for row in lists if row["name"] == name)


def ensure_order_list(origin: str, key: str, name: str, *, allow_loopback: bool) -> tuple[int, bool]:
    lists = _get_lists(origin, key, allow_loopback)
    matches = _matching(lists, name)
    created_id = None
    made = False
    if not matches:
        made = True
        status, blob = exchange(
            origin,
            key,
            "POST",
            "/api/objects/shopping_lists",
            {"name": name},
            allow_loopback=allow_loopback,
        )
        if status not in {200, 201}:
            raise GrocyError(_error_text(status, blob, key))
        payload = _json_body(blob, key)
        if isinstance(payload, dict):
            if "created_object_id" in payload:
                created_id = require_id(payload["created_object_id"])
            elif "id" in payload:
                created_id = require_id(payload["id"])
        lists = _get_lists(origin, key, allow_loopback)
        matches = _matching(lists, name)
        if created_id is None and len(matches) != 1:
            raise GrocyError("Grocy did not say which shopping list it created")
    if not matches:
        raise GrocyError("Grocy did not keep the shopping list")
    keep = matches[0]
    created = made
    if made and created_id is not None and created_id != keep:
        status, blob = exchange(
            origin,
            key,
            "DELETE",
            f"/api/objects/shopping_lists/{created_id}",
            allow_loopback=allow_loopback,
        )
        if status not in {200, 204}:
            raise GrocyError(_error_text(status, blob, key) or "Grocy could not remove the extra shopping list")
        created = False
    return keep, created


def add_product(origin: str, key: str, product_id: int, *, home: str, allow_loopback: bool = False, now: datetime | None = None) -> dict[str, object]:
    if product_id < 1 or product_id > 2_000_000_000:
        raise GrocyError("product id is not valid")
    name = next_order_list_name(now or datetime.now(ZONE))
    dirfd = state_dir(home)
    lock = -1
    try:
        lock = lock_add(dirfd)
        keep, created = ensure_order_list(origin, key, name, allow_loopback=allow_loopback)
        status, blob = exchange(
            origin,
            key,
            "POST",
            "/api/stock/shoppinglist/add-product",
            {"product_id": product_id, "list_id": keep, "product_amount": 1},
            allow_loopback=allow_loopback,
        )
        if status not in {200, 204}:
            raise GrocyError(_error_text(status, blob, key))
        return {"ok": True, "list": name, "list_id": keep, "created": created, "product_id": product_id}
    finally:
        if lock >= 0:
            _close(lock)
        _close(dirfd)


def _get_lines(origin: str, key: str, allow_loopback: bool) -> list[dict[str, object]]:
    status, blob = exchange(origin, key, "GET", "/api/objects/shopping_list", allow_loopback=allow_loopback)
    if status != 200:
        raise GrocyError(_error_text(status, blob, key))
    return parse_lines(_json_body(blob, key))


def order_lines(origin: str, key: str, name: str, *, allow_loopback: bool = False) -> list[dict[str, str]]:
    matches = _matching(_get_lists(origin, key, allow_loopback), name)
    if not matches:
        return []
    keep = matches[0]
    mine = [line for line in _get_lines(origin, key, allow_loopback) if line["list_id"] == keep]
    products = [line for line in mine if line.get("kind") == "product"]
    store = [line for line in mine if line.get("kind") == "store"]
    items: list[dict[str, str]] = []
    if products:
        status, blob = exchange(origin, key, "GET", "/api/objects/products", allow_loopback=allow_loopback)
        if status != 200:
            raise GrocyError(_error_text(status, blob, key))
        items.extend(group_order(products, parse_products(_json_body(blob, key))))
    for line in sorted(store, key=lambda row: str(row["name"]).casefold()):
        items.append({
            "id": f"s{int(line['id'])}",
            "name": str(line["name"]),
            "amount": format_total(float(line["amount"])),
            "price": str(line["price"]),
            "loyalty": str(line["loyalty"]),
            "krogerId": str(line["kroger_id"]),
            "upc": str(line["upc"]),
        })
    return items


def remove_product(origin: str, key: str, product_id: int, *, home: str, allow_loopback: bool = False, now: datetime | None = None) -> dict[str, object]:
    if product_id < 1 or product_id > 2_000_000_000:
        raise GrocyError("product id is not valid")
    name = next_order_list_name(now or datetime.now(ZONE))
    dirfd = state_dir(home)
    lock = -1
    try:
        lock = lock_add(dirfd)
        matches = _matching(_get_lists(origin, key, allow_loopback), name)
        if not matches:
            raise GrocyError("That item is not on the order")
        keep = matches[0]
        mine = [
            line for line in _get_lines(origin, key, allow_loopback)
            if line["list_id"] == keep and line.get("kind") == "product" and line.get("product_id") == product_id
        ]
        if not mine:
            raise GrocyError("That item is not on the order")
        if len(mine) > 40:
            raise GrocyError("too many lines for that item")
        for line in mine:
            status, blob = exchange(
                origin,
                key,
                "DELETE",
                f"/api/objects/shopping_list/{int(line['id'])}",
                allow_loopback=allow_loopback,
            )
            if status not in {200, 204}:
                raise GrocyError(_error_text(status, blob, key))
        return {"ok": True, "list": name, "list_id": keep, "product_id": product_id, "removed": len(mine)}
    finally:
        if lock >= 0:
            _close(lock)
        _close(dirfd)


def _one_store_line(origin: str, key: str, line_id: int, *, allow_loopback: bool, now: datetime | None) -> tuple[str, int, dict[str, object]]:
    if line_id < 1 or line_id > 2_000_000_000:
        raise GrocyError("That item is not on the order")
    name = next_order_list_name(now or datetime.now(ZONE))
    matches = _matching(_get_lists(origin, key, allow_loopback), name)
    if not matches:
        raise GrocyError("That item is not on the order")
    keep = matches[0]
    mine = [
        line for line in _get_lines(origin, key, allow_loopback)
        if line["list_id"] == keep and line.get("kind") == "store" and int(line["id"]) == line_id
    ]
    if len(mine) != 1:
        raise GrocyError("That item is not on the order")
    return name, keep, mine[0]


def set_store_amount(
    origin: str,
    key: str,
    line_id: int,
    quantity: int,
    *,
    home: str,
    allow_loopback: bool = False,
    now: datetime | None = None,
) -> dict[str, object]:
    qty = store_qty(quantity)
    dirfd = state_dir(home)
    lock = -1
    try:
        lock = lock_add(dirfd)
        name, keep, line = _one_store_line(origin, key, line_id, allow_loopback=allow_loopback, now=now)
        status, blob = exchange(
            origin,
            key,
            "PUT",
            f"/api/objects/shopping_list/{int(line['id'])}",
            {"amount": qty},
            allow_loopback=allow_loopback,
        )
        if status not in {200, 204}:
            raise GrocyError(_error_text(status, blob, key))
        return {"ok": True, "list": name, "list_id": keep, "line_id": int(line["id"]), "amount": qty}
    finally:
        if lock >= 0:
            _close(lock)
        _close(dirfd)


def remove_store_line(
    origin: str,
    key: str,
    line_id: int,
    *,
    home: str,
    allow_loopback: bool = False,
    now: datetime | None = None,
) -> dict[str, object]:
    dirfd = state_dir(home)
    lock = -1
    try:
        lock = lock_add(dirfd)
        name, keep, line = _one_store_line(origin, key, line_id, allow_loopback=allow_loopback, now=now)
        status, blob = exchange(
            origin,
            key,
            "DELETE",
            f"/api/objects/shopping_list/{int(line['id'])}",
            allow_loopback=allow_loopback,
        )
        if status not in {200, 204}:
            raise GrocyError(_error_text(status, blob, key))
        return {"ok": True, "list": name, "list_id": keep, "line_id": int(line["id"]), "removed": 1}
    finally:
        if lock >= 0:
            _close(lock)
        _close(dirfd)


def fetch_stock(origin: str, key: str, *, allow_loopback: bool = False, now: datetime | None = None) -> dict[str, object]:
    name = next_order_list_name(now or datetime.now(ZONE))
    status, blob = exchange(origin, key, "GET", "/api/stock", allow_loopback=allow_loopback)
    if status != 200:
        raise GrocyError(_error_text(status, blob, key))
    items = parse_stock(_json_body(blob, key))
    order = order_lines(origin, key, name, allow_loopback=allow_loopback)
    return {"ok": True, "list": name, "items": items, "order": order}


def global_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return bool(ip.is_global)


def resolve_global(host: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise GrocyError("Kroger did not respond") from exc
    pins: list[str] = []
    for info in infos:
        ip = info[4][0]
        if not global_ip(ip):
            raise GrocyError("Kroger address is not public")
        if ip not in pins:
            pins.append(ip)
    if not pins:
        raise GrocyError("Kroger address is not public")
    return pins[0]


def _kroger_origin(origin: str, allow_loopback: bool) -> tuple[str, int, str | None]:
    parts = urllib.parse.urlsplit(origin)
    if parts.username or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise GrocyError("refusing origin")
    if parts.scheme == "https" and parts.hostname == KROGER_HOST and parts.port in {None, 443}:
        return KROGER_HOST, 443, resolve_global(KROGER_HOST, 443)
    if (
        allow_loopback
        and parts.scheme == "http"
        and parts.hostname == "127.0.0.1"
        and parts.port is not None
        and 1 <= parts.port <= 65535
    ):
        return "127.0.0.1", parts.port, None
    raise GrocyError("refusing origin")


def _kroger_opener(pin: str | None, port: int) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({}), _NoRedirect()]
    if pin is not None:
        context = ssl.create_default_context()

        def connection(host, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, context=None, check_hostname=None):
            return _PinnedHTTPSConnection(
                host,
                timeout=timeout,
                context=context,
                check_hostname=check_hostname,
                pin=pin,
                port=port,
            )

        class _Handler(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(connection, req, context=context)

        handlers.append(_Handler())
    return urllib.request.build_opener(*handlers)


def kroger_exchange(
    origin: str,
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    bearer: str | None = None,
    basic: str | None = None,
    allow_loopback: bool = False,
    secrets: tuple[str, ...] = (),
) -> tuple[int, bytes]:
    host, port, pin = _kroger_origin(origin, allow_loopback)
    if method not in {"GET", "POST"}:
        raise GrocyError("refusing method")
    if "cart" in path.lower() or re.fullmatch(r"/v1/[A-Za-z0-9_./-]{1,80}", path) is None or ".." in path:
        raise GrocyError("refusing path")
    if (bearer is None) == (basic is None):
        raise GrocyError("refusing Kroger credentials")
    if bearer is not None and TOKEN_RE.fullmatch(bearer) is None:
        raise GrocyError("Kroger token was not valid")
    if basic is not None and re.fullmatch(r"[A-Za-z0-9+/=]{16,512}", basic) is None:
        raise GrocyError("refusing Kroger credentials")
    url = f"{'https' if pin else 'http'}://{host}:{port}{path}"
    if query:
        for key, value in query.items():
            if re.fullmatch(r"[A-Za-z.]{1,40}", key) is None:
                raise GrocyError("refusing query")
            if "\r" in value or "\n" in value or len(value) > 80:
                raise GrocyError("refusing query")
        url = url + "?" + urllib.parse.urlencode(query)
    data = None
    headers = {"Accept": "application/json", "User-Agent": "jordan-grocy"}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    else:
        headers["Authorization"] = f"Basic {basic}"
    if form is not None:
        data = urllib.parse.urlencode(form).encode("ascii")
        if len(data) > 256:
            raise GrocyError("request too large")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = _kroger_opener(pin, port)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            status = int(resp.status)
            blob = resp.read(MAX_BODY + 1)
    except GrocyError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            status = int(exc.code)
            blob = exc.read(MAX_BODY + 1)
        finally:
            exc.close()
    except (TimeoutError, socket.timeout, ssl.SSLError, urllib.error.URLError) as exc:
        raise GrocyError("Kroger did not respond") from exc
    if len(blob) > MAX_BODY:
        raise GrocyError("Kroger response was too large")
    if 300 <= status < 400:
        raise GrocyError("redirect refused")
    for secret in secrets:
        if secret and secret.encode() in blob:
            blob = blob.replace(secret.encode(), b"")
    return status, blob


def _kroger_error(status: int, blob: bytes, secrets: tuple[str, ...]) -> str:
    text = _error_text(status, blob, "")
    text = text.replace("Grocy returned", "Kroger returned", 1)
    for secret in secrets:
        text = text.replace(secret, "") if secret else text
    return plain(text) or f"Kroger returned {status}"


def cached_token(dirfd: int) -> str | None:
    try:
        raw = read_private_file(dirfd, "kroger.token", MAX_TOKEN)
    except GrocyError:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    token = doc.get("access_token")
    exp = doc.get("exp")
    if not isinstance(token, str) or TOKEN_RE.fullmatch(token) is None:
        return None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(float(exp)):
        return None
    if float(exp) < time.time() + 30:
        return None
    return token


def _parse_expires(value: object) -> int:
    if isinstance(value, bool):
        raise GrocyError("Kroger token was not valid")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{2,6}", value):
        number = int(value)
    else:
        raise GrocyError("Kroger token was not valid")
    if number < 60 or number > 86400:
        raise GrocyError("Kroger token was not valid")
    return number


def request_token(origin: str, client_id: str, secret: str, *, allow_loopback: bool) -> tuple[str, int]:
    if CLIENT_RE.fullmatch(client_id) is None or SECRET_RE.fullmatch(secret) is None:
        raise GrocyError("Kroger key file is not valid")
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode("ascii")
    status, blob = kroger_exchange(
        origin,
        "POST",
        "/v1/connect/oauth2/token",
        form={"grant_type": "client_credentials", "scope": "product.compact"},
        basic=basic,
        allow_loopback=allow_loopback,
        secrets=(client_id, secret),
    )
    if status != 200:
        raise GrocyError(_kroger_error(status, blob, (client_id, secret)))
    try:
        payload = json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GrocyError("Kroger token was not valid") from exc
    if not isinstance(payload, dict):
        raise GrocyError("Kroger token was not valid")
    token = payload.get("access_token")
    if not isinstance(token, str) or TOKEN_RE.fullmatch(token) is None:
        raise GrocyError("Kroger token was not valid")
    expires = _parse_expires(payload.get("expires_in"))
    return token, int(time.time()) + expires


def kroger_access_token(origin: str, client_id: str, secret: str, *, home: str, allow_loopback: bool, force: bool = False) -> str:
    dirfd = state_dir(home)
    lock = -1
    try:
        lock = lock_named(dirfd, "kroger.lock")
        if not force:
            cached = cached_token(dirfd)
            if cached:
                return cached
        token, exp = request_token(origin, client_id, secret, allow_loopback=allow_loopback)
        payload = json.dumps({"access_token": token, "exp": exp}, separators=(",", ":")).encode("ascii")
        write_private(dirfd, "kroger.token", payload)
        return token
    finally:
        if lock >= 0:
            _close(lock)
        _close(dirfd)


def _clean_name(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_NAME:
        return ""
    return plain(value, 120).replace("|", " ").strip()


def hit_from_product(product: object) -> dict[str, object]:
    if not isinstance(product, dict):
        raise GrocyError("store response had an unexpected row")
    raw_id = product.get("productId")
    if not isinstance(raw_id, str) or PRODUCT_RE.fullmatch(raw_id) is None:
        raise GrocyError("store response had an unexpected row")
    name = _clean_name(product.get("description"))
    price = ""
    loyalty = ""
    upc = ""
    items = product.get("items")
    item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
    raw_price = item.get("price") if isinstance(item, dict) else None
    if isinstance(raw_price, dict):
        priced = money(raw_price.get("regular"))
        if priced:
            price = priced
        # promo is the only sale price. 0 and any other key are not a sale.
        if "promo" in raw_price:
            found = money(raw_price.get("promo"))
            if found:
                loyalty = found
    raw_upc = item.get("upc") if isinstance(item, dict) else None
    if isinstance(raw_upc, str) and UPC_RE.fullmatch(raw_upc):
        upc = raw_upc
    return {
        "id": raw_id,
        "name": name,
        "price": price,
        "loyalty": loyalty,
        "upc": upc,
        "complete": bool(name and price),
    }


def parse_search(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        raise GrocyError("store response was not a list")
    data = payload.get("data")
    if not isinstance(data, list):
        raise GrocyError("store response was not a list")
    if len(data) > MAX_HITS:
        raise GrocyError("store response was too large")
    return [hit_from_product(row) for row in data]


def parse_detail(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise GrocyError("store response had an unexpected row")
    data = payload.get("data")
    if isinstance(data, list):
        if len(data) != 1:
            raise GrocyError("store response had an unexpected row")
        data = data[0]
    return hit_from_product(data)


def kroger_call(origin: str, client_id: str, secret: str, path: str, query: dict[str, str], *, home: str, allow_loopback: bool) -> object:
    secrets = (client_id, secret)
    token = kroger_access_token(origin, client_id, secret, home=home, allow_loopback=allow_loopback)
    status, blob = kroger_exchange(
        origin,
        "GET",
        path,
        query=query,
        bearer=token,
        allow_loopback=allow_loopback,
        secrets=secrets,
    )
    if status == 401:
        token = kroger_access_token(origin, client_id, secret, home=home, allow_loopback=allow_loopback, force=True)
        status, blob = kroger_exchange(
            origin,
            "GET",
            path,
            query=query,
            bearer=token,
            allow_loopback=allow_loopback,
            secrets=secrets,
        )
    if status != 200:
        raise GrocyError(_kroger_error(status, blob, secrets))
    try:
        return json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GrocyError("store response was not JSON") from exc


def search_term(text: str) -> str:
    term = plain(text, 80)
    if len(term) < 3:
        raise GrocyError("Enter at least 3 characters.")
    if TERM_RE.fullmatch(term) is None:
        raise GrocyError("Enter a store item to search for.")
    return term


def page_total(payload: object, count: int) -> int:
    if not isinstance(payload, dict):
        return count
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return count
    page = meta.get("pagination")
    if not isinstance(page, dict):
        return count
    total = page.get("total")
    if isinstance(total, bool) or not isinstance(total, int):
        return count
    if total < count or total > 5000:
        return count
    return total


def parse_location(row: object) -> dict[str, str]:
    if not isinstance(row, dict):
        raise GrocyError("store list had an unexpected row")
    loc = row.get("locationId")
    if not isinstance(loc, str) or LOCATION_RE.fullmatch(loc) is None:
        raise GrocyError("store list had an unexpected row")
    raw_name = row.get("name")
    name = plain(raw_name, 80) if isinstance(raw_name, str) and len(raw_name) <= MAX_NAME else ""
    if not name:
        raise GrocyError("store list had an unexpected row")
    addr = row.get("address") if isinstance(row.get("address"), dict) else {}
    raw_line = addr.get("addressLine1")
    raw_city = addr.get("city")
    line = plain(raw_line, 80) if isinstance(raw_line, str) and len(raw_line) <= MAX_NAME else ""
    city = plain(raw_city, 40) if isinstance(raw_city, str) and len(raw_city) <= 40 else ""
    zip_code = addr.get("zipCode") if isinstance(addr.get("zipCode"), str) else ""
    if ZIP_RE.fullmatch(zip_code) is None:
        zip_code = ""
    chain = row.get("chain") if isinstance(row.get("chain"), str) else ""
    if CHAIN_RE.fullmatch(chain) is None:
        chain = ""
    return {"id": loc, "name": name, "line": line, "city": city, "zip": zip_code, "chain": chain}


def parse_locations(payload: object) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        raise GrocyError("store list was not a list")
    data = payload.get("data")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise GrocyError("store list was not a list")
    if len(data) > MAX_STORES:
        raise GrocyError("store list was too large")
    return [parse_location(row) for row in data]


def home_store(
    origin: str,
    client_id: str,
    secret: str,
    location_id: str,
    *,
    home: str,
    allow_loopback: bool = False,
) -> dict[str, object]:
    if LOCATION_RE.fullmatch(location_id) is None:
        raise GrocyError("Kroger key file is not valid")
    payload = kroger_call(
        origin,
        client_id,
        secret,
        f"/v1/locations/{location_id}",
        {},
        home=home,
        allow_loopback=allow_loopback,
    )
    rows = parse_locations(payload)
    if len(rows) != 1 or rows[0]["id"] != location_id:
        raise GrocyError("That store could not be loaded")
    row = rows[0]
    return {"ok": True, "id": row["id"], "name": row["name"], "line": row["line"], "city": row["city"], "zip": row["zip"]}


def nearby_stores(
    origin: str,
    client_id: str,
    secret: str,
    zip_code: str,
    *,
    home: str,
    allow_loopback: bool = False,
    chain: str = DEFAULT_CHAIN,
) -> dict[str, object]:
    if ZIP_RE.fullmatch(zip_code) is None:
        raise GrocyError("Enter a 5-digit ZIP.")
    if CHAIN_RE.fullmatch(chain) is None:
        raise GrocyError("Kroger key file is not valid")
    payload = kroger_call(
        origin,
        client_id,
        secret,
        "/v1/locations",
        {"filter.zipCode.near": zip_code, "filter.chain": chain, "filter.limit": str(MAX_STORES)},
        home=home,
        allow_loopback=allow_loopback,
    )
    return {"ok": True, "zip": zip_code, "stores": parse_locations(payload)}


def save_home_store(
    origin: str,
    client_id: str,
    secret: str,
    location_id: str,
    *,
    home: str,
    allow_loopback: bool = False,
    chain: str = DEFAULT_CHAIN,
) -> dict[str, object]:
    if LOCATION_RE.fullmatch(location_id) is None or CHAIN_RE.fullmatch(chain) is None:
        raise GrocyError("That store could not be saved")
    payload = kroger_call(
        origin,
        client_id,
        secret,
        f"/v1/locations/{location_id}",
        {},
        home=home,
        allow_loopback=allow_loopback,
    )
    rows = parse_locations(payload)
    if len(rows) != 1 or rows[0]["id"] != location_id or rows[0]["chain"] != chain:
        raise GrocyError("That store could not be saved")
    state = state_dir(home)
    lock = -1
    folder = -1
    try:
        lock = lock_named(state, "kroger.lock")
        folder = config_dir(home)
        raw = read_private_file(folder, CONFIG_NAME, MAX_ENV)
        body = replace_location(raw.decode("utf-8", "strict"), location_id)
        write_private(folder, CONFIG_NAME, body.encode("utf-8"))
    finally:
        if folder >= 0:
            _close(folder)
        if lock >= 0:
            _close(lock)
        _close(state)
    row = rows[0]
    return {"ok": True, "id": row["id"], "name": row["name"], "line": row["line"], "city": row["city"], "zip": row["zip"]}


def search_store(
    origin: str,
    client_id: str,
    secret: str,
    location_id: str,
    term: str,
    *,
    home: str,
    allow_loopback: bool = False,
    start: int = 0,
) -> dict[str, object]:
    if LOCATION_RE.fullmatch(location_id) is None:
        raise GrocyError("Kroger key file is not valid")
    if isinstance(start, bool) or not isinstance(start, int) or start < 0 or start > MAX_PAGE_START:
        raise GrocyError("Enter a store item to search for.")
    cleaned = search_term(term)
    payload = kroger_call(
        origin,
        client_id,
        secret,
        "/v1/products",
        {
            "filter.term": cleaned,
            "filter.locationId": location_id,
            "filter.limit": str(MAX_HITS),
            "filter.start": str(start),
        },
        home=home,
        allow_loopback=allow_loopback,
    )
    hits = parse_search(payload)
    return {"ok": True, "store": location_id, "start": start, "total": page_total(payload, len(hits)), "hits": hits}


def require_complete(hit: dict[str, object]) -> None:
    if not hit.get("name"):
        raise GrocyError("That item has no name")
    if not hit.get("price"):
        raise GrocyError("That item has no price")
    if not isinstance(hit.get("id"), str) or PRODUCT_RE.fullmatch(hit["id"]) is None:
        raise GrocyError("That item has no product id")


def fetch_hit(
    origin: str,
    client_id: str,
    secret: str,
    location_id: str,
    product_id: str,
    *,
    home: str,
    allow_loopback: bool,
) -> dict[str, object]:
    if LOCATION_RE.fullmatch(location_id) is None or PRODUCT_RE.fullmatch(product_id) is None:
        raise GrocyError("That item could not be added")
    payload = kroger_call(
        origin,
        client_id,
        secret,
        f"/v1/products/{product_id}",
        {"filter.locationId": location_id},
        home=home,
        allow_loopback=allow_loopback,
    )
    hit = parse_detail(payload)
    if hit["id"] != product_id:
        raise GrocyError("store response had an unexpected row")
    return hit


def store_qty(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 99:
        raise GrocyError("Quantity must be from 1 to 99.")
    return value


def add_store_hit(
    grocy_origin: str,
    grocy_key: str,
    kroger_origin: str,
    client_id: str,
    secret: str,
    location_id: str,
    product_id: str,
    *,
    home: str,
    allow_loopback: bool = False,
    now: datetime | None = None,
    quantity: int = 1,
) -> dict[str, object]:
    qty = store_qty(quantity)
    hit = fetch_hit(
        kroger_origin,
        client_id,
        secret,
        location_id,
        product_id,
        home=home,
        allow_loopback=allow_loopback,
    )
    require_complete(hit)
    name = next_order_list_name(now or datetime.now(ZONE))
    note = store_note(str(hit["name"]), str(hit["price"]), str(hit["loyalty"]), str(hit["id"]), str(hit.get("upc") or ""))
    dirfd = state_dir(home)
    lock = -1
    created = False
    try:
        lock = lock_add(dirfd)
        keep, created = ensure_order_list(grocy_origin, grocy_key, name, allow_loopback=allow_loopback)
        status, blob = exchange(
            grocy_origin,
            grocy_key,
            "POST",
            "/api/objects/shopping_list",
            {"shopping_list_id": keep, "amount": qty, "note": note},
            allow_loopback=allow_loopback,
        )
        if status not in {200, 201}:
            raise GrocyError(_error_text(status, blob, grocy_key))
        return {
            "ok": True,
            "list": name,
            "list_id": keep,
            "created": created,
            "name": hit["name"],
            "amount": qty,
            "krogerId": hit["id"],
            "upc": hit.get("upc") or "",
        }
    except Exception:
        if created:
            try:
                exchange(
                    grocy_origin,
                    grocy_key,
                    "DELETE",
                    f"/api/objects/shopping_lists/{keep}",
                    allow_loopback=allow_loopback,
                )
            except GrocyError:
                pass
        raise
    finally:
        if lock >= 0:
            _close(lock)
        _close(dirfd)


def emit(doc: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(doc, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    home = passwd_home()
    apply_config_zone(home)
    if len(argv) == 1 and argv[0] == "list-name":
        emit({"ok": True, "list": next_order_list_name(datetime.now(ZONE))})
        return 0
    if len(argv) == 1 and argv[0] == "home-store":
        client_id, secret, location_id, _chain = load_kroger(home)
        emit(home_store(KROGER_ORIGIN, client_id, secret, location_id, home=home))
        return 0
    if len(argv) == 2 and argv[0] == "stores" and ZIP_RE.fullmatch(argv[1]):
        client_id, secret, _location_id, chain = load_kroger(home)
        emit(nearby_stores(KROGER_ORIGIN, client_id, secret, argv[1], home=home, chain=chain))
        return 0
    if len(argv) == 2 and argv[0] == "use-store" and LOCATION_RE.fullmatch(argv[1]):
        client_id, secret, _location_id, chain = load_kroger(home)
        emit(save_home_store(KROGER_ORIGIN, client_id, secret, argv[1], home=home, chain=chain))
        return 0
    if len(argv) in {2, 3} and argv[0] == "search":
        start = 0
        if len(argv) == 3:
            if re.fullmatch(r"[0-9]{1,3}", argv[2]) is None:
                emit({"ok": False, "error": "usage: grocy.py stock|list-name|search TERM [START]|stores ZIP|use-store LOCATION|home-store|add PRODUCT_ID|add-store PRODUCT_ID [QTY]|store-qty LINE_ID QTY|store-take LINE_ID|remove PRODUCT_ID"})
                return 2
            start = int(argv[2])
        client_id, secret, location_id, _chain = load_kroger(home)
        emit(search_store(KROGER_ORIGIN, client_id, secret, location_id, argv[1], home=home, start=start))
        return 0
    origin, key = load_credentials(home)
    if len(argv) == 1 and argv[0] == "stock":
        emit(fetch_stock(origin, key))
        return 0
    if len(argv) in {2, 3} and argv[0] == "add-store" and PRODUCT_RE.fullmatch(argv[1]):
        quantity = 1
        if len(argv) == 3:
            if re.fullmatch(r"[1-9][0-9]?", argv[2]) is None:
                emit({"ok": False, "error": "Quantity must be from 1 to 99."})
                return 2
            quantity = int(argv[2])
        client_id, secret, location_id, _chain = load_kroger(home)
        emit(add_store_hit(origin, key, KROGER_ORIGIN, client_id, secret, location_id, argv[1], home=home, quantity=quantity))
        return 0
    if len(argv) == 3 and argv[0] == "store-qty" and ID_RE.fullmatch(argv[1]) and re.fullmatch(r"[1-9][0-9]?", argv[2]):
        emit(set_store_amount(origin, key, int(argv[1]), int(argv[2]), home=home))
        return 0
    if len(argv) == 2 and argv[0] == "store-take" and ID_RE.fullmatch(argv[1]):
        emit(remove_store_line(origin, key, int(argv[1]), home=home))
        return 0
    if len(argv) == 2 and argv[0] in {"add", "remove"} and ID_RE.fullmatch(argv[1]):
        product_id = int(argv[1])
        if argv[0] == "add":
            emit(add_product(origin, key, product_id, home=home))
        else:
            emit(remove_product(origin, key, product_id, home=home))
        return 0
    emit({"ok": False, "error": "usage: grocy.py stock|list-name|search TERM [START]|stores ZIP|use-store LOCATION|home-store|add PRODUCT_ID|add-store PRODUCT_ID [QTY]|store-qty LINE_ID QTY|store-take LINE_ID|remove PRODUCT_ID"})
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except GrocyError as exc:
        emit({"ok": False, "error": plain(str(exc)) or "Grocy request failed"})
        sys.exit(1)
    except Exception:
        emit({"ok": False, "error": "grocy helper failed"})
        sys.exit(1)
