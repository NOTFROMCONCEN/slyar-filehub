"""Web 文件分享服务

提供现代化单页文件管理界面与 JSON API：
- 浏览/面包屑导航/排序/搜索
- 列表/网格双视图、明暗主题
- 图片/视频/音频/PDF/文本预览
- 流式多文件上传（带进度）、新建文件夹、重命名、删除
- 单文件下载、多选 ZIP 打包下载
- 路径穿越防护与文件名净化

所有前端资源内嵌本文件，无需外部网络（适配局域网离线环境）。
"""

import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socketserver
import sys
import threading
import time
import urllib.parse
import zipfile
import http.server

WEB_LOGGER = logging.getLogger("WebServer")

mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/x-m4a", ".m4a")
mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("text/markdown", ".md")

# 作为纯文本预览的类型（禁止在站内渲染 HTML/SVG，避免脚本注入）
FORCE_TEXT_EXTS = {".html", ".htm", ".svg", ".xml", ".xhtml"}

MAX_TEXT_PREVIEW = 512 * 1024
MAX_FIELD_SIZE = 64 * 1024

# 文本快享：内存存储（重启即清空），限制数量与单条大小
PASTE_LOCK = threading.Lock()
PASTES = {}
MAX_PASTES = 100
MAX_PASTE_SIZE = 1024 * 1024
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_ENTRIES = 100_000
SEARCH_TIME_BUDGET = 15
MAX_ZIP_ENTRIES = 100_000
MAX_ZIP_BYTES = 100 * 1024 ** 3  # 100 GiB；单文件上传/下载不设大小上限
MAX_ZIP_CONCURRENT = 2
ZIP_SLOTS = threading.BoundedSemaphore(MAX_ZIP_CONCURRENT)


def sanitize_filename(name):
    """净化文件/目录名：仅保留最后一级名称，替换危险字符，拒绝 . / .. / 空"""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip()
    if name in ("", ".", ".."):
        return None
    return name


def safe_join(root, rel):
    """把相对路径安全拼接到根目录内，任何越界尝试返回 None"""
    root = os.path.realpath(root)
    rel = (rel or "").replace("\\", "/").strip("/")
    if rel in ("", "."):
        return root
    parts = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    target = os.path.realpath(os.path.join(root, *parts))
    try:
        inside = os.path.normcase(os.path.commonpath((root, target))) == os.path.normcase(root)
    except ValueError:
        inside = False
    if inside:
        return target
    return None


def unique_path(path):
    """目标已存在时追加序号，避免覆盖"""
    if not os.path.lexists(path):
        return path
    base, ext = os.path.splitext(path)
    for i in range(1, 1000):
        candidate = f"{base} ({i}){ext}"
        if not os.path.lexists(candidate):
            return candidate
    return None


def guess_mime(path, for_preview=False):
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    ext = os.path.splitext(path)[1].lower()
    if for_preview and (ext in FORCE_TEXT_EXTS or mime.startswith("text/")):
        mime = "text/plain"
    if for_preview and mime.startswith("text/"):
        mime += "; charset=utf-8"
    return mime


class _MultipartReader:
    """流式 multipart 解析器：边读边写盘，支持大文件上传且不受内存限制"""

    def __init__(self, rfile, boundary, content_length):
        self.r = rfile
        self.delim = b"\r\n--" + boundary
        self.buf = b""
        self.remaining = content_length

    def _fill(self):
        if self.remaining <= 0:
            raise EOFError("multipart 数据提前结束")
        chunk = self.r.read1(min(256 * 1024, self.remaining))
        if not chunk:
            raise EOFError("multipart 数据提前结束")
        self.remaining -= len(chunk)
        self.buf += chunk

    def read_line(self, limit=8192):
        while b"\r\n" not in self.buf:
            if len(self.buf) > limit:
                raise ValueError("multipart 行过长")
            self._fill()
        line, self.buf = self.buf.split(b"\r\n", 1)
        if len(line) > limit:
            raise ValueError("multipart 行过长")
        return line

    def stream_body(self, write):
        """把 body 写入 write() 直到边界；返回 True 表示已到结束边界('--')"""
        while True:
            idx = self.buf.find(self.delim)
            while idx == -1:
                keep = len(self.delim) - 1
                if len(self.buf) > keep:
                    write(self.buf[:-keep])
                    self.buf = self.buf[-keep:]
                self._fill()
                idx = self.buf.find(self.delim)
            write(self.buf[:idx])
            self.buf = self.buf[idx + len(self.delim):]
            if self.buf.startswith(b"--"):
                self.buf = self.buf[2:]
                return True
            self.read_line()
            return False


class _CountingWriter:
    """给 socket 写对象补上 tell()/flush()，供 zipfile 流式写出"""

    def __init__(self, wfile):
        self._w = wfile
        self._n = 0

    def write(self, b):
        self._n += len(b)
        self._w.write(b)

    def tell(self):
        return self._n

    def flush(self):
        pass


class WebFileHandler(http.server.BaseHTTPRequestHandler):
    """文件分享 HTTP 处理器（目录/密码/只读通过 make_web_handler 绑定）"""

    directory = os.path.expanduser("~")
    auth_password = ""
    readonly = False
    session_token = None
    server_version = "SlyarWebShare/2.1"
    protocol_version = "HTTP/1.1"

    # ---------- 基础 ----------

    def log_message(self, format, *args):
        WEB_LOGGER.info("%s - %s", self.address_string(), format % args)

    def _cookie_session(self):
        m = re.search(r"slyar_session=([A-Za-z0-9_\-]+)", self.headers.get("Cookie", ""))
        token = type(self).session_token
        return bool(token and m and hmac.compare_digest(m.group(1), token))

    def _authorized(self):
        return not type(self).auth_password or self._cookie_session()

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_error_page(self, code, message):
        try:
            body = json.dumps({"ok": False, "error": message}, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return None
        if length <= 0 or length > (1 << 20):
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    # ---------- 路由 ----------

    WRITE_ROUTES = {
        "/api/upload",
        "/api/mkdir",
        "/api/rename",
        "/api/delete",
        "/api/paste-create",
        "/api/paste-delete",
    }

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return

            if path == "/login":
                self.serve_login(qs.get("e") and "密码错误，请重试" or "")
                return

            if not self._authorized():
                if path.startswith("/api/"):
                    self.send_error_page(401, "需要登录")
                elif path in ("/", "/index.html") or path.startswith("/paste/"):
                    self.send_redirect("/login")
                else:
                    self.send_error_page(401, "需要登录")
                return

            if path in ("/", "/index.html"):
                self.serve_page()
            elif path == "/api/list":
                self.api_list(qs)
            elif path == "/api/search":
                self.api_search(qs)
            elif path == "/api/paste":
                if qs.get("id"):
                    self.api_paste_get((qs.get("id") or [""])[0])
                else:
                    self.api_paste_list()
            elif path == "/download":
                self.serve_file(qs, attachment=True)
            elif path == "/preview":
                self.serve_file(qs, attachment=False)
            elif path == "/api/zip":
                self.api_zip(qs)
            elif path.startswith("/paste/"):
                self.serve_paste(path[len("/paste/"):].strip("/"))
            else:
                self.send_error_page(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as e:
            WEB_LOGGER.error("处理GET请求失败: %s", e)
            self.send_error_page(500, "服务器内部错误")

    def do_POST(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path == "/login":
                self.handle_login()
                return

            if not self._authorized():
                self.send_error_page(401, "需要登录")
                return

            if path in self.WRITE_ROUTES and type(self).readonly:
                self.send_error_page(403, "只读模式下禁止此操作")
                return

            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                self.send_error_page(400, "Content-Length 无效")
                return
            if path == "/api/upload":
                if length <= 0 or self.headers.get("Transfer-Encoding"):
                    self.send_error_page(411, "上传需要 Content-Length")
                    self.close_connection = True
                    return
                self.api_upload(length)
            elif path in ("/api/mkdir", "/api/rename", "/api/delete", "/api/paste-create", "/api/paste-delete"):
                if length > (1 << 20):
                    self.send_error_page(413, "请求体过大")
                    return
                body = self.read_json_body()
                if body is None:
                    self.send_error_page(400, "请求体格式错误")
                    return
                if path == "/api/mkdir":
                    self.api_mkdir(body)
                elif path == "/api/rename":
                    self.api_rename(body)
                elif path == "/api/delete":
                    self.api_delete(body)
                elif path == "/api/paste-create":
                    self.api_paste_create(body)
                else:
                    self.api_paste_delete(body)
            else:
                self.send_error_page(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except TimeoutError:
            self.close_connection = True
            self.send_error_page(408, "连接空闲超时")
        except Exception as e:
            WEB_LOGGER.error("处理POST请求失败: %s", e)
            self.send_error_page(500, "服务器内部错误")

    # ---------- 登录 ----------

    def serve_login(self, error=""):
        body = LOGIN_PAGE.replace("__ERROR__", error).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_login(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > 4096:
            self.send_redirect("/login?e=1")
            return
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "ignore"))
        password = (fields.get("password") or [""])[0]
        expected = type(self).auth_password or ""
        if password and hmac.compare_digest(password.encode(), expected.encode()):
            WEB_LOGGER.info("Web 登录成功: %s", self.address_string())
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"slyar_session={type(self).session_token}; Path=/; HttpOnly; SameSite=Lax",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            WEB_LOGGER.warning("Web 登录失败: %s", self.address_string())
            self.serve_login("密码错误，请重试")

    # ---------- 页面 ----------

    def serve_page(self):
        body = PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- API ----------

    def api_list(self, qs):
        rel = (qs.get("path") or [""])[0]
        target = safe_join(self.directory, rel)
        if target is None:
            self.send_error_page(400, "非法路径")
            return
        if not os.path.isdir(target):
            self.send_error_page(404, "目录不存在")
            return
        items = []
        for name in os.listdir(target):
            full = os.path.join(target, name)
            try:
                st = os.stat(full)
                is_dir = os.path.isdir(full)
                items.append(
                    {
                        "name": name,
                        "dir": is_dir,
                        "size": 0 if is_dir else st.st_size,
                        "mtime": int(st.st_mtime),
                    }
                )
            except OSError:
                continue
        self.send_json(
            {
                "ok": True,
                "path": rel.replace("\\", "/"),
                "items": items,
                "readonly": type(self).readonly,
                "auth": bool(type(self).auth_password),
            }
        )

    def api_search(self, qs):
        """跨子目录递归搜索文件名"""
        q = (qs.get("q") or [""])[0].strip().lower()
        if not q:
            self.send_json({"ok": True, "results": [], "truncated": False})
            return
        root = os.path.realpath(self.directory)
        results = []
        scanned = 0
        deadline = time.monotonic() + SEARCH_TIME_BUDGET
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
            if rel_dir == ".":
                rel_dir = ""
            for name in dirnames + filenames:
                scanned += 1
                if scanned > MAX_SEARCH_ENTRIES or time.monotonic() >= deadline:
                    self.send_json({"ok": True, "results": results, "truncated": True})
                    return
                if q not in name.lower():
                    continue
                full = os.path.join(dirpath, name)
                if safe_join(root, os.path.relpath(full, root)) is None:
                    continue
                try:
                    st = os.stat(full)
                    is_dir = os.path.isdir(full)
                except OSError:
                    continue
                results.append(
                    {
                        "name": name,
                        "dir": is_dir,
                        "size": 0 if is_dir else st.st_size,
                        "mtime": int(st.st_mtime),
                        "path": f"{rel_dir}/{name}" if rel_dir else name,
                    }
                )
                if len(results) >= MAX_SEARCH_RESULTS:
                    self.send_json(
                        {"ok": True, "results": results, "truncated": True}
                    )
                    return
        self.send_json({"ok": True, "results": results, "truncated": False})

    # ---------- 文本快享 ----------

    def api_paste_create(self, body):
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self.send_error_page(400, "内容不能为空")
            return
        if len(text.encode("utf-8")) > MAX_PASTE_SIZE:
            self.send_error_page(413, "内容超过 1MB 限制")
            return
        title = sanitize_filename(body.get("title", "") or "")
        paste_id = secrets.token_hex(4)
        with PASTE_LOCK:
            while paste_id in PASTES:
                paste_id = secrets.token_hex(4)
            PASTES[paste_id] = {
                "title": title or "未命名",
                "text": text,
                "time": int(time.time()),
            }
            while len(PASTES) > MAX_PASTES:
                PASTES.pop(next(iter(PASTES)))
        WEB_LOGGER.info("创建快享 %s (%d 字符)", paste_id, len(text))
        self.send_json({"ok": True, "id": paste_id})

    def api_paste_list(self):
        with PASTE_LOCK:
            items = [
                {
                    "id": pid,
                    "title": p["title"],
                    "time": p["time"],
                    "size": len(p["text"].encode("utf-8")),
                }
                for pid, p in reversed(list(PASTES.items()))
            ]
        self.send_json({"ok": True, "items": items, "readonly": type(self).readonly})

    def api_paste_get(self, paste_id):
        with PASTE_LOCK:
            paste = PASTES.get(paste_id)
        if paste is None:
            self.send_error_page(404, "快享不存在或已过期")
            return
        self.send_json(
            {"ok": True, "id": paste_id, "title": paste["title"], "text": paste["text"]}
        )

    def api_paste_delete(self, body):
        paste_id = body.get("id", "")
        with PASTE_LOCK:
            removed = PASTES.pop(paste_id, None)
        if removed is None:
            self.send_error_page(404, "快享不存在")
            return
        self.send_json({"ok": True})

    def serve_paste(self, paste_id):
        with PASTE_LOCK:
            paste = PASTES.get(paste_id)
        if paste is None:
            self.send_error_page(404, "快享不存在或已过期")
            return
        data = json.dumps(
            {"title": paste["title"], "time": paste["time"], "text": paste["text"]}
        ).replace("</", "<\\/") + ""
        body = PASTE_PAGE.replace("__ID__", paste_id).replace(
            "__DATA__", data
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, qs, attachment):
        rel = (qs.get("path") or [""])[0]
        target = safe_join(self.directory, rel)
        if target is None or not os.path.isfile(target):
            self.send_error_page(404, "文件不存在")
            return
        mime = guess_mime(target, for_preview=not attachment)
        size = os.path.getsize(target)
        etag = f'W/"{int(os.path.getmtime(target))}-{size}"'
        if not attachment and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return

        # Range 断点续传（大文件下载续传 / 视频进度条拖动）
        start = end = None
        range_header = self.headers.get("Range")
        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                    end = size - 1
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)

        name = os.path.basename(target)
        self.send_response(200 if start is None else 206)
        self.send_header("Content-Type", mime)
        self.send_header("ETag", etag)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        if start is None:
            self.send_header("Content-Length", str(size))
        else:
            length = end - start + 1
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if attachment:
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}",
            )
        else:
            self.send_header("Cache-Control", "private, max-age=60")
        self.end_headers()
        with open(target, "rb") as f:
            if start is not None:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            else:
                shutil.copyfileobj(f, self.wfile)

    def api_zip(self, qs):
        """限制作业数量和并发，不限制单文件下载大小。"""
        if not ZIP_SLOTS.acquire(blocking=False):
            self.send_error_page(429, "同时打包任务过多，请稍后重试")
            return
        try:
            rel = (qs.get("path") or [""])[0]
            root = safe_join(self.directory, rel)
            if root is None or not os.path.isdir(root):
                self.send_error_page(404, "目录不存在")
                return
            names = [sanitize_filename(n) for n in qs.get("name", [])]
            names = [n for n in names if n]
            if not names:
                names = os.listdir(root)
            files = []
            total_bytes = 0
            deadline = time.monotonic() + SEARCH_TIME_BUDGET
            for name in names:
                full = safe_join(root, name)
                if full is None or not os.path.exists(full):
                    continue
                candidates = []
                if os.path.isdir(full):
                    for dirpath, _dirnames, filenames in os.walk(full):
                        for filename in filenames:
                            original = os.path.join(dirpath, filename)
                            relative = os.path.relpath(original, root)
                            checked = safe_join(root, relative)
                            if checked is not None and os.path.isfile(checked):
                                candidates.append((relative, checked))
                            if len(files) + len(candidates) > MAX_ZIP_ENTRIES or time.monotonic() >= deadline:
                                self.send_error_page(413, "打包文件过多或目录扫描超时")
                                return
                elif os.path.isfile(full):
                    candidates.append((name, full))
                for arc, file_path in candidates:
                    try:
                        total_bytes += os.path.getsize(file_path)
                    except OSError:
                        continue
                    if total_bytes > MAX_ZIP_BYTES or len(files) >= MAX_ZIP_ENTRIES:
                        self.send_error_page(413, "打包总量超过 100 GiB 或 100000 个文件")
                        return
                    files.append((arc, file_path))
            if not files:
                self.send_error_page(404, "没有可打包的内容")
                return
            zip_name = f"{os.path.basename(root) or 'files'}_{time.strftime('%Y%m%d_%H%M%S')}.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(zip_name)}")
            self.send_header("Connection", "close")
            self.end_headers()
            writer = _CountingWriter(self.wfile)
            # 局域网中优先传输速度：不再对 10GB 级文件进行逐字节压缩。
            with zipfile.ZipFile(writer, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
                for arc, file_path in files:
                    try:
                        archive.write(file_path, arc)
                    except OSError:
                        continue
            self.close_connection = True
        finally:
            ZIP_SLOTS.release()

    def api_upload(self, content_length):
        # 上传结束后关闭连接，避免未读完的 multipart 尾部污染下一个请求。
        self.close_connection = True
        content_type = self.headers.get("Content-Type", "")
        m = re.search(r'boundary="?([^";]+)"?', content_type)
        if not content_type.startswith("multipart/form-data") or not m:
            self.send_error_page(400, "需要 multipart/form-data")
            return
        boundary = m.group(1).encode("utf-8")
        if len(boundary) > 200:
            self.send_error_page(400, "multipart boundary 过长")
            return
        try:
            saved, skipped = self._parse_multipart(boundary, content_length)
        except (EOFError, ValueError) as e:
            WEB_LOGGER.warning("上传数据格式错误: %s", e)
            self.send_error_page(400, "上传数据不完整或格式错误")
            return
        except Exception as e:
            WEB_LOGGER.error("解析上传数据失败: %s", e)
            self.send_error_page(500, "上传数据解析失败")
            return
        self.send_json({"ok": True, "saved": saved, "skipped": skipped})

    def _parse_multipart(self, boundary, content_length):
        reader = _MultipartReader(self.rfile, boundary, content_length)
        first = reader.read_line()
        if first.strip() != b"--" + boundary:
            raise ValueError("boundary 不匹配")
        saved = []
        skipped = 0
        fields = {}
        finished = False
        while not finished:
            headers = {}
            while True:
                line = reader.read_line()
                if line == b"":
                    break
                if len(headers) >= 32:
                    raise ValueError("multipart 头部过多")
                k, _, v = line.decode("utf-8", "ignore").partition(":")
                headers[k.strip().lower()] = v.strip()
            disp = headers.get("content-disposition", "")
            name_m = re.search(r'name="([^"]*)"', disp)
            file_m = re.search(r'filename="([^"]*)"', disp)
            field_name = name_m.group(1) if name_m else ""
            if not file_m:
                chunks = []
                used = 0

                def _sink(b):
                    nonlocal used
                    if used + len(b) > MAX_FIELD_SIZE:
                        raise ValueError("multipart 字段过大")
                    chunks.append(b)
                    used += len(b)

                finished = reader.stream_body(_sink)
                fields[field_name] = b"".join(chunks).decode("utf-8", "ignore")
            else:
                raw_name = urllib.parse.unquote(file_m.group(1))
                filename = sanitize_filename(raw_name)
                target_dir = self.resolve_upload_directory(fields.get("path", ""))
                dest = None
                output = None
                if filename and target_dir is not None and os.path.isdir(target_dir):
                    requested = os.path.join(target_dir, filename)
                    for _ in range(1000):
                        candidate = unique_path(requested)
                        if candidate is None:
                            break
                        try:
                            output = open(candidate, "xb", buffering=256 * 1024)
                            dest = candidate
                            break
                        except FileExistsError:
                            continue
                if output is None:
                    skipped += 1
                    finished = reader.stream_body(lambda b: None)
                else:
                    try:
                        with output as out:
                            finished = reader.stream_body(out.write)
                    except Exception:
                        if os.path.exists(dest):
                            os.remove(dest)
                        raise
                    saved.append(os.path.basename(dest))
        return saved, skipped

    def resolve_upload_directory(self, relative_path):
        """解析上传目标目录；子类可将上传限制在专用目录。"""
        return safe_join(self.directory, relative_path)

    def api_mkdir(self, body):
        name = sanitize_filename(body.get("name", ""))
        target = safe_join(self.directory, body.get("path", ""))
        if target is None:
            self.send_error_page(400, "非法路径")
            return
        if not name:
            self.send_error_page(400, "文件夹名称不合法")
            return
        path = os.path.join(target, name)
        if os.path.exists(path):
            self.send_error_page(409, "同名文件或文件夹已存在")
            return
        try:
            os.makedirs(path)
        except OSError as e:
            self.send_error_page(500, f"创建失败: {e}")
            return
        WEB_LOGGER.info("新建文件夹: %s", path)
        self.send_json({"ok": True})

    def api_rename(self, body):
        target = safe_join(self.directory, body.get("path", ""))
        old = sanitize_filename(body.get("old", ""))
        new = sanitize_filename(body.get("new", ""))
        if target is None:
            self.send_error_page(400, "非法路径")
            return
        if not old or not new:
            self.send_error_page(400, "名称不合法")
            return
        src = os.path.join(target, old)
        dst = os.path.join(target, new)
        if not os.path.exists(src):
            self.send_error_page(404, "原文件不存在")
            return
        if os.path.exists(dst):
            self.send_error_page(409, "目标名称已存在")
            return
        try:
            os.rename(src, dst)
        except OSError as e:
            self.send_error_page(500, f"重命名失败: {e}")
            return
        WEB_LOGGER.info("重命名: %s -> %s", src, dst)
        self.send_json({"ok": True})

    def api_delete(self, body):
        target = safe_join(self.directory, body.get("path", ""))
        if target is None:
            self.send_error_page(400, "非法路径")
            return
        names = body.get("names", [])
        if not isinstance(names, list) or not names:
            self.send_error_page(400, "未指定删除对象")
            return
        deleted = 0
        for name in names:
            clean = sanitize_filename(name if isinstance(name, str) else "")
            if not clean:
                continue
            path = os.path.join(target, clean)
            if os.path.abspath(path) == os.path.abspath(self.directory):
                continue
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                elif os.path.exists(path) or os.path.islink(path):
                    os.remove(path)
                else:
                    continue
                deleted += 1
                WEB_LOGGER.info("删除: %s", path)
            except OSError as e:
                WEB_LOGGER.error("删除失败 %s: %s", path, e)
        self.send_json({"ok": True, "deleted": deleted})


class ThreadedWebServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """线程化Web服务器：并发处理浏览器请求，避免单线程排队卡死"""

    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 128

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(120)  # 空闲超时，不是整个大文件传输的时限
        return request, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_web_handler(directory, password="", readonly=False):
    """绑定共享目录、访问密码与只读模式，返回 handler 类"""

    class BoundWebFileHandler(WebFileHandler):
        pass

    BoundWebFileHandler.directory = os.path.abspath(directory)
    BoundWebFileHandler.auth_password = password or ""
    BoundWebFileHandler.readonly = bool(readonly)
    BoundWebFileHandler.session_token = (
        secrets.token_urlsafe(24) if password else None
    )
    return BoundWebFileHandler


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 - Slyar 文件分享</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  background:radial-gradient(ellipse at 15% 5%,#e1efff 0,transparent 38%),#f2f6fb;color:#1d3046}
.card{background:#fff;border:1px solid #dce6ef;border-top:4px solid #3569c8;
  border-radius:18px;padding:34px;width:min(390px,calc(100vw - 32px));
  box-shadow:0 14px 40px rgba(29,64,103,.11);text-align:left}
.logo{display:inline-flex;width:48px;height:48px;align-items:center;justify-content:center;
  background:#173d69;color:#fff;font:700 18px Consolas,monospace;border-radius:14px}
h1{font-size:22px;margin:20px 0 6px;letter-spacing:.2px}
p{color:#5d7187;font-size:13px;margin:0 0 24px}
input{width:100%;padding:12px 13px;border:1px solid #dce5ee;border-radius:10px;
  background:#f7f9fc;color:#1d3046;font-size:14px;outline:none}
input:focus{border-color:#4b79d2;box-shadow:0 0 0 3px #eaf2ff}
button{width:100%;margin-top:14px;padding:12px;border:1px solid #3569c8;border-radius:10px;
  background:#3569c8;color:#fff;font-size:14px;font-weight:700;cursor:pointer}
button:hover{background:#2857ad}
.err{color:#ad4b54;font-size:12.5px;margin:12px 0 0;min-height:16px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">SF</div>
  <h1>Slyar 文件分享</h1>
  <p>此服务器已开启访问密码</p>
  <form method="post" action="/login">
    <input type="password" name="password" placeholder="请输入访问密码" autofocus required>
    <button type="submit">进 入</button>
  </form>
  <div class="err">__ERROR__</div>
</div>
</body>
</html>
"""

PASTE_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>快享文本 - Slyar 文件分享</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;
  padding:30px 16px;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  background:#edf4f8;color:#20394f}
.card{background:#fff;border:1px solid #cbdbe7;border-top:5px solid #176b9f;
  border-radius:6px;width:100%;max-width:900px;margin:30px auto;
  box-shadow:0 8px 30px rgba(22,64,93,.07);overflow:hidden}
.head{display:flex;align-items:center;gap:10px;padding:18px 22px;
  background:#e8f1f7;border-bottom:1px solid #cbdbe7}
.head h1{font-size:16px;margin:0;flex:1;word-break:break-all}
.meta{color:#557188;font-size:12px;white-space:nowrap}
pre{margin:0;padding:20px;white-space:pre-wrap;word-break:break-all;
  font-size:13px;line-height:1.6;max-height:72vh;overflow:auto;font-family:Consolas,monospace}
.foot{display:flex;gap:10px;justify-content:flex-end;padding:14px 20px;border-top:1px solid #cbdbe7}
button,a.btn{padding:9px 16px;border-radius:4px;border:1px solid #cbdbe7;background:#fff;
  color:#20394f;font-size:13px;cursor:pointer;text-decoration:none}
button.primary{background:#176b9f;border-color:#176b9f;color:#fff}
#copied{color:#117b68;font-size:13px;align-self:center;margin-right:auto;visibility:hidden}
</style>
</head>
<body>
<div class="card">
  <div class="head"><h1 id="title"></h1><span class="meta" id="time"></span></div>
  <pre id="content"></pre>
  <div class="foot">
    <span id="copied">已复制到剪贴板</span>
    <button id="copyBtn" class="primary">📋 复制全文</button>
    <a class="btn" href="/">🏠 主页</a>
  </div>
</div>
<script>
const PASTE = __DATA__;
document.title = "快享: " + PASTE.title;
document.getElementById("title").textContent = PASTE.title;
const d = new Date(PASTE.time * 1000), p = n => String(n).padStart(2, "0");
document.getElementById("time").textContent =
  d.getFullYear() + "-" + p(d.getMonth()+1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
document.getElementById("content").textContent = PASTE.text;
document.getElementById("copyBtn").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(PASTE.text);
    const tip = document.getElementById("copied");
    tip.style.visibility = "visible";
    setTimeout(() => tip.style.visibility = "hidden", 1500);
  } catch (e) { alert("复制失败，请手动选择文本复制"); }
});
</script>
</body>
</html>
"""


PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Slyar 文件分享</title>
<style>
:root{
  --bg:#edf4f8;--panel:#ffffff;--text:#20394f;--muted:#58738a;--border:#cbdbe7;
  --accent:#176b9f;--accent-weak:#e4f1f9;--danger:#a3454c;--danger-weak:#faeeee;
  --hover:#f3f8fb;--shadow:0 3px 16px rgba(22,64,93,.07);
  --navy:#153f5d;--soft:#e8f1f7;
}
[data-theme="dark"]{
  --bg:#102737;--panel:#19364b;--text:#e9f3fa;--muted:#a5bed0;--border:#34536a;
  --accent:#77bce7;--accent-weak:#224b67;--danger:#ef9ba1;--danger-weak:#492e3a;
  --hover:#24465c;--shadow:0 3px 16px rgba(0,0,0,.18);
  --navy:#0c2232;--soft:#213f55;
}
*{box-sizing:border-box}
[hidden]{display:none !important}
body{
  margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  font-size:14px;min-height:100vh;display:flex;flex-direction:column;
}
.topbar{
  position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:14px;
  padding:12px 20px;background:var(--panel);box-shadow:var(--shadow);
}
.brand{font-weight:700;font-size:16px;white-space:nowrap}
.search{
  flex:1;max-width:420px;padding:8px 14px;border:1px solid var(--border);
  border-radius:10px;background:var(--bg);color:var(--text);outline:none;font-size:13px;
}
.search:focus{border-color:var(--accent)}
.spacer{flex:1}
.icon-btn{
  width:34px;height:34px;border:1px solid var(--border);border-radius:9px;
  background:var(--panel);cursor:pointer;font-size:15px;color:var(--text);
  display:inline-flex;align-items:center;justify-content:center;flex:none;
}
.icon-btn:hover{background:var(--hover)}
.crumbs{
  padding:14px 20px 0;display:flex;flex-wrap:wrap;align-items:center;gap:2px;
  font-size:15px;font-weight:600;
}
.crumbs a{color:var(--muted);text-decoration:none;padding:3px 8px;border-radius:8px}
.crumbs a:hover{background:var(--hover);color:var(--text)}
.crumbs .sep{color:var(--muted);opacity:.5}
.crumbs .here{padding:3px 8px}
.toolbar{
  display:flex;align-items:center;gap:10px;padding:12px 20px;flex-wrap:wrap;
}
.btn{
  padding:8px 16px;border:1px solid var(--border);border-radius:10px;
  background:var(--panel);color:var(--text);cursor:pointer;font-size:13px;
  display:inline-flex;align-items:center;gap:6px;transition:all .15s;
}
.btn:hover:not(:disabled){background:var(--hover);transform:translateY(-1px)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover:not(:disabled){filter:brightness(1.08);background:var(--accent)}
.btn.danger{color:var(--danger)}
.btn.danger:hover:not(:disabled){background:var(--danger-weak)}
.count-hint{color:var(--muted);font-size:13px}
.list-head{
  display:flex;align-items:center;gap:12px;padding:8px 20px;color:var(--muted);
  font-size:12px;position:sticky;top:59px;background:var(--bg);z-index:10;
}
.list-head .col{
  background:none;border:none;color:var(--muted);cursor:pointer;font-size:12px;
  padding:4px 2px;font-family:inherit;
}
.list-head .col:hover{color:var(--text)}
.list-head .arrow{font-size:10px}
.list{padding:0 20px 10px;display:flex;flex-direction:column;gap:6px}
.row{
  display:flex;align-items:center;gap:12px;padding:10px 14px;background:var(--panel);
  border:1px solid var(--border);border-radius:12px;transition:all .15s;
}
.row:hover{background:var(--hover);transform:translateY(-1px)}
.row.selected{border-color:var(--accent);background:var(--accent-weak)}
.cell-check{display:flex;align-items:center;flex:none}
.cell-check input{width:16px;height:16px;accent-color:var(--accent);cursor:pointer}
.cell-icon{font-size:20px;flex:none;width:28px;text-align:center}
.cell-name{
  flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  cursor:pointer;font-weight:500;
}
.cell-name:hover{color:var(--accent)}
.cell-size{width:90px;text-align:right;color:var(--muted);font-size:12px;flex:none}
.cell-mtime{width:150px;color:var(--muted);font-size:12px;flex:none}
.cell-ops{display:flex;gap:6px;flex:none;opacity:0;transition:opacity .15s}
.row:hover .cell-ops{opacity:1}
.op{
  width:30px;height:30px;border:none;border-radius:8px;background:var(--hover);
  cursor:pointer;font-size:14px;display:inline-flex;align-items:center;justify-content:center;
}
.op:hover{background:var(--accent-weak)}
.op.del:hover{background:var(--danger-weak)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;padding:0 20px 10px}
.card{
  position:relative;background:var(--panel);border:1px solid var(--border);
  border-radius:14px;padding:14px 10px;text-align:center;cursor:pointer;transition:all .15s;
}
.card:hover{transform:translateY(-2px);box-shadow:var(--shadow)}
.card.selected{border-color:var(--accent);background:var(--accent-weak)}
.card .thumb{width:100%;height:100px;object-fit:cover;border-radius:9px;background:var(--hover)}
.card .card-icon{font-size:42px;height:100px;display:flex;align-items:center;justify-content:center}
.card-name{margin-top:8px;font-size:12px;font-weight:500;word-break:break-all;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card-meta{margin-top:4px;color:var(--muted);font-size:11px}
.card .cell-check{position:absolute;top:8px;left:8px}
.empty{text-align:center;padding:70px 0;color:var(--muted)}
.empty .empty-icon{font-size:52px;margin-bottom:12px}
.stats{padding:14px 20px 26px;color:var(--muted);font-size:12px;margin-top:auto}
.upload-zone{
  margin:0 20px 4px;padding:12px 16px;border:2px dashed var(--border);border-radius:12px;
  color:var(--muted);text-align:center;font-size:13px;transition:all .15s;
}
.upload-zone.dragover{border-color:var(--accent);color:var(--accent);background:var(--accent-weak)}
.progress{
  display:flex;align-items:center;gap:10px;margin-top:10px;font-size:12px;color:var(--text);
}
.bar-track{
  flex:1;height:7px;background:var(--hover);border-radius:4px;overflow:hidden;
}
.bar{height:100%;width:0;background:var(--accent);border-radius:4px;transition:width .2s}
.modal{
  position:fixed;inset:0;z-index:200;background:rgba(10,14,25,.55);
  display:flex;align-items:center;justify-content:center;padding:20px;
}
.modal-box{
  background:var(--panel);border-radius:16px;width:100%;max-width:560px;
  max-height:86vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.3);
}
.preview-box{max-width:920px}
.modal-head{
  display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:14px 18px;border-bottom:1px solid var(--border);font-weight:600;
  word-break:break-all;
}
.modal-body{padding:18px}
.modal-foot{display:flex;justify-content:flex-end;gap:10px;margin-top:16px}
.text-input{
  width:100%;padding:9px 12px;border:1px solid var(--border);border-radius:9px;
  background:var(--bg);color:var(--text);outline:none;font-size:14px;
}
.text-input:focus{border-color:var(--accent)}
.pv-body{padding:18px;overflow:auto;min-height:120px}
.pv-body img{max-width:100%;border-radius:10px;display:block;margin:0 auto}
.pv-body video{width:100%;border-radius:10px}
.pv-body iframe{width:100%;height:65vh;border:none;border-radius:10px}
.pv-body pre{
  white-space:pre-wrap;word-break:break-all;background:var(--hover);
  padding:14px;border-radius:10px;font-size:12.5px;line-height:1.55;max-height:62vh;overflow:auto;
}
.pv-fallback{text-align:center;color:var(--muted);padding:30px 0}
.toasts{position:fixed;right:18px;bottom:18px;z-index:300;display:flex;flex-direction:column;gap:8px}
.toast{
  background:var(--panel);border:1px solid var(--border);border-left:4px solid var(--accent);
  padding:10px 16px;border-radius:10px;box-shadow:var(--shadow);font-size:13px;
  animation:slidein .2s ease;max-width:340px;
}
.toast.error{border-left-color:var(--danger)}
.toast.ok{border-left-color:#30a46c}
@keyframes slidein{from{transform:translateX(30px);opacity:0}to{transform:none;opacity:1}}
.drag-overlay{
  position:fixed;inset:0;z-index:400;background:rgba(59,130,246,.16);backdrop-filter:blur(2px);
  display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:700;
  color:var(--accent);border:3px dashed var(--accent);margin:10px;border-radius:18px;
}
.path-hint{
  color:var(--muted);font-size:11px;margin-top:2px;font-weight:400;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
textarea.text-input{resize:vertical;font-family:inherit;line-height:1.5}
.paste-list{margin-top:14px;display:flex;flex-direction:column;gap:6px;max-height:240px;overflow:auto}
.paste-item{display:flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid var(--border);border-radius:9px;font-size:12.5px}
.paste-item .pt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
.paste-item .pm{color:var(--muted);white-space:nowrap}
.paste-empty{color:var(--muted);text-align:center;padding:14px 0;font-size:12.5px}
/* 蓝白工业面板：清晰的导航、工具区与文件数据表。 */
.topbar{
  min-height:74px;padding:0;background:var(--navy);border-bottom:3px solid #2b83b7;
  box-shadow:none;
}
.topbar-inner{
  width:min(1260px,calc(100% - 48px));margin:auto;display:flex;align-items:center;
  gap:18px;min-height:71px;
}
.brand{display:flex;align-items:center;gap:12px;color:#fff;font-size:15px}
.brand-mark{
  width:40px;height:40px;display:flex;align-items:center;justify-content:center;
  flex:none;background:#2a7cac;border:1px solid #79a9c7;border-radius:4px;
  font:700 14px Consolas,monospace;letter-spacing:.5px;
}
.brand-copy{display:flex;flex-direction:column;gap:3px}
.brand-copy strong{font-size:15px;line-height:1.1}
.brand-copy small{font:10px Consolas,monospace;letter-spacing:1px;color:#b9d5e6}
.search{
  max-width:440px;height:39px;border:1px solid #7ba4bc;border-radius:4px;
  background:#fff;color:#20394f;font-size:13px;
}
.search:focus{border-color:#9bd1f2;box-shadow:0 0 0 3px rgba(155,209,242,.2)}
.topbar .icon-btn{
  width:38px;height:38px;background:rgba(255,255,255,.06);
  border-color:#638aa3;border-radius:4px;color:#f4f9fc;
}
.topbar .icon-btn:hover{background:rgba(255,255,255,.16)}
.workspace-head,.crumbs,.toolbar,.upload-zone,main,.stats{
  width:min(1260px,calc(100% - 48px));margin-left:auto;margin-right:auto;
}
.workspace-head{padding:27px 0 18px;border-bottom:1px solid var(--border)}
.eyebrow{
  color:var(--accent);font:700 11px Consolas,monospace;letter-spacing:1.6px;
  margin-bottom:7px;
}
.workspace-title{display:flex;align-items:center;justify-content:space-between;gap:16px}
.workspace-title h1{margin:0;font-size:25px;letter-spacing:.2px;line-height:1.25}
.workspace-title p{margin:5px 0 0;color:var(--muted);font-size:13px}
.network-pill{
  color:var(--accent);background:var(--accent-weak);border:1px solid var(--border);
  border-radius:3px;padding:7px 10px;font:700 11px Consolas,monospace;
  white-space:nowrap;
}
.crumbs{padding:14px 0 0;font-size:13px;font-weight:600}
.crumbs a,.crumbs .here{border-radius:3px;padding:5px 7px}
.crumbs .here{color:var(--accent)}
.toolbar{padding:14px 0 16px;gap:8px}
.btn{
  min-height:38px;padding:8px 14px;border-radius:4px;font-weight:600;
  transition:background .15s,border-color .15s;
}
.btn:hover:not(:disabled){transform:none;border-color:var(--accent)}
.btn.primary:hover:not(:disabled){background:#105781;border-color:#105781;filter:none}
.btn.danger{border-color:var(--border)}
.btn:focus-visible,.icon-btn:focus-visible,.op:focus-visible{
  outline:2px solid var(--accent);outline-offset:2px;
}
.readonly-badge{
  background:var(--soft);border:1px solid var(--border);border-radius:3px;
  color:var(--accent);padding:7px 10px;font-weight:700;
}
#selHint:empty{display:none}
.upload-zone{
  margin-bottom:16px;padding:15px 16px;border-width:1px;border-radius:5px;
  background:var(--panel);font-size:12.5px;
}
.file-panel{
  background:var(--panel);border:1px solid var(--border);border-radius:6px;
  overflow:hidden;min-height:260px;flex:1;
}
.list-head{
  position:relative;top:0;padding:11px 16px;background:var(--soft);
  border-bottom:1px solid var(--border);font-size:12px;
}
.list-head .col{font-weight:700;font-family:inherit}
.list-head .col.name{flex:1;text-align:left;margin-left:40px}
.list-head .col.size{width:90px;flex:none;text-align:right}
.list-head .col.mtime{width:150px;flex:none;text-align:left}
.list-head::after{content:"";width:138px;flex:none}
.list{padding:0;gap:0}
.row{
  min-height:58px;padding:11px 16px;border:0;border-bottom:1px solid var(--border);
  border-radius:0;background:var(--panel);
}
.row:last-child{border-bottom:0}
.row:hover{transform:none;background:var(--hover)}
.row.selected{border-color:var(--border);box-shadow:inset 3px 0 var(--accent)}
.cell-name{font-weight:600}
.cell-ops{opacity:1;width:138px;justify-content:flex-end}
.op{
  background:transparent;border:1px solid transparent;border-radius:4px;
  color:var(--muted);
}
.op:hover{border-color:var(--border);color:var(--accent)}
.grid{padding:16px;gap:12px}
.card{border-radius:5px;padding:15px 11px}
.card:hover{transform:none;border-color:var(--accent);box-shadow:var(--shadow)}
.card .thumb{border-radius:3px}
.empty{padding:68px 16px}
.stats{padding:14px 4px 30px;margin-top:0}
.modal-box{border:1px solid var(--border);border-top:4px solid var(--accent);border-radius:6px}
.modal-head{background:var(--soft)}
.text-input,.pv-body pre,.toast,.paste-item{border-radius:4px}
.drag-overlay{background:rgba(23,107,159,.16);border-radius:6px}
[data-theme="dark"] .btn.primary{color:#102737}
[data-theme="dark"] .btn.primary:hover:not(:disabled){background:#96d0f0;border-color:#96d0f0}
@media (max-width:720px){
  .cell-mtime,.list-head .col.mtime{display:none}
  .cell-size{width:70px}
  .list-head .col.size{width:70px}
  .list-head::after{width:115px}
  .cell-ops{opacity:1;width:115px}
  .search{max-width:none}
  .topbar-inner{width:calc(100% - 24px);flex-wrap:wrap;gap:8px;padding:10px 0}
  .brand{flex:1}
  .brand-copy small{font-size:9px}
  .topbar .spacer{display:none}
  .search{order:4;flex:0 0 100%;min-width:0}
  .workspace-head,.crumbs,.toolbar,.upload-zone,main,.stats{width:calc(100% - 24px)}
  .workspace-head{padding:18px 0 15px}
  .workspace-title h1{font-size:21px}
  .network-pill{display:none}
  .toolbar .spacer{display:none}
  .toolbar{gap:7px}
  .btn{padding:7px 10px;font-size:12px}
  .row{gap:7px;padding:10px 9px}
  .cell-ops{gap:1px}
  .op{width:28px;height:28px}
  .cell-icon{width:24px;font-size:18px}
}
@media (max-width:540px){
  .cell-size,.list-head .col.size{display:none}
  .list-head::after{display:none}
  .row{flex-wrap:wrap}
  .cell-name{flex-basis:calc(100% - 64px)}
  .cell-ops{width:100%;justify-content:flex-end}
}
</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <div class="brand"><span class="brand-mark">SF</span><span class="brand-copy"><strong>Slyar 文件分享</strong><small>FILE TRANSFER / WEB CONSOLE</small></span></div>
    <input id="searchBox" class="search" type="search" placeholder="搜索当前目录 · 回车搜索全部文件" aria-label="搜索文件">
    <div class="spacer"></div>
    <button id="viewBtn" class="icon-btn" title="切换列表/网格视图" aria-label="切换列表或网格视图">▦</button>
    <button id="themeBtn" class="icon-btn" title="切换明暗主题" aria-label="切换明暗主题">☾</button>
  </div>
</header>
<section class="workspace-head">
  <div class="eyebrow">WORKSPACE / 01</div>
  <div class="workspace-title"><div><h1>共享文件</h1><p>浏览、传输与管理当前共享目录</p></div><span class="network-pill">● LAN READY</span></div>
</section>
<nav class="crumbs" id="crumbs"></nav>
<div class="toolbar">
  <button id="uploadBtn" class="btn primary">上传文件</button>
  <input id="fileInput" type="file" multiple hidden>
  <button id="mkdirBtn" class="btn">新建文件夹</button>
  <span id="roBadge" class="count-hint readonly-badge" hidden>只读模式</span>
  <span class="spacer"></span>
  <span id="selHint" class="count-hint"></span>
  <button id="pasteBtn" class="btn">快享文本</button>
  <button id="zipBtn" class="btn">打包下载</button>
  <button id="delBtn" class="btn danger" disabled>删除</button>
</div>
<div class="upload-zone" id="dropZone">
  将文件拖放到此处，或点击「上传文件」<span id="upTip"></span>
  <div id="progWrap" class="progress" hidden>
    <div class="bar-track"><div class="bar" id="progBar"></div></div>
    <span id="progText"></span>
  </div>
</div>
<main class="file-panel">
  <div class="list-head" id="listHead">
    <label class="cell-check"><input type="checkbox" id="selAll" title="全选"></label>
    <button class="col name" data-sort="name">名称 <span class="arrow" data-a="name"></span></button>
    <button class="col size" data-sort="size">大小 <span class="arrow" data-a="size"></span></button>
    <button class="col mtime" data-sort="mtime">修改时间 <span class="arrow" data-a="mtime"></span></button>
  </div>
  <div id="fileList" class="list"></div>
  <div id="empty" class="empty" hidden><div class="empty-icon">🗂️</div>此文件夹为空</div>
</main>
<footer class="stats" id="stats"></footer>

<div id="pvModal" class="modal" hidden>
  <div class="modal-box preview-box">
    <div class="modal-head"><span id="pvTitle"></span><button class="icon-btn" id="pvClose">✕</button></div>
    <div id="pvBody" class="pv-body"></div>
  </div>
</div>
<div id="inModal" class="modal" hidden>
  <div class="modal-box" style="max-width:420px">
    <div class="modal-head"><span id="inTitle"></span><button class="icon-btn" id="inClose">✕</button></div>
    <div class="modal-body">
      <input id="inInput" class="text-input" type="text">
      <div class="modal-foot">
        <button id="inCancel" class="btn">取消</button>
        <button id="inOk" class="btn primary">确定</button>
      </div>
    </div>
  </div>
</div>
<div id="cfModal" class="modal" hidden>
  <div class="modal-box" style="max-width:420px">
    <div class="modal-head"><span>确认操作</span><button class="icon-btn" id="cfClose">✕</button></div>
    <div class="modal-body">
      <p id="cfText" style="margin:0;line-height:1.6"></p>
      <div class="modal-foot">
        <button id="cfCancel" class="btn">取消</button>
        <button id="cfOk" class="btn danger">删除</button>
      </div>
    </div>
  </div>
</div>
<div id="toasts" class="toasts"></div>
<div id="dragOverlay" class="drag-overlay" hidden>📥 松开鼠标，上传文件</div>
<div id="pasteModal" class="modal" hidden>
  <div class="modal-box">
    <div class="modal-head"><span>📋 文本快享</span><button class="icon-btn" id="pmClose">✕</button></div>
    <div class="modal-body">
      <textarea id="pasteText" class="text-input" rows="5" placeholder="粘贴要分享的文本、代码片段…（生成链接后任何设备打开即可获取）"></textarea>
      <div class="modal-foot">
        <button id="pasteCreate" class="btn primary">生成快享链接</button>
      </div>
      <div id="pasteList" class="paste-list"></div>
    </div>
  </div>
</div>

<script>
"use strict";
const $ = s => document.querySelector(s);
const state = {
  path: "", items: [], sel: new Set(), rendered: [],
  sortKey: "name", sortDir: 1, filter: "",
  view: localStorage.getItem("slyar_view") || "list",
  readonly: false, mode: "browse", results: [], searchQ: "",
};
const IMG = [".jpg",".jpeg",".png",".gif",".bmp",".webp",".svg",".ico",".avif"];
const VID = [".mp4",".webm",".mov",".m4v"];
const AUD = [".mp3",".wav",".flac",".ogg",".m4a",".aac",".opus"];
const PDF = [".pdf"];
const TXT = [".txt",".md",".py",".js",".css",".json",".xml",".html",".htm",".ini",".log",
  ".csv",".yml",".yaml",".bat",".ps1",".sh",".c",".cpp",".h",".java",".ts",".sql",".toml",".conf"];
const ZIP_ = [".zip",".rar",".7z",".tar",".gz",".bz2",".xz"];
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const extOf = n => { const i = n.lastIndexOf("."); return i < 0 ? "" : n.slice(i).toLowerCase(); };
const fmtSize = n => {
  if (n === 0) return "0 B";
  const u = ["B","KB","MB","GB","TB"]; const i = Math.min(Math.floor(Math.log(n)/Math.log(1024)), 4);
  return (n / Math.pow(1024, i)).toFixed(i ? 1 : 0) + " " + u[i];
};
const fmtTime = ts => {
  const d = new Date(ts * 1000), p = n => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth()+1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
};
const joinPath = name => state.path ? state.path + "/" + name : name;
const relOf = it => it.path !== undefined ? it.path : joinPath(it.name);
const dirOf = rel => { const i = rel.lastIndexOf("/"); return i < 0 ? "" : rel.slice(0, i); };

function toast(msg, type) {
  const el = document.createElement("div");
  el.className = "toast " + (type || "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 3200);
}
async function api(url, opts) {
  let r, data = null;
  try { r = await fetch(url, opts); } catch (e) { throw new Error("网络错误"); }
  try { data = await r.json(); } catch (e) {}
  if (!r.ok || (data && data.ok === false)) throw new Error((data && data.error) || ("HTTP " + r.status));
  return data || {};
}

/* ---------- 路由 ---------- */
const currentRel = () => decodeURIComponent(location.hash.replace(/^#\\/?/, ""));
function nav(rel) { location.hash = "#/" + rel.replace(/^\\/+/, ""); }
window.addEventListener("hashchange", load);

async function load() {
  state.path = currentRel();
  state.sel.clear();
  try {
    const data = await api("/api/list?path=" + encodeURIComponent(state.path));
    state.items = data.items || [];
    state.readonly = !!data.readonly;
    applyReadonlyUI();
  } catch (e) {
    toast(e.message, "error");
    state.items = [];
    if (state.path) nav("");
    return;
  }
  render();
}

function applyReadonlyUI() {
  const ro = state.readonly;
  $("#roBadge").hidden = !ro;
  $("#uploadBtn").hidden = ro;
  $("#mkdirBtn").hidden = ro;
  $("#delBtn").hidden = ro;
  $("#pasteCreate").style.display = ro ? "none" : "";
}

/* ---------- 渲染 ---------- */
function visibleItems() {
  const q = state.filter.trim().toLowerCase();
  const arr = state.items.filter(it => !q || it.name.toLowerCase().includes(q));
  const k = state.sortKey, d = state.sortDir;
  arr.sort((a, b) => {
    if (a.dir !== b.dir) return a.dir ? -1 : 1;
    const va = k === "name" ? a.name.toLowerCase() : a[k];
    const vb = k === "name" ? b.name.toLowerCase() : b[k];
    return va < vb ? -d : va > vb ? d : 0;
  });
  return arr;
}
function iconOf(it) {
  if (it.dir) return "📁";
  const e = extOf(it.name);
  if (IMG.includes(e)) return "🖼️";
  if (VID.includes(e)) return "🎬";
  if (AUD.includes(e)) return "🎵";
  if (PDF.includes(e)) return "📕";
  if (ZIP_.includes(e)) return "📦";
  if (TXT.includes(e)) return "📝";
  if ([".doc",".docx"].includes(e)) return "📘";
  if ([".xls",".xlsx"].includes(e)) return "📗";
  if ([".ppt",".pptx"].includes(e)) return "📙";
  if ([".exe",".msi",".bat",".ps1"].includes(e)) return "⚙️";
  if ([".apk"].includes(e)) return "🤖";
  return "📄";
}
function opsHtml(it) {
  let ops = "";
  if (!it.dir) ops += `<button class="op" data-op="preview" title="预览">👁</button>`;
  ops += `<button class="op" data-op="download" title="下载">⬇</button>`;
  if (!state.readonly) {
    ops += `<button class="op" data-op="rename" title="重命名">✏️</button>`;
    ops += `<button class="op del" data-op="delete" title="删除">🗑</button>`;
  }
  return ops;
}
function render() {
  if (state.mode === "search") return renderSearch();
  document.title = "Slyar 文件分享 - " + (state.path.split("/").pop() || "根目录");
  renderCrumbs();
  const arr = visibleItems();
  state.rendered = arr;
  const isGrid = state.view === "grid";
  $("#listHead").style.display = isGrid ? "none" : "flex";
  $("#fileList").className = isGrid ? "grid" : "list";
  let html = "";
  arr.forEach((it, idx) => {
    const checked = state.sel.has(it.name) ? " checked" : "";
    const enc = encodeURIComponent(joinPath(it.name));
    if (isGrid) {
      const isImg = !it.dir && IMG.includes(extOf(it.name));
      html += `<div class="card${checked ? " selected" : ""}" data-idx="${idx}">
        <label class="cell-check"><input type="checkbox" class="selbox" data-name="${esc(it.name)}"${checked}></label>
        ${isImg ? `<img class="thumb" loading="lazy" src="/preview?path=${enc}" alt="">` : `<div class="card-icon">${iconOf(it)}</div>`}
        <div class="card-name" title="${esc(it.name)}">${esc(it.name)}</div>
        <div class="card-meta">${it.dir ? "文件夹" : fmtSize(it.size)}</div>
      </div>`;
    } else {
      html += `<div class="row${checked ? " selected" : ""}" data-idx="${idx}">
        <label class="cell-check"><input type="checkbox" class="selbox" data-name="${esc(it.name)}"${checked}></label>
        <span class="cell-icon">${iconOf(it)}</span>
        <span class="cell-name" title="${esc(it.name)}">${esc(it.name)}</span>
        <span class="cell-size">${it.dir ? "—" : fmtSize(it.size)}</span>
        <span class="cell-mtime">${it.dir ? "—" : fmtTime(it.mtime)}</span>
        <span class="cell-ops">${opsHtml(it)}</span>
      </div>`;
    }
  });
  $("#fileList").innerHTML = html;
  $("#empty").innerHTML = `<div class="empty-icon">🗂️</div>此文件夹为空`;
  $("#empty").hidden = arr.length > 0;
  renderStats(arr);
  renderSortArrows();
  updateBatch();
}
function renderSearch() {
  document.title = "搜索: " + state.searchQ + " - Slyar 文件分享";
  renderCrumbs();
  state.rendered = state.results;
  $("#listHead").style.display = "none";
  $("#fileList").className = "list";
  let html = "";
  state.results.forEach((it, idx) => {
    html += `<div class="row" data-idx="${idx}">
      <label class="cell-check" style="visibility:hidden"><input type="checkbox" disabled></label>
      <span class="cell-icon">${iconOf(it)}</span>
      <span class="cell-name">${esc(it.name)}<div class="path-hint" title="/${esc(it.path)}">📁 /${esc(it.path)}</div></span>
      <span class="cell-size">${it.dir ? "—" : fmtSize(it.size)}</span>
      <span class="cell-mtime">${it.dir ? "—" : fmtTime(it.mtime)}</span>
      <span class="cell-ops">${opsHtml(it)}</span>
    </div>`;
  });
  $("#fileList").innerHTML = html;
  $("#empty").innerHTML = `<div class="empty-icon">🔍</div>没有找到 “${esc(state.searchQ)}” 相关内容`;
  $("#empty").hidden = state.results.length > 0;
  $("#stats").textContent = `搜索到 ${state.results.length} 项${state.truncated ? "（结果过多，仅显示前 200 条）" : ""}`;
  $("#selAll").checked = false;
  updateBatch();
}
function renderCrumbs() {
  if (state.mode === "search") {
    $("#crumbs").innerHTML = `🔍 搜索 “${esc(state.searchQ)}” <span class="sep">·</span> <a href="#" class="back-browse">返回浏览</a>`;
    return;
  }
  const parts = state.path ? state.path.split("/") : [];
  let html = `<a href="#/">🏠 根目录</a>`;
  let acc = "";
  for (const p of parts) {
    acc += (acc ? "/" : "") + p;
    const isLast = p === parts[parts.length - 1];
    html += `<span class="sep">›</span>`;
    html += isLast ? `<span class="here">${esc(p)}</span>` : `<a href="#/${encodeURIComponent(acc)}">${esc(p)}</a>`;
  }
  $("#crumbs").innerHTML = html;
}
function renderStats(arr) {
  const dirs = arr.filter(i => i.dir).length;
  const files = arr.length - dirs;
  const total = arr.reduce((s, i) => s + (i.dir ? 0 : i.size), 0);
  $("#stats").textContent = `${arr.length} 个项目：${dirs} 个文件夹 · ${files} 个文件 · 共 ${fmtSize(total)}`;
}
function renderSortArrows() {
  document.querySelectorAll(".arrow").forEach(a => {
    a.textContent = a.dataset.a === state.sortKey ? (state.sortDir > 0 ? "▲" : "▼") : "";
  });
}
function updateBatch() {
  const n = state.sel.size;
  $("#delBtn").disabled = n === 0;
  $("#selHint").textContent = n ? `已选 ${n} 项` : "";
  const allSel = state.items.length > 0 && state.sel.size === state.items.length;
  $("#selAll").checked = allSel;
}

/* ---------- 交互 ---------- */
function itemOfRow(el) {
  const holder = el.closest("[data-idx]");
  if (!holder) return null;
  return (state.rendered || [])[+holder.dataset.idx] || null;
}
$("#fileList").addEventListener("click", e => {
  const opBtn = e.target.closest("[data-op]");
  if (opBtn) {
    const it = itemOfRow(opBtn);
    if (it) runOp(opBtn.dataset.op, it);
    return;
  }
  const box = e.target.closest(".selbox");
  if (box) {
    if (box.checked) state.sel.add(box.dataset.name);
    else state.sel.delete(box.dataset.name);
    const row = box.closest(".row, .card");
    if (row) row.classList.toggle("selected", box.checked);
    updateBatch();
    return;
  }
  const nameEl = e.target.closest(".cell-name, .card-name, .thumb, .card-icon, .path-hint");
  if (nameEl) {
    const it = itemOfRow(nameEl);
    if (!it) return;
    if (it.dir) nav(relOf(it));
    else runOp("preview", it);
  }
});
document.querySelectorAll(".list-head .col[data-sort]").forEach(btn => {
  btn.addEventListener("click", () => {
    const k = btn.dataset.sort;
    if (state.sortKey === k) state.sortDir *= -1;
    else { state.sortKey = k; state.sortDir = 1; }
    render();
  });
});
$("#selAll").addEventListener("change", e => {
  state.sel.clear();
  if (e.target.checked) visibleItems().forEach(i => state.sel.add(i.name));
  render();
});
$("#searchBox").addEventListener("input", e => {
  if (state.mode === "search") { state.mode = "browse"; state.results = []; state.searchQ = ""; }
  state.filter = e.target.value;
  render();
});
$("#searchBox").addEventListener("keydown", e => {
  if (e.key === "Enter") globalSearch(e.target.value);
});
$("#crumbs").addEventListener("click", e => {
  if (e.target.closest(".back-browse")) {
    e.preventDefault();
    exitSearch();
  }
});
async function globalSearch(q) {
  q = (q || "").trim();
  if (!q) { exitSearch(); return; }
  try {
    const d = await api("/api/search?q=" + encodeURIComponent(q));
    state.mode = "search";
    state.searchQ = q;
    state.results = d.results || [];
    state.truncated = !!d.truncated;
    state.sel.clear();
    render();
  } catch (e) { toast(e.message, "error"); }
}
function exitSearch() {
  if (state.mode !== "search") return;
  state.mode = "browse";
  state.results = [];
  state.searchQ = "";
  $("#searchBox").value = "";
  state.filter = "";
  render();
}
$("#viewBtn").addEventListener("click", () => {
  state.view = state.view === "list" ? "grid" : "list";
  localStorage.setItem("slyar_view", state.view);
  render();
});
$("#themeBtn").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  setTheme(cur === "dark" ? "light" : "dark");
});
function setTheme(mode) {
  document.documentElement.dataset.theme = mode;
  localStorage.setItem("slyar_theme", mode);
  $("#themeBtn").textContent = mode === "dark" ? "☀" : "☾";
}
setTheme(localStorage.getItem("slyar_theme") || "light");

async function runOp(op, it) {
  const rel = relOf(it);
  const dir = dirOf(rel);
  if (op === "download") { location.href = "/download?path=" + encodeURIComponent(rel); return; }
  if (op === "preview") { return previewItem(it); }
  if (op === "rename") {
    const newName = await inputModal("重命名", it.name);
    if (!newName || newName === it.name) return;
    try {
      await api("/api/rename", jsonBody({ path: dir, old: it.name, new: newName }));
      toast("重命名成功", "ok"); reloadView();
    } catch (e) { toast(e.message, "error"); }
    return;
  }
  if (op === "delete") {
    const ok = await confirmModal(`确定删除「${it.name}」？${it.dir ? "文件夹内所有内容将一并删除，" : ""}此操作不可恢复！`);
    if (!ok) return;
    try {
      await api("/api/delete", jsonBody({ path: dir, names: [it.name] }));
      toast("已删除", "ok"); reloadView();
    } catch (e) { toast(e.message, "error"); }
  }
}
function reloadView() {
  if (state.mode === "search") globalSearch(state.searchQ);
  else load();
}
const jsonBody = obj => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj) });

/* ---------- 上传 ---------- */
$("#uploadBtn").addEventListener("click", () => $("#fileInput").click());
$("#fileInput").addEventListener("change", e => { uploadFiles(e.target.files); e.target.value = ""; });
function uploadFiles(list) {
  const files = Array.from(list);
  if (!files.length) return;
  let i = 0, okCount = 0, failCount = 0;
  $("#progWrap").hidden = false;
  const next = () => {
    if (i >= files.length) {
      $("#progWrap").hidden = true;
      toast(`上传完成：${okCount} 成功${failCount ? "，" + failCount + " 失败" : ""}`, failCount ? "error" : "ok");
      load();
      return;
    }
    uploadOne(files[i++]).then(ok => { ok ? okCount++ : failCount++; next(); });
  };
  next();
}
function uploadOne(file) {
  return new Promise(resolve => {
    const fd = new FormData();
    fd.append("path", state.path);
    fd.append("file", file, file.name);
    const xhr = new XMLHttpRequest();
    const t0 = Date.now();
    xhr.open("POST", "/api/upload");
    $("#upTip").textContent = `正在上传「${file.name}」…`;
    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      const pct = Math.round(e.loaded / e.total * 100);
      $("#progBar").style.width = pct + "%";
      const sec = (Date.now() - t0) / 1000 || 1;
      $("#progText").textContent = `${pct}% · ${fmtSize(e.loaded)}/${fmtSize(e.total)} · ${fmtSize(e.loaded / sec)}/s`;
    };
    xhr.onload = () => {
      $("#upTip").textContent = "";
      $("#progBar").style.width = "0";
      if (xhr.status === 200) {
        try {
          const d = JSON.parse(xhr.responseText);
          if (d.saved && d.saved.length && d.saved[0] !== file.name) toast(`同名文件已存在，保存为「${d.saved[0]}」`);
        } catch (e) {}
        resolve(true);
      } else { toast(`「${file.name}」上传失败 (HTTP ${xhr.status})`, "error"); resolve(false); }
    };
    xhr.onerror = () => { $("#upTip").textContent = ""; toast(`「${file.name}」上传失败`, "error"); resolve(false); };
    xhr.send(fd);
  });
}
let dragDepth = 0;
window.addEventListener("dragenter", e => {
  if (Array.from(e.dataTransfer.types).includes("Files")) {
    dragDepth++;
    $("#dragOverlay").hidden = false;
  }
});
window.addEventListener("dragleave", () => {
  if (--dragDepth <= 0) { dragDepth = 0; $("#dragOverlay").hidden = true; }
});
window.addEventListener("dragover", e => e.preventDefault());
window.addEventListener("drop", e => {
  e.preventDefault();
  dragDepth = 0;
  $("#dragOverlay").hidden = true;
  if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
});

/* ---------- 新建文件夹 / 删除选中 / ZIP ---------- */
$("#mkdirBtn").addEventListener("click", async () => {
  const name = await inputModal("新建文件夹", "");
  if (!name) return;
  try {
    await api("/api/mkdir", jsonBody({ path: state.path, name }));
    toast("文件夹已创建", "ok"); load();
  } catch (e) { toast(e.message, "error"); }
});
$("#delBtn").addEventListener("click", async () => {
  const names = Array.from(state.sel);
  if (!names.length) return;
  const ok = await confirmModal(`确定删除选中的 ${names.length} 项？文件夹内容将一并删除，此操作不可恢复！`);
  if (!ok) return;
  try {
    const d = await api("/api/delete", jsonBody({ path: state.path, names }));
    toast(`已删除 ${d.deleted} 项`, "ok"); load();
  } catch (e) { toast(e.message, "error"); }
});
$("#zipBtn").addEventListener("click", () => {
  const qs = Array.from(state.sel).map(n => "name=" + encodeURIComponent(n)).join("&");
  location.href = `/api/zip?path=${encodeURIComponent(state.path)}${qs ? "&" + qs : ""}`;
  toast("正在打包下载…");
});

/* ---------- 文本快享 ---------- */
$("#pasteBtn").addEventListener("click", () => {
  applyReadonlyUI();
  refreshPasteList();
  $("#pasteModal").hidden = false;
});
$("#pmClose").addEventListener("click", () => { $("#pasteModal").hidden = true; });
$("#pasteCreate").addEventListener("click", async () => {
  const text = $("#pasteText").value;
  if (!text.trim()) { toast("内容不能为空", "error"); return; }
  try {
    const d = await api("/api/paste-create", jsonBody({ text }));
    $("#pasteText").value = "";
    toast("快享已创建", "ok");
    refreshPasteList();
    const link = location.origin + "/paste/" + d.id;
    try { await navigator.clipboard.writeText(link); toast("链接已复制：" + link); }
    catch (e) { toast("链接：/paste/" + d.id); }
  } catch (e) { toast(e.message, "error"); }
});
async function refreshPasteList() {
  try {
    const d = await api("/api/paste");
    const el = $("#pasteList");
    if (!d.items.length) { el.innerHTML = `<div class="paste-empty">暂无快享记录</div>`; return; }
    el.innerHTML = d.items.map(p => `
      <div class="paste-item">
        <span class="pt" title="${esc(p.title)}">${esc(p.title)}</span>
        <span class="pm">${fmtTime(p.time)} · ${fmtSize(p.size)}</span>
        <button class="op" data-pact="open" data-id="${esc(p.id)}" title="打开页面">🌐</button>
        <button class="op" data-pact="copy" data-id="${esc(p.id)}" title="复制内容">📋</button>
        ${d.readonly ? "" : `<button class="op del" data-pact="del" data-id="${esc(p.id)}" title="删除">🗑</button>`}
      </div>`).join("");
  } catch (e) { toast(e.message, "error"); }
}
$("#pasteList").addEventListener("click", async e => {
  const btn = e.target.closest("[data-pact]");
  if (!btn) return;
  const id = btn.dataset.id;
  if (btn.dataset.pact === "open") {
    window.open("/paste/" + id, "_blank");
  } else if (btn.dataset.pact === "copy") {
    try {
      const d = await api("/api/paste?id=" + encodeURIComponent(id));
      await navigator.clipboard.writeText(d.text);
      toast("内容已复制", "ok");
    } catch (err) { toast("复制失败", "error"); }
  } else {
    try { await api("/api/paste-delete", jsonBody({ id })); refreshPasteList(); toast("已删除", "ok"); }
    catch (err) { toast(err.message, "error"); }
  }
});

/* ---------- 模态框 ---------- */
let inResolve = null, cfResolve = null;
function inputModal(title, value) {
  return new Promise(res => {
    inResolve = res;
    $("#inTitle").textContent = title;
    $("#inInput").value = value || "";
    $("#inModal").hidden = false;
    setTimeout(() => { $("#inInput").focus(); $("#inInput").select(); }, 30);
  });
}
function confirmModal(text) {
  return new Promise(res => {
    cfResolve = res;
    $("#cfText").textContent = text;
    $("#cfModal").hidden = false;
  });
}
function closeInput(val) { $("#inModal").hidden = true; if (inResolve) { inResolve(val); inResolve = null; } }
function closeConfirm(val) { $("#cfModal").hidden = true; if (cfResolve) { cfResolve(val); cfResolve = null; } }
$("#inOk").addEventListener("click", () => closeInput($("#inInput").value.trim()));
$("#inCancel").addEventListener("click", () => closeInput(null));
$("#inClose").addEventListener("click", () => closeInput(null));
$("#inInput").addEventListener("keydown", e => { if (e.key === "Enter") closeInput($("#inInput").value.trim()); });
$("#cfOk").addEventListener("click", () => closeConfirm(true));
$("#cfCancel").addEventListener("click", () => closeConfirm(false));
$("#cfClose").addEventListener("click", () => closeConfirm(false));
document.querySelectorAll(".modal").forEach(m => {
  m.addEventListener("click", e => {
    if (e.target === m) {
      if (m.id === "inModal") closeInput(null);
      else if (m.id === "cfModal") closeConfirm(false);
      else m.hidden = true;
    }
  });
});
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (!$("#pvModal").hidden) { $("#pvModal").hidden = true; $("#pvBody").innerHTML = ""; }
  else if (!$("#pasteModal").hidden) $("#pasteModal").hidden = true;
  else if (!$("#inModal").hidden) closeInput(null);
  else if (!$("#cfModal").hidden) closeConfirm(false);
  else if (state.sel.size) { state.sel.clear(); render(); }
});
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a" &&
      !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
    e.preventDefault();
    state.sel.clear();
    visibleItems().forEach(i => state.sel.add(i.name));
    render();
  }
});

/* ---------- 预览 ---------- */
function previewItem(it) {
  const e = extOf(it.name);
  const url = "/preview?path=" + encodeURIComponent(relOf(it));
  $("#pvTitle").textContent = it.name;
  const body = $("#pvBody");
  if (IMG.includes(e)) {
    body.innerHTML = `<img src="${url}" alt="预览">`;
  } else if (VID.includes(e)) {
    body.innerHTML = `<video controls autoplay src="${url}"></video>`;
  } else if (AUD.includes(e)) {
    body.innerHTML = `<audio controls autoplay src="${url}" style="width:100%"></audio>`;
  } else if (PDF.includes(e)) {
    body.innerHTML = `<iframe src="${url}"></iframe>`;
  } else if (TXT.includes(e)) {
    if (it.size > 2 * 1024 * 1024) {
      body.innerHTML = `<div class="pv-fallback">文件过大，不支持在线预览<br><br><a class="btn" href="/download?path=${encodeURIComponent(relOf(it))}">⬇ 下载查看</a></div>`;
    } else {
      body.innerHTML = `<pre>加载中…</pre>`;
      fetch(url)
        .then(r => r.text())
        .then(t => {
          if (t.length > 512 * 1024) t = t.slice(0, 512 * 1024) + "\\n\\n…（内容过长，已截断）";
          body.innerHTML = `<pre>${esc(t)}</pre>`;
        })
        .catch(() => { body.innerHTML = `<div class="pv-fallback">加载失败</div>`; });
    }
  } else {
    body.innerHTML = `<div class="pv-fallback">该类型暂不支持预览<br><br><a class="btn" href="/download?path=${encodeURIComponent(relOf(it))}">⬇ 下载文件</a></div>`;
  }
  $("#pvModal").hidden = false;
}
$("#pvClose").addEventListener("click", () => { $("#pvModal").hidden = true; $("#pvBody").innerHTML = ""; });

load();
</script>
</body>
</html>
"""
