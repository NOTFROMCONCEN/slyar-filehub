"""传输服务的本机回归测试，不使用实际共享目录或配置。"""

import base64
import ftplib
import http.client
import io
import json
import logging
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

import paramiko

from config_manager import ConfigManager, ftp_permissions_from_config
from ftp_server_gui import FTPServerGUI, HeadlessServer
from sftp_service import SFTPService
from web_server import ThreadedWebServer, make_web_handler, safe_join
from webdav_service import WebDAVService


def _basic_auth(username="user", password="pass"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _request(conn, method, path, body=None, headers=None):
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    return response.status, response.read()


class TransferServiceTests(unittest.TestCase):
    def test_default_ftp_permissions_are_readonly(self):
        self.assertEqual(ftp_permissions_from_config(None), "elr")
        self.assertEqual(ftp_permissions_from_config({"upload": True}), "elrw")
        self.assertFalse(ConfigManager().default_config["permissions"]["delete"])

    def test_ftp_requires_account_and_respects_readonly(self):
        with tempfile.TemporaryDirectory() as folder:
            (pathlib.Path(folder) / "hello.txt").write_text("hello")
            with mock.patch.object(HeadlessServer, "setup_logging", autospec=True):
                server = HeadlessServer()
            server.logger = logging.getLogger("test_ftp")
            self.assertFalse(server.start_ftp_service(0, username="user", password="", directory=folder))
            self.assertFalse(server.start_ftp_service(0, username="anonymous", password="pass", directory=folder))
            self.assertTrue(server.start_ftp_service(0, username="user", password="pass", directory=folder))
            try:
                port = server.ftp_server.socket.getsockname()[1]
                self.assertEqual(server.ftp_server.socket.getsockname()[0], "0.0.0.0")
                with ftplib.FTP() as ftp:
                    ftp.connect("127.0.0.1", port, timeout=5)
                    with self.assertRaises(ftplib.error_perm):
                        ftp.login("anonymous", "guest@example.com")
                    ftp.login("user", "pass")
                    self.assertIn("hello.txt", ftp.nlst())
                    with self.assertRaises(ftplib.error_perm):
                        ftp.storbinary("STOR new.txt", io.BytesIO(b"blocked"))
            finally:
                server.stop_all_services()
            self.assertTrue(server.start_ftp_service(
                0, username="user", password="pass", directory=folder,
                permissions={"download": True, "upload": True},
            ))
            try:
                port = server.ftp_server.socket.getsockname()[1]
                with ftplib.FTP() as ftp:
                    ftp.connect("127.0.0.1", port, timeout=5)
                    ftp.login("user", "pass")
                    ftp.storbinary("STOR allowed.txt", io.BytesIO(b"allowed"))
                self.assertEqual((pathlib.Path(folder) / "allowed.txt").read_bytes(), b"allowed")
            finally:
                server.stop_all_services()

    def test_web_only_gui_does_not_require_ftp_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            app = mock.MagicMock()
            app.enable_ftp.get.return_value = False
            app.enable_sftp.get.return_value = False
            app.enable_web.get.return_value = True
            app.enable_webdav.get.return_value = False
            app.username_entry.get.return_value = ""
            app.password_entry.get.return_value = ""
            app.directory_entry.get.return_value = folder
            app.max_conn_entry.get.return_value = ""
            app.web_port_entry.get.return_value = "8080"
            app.web_password_entry.get.return_value = ""
            app.web_readonly_var.get.return_value = False
            with mock.patch("ftp_server_gui.messagebox.showerror") as showerror, mock.patch(
                "ftp_server_gui.messagebox.showinfo"
            ):
                FTPServerGUI.start_all_services(app)
            showerror.assert_not_called()
            app.start_web_server.assert_called_once_with(
                8080, folder, password="", readonly=False,
            )

    def test_webdav_only_gui_requires_account_but_starts_service(self):
        with tempfile.TemporaryDirectory() as folder:
            app = mock.MagicMock()
            app.enable_ftp.get.return_value = False
            app.enable_sftp.get.return_value = False
            app.enable_web.get.return_value = False
            app.enable_webdav.get.return_value = True
            app.username_entry.get.return_value = "user"
            app.password_entry.get.return_value = "pass"
            app.directory_entry.get.return_value = folder
            app.max_conn_entry.get.return_value = ""
            app.webdav_port_entry.get.return_value = "5005"
            app.get_permission_config.return_value = {"download": True}
            with mock.patch("ftp_server_gui.messagebox.showerror") as showerror, mock.patch(
                "ftp_server_gui.messagebox.showinfo"
            ):
                FTPServerGUI.start_all_services(app)
            showerror.assert_not_called()
            app.start_webdav_server.assert_called_once_with(
                5005, "user", "pass", folder, {"download": True},
            )

    def test_tray_failure_does_not_hide_the_window(self):
        app = mock.MagicMock()
        app.tray_available = False
        FTPServerGUI.hide_to_tray(app)
        app.quit_program.assert_called_once_with()
        app.root.withdraw.assert_not_called()

    def test_sftp_transfer_permissions_and_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / "hello.txt").write_text("hello")
            key_path = str(root / "host_key.pem")
            with mock.patch("sftp_service._host_key_path", return_value=key_path):
                service = SFTPService(0, "user", "pass", folder, {"download": True})
                service.start()
                self.assertEqual(service.listener.getsockname()[0], "0.0.0.0")
                first_key = service.host_key.get_fingerprint()
                try:
                    rejected = paramiko.Transport(("127.0.0.1", service.port))
                    with self.assertRaises(paramiko.AuthenticationException):
                        rejected.connect(username="user", password="wrong")
                    rejected.close()
                    transport = paramiko.Transport(("127.0.0.1", service.port))
                    transport.connect(username="user", password="pass")
                    client = paramiko.SFTPClient.from_transport(transport)
                    try:
                        self.assertIn("hello.txt", client.listdir("."))
                        with client.open("hello.txt", "r") as stream:
                            self.assertEqual(stream.read(), b"hello")
                        with self.assertRaises(OSError):
                            client.open("blocked.txt", "w")
                    finally:
                        client.close()
                        transport.close()
                finally:
                    service.stop()
                self.assertFalse(service.thread and service.thread.is_alive())
                writable = SFTPService(0, "user", "pass", folder, {
                    "download": True, "upload": True, "rename": True, "delete": True, "mkdir": True,
                })
                writable.start()
                self.assertEqual(writable.host_key.get_fingerprint(), first_key)
                try:
                    transport = paramiko.Transport(("127.0.0.1", writable.port))
                    transport.connect(username="user", password="pass")
                    client = paramiko.SFTPClient.from_transport(transport)
                    try:
                        with client.open("new.txt", "w") as stream:
                            stream.write(b"new")
                        client.rename("new.txt", "renamed.txt")
                        self.assertEqual((root / "renamed.txt").read_bytes(), b"new")
                        client.remove("renamed.txt")
                        client.mkdir("folder")
                        client.rmdir("folder")
                    finally:
                        client.close()
                        transport.close()
                finally:
                    writable.stop()

    def test_webdav_crud_locks_and_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            base = pathlib.Path(folder)
            root = base / "shared"
            outside = base / "private"
            root.mkdir()
            outside.mkdir()
            (outside / "secret.txt").write_text("secret")
            (root / "linked").symlink_to(outside, target_is_directory=True)
            writable = {
                "download": True, "upload": True,
                "rename": True, "delete": True, "mkdir": True,
            }
            service = WebDAVService(0, "user", "pass", str(root), writable)
            service.start()
            try:
                port = service.server.server_address[1]
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("OPTIONS", "/", headers=_basic_auth())
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("2", response.getheader("DAV", ""))
                response.read()

                status, _ = _request(conn, "PROPFIND", "/")
                self.assertEqual(status, 401)
                status, _ = _request(
                    conn, "PROPFIND", "/", headers=_basic_auth(password="wrong")
                )
                self.assertEqual(status, 401)

                status, _ = _request(
                    conn, "GET", "/../secret.txt", headers=_basic_auth()
                )
                self.assertEqual(status, 403)
                status, _ = _request(
                    conn, "GET", "/linked/secret.txt", headers=_basic_auth()
                )
                self.assertEqual(status, 403)

                status, _ = _request(
                    conn, "PUT", "/hello.txt", body=b"hello dav",
                    headers={**_basic_auth(), "Content-Length": "9"},
                )
                self.assertEqual(status, 201)
                status, body = _request(conn, "GET", "/hello.txt", headers=_basic_auth())
                self.assertEqual((status, body), (200, b"hello dav"))
                status, body = _request(
                    conn, "PROPFIND", "/", headers={**_basic_auth(), "Depth": "1"}
                )
                self.assertEqual(status, 207)
                self.assertIn(b"hello.txt", body)
                status, _ = _request(
                    conn, "PROPFIND", "/hello.txt", headers={**_basic_auth(), "Depth": "0"}
                )
                self.assertEqual(status, 207)
                status, _ = _request(conn, "HEAD", "/hello.txt", headers=_basic_auth())
                self.assertEqual(status, 200)

                status, _ = _request(
                    conn, "MKCOL", "/docs", headers=_basic_auth()
                )
                self.assertEqual(status, 201)
                status, _ = _request(
                    conn, "PUT", "/docs/a.txt", body=b"aaa",
                    headers={**_basic_auth(), "Content-Length": "3"},
                )
                self.assertEqual(status, 201)
                status, _ = _request(
                    conn, "COPY", "/docs/a.txt",
                    headers={**_basic_auth(), "Destination": "http://x/docs/b.txt"},
                )
                self.assertEqual(status, 201)
                status, _ = _request(
                    conn, "MOVE", "/docs/b.txt",
                    headers={
                        **_basic_auth(),
                        "Destination": "http://x/docs/c.txt",
                        "Overwrite": "F",
                    },
                )
                self.assertEqual(status, 201)
                status, _ = _request(
                    conn, "MOVE", "/docs/c.txt",
                    headers={
                        **_basic_auth(),
                        "Destination": "http://x/../outside.txt",
                    },
                )
                self.assertEqual(status, 403)
                self.assertFalse((base / "outside.txt").exists())

                status, body = _request(
                    conn, "LOCK", "/hello.txt",
                    headers={**_basic_auth(), "Timeout": "Second-120"},
                    body=(
                        '<?xml version="1.0"?><D:lockinfo xmlns:D="DAV:">'
                        "<D:lockscope><D:exclusive/></D:lockscope>"
                        "<D:locktype><D:write/></D:locktype>"
                        "<D:owner>tester</D:owner></D:lockinfo>"
                    ),
                )
                self.assertEqual(status, 200)
                self.assertIn(b"opaquelocktoken:", body)
                token_start = body.index(b"opaquelocktoken:")
                token = body[token_start:body.index(b"<", token_start)].decode()

                status, _ = _request(
                    conn, "PUT", "/hello.txt", body=b"blocked",
                    headers={**_basic_auth(), "Content-Length": "7"},
                )
                self.assertEqual(status, 423)
                status, _ = _request(
                    conn, "PUT", "/hello.txt", body=b"updated",
                    headers={
                        **_basic_auth(),
                        "Content-Length": "7",
                        "If": f"(<{token}>)",
                    },
                )
                self.assertEqual(status, 204)
                self.assertEqual((root / "hello.txt").read_bytes(), b"updated")
                status, _ = _request(
                    conn, "UNLOCK", "/hello.txt",
                    headers={**_basic_auth(), "Lock-Token": f"<{token}>"},
                )
                self.assertEqual(status, 204)
                status, _ = _request(
                    conn, "PUT", "/hello.txt", body=b"final",
                    headers={**_basic_auth(), "Content-Length": "5"},
                )
                self.assertEqual(status, 204)

                status, _ = _request(
                    conn, "DELETE", "/docs/a.txt", headers=_basic_auth()
                )
                self.assertEqual(status, 204)
                self.assertFalse((root / "docs" / "a.txt").exists())
                conn.close()
            finally:
                service.stop()

            readonly = WebDAVService(0, "user", "pass", str(root), {"download": True})
            readonly.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", readonly.port, timeout=10)
                for method, path in (("PUT", "/new.txt"), ("DELETE", "/hello.txt"), ("MKCOL", "/x")):
                    status, _ = _request(conn, method, path, body=b"",
                                         headers={**_basic_auth(), "Content-Length": "0"})
                    self.assertEqual(status, 403)
                status, body = _request(conn, "GET", "/hello.txt", headers=_basic_auth())
                self.assertEqual((status, body), (200, b"final"))
                status, _ = _request(conn, "LOCK", "/hello.txt",
                                     headers={**_basic_auth()}, body=b"")
                self.assertEqual(status, 403)
                conn.close()
            finally:
                readonly.stop()

    def test_headless_starts_and_stops_webdav(self):
        with tempfile.TemporaryDirectory() as folder:
            (pathlib.Path(folder) / "readme.txt").write_text("ok")
            with mock.patch.object(HeadlessServer, "setup_logging", autospec=True):
                server = HeadlessServer()
            server.logger = logging.getLogger("test_webdav")
            self.assertFalse(server.start_webdav_service(0, username="user", password="", directory=folder))
            self.assertTrue(server.start_webdav_service(
                0, username="user", password="pass", directory=folder,
                permissions={"download": True},
            ))
            try:
                port = server.webdav_server.server.server_address[1]
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                status, body = _request(conn, "GET", "/readme.txt", headers=_basic_auth())
                self.assertEqual((status, body), (200, b"ok"))
                conn.close()
            finally:
                server.stop_all_services()
            self.assertFalse(server.webdav_running)

    def test_web_large_streamed_upload_and_symlink_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            base = pathlib.Path(folder)
            root = base / "shared"
            outside = base / "private"
            root.mkdir()
            outside.mkdir()
            (outside / "secret.txt").write_text("secret")
            (root / "linked").symlink_to(outside, target_is_directory=True)
            self.assertIsNone(safe_join(str(root), "linked/secret.txt"))
            server = ThreadedWebServer(("127.0.0.1", 0), make_web_handler(str(root)))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                conn.request("GET", "/preview?path=linked%2Fsecret.txt")
                self.assertEqual(conn.getresponse().status, 404)
                conn.close()
                content = b"a" * (5 * 1024 * 1024)
                boundary = "test-boundary-123"
                body = (
                    b"--test-boundary-123\r\nContent-Disposition: form-data; name=\"path\"\r\n\r\n\r\n"
                    b"--test-boundary-123\r\nContent-Disposition: form-data; name=\"file\"; filename=\"large.bin\"\r\n"
                    b"Content-Type: application/octet-stream\r\n\r\n" + content +
                    b"\r\n--test-boundary-123--\r\n"
                )
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
                conn.request("POST", "/api/upload", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
                response = conn.getresponse()
                response_body = response.read()
                self.assertEqual(response.status, 200, response_body)
                self.assertEqual(json.loads(response_body)["saved"], ["large.bin"])
                conn.close()
                self.assertEqual((root / "large.bin").read_bytes(), content)
                broken = (
                    b"--test-boundary-123\r\nContent-Disposition: form-data; name=\"file\"; filename=\"partial.bin\"\r\n\r\n"
                    b"incomplete"
                )
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                conn.request("POST", "/api/upload", broken, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
                self.assertEqual(conn.getresponse().status, 400)
                conn.close()
                self.assertFalse((root / "partial.bin").exists())
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_search_and_zip_have_work_budgets(self):
        with tempfile.TemporaryDirectory() as folder:
            (pathlib.Path(folder) / "a.txt").write_text("ab")
            (pathlib.Path(folder) / "b.txt").write_text("cd")
            server = ThreadedWebServer(("127.0.0.1", 0), make_web_handler(folder))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with mock.patch("web_server.MAX_SEARCH_ENTRIES", 1):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                    conn.request("GET", "/api/search?q=absent")
                    data = json.loads(conn.getresponse().read())
                    self.assertTrue(data["truncated"])
                    conn.close()
                with mock.patch("web_server.MAX_ZIP_BYTES", 1):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                    conn.request("GET", "/api/zip?name=a.txt")
                    self.assertEqual(conn.getresponse().status, 413)
                    conn.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
