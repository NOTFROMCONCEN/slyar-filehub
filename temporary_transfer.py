"""为一次性的局域网双向文件传输提供受限 Web 入口。"""

import json
import logging
import os
import secrets
import time
import urllib.parse

from web_server import LOGIN_PAGE, WebFileHandler, safe_join

LOGGER = logging.getLogger("TemporaryTransfer")


class TemporaryTransferHandler(WebFileHandler):
    """只提供发送目录的浏览/下载与接收目录的上传。"""

    receive_directory = ""
    expires_at = 0

    def _expired(self):
        return time.time() >= type(self).expires_at

    def _send_expired(self):
        self.close_connection = True
        self.send_error_page(410, "本次临时传输已结束")

    def do_GET(self):
        try:
            if self._expired():
                self._send_expired()
                return

            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/login":
                self.serve_login(qs.get("e") and "访问码错误，请重试" or "")
                return
            if not self._authorized():
                if path.startswith("/api/"):
                    self.send_error_page(401, "需要访问码")
                else:
                    self.send_redirect("/login")
                return

            if path in ("/", "/index.html"):
                self.serve_transfer_page()
            elif path == "/api/list":
                self.api_transfer_list(qs)
            elif path == "/download":
                self.serve_file(qs, attachment=True)
            elif path == "/logout":
                self.send_response(303)
                self.send_header("Location", "/login")
                self.send_header(
                    "Set-Cookie",
                    "slyar_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_error_page(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:
            LOGGER.exception("处理临时传输 GET 请求失败")
            self.send_error_page(500, "服务器内部错误")

    def do_POST(self):
        try:
            if self._expired():
                self._send_expired()
                return

            path = urllib.parse.urlparse(self.path).path
            if path == "/login":
                self.handle_transfer_login()
                return
            if not self._authorized():
                self.send_error_page(401, "需要访问码")
                return
            if path != "/api/upload":
                self.send_error_page(404, "Not Found")
                return

            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                self.send_error_page(400, "Content-Length 无效")
                return
            if length <= 0 or self.headers.get("Transfer-Encoding"):
                self.send_error_page(411, "上传需要 Content-Length")
                self.close_connection = True
                return
            self.api_upload(length)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except TimeoutError:
            self.close_connection = True
            self.send_error_page(408, "连接空闲超时")
        except Exception:
            LOGGER.exception("处理临时传输 POST 请求失败")
            self.send_error_page(500, "服务器内部错误")

    def handle_transfer_login(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self.send_redirect("/login?e=1")
            return
        if length <= 0 or length > 4096:
            self.send_redirect("/login?e=1")
            return
        fields = urllib.parse.parse_qs(
            self.rfile.read(length).decode("utf-8", "ignore")
        )
        code = (fields.get("password") or [""])[0]
        expected = type(self).auth_password or ""
        if code and secrets.compare_digest(code, expected):
            max_age = max(1, int(type(self).expires_at - time.time()))
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"slyar_session={type(self).session_token}; Path=/; HttpOnly; "
                f"SameSite=Strict; Max-Age={max_age}",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            LOGGER.info("临时传输登录成功: %s", self.address_string())
        else:
            LOGGER.warning("临时传输访问码错误: %s", self.address_string())
            self.serve_login("访问码错误，请重试")

    def serve_login(self, error=""):
        page = (
            LOGIN_PAGE.replace("Slyar 文件分享", "Slyar 临时传输")
            .replace("此服务器已开启访问密码", "请输入发送方提供的 8 位访问码")
            .replace("请输入访问密码", "请输入访问码")
            .replace("进 入", "进入临时传输")
            .replace("__ERROR__", error)
        )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def resolve_upload_directory(self, relative_path):
        """临时接收只写入用户指定的接收目录，忽略客户端路径。"""
        target = os.path.realpath(type(self).receive_directory)
        if not os.path.isdir(target):
            return None
        return target

    def api_transfer_list(self, qs):
        rel = (qs.get("path") or [""])[0]
        target = safe_join(self.directory, rel)
        if target is None or not os.path.isdir(target):
            self.send_error_page(404, "目录不存在")
            return
        items = []
        for name in os.listdir(target):
            if name.startswith("."):
                continue
            full = os.path.join(target, name)
            if os.path.islink(full):
                continue
            try:
                st = os.stat(full, follow_symlinks=False)
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
        items.sort(key=lambda item: (not item["dir"], item["name"].casefold()))
        body = json.dumps(
            {"ok": True, "path": rel.replace("\\", "/"), "items": items},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_transfer_page(self):
        page = TRANSFER_PAGE.replace(
            "__DEADLINE_MS__", str(int(type(self).expires_at * 1000))
        )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def make_temporary_transfer_handler(send_directory, receive_directory, access_code, expires_at):
    """构造只供本次会话使用的请求处理器。"""
    send_directory = os.path.abspath(send_directory)
    receive_directory = os.path.abspath(receive_directory)
    if not os.path.isdir(send_directory) or not os.path.isdir(receive_directory):
        raise ValueError("发送目录和接收目录都必须存在")
    if not access_code:
        raise ValueError("临时传输访问码不能为空")

    class BoundTemporaryTransferHandler(TemporaryTransferHandler):
        pass

    BoundTemporaryTransferHandler.directory = send_directory
    BoundTemporaryTransferHandler.receive_directory = receive_directory
    BoundTemporaryTransferHandler.auth_password = access_code
    BoundTemporaryTransferHandler.readonly = False
    BoundTemporaryTransferHandler.expires_at = float(expires_at)
    BoundTemporaryTransferHandler.session_token = secrets.token_urlsafe(24)
    return BoundTemporaryTransferHandler


TRANSFER_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>临时文件交换</title>
<style>
:root{color-scheme:light;--bg:#f2f6fb;--card:#fff;--ink:#1d3046;--muted:#667b91;--line:#dce6ef;--blue:#2869c7;--blue-soft:#e9f2ff;--teal:#16826f;--shadow:0 12px 34px rgba(29,64,103,.08)}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 12% 0,#e1efff 0,transparent 36%),var(--bg);color:var(--ink);font:15px/1.55 "Segoe UI","Microsoft YaHei",sans-serif;min-height:100vh}
.shell{max-width:1100px;margin:auto;padding:30px 22px 48px}.top{display:flex;align-items:center;gap:12px;margin-bottom:24px}.mark{width:42px;height:42px;border-radius:14px;background:#173d69;color:#fff;display:grid;place-items:center;font-weight:800;letter-spacing:.04em;box-shadow:0 7px 16px #173d6926}.brand strong{display:block;font-size:16px}.brand small{color:var(--muted);font-size:11px;letter-spacing:.12em}.logout{margin-left:auto;text-decoration:none;color:var(--muted);border:1px solid var(--line);padding:8px 13px;border-radius:10px;background:#ffffffa8}
.hero{border-radius:22px;padding:30px 34px;background:linear-gradient(120deg,#153b64,#2468a6 72%,#3485ad);color:white;box-shadow:var(--shadow);position:relative;overflow:hidden}.hero:after{content:"";position:absolute;width:260px;height:260px;border:1px solid #ffffff24;border-radius:50%;right:-56px;top:-130px;box-shadow:0 0 0 26px #ffffff0b,0 0 0 54px #ffffff08}.eyebrow{font-size:11px;letter-spacing:.18em;opacity:.76}.hero h1{margin:8px 0;font-size:29px;line-height:1.2}.hero p{margin:0;color:#e1effb}.timer{margin-top:19px;display:inline-flex;align-items:center;gap:8px;background:#ffffff1c;border:1px solid #ffffff33;border-radius:999px;padding:7px 12px;font-size:13px}
.grid{display:grid;grid-template-columns:1.1fr .9fr;gap:18px;margin-top:20px}.panel{background:var(--card);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);overflow:hidden;min-width:0}.panel-head{display:flex;align-items:center;gap:11px;padding:18px 20px;border-bottom:1px solid var(--line)}.step{width:30px;height:30px;border-radius:10px;background:var(--blue-soft);color:var(--blue);display:grid;place-items:center;font-weight:800}.panel-head h2{font-size:16px;margin:0}.panel-head p{font-size:12px;color:var(--muted);margin:2px 0 0}.content{padding:16px 20px 20px}.crumbs{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:0 0 12px}.crumbs button{border:0;background:transparent;color:var(--blue);font:inherit;cursor:pointer;padding:2px 4px}.crumbs span{color:#9aabba}.file-list{border:1px solid var(--line);border-radius:12px;overflow:hidden;min-height:230px}.file{display:flex;align-items:center;gap:10px;padding:11px 12px;border-bottom:1px solid #edf1f5}.file:last-child{border:0}.file:hover{background:#f8fbff}.file-icon{width:30px;height:30px;border-radius:9px;background:#f1f5f9;display:grid;place-items:center}.file-name{min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.file-meta{color:var(--muted);font-size:12px;margin-left:8px;white-space:nowrap}.file-action,.primary{border:0;border-radius:9px;padding:8px 12px;cursor:pointer;font:inherit;font-size:13px}.file-action{background:var(--blue-soft);color:var(--blue)}.file-action:hover{background:#dceaff}.empty{padding:50px 15px;text-align:center;color:var(--muted)}
.drop{border:1.5px dashed #b9cce0;border-radius:15px;background:#f8fbff;min-height:205px;padding:25px 18px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;cursor:pointer;transition:.15s}.drop:hover,.drop.drag{border-color:var(--blue);background:#f0f6ff}.upload-icon{width:48px;height:48px;border-radius:16px;background:#e7f0fd;color:var(--blue);display:grid;place-items:center;font-size:23px;margin-bottom:10px}.drop strong{font-size:15px}.drop span{font-size:12px;color:var(--muted);margin-top:4px}.primary{background:var(--blue);color:#fff;margin-top:14px;padding:10px 17px;font-weight:650}.primary:hover{background:#205caf}.progress-wrap{margin-top:14px;display:none}.progress-label{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-bottom:6px}.track{height:7px;border-radius:9px;background:#e8eef5;overflow:hidden}.bar{height:100%;width:0;background:linear-gradient(90deg,#2874d1,#18a187);transition:width .15s}.notice{margin-top:15px;padding:11px 13px;background:#f5f8fc;border-radius:10px;color:var(--muted);font-size:12px}.toast{position:fixed;left:50%;bottom:25px;transform:translateX(-50%);background:#193d62;color:#fff;border-radius:11px;padding:10px 16px;box-shadow:var(--shadow);z-index:5}.toast.err{background:#a9404d}
@media(max-width:760px){.shell{padding:19px 13px 30px}.hero{padding:25px 22px}.hero h1{font-size:24px}.grid{grid-template-columns:1fr}.file-list{min-height:150px}.file-meta{display:none}}
</style>
</head>
<body>
<div class="shell">
  <header class="top"><div class="mark">SF</div><div class="brand"><strong>Slyar · 临时传输</strong><small>LOCAL NETWORK FILE EXCHANGE</small></div><a class="logout" href="/logout">退出本次传输</a></header>
  <section class="hero"><div class="eyebrow">PRIVATE SESSION · LAN</div><h1>文件交换，轻松来回</h1><p>下载发送目录中的文件，也可以把文件传回这台电脑。</p><div class="timer">◷ <span>本次服务剩余</span><strong id="countdown">--:--:--</strong></div></section>
  <main class="grid">
    <section class="panel"><div class="panel-head"><span class="step">01</span><div><h2>从这台电脑下载</h2><p>只能浏览和下载发送目录中的文件</p></div></div><div class="content"><nav class="crumbs" id="crumbs"></nav><div class="file-list" id="files"><div class="empty">正在读取文件…</div></div></div></section>
    <section class="panel"><div class="panel-head"><span class="step">02</span><div><h2>上传到这台电脑</h2><p>上传内容会保存到本次指定的接收目录</p></div></div><div class="content"><div class="drop" id="drop"><div class="upload-icon">↑</div><strong>拖放文件到这里</strong><span>或点击选择一个或多个文件</span><input id="picker" type="file" multiple hidden></div><button class="primary" id="choose">选择文件</button><div class="progress-wrap" id="progress"><div class="progress-label"><span id="progressName">准备上传</span><strong id="progressPct">0%</strong></div><div class="track"><div class="bar" id="bar"></div></div></div><div class="notice">接收目录不对浏览器开放。已有同名文件会自动另存为新文件。</div></div></section>
  </main>
</div>
<script>
const deadline=__DEADLINE_MS__, $=id=>document.getElementById(id);let current="", timer=null;
function size(n){if(!n)return "0 B";const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<u.length-1){n/=1024;i++}return `${n.toFixed(i?1:0)} ${u[i]}`}
function toast(message,error=false){const el=document.createElement("div");el.className="toast"+(error?" err":"");el.textContent=message;document.body.appendChild(el);setTimeout(()=>el.remove(),3000)}
function tick(){let sec=Math.max(0,Math.floor((deadline-Date.now())/1000));const h=String(Math.floor(sec/3600)).padStart(2,"0");const m=String(Math.floor(sec%3600/60)).padStart(2,"0");const s=String(sec%60).padStart(2,"0");$("countdown").textContent=`${h}:${m}:${s}`;if(sec===0){clearInterval(timer);document.body.innerHTML="<main class='shell'><section class='hero'><h1>本次传输已结束</h1><p>请联系文件发送方重新开启临时传输。</p></section></main>"}}
async function load(path=current){current=path;try{const r=await fetch("/api/list?path="+encodeURIComponent(path),{cache:"no-store"});if(!r.ok)throw Error("无法读取发送目录");const data=await r.json();render(data.items||[]);crumbs(path)}catch(e){$("files").innerHTML="<div class='empty'>"+e.message+"</div>"}}
function crumbs(path){const box=$("crumbs");box.replaceChildren();const root=document.createElement("button");root.textContent="发送目录";root.onclick=()=>load("");box.append(root);let part="";for(const name of path.split("/").filter(Boolean)){const sep=document.createElement("span");sep.textContent="/";box.append(sep);part=part?part+"/"+name:name;const btn=document.createElement("button");btn.textContent=name;const dest=part;btn.onclick=()=>load(dest);box.append(btn)}}
function render(items){const box=$("files");box.replaceChildren();if(!items.length){const empty=document.createElement("div");empty.className="empty";empty.textContent="这个目录暂时没有文件";box.append(empty);return}for(const item of items){const row=document.createElement("div");row.className="file";const icon=document.createElement("span");icon.className="file-icon";icon.textContent=item.dir?"📁":"📄";const name=document.createElement("span");name.className="file-name";name.textContent=item.name;name.title=item.name;const meta=document.createElement("span");meta.className="file-meta";meta.textContent=item.dir?"文件夹":size(item.size);row.append(icon,name,meta);const action=document.createElement("button");action.className="file-action";action.textContent=item.dir?"打开":"下载";action.onclick=()=>{const rel=current?current+"/"+item.name:item.name;if(item.dir)load(rel);else location.href="/download?path="+encodeURIComponent(rel)};row.append(action);box.append(row)}}
function uploadFiles(list){const files=Array.from(list);if(!files.length)return;let i=0,failed=0;$("progress").style.display="block";function next(){if(i>=files.length){$("progressName").textContent="上传完成";$("progressPct").textContent="100%";$("bar").style.width="100%";toast(failed?`完成，${failed} 个文件失败`:"文件已传到接收目录",!!failed);setTimeout(()=>{$("progress").style.display="none"},1400);return}const file=files[i++],fd=new FormData();fd.append("file",file,file.name);const xhr=new XMLHttpRequest();xhr.open("POST","/api/upload");$("progressName").textContent=`正在上传 ${file.name}`;xhr.upload.onprogress=e=>{if(e.lengthComputable){const pct=Math.round(e.loaded/e.total*100);$("progressPct").textContent=pct+"%";$("bar").style.width=pct+"%"}};xhr.onload=()=>{if(xhr.status!==200)failed++;next()};xhr.onerror=()=>{failed++;next()};xhr.send(fd)}next()}
$("choose").onclick=()=>$("picker").click();$("picker").onclick=e=>e.stopPropagation();$("drop").onclick=e=>{if(e.target!==$("picker"))$("picker").click()};$("picker").onchange=e=>uploadFiles(e.target.files);$("drop").ondragover=e=>{e.preventDefault();$("drop").classList.add("drag")};$("drop").ondragleave=()=>$("drop").classList.remove("drag");$("drop").ondrop=e=>{e.preventDefault();$("drop").classList.remove("drag");uploadFiles(e.dataTransfer.files)};tick();timer=setInterval(tick,1000);load();
</script>
</body>
</html>
"""

