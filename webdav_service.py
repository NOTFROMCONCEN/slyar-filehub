"""内网 WebDAV 服务（Class 1 + Class 2 锁），供 GUI 和命令行入口共用。

用 Windows 资源管理器"映射网络驱动器"、WinSCP、RaiDrive、Cyberduck 等
客户端即可像本地磁盘一样浏览共享目录。仅使用 Python 标准库。
"""

import base64
import errno
import hmac
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape

LOGGER = logging.getLogger(__name__)

DAV_NS = "DAV:"
LOCK_TIMEOUT_DEFAULT = 600
LOCK_TIMEOUT_MAX = 3600
MAX_PROPFIND_ENTRIES = 50000
LOCK_TOKEN_RE = re.compile(r"<(opaquelocktoken:[^>]+|urn:uuid:[^>]+)>")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

WEBDAV_METHODS = (
    "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, PROPPATCH, "
    "MKCOL, COPY, MOVE, LOCK, UNLOCK"
)


class _Locked(Exception):
    """请求的资源被其他持有者的锁保护。"""


class LockManager:
    """内存写锁表（Class 2）；程序重启后锁自然失效。"""

    def __init__(self):
        self._mutex = threading.Lock()
        self._locks = {}

    @staticmethod
    def new_token():
        return f"opaquelocktoken:{uuid.uuid4()}"

    @staticmethod
    def _covers(lock, path):
        if lock["path"] == path:
            return True
        return lock["depth"] == "infinity" and path.startswith(
            lock["path"].rstrip("/") + "/"
        )

    def _purge(self, now):
        expired = [token for token, lock in self._locks.items() if lock["expires"] <= now]
        for token in expired:
            self._locks.pop(token, None)

    def conflicts(self, path):
        now = time.time()
        with self._mutex:
            self._purge(now)
            return [dict(lock) for lock in self._locks.values() if self._covers(lock, path)]

    def acquire(self, path, depth, exclusive, timeout, owner):
        now = time.time()
        with self._mutex:
            self._purge(now)
            for lock in self._locks.values():
                covers_new = self._covers(lock, path)
                covered_by_new = lock["path"] == path or (
                    depth == "infinity"
                    and lock["path"].startswith(path.rstrip("/") + "/")
                )
                if (covers_new or covered_by_new) and (exclusive or lock["exclusive"]):
                    return None
            token = self.new_token()
            lock = {
                "token": token,
                "path": path,
                "depth": depth,
                "exclusive": exclusive,
                "owner": owner,
                "expires": now + timeout,
            }
            self._locks[token] = lock
            return dict(lock)

    def refresh(self, token, timeout):
        now = time.time()
        with self._mutex:
            self._purge(now)
            lock = self._locks.get(token)
            if lock is None:
                return None
            lock["expires"] = now + timeout
            return dict(lock)

    def release(self, token):
        with self._mutex:
            return self._locks.pop(token, None) is not None


class _WebDAVHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SlyarWebDAV/1.0"

    # 由 make_webdav_handler 注入
    root = ""
    username = ""
    password = ""
    permissions = {}
    locks = None

    # ---------- 基础工具 ----------

    def log_message(self, fmt, *args):
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def _can(self, action, default=False):
        return bool(self.permissions.get(action, default))

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:].strip()).decode("utf-8")
        except Exception:
            return False
        username, _, password = raw.partition(":")
        ok_user = hmac.compare_digest(username.encode(), self.username.encode())
        ok_pass = hmac.compare_digest(password.encode(), self.password.encode())
        return ok_user and ok_pass

    def _challenge(self):
        self._send_empty(401, {"WWW-Authenticate": 'Basic realm="Slyar WebDAV"'})

    def _send_empty(self, status, extra=None):
        self.send_response(status)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_bytes(self, status, body, content_type, extra=None):
        self.send_response(status)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drain_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length) or b""

    def _resolve(self, url_path):
        path = unquote(url_path).split("?", 1)[0].replace("\\", "/")
        parts = []
        for part in path.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                raise PermissionError("path traversal")
            parts.append(part)
        rel = "/" + "/".join(parts)
        target = os.path.realpath(os.path.join(self.root, *parts))
        try:
            inside = os.path.normcase(
                os.path.commonpath((self.root, target))
            ) == os.path.normcase(self.root)
        except ValueError:
            inside = False
        if not inside:
            raise PermissionError("outside shared directory")
        return rel, target

    @staticmethod
    def _etag(st):
        return f"{st.st_mtime_ns:x}-{st.st_size:x}"

    @staticmethod
    def _creation_iso(st):
        return datetime.fromtimestamp(st.st_ctime, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def _provided_tokens(self):
        header = " ".join(
            value for key, value in self.headers.items() if key.lower() == "if"
        )
        return set(LOCK_TOKEN_RE.findall(header))

    def _check_locks(self, rel):
        provided = self._provided_tokens()
        for lock in self.locks.conflicts(rel):
            if lock["token"] not in provided:
                raise _Locked(lock["token"])

    @staticmethod
    def _parse_timeout(header_value):
        match = re.search(r"Second-(\d+)", header_value or "")
        timeout = int(match.group(1)) if match else LOCK_TIMEOUT_DEFAULT
        return max(1, min(timeout, LOCK_TIMEOUT_MAX))

    @staticmethod
    def _parse_lock_scope(body):
        try:
            tree = ET.fromstring(body)
        except ET.ParseError:
            return True, b""
        exclusive = tree.find(f".//{{{DAV_NS}}}exclusive") is not None
        owner = tree.find(f".//{{{DAV_NS}}}owner")
        owner_xml = ET.tostring(owner, encoding="utf-8") if owner is not None else b""
        return exclusive, owner_xml[:512]

    # ---------- 分发 ----------

    def _dispatch(self, action):
        try:
            if not self._authorized():
                return self._challenge()
            action()
        except _Locked:
            try:
                if int(self.headers.get("Content-Length") or 0) > 0:
                    # 兜底：带请求体的请求若锁拒绝时未读完，直接断开避免污染下一请求
                    self.close_connection = True
            except ValueError:
                self.close_connection = True
            self._send_empty(423)
        except PermissionError:
            self._send_empty(403)
        except FileNotFoundError:
            self._send_empty(404)
        except FileExistsError:
            self._send_empty(405)
        except NotADirectoryError:
            self._send_empty(409)
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                self._send_empty(403)
            elif exc.errno == errno.ENOSPC:
                self._send_empty(507)
            else:
                LOGGER.exception("WebDAV 请求处理失败")
                self._send_empty(500)
        except Exception:
            LOGGER.exception("WebDAV 请求处理失败")
            try:
                self._send_empty(500)
            except Exception:
                self.close_connection = True

    def do_OPTIONS(self):
        self._dispatch(self._options)

    def do_GET(self):
        self._dispatch(self._get)

    def do_HEAD(self):
        self._dispatch(self._head)

    def do_PUT(self):
        self._dispatch(self._put)

    def do_DELETE(self):
        self._dispatch(self._delete)

    def do_MKCOL(self):
        self._dispatch(self._mkcol)

    def do_PROPFIND(self):
        self._dispatch(self._propfind)

    def do_PROPPATCH(self):
        self._dispatch(self._proppatch)

    def do_COPY(self):
        self._dispatch(self._copy)

    def do_MOVE(self):
        self._dispatch(self._move)

    def do_LOCK(self):
        self._dispatch(self._lock)

    def do_UNLOCK(self):
        self._dispatch(self._unlock)

    # ---------- 各方法实现 ----------

    def _options(self):
        self.send_response(200)
        self.send_header("DAV", "1, 2")
        self.send_header("Allow", WEBDAV_METHODS)
        self.send_header("MS-Author-Via", "DAV")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _get(self):
        self._serve_file(send_body=True)

    def _head(self):
        self._serve_file(send_body=False)

    def _serve_file(self, send_body):
        if not self._can("download", True):
            return self._send_empty(403)
        rel, target = self._resolve(self.path)
        if not os.path.exists(target):
            return self._send_empty(404)
        if os.path.isdir(target):
            return self._serve_directory(rel, target, send_body)

        st = os.stat(target)
        size = st.st_size
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype = f"{ctype}; charset=utf-8"

        status = 200
        start, end = 0, size - 1
        match = RANGE_RE.match((self.headers.get("Range") or "").strip())
        if match and size:
            first, last = match.groups()
            if first == "" and last:
                start = max(0, size - int(last))
            elif first:
                start = min(int(first), size - 1)
                end = min(int(last), size - 1) if last else size - 1
            if start > end:
                return self._send_empty(
                    416, {"Content-Range": f"bytes */{size}"}
                )
            status = 206

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self._http_date(st.st_mtime))
        self.send_header("ETag", f'"{self._etag(st)}"')
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not send_body or size == 0:
            return
        with open(target, "rb") as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    @staticmethod
    def _http_date(timestamp):
        return formatdate(timestamp, usegmt=True)

    def _serve_directory(self, rel, target, send_body):
        if send_body:
            items = sorted(os.listdir(target), key=str.lower)
            rows = []
            base = quote(rel)
            for name in items:
                href = base.rstrip("/") + "/" + quote(name)
                suffix = "/" if os.path.isdir(os.path.join(target, name)) else ""
                rows.append(f'<li><a href="{href}">{escape(name)}{suffix}</a></li>')
            html = (
                '<!doctype html><meta charset="utf-8">'
                f"<title>WebDAV {escape(rel)}</title>"
                f"<h2>WebDAV 目录 {escape(rel)}</h2><ul>{''.join(rows)}</ul>"
            ).encode("utf-8")
            return self._send_bytes(200, html, "text/html; charset=utf-8")
        self._send_empty(200)

    def _put(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self.close_connection = True
            return self._send_empty(411)
        if not self._can("upload"):
            self._drain_body()
            return self._send_empty(403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        rel, target = self._resolve(self.path)
        if os.path.isdir(target):
            self._drain_body()
            return self._send_empty(409)
        parent = os.path.dirname(target)
        if not os.path.isdir(parent):
            self._drain_body()
            return self._send_empty(409)

        existed = os.path.exists(target)
        tmp = f"{target}.webdav-{threading.get_ident()}.tmp"
        try:
            try:
                with open(tmp, "wb") as out:
                    remaining = length
                    while remaining > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ConnectionError("truncated request body")
                        out.write(chunk)
                        remaining -= len(chunk)
                # 锁检查放在请求体读取之后，保证 keep-alive 连接不残留字节
                self._check_locks(rel)
            except _Locked:
                return self._send_empty(423)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        self._send_empty(204 if existed else 201)

    def _delete(self):
        if not self._can("delete"):
            return self._send_empty(403)
        self._drain_body()
        rel, target = self._resolve(self.path)
        if not os.path.lexists(target):
            return self._send_empty(404)
        if os.path.normcase(target) == os.path.normcase(self.root):
            return self._send_empty(403)
        self._check_locks(rel)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
        self._send_empty(204)

    def _mkcol(self):
        if not self._can("mkdir"):
            return self._send_empty(403)
        self._drain_body()
        rel, target = self._resolve(self.path)
        if os.path.lexists(target):
            return self._send_empty(405)
        if not os.path.isdir(os.path.dirname(target)):
            return self._send_empty(409)
        self._check_locks(os.path.dirname(rel).rstrip("/") or "/")
        os.mkdir(target)
        self._send_empty(201)

    def _propfind(self):
        self._drain_body()
        if not self._can("download", True):
            return self._send_empty(403)
        rel, target = self._resolve(self.path)
        if not os.path.lexists(target):
            return self._send_empty(404)
        depth = (self.headers.get("Depth") or "infinity").strip().lower()
        if depth not in ("0", "1", "infinity"):
            depth = "infinity"
        entries = []
        self._collect_props(target, rel, depth, entries)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<D:multistatus xmlns:D="{DAV_NS}">' + "".join(entries) + "</D:multistatus>"
        ).encode("utf-8")
        self._send_bytes(
            207, body, 'application/xml; charset="utf-8"'
        )

    def _collect_props(self, target, rel, depth, entries):
        if len(entries) >= MAX_PROPFIND_ENTRIES:
            return
        entries.append(self._prop_response(rel, target))
        if os.path.isdir(target) and not os.path.islink(target) and depth in ("1", "infinity"):
            child_depth = "1" if depth == "1" else "infinity"
            try:
                with os.scandir(target) as iterator:
                    children = list(iterator)
            except OSError:
                return
            for entry in children:
                if len(entries) >= MAX_PROPFIND_ENTRIES:
                    return
                child_rel = rel.rstrip("/") + "/" + entry.name
                self._collect_props(
                    os.path.join(target, entry.name), child_rel, child_depth, entries
                )

    def _prop_response(self, rel, target):
        try:
            st = os.stat(target)
        except OSError:
            return ""
        is_dir = os.path.isdir(target)
        displayname = rel.rstrip("/").rsplit("/", 1)[-1] or "/"
        href = quote(rel)
        parts = [
            "<D:response>",
            f"<D:href>{href}</D:href>",
            "<D:propstat><D:prop>",
            f"<D:displayname>{escape(displayname)}</D:displayname>",
            "<D:resourcetype>"
            + ("<D:collection/>" if is_dir else "")
            + "</D:resourcetype>",
        ]
        if not is_dir:
            ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
            parts.append(f"<D:getcontentlength>{st.st_size}</D:getcontentlength>")
            parts.append(f"<D:getcontenttype>{escape(ctype)}</D:getcontenttype>")
        parts.extend(
            [
                f"<D:getlastmodified>{self._http_date(st.st_mtime)}</D:getlastmodified>",
                f"<D:creationdate>{self._creation_iso(st)}</D:creationdate>",
                f"<D:getetag>&quot;{self._etag(st)}&quot;</D:getetag>",
                "<D:supportedlock>"
                '<D:lockentry><D:lockscope><D:exclusive/></D:lockscope>'
                "<D:locktype><D:write/></D:locktype></D:lockentry>"
                "</D:supportedlock>",
                self._lock_discovery(rel),
            ]
        )
        parts.append("</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>")
        parts.append("</D:response>")
        return "".join(parts)

    def _lock_discovery(self, rel):
        locks = self.locks.conflicts(rel)
        if not locks:
            return "<D:lockdiscovery/>"
        chunks = ["<D:lockdiscovery>"]
        for lock in locks:
            timeout = max(1, int(lock["expires"] - time.time()))
            scope = "<D:exclusive/>" if lock["exclusive"] else "<D:shared/>"
            owner = lock["owner"].decode("utf-8", "replace") if lock["owner"] else ""
            chunks.append(
                "<D:activelock>"
                "<D:locktype><D:write/></D:locktype>"
                f"<D:lockscope>{scope}</D:lockscope>"
                f"<D:depth>{lock['depth']}</D:depth>"
                f"<D:timeout>Second-{timeout}</D:timeout>"
                f"<D:locktoken><D:href>{escape(lock['token'])}</D:href></D:locktoken>"
                f"<D:owner>{escape(owner)}</D:owner>"
                "</D:activelock>"
            )
        chunks.append("</D:lockdiscovery>")
        return "".join(chunks)

    def _proppatch(self):
        self._drain_body()
        rel, target = self._resolve(self.path)
        if not os.path.lexists(target):
            return self._send_empty(404)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<D:multistatus xmlns:D="{DAV_NS}">'
            f"<D:response><D:href>{quote(rel)}</D:href>"
            "<D:propstat><D:prop/>"
            "<D:status>HTTP/1.1 200 OK</D:status>"
            "</D:propstat></D:response></D:multistatus>"
        ).encode("utf-8")
        self._send_bytes(207, body, 'application/xml; charset="utf-8"')

    def _copy(self):
        self._copymove(copy=True)

    def _move(self):
        self._copymove(copy=False)

    def _copymove(self, copy):
        if not self._can("rename"):
            return self._send_empty(403)
        self._drain_body()
        src_rel, src = self._resolve(self.path)
        if not os.path.lexists(src):
            return self._send_empty(404)
        if os.path.normcase(src) == os.path.normcase(self.root):
            return self._send_empty(403)
        destination = self.headers.get("Destination") or ""
        if not destination:
            return self._send_empty(400)
        try:
            dest_rel, dest = self._resolve(urlsplit(destination).path)
        except PermissionError:
            return self._send_empty(403)
        if os.path.normcase(dest) == os.path.normcase(self.root):
            return self._send_empty(403)
        if os.path.normcase(src) == os.path.normcase(dest):
            return self._send_empty(403)
        overwrite = (self.headers.get("Overwrite") or "T").strip().upper() != "F"
        overwrote = False
        if os.path.lexists(dest):
            if not overwrite:
                return self._send_empty(412)
            self._check_locks(dest_rel)
            overwrote = True
            if os.path.isdir(dest) and not os.path.islink(dest):
                shutil.rmtree(dest)
            else:
                os.remove(dest)
        self._check_locks(src_rel)
        if copy:
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, dest)
            else:
                shutil.copy2(src, dest)
        else:
            shutil.move(src, dest)
        self._send_empty(204 if overwrote else 201)

    def _lock(self):
        if not any(self._can(name) for name in ("upload", "delete", "rename", "mkdir")):
            return self._send_empty(403)
        body = self._drain_body()
        rel, _ = self._resolve(self.path)
        depth = (self.headers.get("Depth") or "infinity").strip().lower()
        depth = "0" if depth == "0" else "infinity"
        timeout = self._parse_timeout(self.headers.get("Timeout"))
        provided = self._provided_tokens()

        if not body.strip():
            token = next(iter(provided), None)
            if token:
                lock = self.locks.refresh(token, timeout)
                if lock is not None:
                    return self._send_lock_response(lock, new=False)
            return self._send_empty(412)

        exclusive, owner = self._parse_lock_scope(body)
        lock = self.locks.acquire(rel, depth, exclusive, timeout, owner)
        if lock is None:
            return self._send_empty(423)
        self._send_lock_response(lock, new=True)

    def _send_lock_response(self, lock, new):
        timeout = max(1, min(int(lock["expires"] - time.time()), LOCK_TIMEOUT_MAX))
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<D:prop xmlns:D="{DAV_NS}">'
            "<D:lockdiscovery><D:activelock>"
            "<D:locktype><D:write/></D:locktype>"
            + (
                "<D:lockscope><D:exclusive/></D:lockscope>"
                if lock.get("exclusive")
                else "<D:lockscope><D:shared/></D:lockscope>"
            )
            + f"<D:depth>{lock.get('depth', 'infinity')}</D:depth>"
            f"<D:timeout>Second-{timeout}</D:timeout>"
            f"<D:locktoken><D:href>{escape(lock['token'])}</D:href></D:locktoken>"
            "</D:activelock></D:lockdiscovery></D:prop>"
        ).encode("utf-8")
        self.send_response(200)
        if new:
            self.send_header("Lock-Token", f"<{lock['token']}>")
        self.send_header("Content-Type", 'application/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _unlock(self):
        self._drain_body()
        token = (self.headers.get("Lock-Token") or "").strip().strip("<>")
        if token and self.locks.release(token):
            return self._send_empty(204)
        self._send_empty(409)


def make_webdav_handler(root, username, password, permissions=None):
    """创建绑定共享目录与账户的请求处理器类（与 make_web_handler 风格一致）。"""
    return type(
        "ConfiguredWebDAVHandler",
        (_WebDAVHandler,),
        {
            "root": os.path.realpath(root),
            "username": username,
            "password": password,
            "permissions": permissions or {},
            "locks": LockManager(),
        },
    )


class WebDAVService:
    """线程化 WebDAV 服务，接口与 SFTPService 保持一致。"""

    def __init__(self, port, username, password, directory, permissions=None):
        self.port = port
        self.username = username
        self.password = password
        self.directory = directory
        self.permissions = permissions or {}
        self.server = None
        self.thread = None
        self.stopped = threading.Event()

    def start(self):
        if (
            not self.username
            or not self.password
            or self.username.lower() == "anonymous"
        ):
            raise ValueError("WebDAV 需要非 anonymous 的用户名和密码")
        if not os.path.isdir(self.directory):
            raise ValueError("WebDAV 共享目录不存在")
        handler = make_webdav_handler(
            self.directory, self.username, self.password, self.permissions
        )
        try:
            self.server = ThreadingHTTPServer(("", self.port), handler)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10048:
                raise OSError(
                    f"端口 {self.port} 已被其他程序占用，请更换 WebDAV 端口或关闭占用该端口的程序"
                )
            raise
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.stopped.clear()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        LOGGER.info("WebDAV 服务已启动在端口 %s", self.port)

    def stop(self):
        self.stopped.set()
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            finally:
                self.server = None
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None
        LOGGER.info("WebDAV 服务已停止")
