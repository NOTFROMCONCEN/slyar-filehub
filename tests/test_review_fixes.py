"""代码 REVIEW 修复项的回归测试。

只使用标准库与不依赖第三方包的本地模块，可在未安装
pyftpdlib / paramiko / psutil / pytest 的环境中用
`python -m unittest tests/test_review_fixes.py -v` 直接运行。
"""

import http.client
import json
import logging
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_manager as cm
import temporary_transfer
import web_server
import webdav_service

logging.disable(logging.CRITICAL)


def request(conn, method, path, body=None, headers=None):
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    return response.status, dict(response.getheaders()), response.read()


class WebFixture:
    """在临时目录上启一个真实 HTTP 服务。"""

    def __init__(self, handler, server_cls=web_server.ThreadedWebServer):
        self.server = server_cls(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def connect(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)


class ConfigManagerTests(unittest.TestCase):
    def test_existing_permission_contract_is_unchanged(self):
        self.assertEqual(cm.ftp_permissions_from_config(None), "elr")
        self.assertEqual(cm.ftp_permissions_from_config({"upload": True}), "elrw")
        self.assertEqual(
            cm.ftp_permissions_from_config({"upload": True}, allow_append=True),
            "elrwa",
        )
        self.assertFalse(cm.ConfigManager().default_config["permissions"]["delete"])

    def test_defaults_are_not_shared(self):
        first = cm.ConfigManager("__missing__.json").load_config()
        first["permissions"]["delete"] = True
        second = cm.ConfigManager("__missing__.json").load_config()
        self.assertFalse(second["permissions"]["delete"])

    def test_permissions_deep_merge_and_corrupt_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "cfg.json"
            path.write_text(
                json.dumps({"permissions": {"upload": True}, "ftp_port": "2121"}),
                encoding="utf-8",
            )
            cfg = cm.ConfigManager(str(path)).load_config()
            self.assertTrue(cfg["permissions"]["upload"])
            self.assertFalse(cfg["permissions"]["delete"])
            self.assertEqual(cfg["web_port"], "8080")

            path.write_text("not json", encoding="utf-8")
            self.assertEqual(cm.ConfigManager(str(path)).load_config()["ftp_port"], "21")

    def test_port_validation_and_conflicts(self):
        base = {
            "ftp_port": "2121", "web_port": "8080", "webdav_port": "5005",
            "max_connections": "100", "username": "u", "password": "p",
            "directory": os.getcwd(),
        }
        self.assertEqual(
            cm.validate_service_config(base, {"ftp": True, "web": True}), []
        )
        self.assertTrue(any(
            "冲突" in e for e in cm.validate_service_config(
                {**base, "web_port": "2121"}, {"ftp": True, "web": True}
            )
        ))
        self.assertTrue(any(
            "65535" in e for e in cm.validate_service_config(
                {**base, "ftp_port": "65535"}, {"ftp": True, "sftp": True}
            )
        ))
        self.assertEqual(cm.normalize_ports({"ftp_port": "oops"})["ftp_port"], 21)
        self.assertTrue(cm.security_warnings({
            "enable_web": True, "web_password": "", "web_readonly": False,
            "bind_address": "",
        }, {"web": True}))


class WebServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "hello.txt").write_text("hello")
        web_server.LOGIN_LIMITER.reset()

    def tearDown(self):
        web_server.LOGIN_LIMITER.reset()
        self.tmp.cleanup()

    def test_browse_and_security_headers(self):
        fixture = WebFixture(web_server.make_web_handler(str(self.root)))
        try:
            conn = fixture.connect()
            status, headers, body = request(conn, "GET", "/")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(headers.get("X-Frame-Options"), "SAMEORIGIN")
            self.assertEqual(headers.get("Referrer-Policy"), "same-origin")
            self.assertIn(b"Slyar", body)

            status, _, body = request(conn, "GET", "/api/list")
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertFalse(data["truncated"])
            self.assertEqual(data["items"][0]["name"], "hello.txt")
            conn.close()
        finally:
            fixture.close()
        self.assertTrue(web_server.ThreadedWebServer.allow_reuse_address)

    def test_cross_site_write_rejected_and_same_site_allowed(self):
        fixture = WebFixture(web_server.make_web_handler(str(self.root)))
        try:
            conn = fixture.connect()
            payload = json.dumps({"path": "", "name": "blocked"})
            status, _, _ = request(
                conn, "POST", "/api/mkdir", body=payload,
                headers={"Content-Type": "application/json",
                         "Origin": "http://evil.example"},
            )
            self.assertEqual(status, 403)

            conn2 = fixture.connect()
            payload2 = json.dumps({"path": "", "name": "allowed"})
            status, _, _ = request(
                conn2, "POST", "/api/mkdir", body=payload2,
                headers={"Content-Type": "application/json",
                         "Origin": f"http://127.0.0.1:{fixture.port}"},
            )
            self.assertEqual(status, 200)
            self.assertTrue((self.root / "allowed").is_dir())
            conn.close(); conn2.close()
        finally:
            fixture.close()

    def test_logout_route_and_login_rate_limit(self):
        fixture = WebFixture(
            web_server.make_web_handler(str(self.root), password="secret")
        )
        try:
            conn = fixture.connect()
            status, headers, _ = request(conn, "GET", "/logout")
            self.assertEqual(status, 303)
            self.assertIn("Max-Age=0", headers.get("Set-Cookie", ""))
            conn.close()

            # 连续失败达到阈值后锁定；成功后计数清零
            for _ in range(10):
                c = fixture.connect()
                status, _, _ = request(
                    c, "POST", "/login", body="password=wrong",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                self.assertEqual(status, 200)
                c.close()
            c = fixture.connect()
            status, _, body = request(
                c, "POST", "/login", body="password=wrong",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertIn("尝试次数过多", body.decode("utf-8", "ignore"))
            c.close()

            web_server.LOGIN_LIMITER.reset()
            c = fixture.connect()
            status, headers, _ = request(
                c, "POST", "/login", body="password=secret",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(status, 303)
            self.assertIn("slyar_session=", headers.get("Set-Cookie", ""))
            c.close()
        finally:
            fixture.close()

    def test_streaming_failure_does_not_emit_second_response(self):
        class Broken(web_server.make_web_handler(str(self.root))):
            def serve_file(self, qs, attachment):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "5")
                self.end_headers()
                self.wfile.write(b"he")
                raise RuntimeError("simulated streaming failure")

        fixture = WebFixture(Broken)
        try:
            conn = fixture.connect()
            conn.request("GET", "/download?path=hello.txt")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            # 连接提前关闭会产生 IncompleteRead，这正说明没有追加第二个响应
            try:
                rest = response.read()
            except http.client.IncompleteRead as exc:
                rest = exc.partial
            self.assertEqual(rest, b"he")
            self.assertNotIn(b"HTTP/1.1", rest)
            conn.close()
        finally:
            fixture.close()

    def test_optional_limits_default_to_unlimited(self):
        self.assertEqual(web_server.MAX_UPLOAD_BYTES, 0)
        self.assertEqual(web_server.MAX_LIST_ENTRIES, 0)


class WebDAVTests(unittest.TestCase):
    def test_start_stop_and_propfind_with_new_limits(self):
        import base64
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder) / "shared"
            root.mkdir()
            (root / "a.txt").write_text("a")
            token = base64.b64encode(b"user:pass").decode()
            auth = {"Authorization": f"Basic {token}"}
            service = webdav_service.WebDAVService(
                0, "user", "pass", str(root),
                {"download": True, "upload": True},
            )
            service.start()
            try:
                conn = http.client.HTTPConnection(
                    "127.0.0.1", service.server.server_address[1], timeout=10
                )
                status, headers, _ = request(conn, "PROPFIND", "/", headers={**auth, "Depth": "1"})
                self.assertEqual(status, 207)
                self.assertEqual(headers.get("X-Frame-Options"), "SAMEORIGIN")
                status, _, body = request(conn, "GET", "/a.txt", headers=auth)
                self.assertEqual((status, body), (200, b"a"))
                conn.close()
                self.assertIsNotNone(service.server._slots)
            finally:
                service.stop()
        self.assertEqual(webdav_service.MAX_WEBDAV_CONNECTIONS, 64)
        self.assertGreater(webdav_service.PROPFIND_TIME_BUDGET, 0)


class TemporaryTransferTests(unittest.TestCase):
    def test_non_ascii_code_returns_login_page_not_500(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            send, recv = root / "send", root / "recv"
            send.mkdir(); recv.mkdir()
            handler = temporary_transfer.make_temporary_transfer_handler(
                str(send), str(recv), "ABCD2345", time.time() + 600
            )
            fixture = WebFixture(handler)
            web_server.LOGIN_LIMITER.reset()
            try:
                conn = fixture.connect()
                status, _, body = request(
                    conn, "POST", "/login", body="password=%E4%B8%AD%E6%96%87",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                self.assertEqual(status, 200)
                self.assertIn("访问码", body.decode("utf-8", "ignore"))
                conn.close()

                conn = fixture.connect()
                status, headers, _ = request(
                    conn, "POST", "/login", body="password=ABCD2345",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                self.assertEqual(status, 303)
                cookie = headers.get("Set-Cookie", "").split(";")[0]
                conn.close()

                conn2 = fixture.connect()
                boundary = "reviewboundary"
                body = (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="file"; filename="x.txt"\r\n\r\n'
                    f"data\r\n--{boundary}--\r\n"
                ).encode()
                status, _, _ = request(
                    conn2, "POST", "/api/upload", body=body,
                    headers={
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                        "Cookie": cookie, "Origin": "http://evil.example",
                    },
                )
                self.assertEqual(status, 403)
                conn2.close()
            finally:
                fixture.close()
                web_server.LOGIN_LIMITER.reset()


class PackagingTests(unittest.TestCase):
    def test_clean_build_is_anchored_to_project_dir(self):
        import packtoexe
        original_rmtree = packtoexe.shutil.rmtree
        original_remove = packtoexe.os.remove
        with tempfile.TemporaryDirectory() as outside:
            for name in ("build", "dist", "__pycache__"):
                os.mkdir(os.path.join(outside, name))
            targets = []
            packtoexe.shutil.rmtree = lambda p, *a, **k: targets.append(p)
            packtoexe.os.remove = lambda p, *a, **k: targets.append(p)
            previous = os.getcwd()
            os.chdir(outside)
            try:
                try:
                    packtoexe.clean_build()
                finally:
                    os.chdir(previous)
                    packtoexe.shutil.rmtree = original_rmtree
                    packtoexe.os.remove = original_remove
            except Exception:
                packtoexe.shutil.rmtree = original_rmtree
                packtoexe.os.remove = original_remove
                raise
            self.assertTrue(targets)
            for target in targets:
                self.assertTrue(
                    os.path.abspath(target).startswith(
                        os.path.abspath(packtoexe.PROJECT_DIR) + os.sep
                    ),
                    target,
                )
            self.assertTrue(os.path.isdir(os.path.join(outside, "dist")))

    def test_version_args_never_breaks_build(self):
        import packtoexe
        original = packtoexe.create_version_file
        try:
            packtoexe.create_version_file = lambda: (_ for _ in ()).throw(OSError("boom"))
            self.assertEqual(packtoexe.version_args(), [])
        finally:
            packtoexe.create_version_file = original


def _load_gui_module():
    """本机缺少第三方依赖时安装最小 stub，仅用于验证与 GUI 无关的纯逻辑。"""
    try:
        import ftp_server_gui
        return ftp_server_gui
    except ImportError:
        pass
    import types
    import importlib

    for name in (
        "tkinter", "pyftpdlib", "pyftpdlib.authorizers",
        "pyftpdlib.handlers", "pyftpdlib.servers", "psutil",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["tkinter"] = mock.MagicMock()
    sys.modules["pyftpdlib.authorizers"].DummyAuthorizer = object
    sys.modules["pyftpdlib.handlers"].FTPHandler = type("FTPHandler", (), {})
    sys.modules["pyftpdlib.servers"].FTPServer = object
    sys.modules["psutil"].net_if_addrs = lambda: {}
    for sub in ("authorizers", "handlers", "servers"):
        setattr(sys.modules["pyftpdlib"], sub, sys.modules[f"pyftpdlib.{sub}"])
    sys.modules.pop("ftp_server_gui", None)
    return importlib.import_module("ftp_server_gui")


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gui = _load_gui_module()

    def test_quit_stops_webdav_only_instance(self):
        app = mock.MagicMock()
        app.ftp_running = app.sftp_running = False
        app.web_running = app.temp_transfer_running = False
        app.webdav_running = True
        self.gui.FTPServerGUI.quit_program(app)
        app.stop_all_services.assert_called_once_with(notify=False)
        app.save_config.assert_called_once()
        app.root.quit.assert_called_once()

    def test_rollback_keeps_temporary_transfer(self):
        from types import SimpleNamespace

        ftp_srv = mock.MagicMock()
        web_srv = mock.MagicMock()
        dav_srv = mock.MagicMock()
        app = SimpleNamespace(
            _service_ports={"ftp": 2121, "web": 8080, "webdav": 5005, "temp": 6000},
            ftp_running=True, ftp_server=ftp_srv,
            sftp_running=False, sftp_server=None,
            web_running=True, web_server=web_srv,
            webdav_running=True, webdav_server=dav_srv,
            start_button=mock.MagicMock(), stop_button=mock.MagicMock(),
            web_button=mock.MagicMock(), log_error=mock.MagicMock(),
            stop_temporary_transfer=mock.MagicMock(),
        )
        self.gui.FTPServerGUI._rollback_services(app)
        self.assertEqual(app._service_ports, {"temp": 6000})
        app.stop_temporary_transfer.assert_not_called()
        self.assertTrue(ftp_srv.close_all.called)
        self.assertTrue(web_srv.shutdown.called)
        self.assertTrue(dav_srv.stop.called)

    def test_queue_handler_receives_service_logs(self):
        import queue as queue_module

        class Dummy:
            def __init__(self):
                self.log_queue = queue_module.Queue()

            def log_info(self, message):
                self.logger.info(message)

            def log_warning(self, message):
                self.logger.warning(message)

            def log_error(self, message):
                self.logger.error(message)

        root = logging.getLogger()
        old_handlers = list(root.handlers)
        old_disable = logging.root.manager.disable
        tmp = tempfile.TemporaryDirectory()
        previous = os.getcwd()
        os.chdir(tmp.name)
        try:
            for handler in old_handlers:
                root.removeHandler(handler)
            logging.disable(logging.NOTSET)
            dummy = Dummy()
            self.gui.FTPServerGUI.setup_logging(dummy)
            logging.getLogger("WebServer").info("probe-web-service")
            messages = []
            while not dummy.log_queue.empty():
                messages.append(dummy.log_queue.get_nowait())
            self.assertTrue(any("probe-web-service" in m for m in messages), messages)
            self.assertEqual(
                len([h for h in root.handlers
                     if isinstance(h, self.gui.LogHandler)]), 1
            )
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
            for handler in old_handlers:
                root.addHandler(handler)
            logging.disable(old_disable)
            os.chdir(previous)
            tmp.cleanup()


class BindAddressTests(unittest.TestCase):
    def test_webdav_bind_address_is_effective_and_default_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            service = webdav_service.WebDAVService(
                0, "u", "p", folder, {"download": True},
                bind_address="127.0.0.1",
            )
            service.start()
            try:
                self.assertEqual(service.server.server_address[0], "127.0.0.1")
            finally:
                service.stop()

            default = webdav_service.WebDAVService(
                0, "u", "p", folder, {"download": True}
            )
            self.assertEqual(default.bind_address, "")

    def test_config_template_and_default_keep_bind_address_empty(self):
        self.assertEqual(cm.ConfigManager().default_config["bind_address"], "")
        example_path = os.path.join(
            os.path.dirname(os.path.abspath(cm.__file__)),
            "ftp_config.example.json",
        )
        example = json.loads(
            pathlib.Path(example_path).read_text(encoding="utf-8")
        )
        self.assertEqual(example.get("bind_address", ""), "")


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
