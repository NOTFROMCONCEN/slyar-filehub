"""Paramiko SFTP 服务，供 GUI 和命令行入口共用。"""

import errno
import hmac
import logging
import os
import socket
import threading
import time

import paramiko

LOGGER = logging.getLogger(__name__)


class _AuthServer(paramiko.ServerInterface):
    def __init__(self, username, password):
        self.username = username
        self.password = password

    def check_auth_password(self, username, password):
        valid_user = hmac.compare_digest(username.encode(), self.username.encode())
        valid_password = hmac.compare_digest(password.encode(), self.password.encode())
        return paramiko.AUTH_SUCCESSFUL if valid_user and valid_password else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username):
        return "password"


class _FileInterface(paramiko.SFTPServerInterface):
    def __init__(self, server, root, permissions):
        super().__init__(server)
        self.root = os.path.realpath(root)
        self.permissions = permissions

    def _resolve(self, path, follow_leaf=True):
        parts = []
        for part in path.replace("\\", "/").split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                raise PermissionError("path traversal")
            parts.append(part)
        if follow_leaf or not parts:
            target = os.path.realpath(os.path.join(self.root, *parts))
        else:
            parent = os.path.realpath(os.path.join(self.root, *parts[:-1]))
            target = os.path.join(parent, parts[-1])
        try:
            inside = os.path.normcase(os.path.commonpath((self.root, target))) == os.path.normcase(self.root)
        except ValueError:
            inside = False
        if not inside:
            raise PermissionError("outside shared directory")
        return target

    @staticmethod
    def _error(exc):
        if isinstance(exc, PermissionError):
            return paramiko.SFTP_PERMISSION_DENIED
        if isinstance(exc, ValueError):
            return paramiko.SFTP_FAILURE
        return paramiko.SFTPServer.convert_errno(getattr(exc, "errno", errno.EIO))

    @staticmethod
    def _attr(path, name=None):
        attr = paramiko.SFTPAttributes.from_stat(os.stat(path))
        if name is not None:
            attr.filename = name
        return attr

    def list_folder(self, path):
        try:
            folder = self._resolve(path)
            result = []
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        item = self._resolve(path.rstrip("/") + "/" + entry.name)
                        result.append(self._attr(item, entry.name))
                    except (OSError, ValueError):
                        continue
            return result
        except (OSError, ValueError) as exc:
            return self._error(exc)

    def stat(self, path):
        try:
            return self._attr(self._resolve(path))
        except (OSError, ValueError) as exc:
            return self._error(exc)

    def lstat(self, path):
        return self.stat(path)

    def open(self, path, flags, attr):
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if writing and not self.permissions.get("upload", False):
            return paramiko.SFTP_PERMISSION_DENIED
        if not (flags & os.O_WRONLY) and not self.permissions.get("download", True):
            return paramiko.SFTP_PERMISSION_DENIED
        try:
            target = self._resolve(path)
            fd = os.open(target, flags, 0o666)
            mode = "r+b" if flags & os.O_RDWR else ("wb" if writing else "rb")
            fileobj = os.fdopen(fd, mode, buffering=0)
            handle = paramiko.SFTPHandle(flags)
            handle.filename = target
            if not (flags & os.O_WRONLY):
                handle.readfile = fileobj
            if writing:
                handle.writefile = fileobj
            return handle
        except (OSError, ValueError) as exc:
            return self._error(exc)

    def remove(self, path):
        if not self.permissions.get("delete", False):
            return paramiko.SFTP_PERMISSION_DENIED
        try:
            target = self._resolve(path, follow_leaf=False)
            if target == self.root:
                raise PermissionError("cannot remove root")
            os.remove(target)
            return paramiko.SFTP_OK
        except (OSError, ValueError) as exc:
            return self._error(exc)

    def rename(self, oldpath, newpath):
        if not self.permissions.get("rename", False):
            return paramiko.SFTP_PERMISSION_DENIED
        try:
            source = self._resolve(oldpath, follow_leaf=False)
            target = self._resolve(newpath, follow_leaf=False)
            if source == self.root or target == self.root:
                raise PermissionError("cannot rename root")
            if os.path.lexists(target):
                raise FileExistsError(errno.EEXIST, "target exists", target)
            os.rename(source, target)
            return paramiko.SFTP_OK
        except (OSError, ValueError) as exc:
            return self._error(exc)

    def mkdir(self, path, attr):
        if not self.permissions.get("mkdir", False):
            return paramiko.SFTP_PERMISSION_DENIED
        try:
            os.mkdir(self._resolve(path, follow_leaf=False))
            return paramiko.SFTP_OK
        except (OSError, ValueError) as exc:
            return self._error(exc)

    def rmdir(self, path):
        if not self.permissions.get("delete", False):
            return paramiko.SFTP_PERMISSION_DENIED
        try:
            target = self._resolve(path, follow_leaf=False)
            if target == self.root:
                raise PermissionError("cannot remove root")
            os.rmdir(target)
            return paramiko.SFTP_OK
        except (OSError, ValueError) as exc:
            return self._error(exc)


def _host_key_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    folder = os.path.join(base, "SlyarFTPServer")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "sftp_host_key.pem")


def _load_host_key():
    path = _host_key_path()
    if os.path.exists(path):
        return paramiko.RSAKey.from_private_key_file(path)
    key = paramiko.RSAKey.generate(3072)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return paramiko.RSAKey.from_private_key_file(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            key.write_private_key(output)
    except Exception:
        os.remove(path)
        raise
    return key


class SFTPService:
    def __init__(self, port, username, password, directory, permissions=None):
        self.port = port
        self.username = username
        self.password = password
        self.directory = directory
        self.permissions = permissions or {}
        self.host_key = None
        self.listener = None
        self.thread = None
        self.stopped = threading.Event()
        self.transports = set()
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(32)

    def start(self):
        if not self.username or not self.password or self.username.lower() == "anonymous":
            raise ValueError("SFTP 需要非 anonymous 的用户名和密码")
        if not os.path.isdir(self.directory):
            raise ValueError("SFTP 共享目录不存在")
        self.host_key = _load_host_key()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("", self.port))
            listener.listen(100)
            listener.settimeout(0.5)
        except Exception:
            listener.close()
            raise
        self.listener = listener
        self.port = listener.getsockname()[1]
        self.stopped.clear()
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        try:
            self.thread.start()
        except Exception:
            listener.close()
            self.listener = None
            self.thread = None
            raise

    def _accept_loop(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.slots.acquire(blocking=False):
                client.close()
                continue
            try:
                threading.Thread(target=self._serve_client, args=(client,), daemon=True).start()
            except Exception:
                self.slots.release()
                client.close()
                LOGGER.exception("SFTP client worker could not start")

    def _serve_client(self, client):
        transport = None
        try:
            client.settimeout(120)
            transport = paramiko.Transport(client)
            with self.lock:
                self.transports.add(transport)
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler(
                "sftp", paramiko.SFTPServer, _FileInterface,
                root=self.directory, permissions=self.permissions,
            )
            transport.start_server(server=_AuthServer(self.username, self.password))
            channels = []
            while transport.is_active() and not self.stopped.is_set():
                channel = transport.accept(1)
                if channel is not None:
                    channels.append(channel)
                channels[:] = [item for item in channels if not item.closed]
        except (OSError, paramiko.SSHException):
            LOGGER.exception("SFTP connection failed")
        finally:
            if transport is not None:
                transport.close()
            else:
                client.close()
            with self.lock:
                self.transports.discard(transport)
            self.slots.release()

    def stop(self):
        self.stopped.set()
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        with self.lock:
            transports = list(self.transports)
        for transport in transports:
            transport.close()
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None
