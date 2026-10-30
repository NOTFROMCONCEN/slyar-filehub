import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
import threading
import socket
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer
import os
import sys
import ctypes
import psutil
import subprocess
import webbrowser
import urllib.parse
import argparse
import secrets
import time
from config_manager import ConfigManager, ftp_permissions_from_config
from tray_manager import TrayManager, TRAY_AVAILABLE
from web_server import ThreadedWebServer, make_web_handler
from temporary_transfer import make_temporary_transfer_handler
from webdav_service import WebDAVService

# 尝试导入SFTP相关库
try:
    import paramiko
    from sftp_service import SFTPService

    SFTP_AVAILABLE = True
except ImportError:
    SFTP_AVAILABLE = False

import logging
import datetime
import queue
from threading import Lock


class LogHandler(logging.Handler):
    """自定义日志处理器，将日志发送到队列"""

    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        """发送日志记录到队列"""
        try:
            log_entry = self.format(record)
            self.log_queue.put(log_entry)
        except Exception:
            self.handleError(record)




class ModernStyle:
    """蓝白工业控制台配色。"""

    BG_PRIMARY = "#f3f6fa"
    BG_SECONDARY = "#eaf0f6"
    BG_CARD = "#ffffff"
    BG_INPUT = "#f7f9fc"
    BG_BUTTON = "#3569c8"
    BG_BUTTON_HOVER = "#2857ad"
    BG_SUCCESS = "#16836f"
    BG_SUCCESS_HOVER = "#116c5b"
    BG_DANGER = "#fff1f1"
    BG_DANGER_HOVER = "#ffe4e5"
    BG_INFO = "#eaf2ff"
    BG_INFO_HOVER = "#dce9ff"

    TEXT_PRIMARY = "#1d3046"
    TEXT_SECONDARY = "#5d7187"
    TEXT_MUTED = "#8797aa"
    TEXT_ON_DARK = "#ffffff"
    TEXT_DANGER = "#ad4b54"
    STATUS_ON = "#16836f"
    BORDER = "#dce5ee"
    BORDER_FOCUS = "#4b79d2"

    # 字体配置
    FONT_FAMILY = "Microsoft YaHei UI"
    FONT_FAMILY_ALT = "Microsoft YaHei"
    FONT_SIZE_LARGE = 15
    FONT_SIZE_NORMAL = 11
    FONT_SIZE_SMALL = 10

    # 间距配置
    PADDING_LARGE = 16
    PADDING_NORMAL = 12
    PADDING_SMALL = 6
    PADDING_TINY = 4


class FTPServerGUI:
    def __init__(self, root, start_hidden=False):
        self.root = root
        self.root.title("Slyar FTP Server · 文件传输控制台")
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        window_w = max(640, min(1240, screen_w - 96))
        window_h = max(560, min(900, screen_h - 60))
        self.root.geometry(
            f"{window_w}x{window_h}+{max(0, (screen_w - window_w) // 2)}"
            f"+{max(0, (screen_h - window_h) // 2)}"
        )
        self.root.resizable(True, True)
        self.root.minsize(min(1000, window_w), min(680, window_h))

        # 各服务运行时实际使用的端口（用于状态栏显示与连接数统计）
        self._service_ports = {}

        # 应用现代化样式
        self.style = ModernStyle()
        self.optimize_fonts()
        self.setup_theme()

        # 应用图标
        self._icon_path = self._find_icon_path()
        self._apply_icon(self.root)

        # 服务器相关变量
        self.ftp_server = None
        self.sftp_server = None
        self.web_server = None
        self.webdav_server = None
        self.temp_transfer_server = None
        self.ftp_thread = None
        self.sftp_thread = None
        self.web_thread = None
        self.webdav_thread = None

        # 服务状态
        self.ftp_running = False
        self.sftp_running = False
        self.web_running = False
        self.webdav_running = False
        self.temp_transfer_running = False
        self.temp_transfer_deadline = None
        self.temp_transfer_url = ""
        self.temp_transfer_code = ""
        self._temp_transfer_after_id = None

        # 服务选择变量
        self.enable_ftp = tk.BooleanVar(value=True)
        self.enable_sftp = tk.BooleanVar(value=False)
        self.enable_web = tk.BooleanVar(value=False)
        self.enable_webdav = tk.BooleanVar(value=False)

        # 配置管理器
        self.config_manager = ConfigManager()

        # 日志系统
        self.log_queue = queue.Queue()
        self.log_lock = Lock()
        self.setup_logging()

        # 创建界面
        self.create_widgets()

        # 加载保存的配置
        self.load_config()

        # 更新本机IP
        self.update_local_ip()

        # 首次运行显示使用说明（后台模式启动时不弹窗）
        if not os.path.exists("ftp_config.json") and not start_hidden:
            self.root.after(500, self.show_help)

        # 启动日志更新定时器
        self.update_log_display()

        # 启动运行状态刷新定时器
        self.refresh_status()

        # 关闭窗口时隐藏到系统托盘，程序保持后台常驻
        self._tray_hint_shown = False
        # 创建系统托盘图标（显示窗口 / 开机自启动 / 退出）
        self.tray = TrayManager(self)
        self.tray_available = self.tray.start()
        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.hide_to_tray if self.tray_available else self.quit_program,
        )

        if start_hidden:
            if self.tray_available:
                self.root.withdraw()
                self.log_info("已以后台模式启动，程序驻留系统托盘")
                self.root.after(
                    1500,
                    lambda: self.tray.notify("程序已在后台运行\n双击托盘图标可打开主窗口"),
                )
            else:
                self.root.deiconify()
                self.log_warning("托盘不可用，已显示主窗口以便管理服务和退出")

    def setup_logging(self):
        """设置日志系统"""
        # 创建日志目录
        log_dir = "logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        # 配置日志文件
        log_file = os.path.join(
            log_dir, f"ftp_server_{datetime.datetime.now().strftime('%Y%m%d')}.log"
        )

        # 配置logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.FileHandler(log_file, encoding="utf-8"),
                logging.StreamHandler(),
            ],
        )

        # 创建自定义日志处理器
        self.logger = logging.getLogger("FTPServer")
        self.log_handler = LogHandler(self.log_queue)
        self.logger.addHandler(self.log_handler)

        # 记录启动日志
        self.log_info("Slyar FTP Server 启动")
        self.log_info(f"日志文件: {log_file}")

    def log_info(self, message):
        """记录信息日志"""
        self.logger.info(message)

    def log_warning(self, message):
        """记录警告日志"""
        self.logger.warning(message)

    def log_error(self, message):
        """记录错误日志"""
        self.logger.error(message)

    def update_log_display(self):
        """消费日志队列，在底部状态栏显示最新一条日志"""
        try:
            latest = None
            while not self.log_queue.empty():
                latest = self.log_queue.get_nowait()
            if latest and hasattr(self, "statusbar_text"):
                self.statusbar_text.config(text=latest)
        except Exception:
            pass

        # 每秒更新一次
        self.root.after(1000, self.update_log_display)

    def optimize_fonts(self):
        """优化字体配置，选择最清晰的字体"""
        import tkinter.font as tkFont
        import platform

        # 获取系统可用字体
        available_fonts = tkFont.families()

        # 根据系统选择最佳字体
        system = platform.system()
        if system == "Windows":
            preferred_fonts = [
                "微软雅黑",
                "Microsoft YaHei UI",
                "Microsoft YaHei",
                "Segoe UI",
                "Arial",
                "Helvetica",
                "Tahoma",
            ]
        elif system == "Darwin":  # macOS
            preferred_fonts = [
                "PingFang SC",
                "Hiragino Sans GB",
                "STHeiti",
                "Arial",
                "Helvetica",
            ]
        else:  # Linux
            preferred_fonts = [
                "Noto Sans CJK SC",
                "WenQuanYi Micro Hei",
                "DejaVu Sans",
                "Arial",
                "Helvetica",
            ]

        # 选择最佳字体
        best_font = self.style.FONT_FAMILY_ALT  # 默认备用字体
        for font in preferred_fonts:
            if font in available_fonts:
                best_font = font
                break

        # 更新样式配置
        self.style.FONT_FAMILY = best_font
        print(f"使用字体: {best_font}")

        # 针对高DPI显示器优化
        try:
            # 获取系统DPI缩放比例
            import ctypes

            try:
                # Windows系统获取DPI
                user32 = ctypes.windll.user32
                user32.SetProcessDPIAware()
                dpi = user32.GetDpiForSystem()
                scale_factor = dpi / 96.0
                self.root.tk.call("tk", "scaling", scale_factor)
            except:
                # 备用方案
                self.root.tk.call("tk", "scaling", 1.25)
        except:
            try:
                # 通用缩放设置
                self.root.tk.call("tk", "scaling", 1.2)
            except:
                pass

        # 配置字体抗锯齿
        try:
            # 尝试启用字体平滑
            self.root.tk.call(
                "tk", "font", "configure", self.style.FONT_FAMILY, "-antialias", "true"
            )
        except:
            pass

    def setup_theme(self):
        """设置现代化主题"""
        self.root.configure(bg=self.style.BG_PRIMARY)

        # 配置ttk样式
        style = ttk.Style()
        style.theme_use("clam")

        # 配置各种组件样式
        style.configure(
            "Card.TFrame", background=self.style.BG_CARD, relief="flat", borderwidth=0
        )
        style.configure(
            "Modern.TLabel",
            background=self.style.BG_CARD,
            foreground=self.style.TEXT_PRIMARY,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL, "normal"),
        )
        style.configure(
            "Title.TLabel",
            background=self.style.BG_CARD,
            foreground=self.style.TEXT_PRIMARY,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_LARGE, "bold"),
        )
        style.configure(
            "Modern.TEntry",
            fieldbackground=self.style.BG_INPUT,
            borderwidth=1,
            relief="solid",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL, "normal"),
        )
        style.configure(
            "Modern.TCheckbutton",
            background=self.style.BG_CARD,
            foreground=self.style.TEXT_PRIMARY,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL, "normal"),
        )
        style.configure(
            "Modern.TButton",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL, "normal"),
        )

        # 配置按钮样式
        style.configure(
            "Success.TButton",
            background=self.style.BG_SUCCESS,
            foreground="white",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL, "bold"),
        )
        style.map(
            "Success.TButton", background=[("active", self.style.BG_SUCCESS_HOVER)]
        )

        style.configure(
            "Danger.TButton",
            background=self.style.BG_DANGER,
            foreground="white",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL, "bold"),
        )
        style.map("Danger.TButton", background=[("active", self.style.BG_DANGER_HOVER)])

        style.configure(
            "Info.TButton",
            background=self.style.BG_INFO,
            foreground="white",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL, "bold"),
        )
        style.map("Info.TButton", background=[("active", self.style.BG_INFO_HOVER)])

    def _find_icon_path(self):
        """定位 app_icon.ico：兼容直接运行与PyInstaller打包运行"""
        candidates = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, "app_icon.ico"))
        candidates.append(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.ico")
        )
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def _apply_icon(self, window):
        """给窗口设置应用图标（图标文件缺失时静默跳过）"""
        if getattr(self, "_icon_path", None):
            try:
                window.iconbitmap(self._icon_path)
            except Exception:
                pass

    def create_widgets(self):
        """主窗口使用固定顶部导航，内容页在中间区域切换。"""
        # 底部状态栏（显示最新日志与运行状态）
        status_bar = tk.Frame(self.root, bg=self.style.BG_SECONDARY, height=34)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        status_bar.pack_propagate(False)

        self.statusbar_dot = tk.Label(
            status_bar,
            text="●",
            font=(self.style.FONT_FAMILY, 10),
            bg=self.style.BG_SECONDARY,
            fg=self.style.TEXT_MUTED,
        )
        self.statusbar_dot.pack(
            side=tk.LEFT, padx=(self.style.PADDING_NORMAL, 4), pady=4
        )

        self.statusbar_text = tk.Label(
            status_bar,
            text="就绪",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_SECONDARY,
            fg=self.style.TEXT_SECONDARY,
            anchor=tk.W,
        )
        self.statusbar_text.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=4)

        # 顶部页面菜单固定显示，滚动内容时不会离开视野
        self.main_nav_bar = tk.Frame(
            self.root,
            bg=self.style.BG_CARD,
            highlightthickness=1,
            highlightbackground=self.style.BORDER,
        )
        self.main_nav_bar.pack(
            side=tk.TOP,
            fill=tk.X,
            padx=self.style.PADDING_LARGE,
            pady=(self.style.PADDING_NORMAL, 0),
        )
        self.main_nav_items = {}

        # 标题栏按字体实际高度伸展，避免高 DPI 下副标题被裁切
        header = tk.Frame(self.root, bg=self.style.BG_PRIMARY)
        header.pack(side=tk.TOP, fill=tk.X, padx=self.style.PADDING_LARGE, pady=(8, 4))
        tk.Frame(header, bg=self.style.BG_BUTTON, width=5, height=52).pack(
            side=tk.LEFT,
            fill=tk.Y,
            padx=(0, self.style.PADDING_NORMAL),
            pady=4,
        )
        title_stack = tk.Frame(header, bg=self.style.BG_PRIMARY)
        title_stack.pack(side=tk.LEFT, anchor="center")
        tk.Label(
            title_stack,
            text="FILE TRANSFER  /  CONTROL PANEL",
            font=("Consolas", 9, "bold"),
            bg=self.style.BG_PRIMARY,
            fg=self.style.BG_BUTTON,
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            title_stack,
            text="Slyar FTP Server",
            font=(self.style.FONT_FAMILY, 18, "bold"),
            bg=self.style.BG_PRIMARY,
            fg=self.style.TEXT_PRIMARY,
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            title_stack,
            text="临时文件交换 · FTP / SFTP / Web",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_PRIMARY,
            fg=self.style.TEXT_SECONDARY,
            anchor="w",
        ).pack(anchor="w")

        self.header_badge = tk.Label(
            header,
            text="● 待机",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL, "bold"),
            bg=self.style.BG_PRIMARY,
            fg=self.style.TEXT_MUTED,
        )
        self.header_badge.pack(side=tk.RIGHT, padx=8)

        # 主区域（滚动画布 + 当前页面）
        main_area = tk.Frame(self.root, bg=self.style.BG_PRIMARY)
        main_area.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        main_canvas = tk.Canvas(
            main_area, bg=self.style.BG_PRIMARY, highlightthickness=0
        )
        scrollbar = ttk.Scrollbar(
            main_area, orient="vertical", command=main_canvas.yview
        )
        self.page_host = tk.Frame(main_canvas, bg=self.style.BG_PRIMARY)
        self.page_host.grid_columnconfigure(0, weight=1)

        self._canvas_window = main_canvas.create_window(
            (0, 0), window=self.page_host, anchor="nw"
        )

        main_canvas.bind(
            "<Configure>",
            lambda e: main_canvas.itemconfigure(self._canvas_window, width=e.width),
        )
        self.page_host.bind(
            "<Configure>",
            lambda e: main_canvas.configure(scrollregion=main_canvas.bbox("all")),
        )
        self.main_canvas = main_canvas
        main_canvas.configure(yscrollcommand=scrollbar.set)

        main_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.create_main_navigation(
            (
                ("transfer", "临时传输"),
                ("services", "服务管理"),
                ("status", "运行状态"),
                ("tools", "日志与帮助"),
            )
        )
        self.create_main_pages()

        # 绑定鼠标滚轮事件
        def _on_mousewheel(event):
            main_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_to_mousewheel(event):
            main_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_from_mousewheel(event):
            main_canvas.unbind_all("<MouseWheel>")

        main_canvas.bind("<Enter>", _bind_to_mousewheel)
        main_canvas.bind("<Leave>", _unbind_from_mousewheel)
        self.show_main_page("transfer")

    def create_main_navigation(self, items):
        """创建固定在窗口顶部的横向页面菜单。"""
        for column, (key, label) in enumerate(items):
            self.main_nav_bar.grid_columnconfigure(column, weight=1, uniform="mainnav")
            item = tk.Frame(self.main_nav_bar, bg=self.style.BG_CARD)
            item.grid(row=0, column=column, sticky="ew")
            button = tk.Button(
                item,
                text=label,
                command=lambda page=key: self.show_main_page(page),
                font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL, "bold"),
                bg=self.style.BG_CARD,
                fg=self.style.TEXT_SECONDARY,
                activebackground=self.style.BG_INFO,
                activeforeground=self.style.BG_BUTTON,
                relief="flat",
                bd=0,
                cursor="hand2",
                pady=11,
            )
            button.pack(fill=tk.X)
            indicator = tk.Frame(item, bg=self.style.BG_CARD, height=3)
            indicator.pack(fill=tk.X, side=tk.BOTTOM)
            self.main_nav_items[key] = (button, indicator)

    def _new_main_page(self, key):
        page = tk.Frame(self.page_host, bg=self.style.BG_PRIMARY)
        self.main_pages[key] = page
        return page

    def create_main_pages(self):
        """把临时传输、服务、状态和工具拆成独立页面。"""
        self.main_pages = {}

        # 临时传输
        transfer_page = self._new_main_page("transfer")
        transfer_page.grid_columnconfigure(0, weight=1)
        transfer_page.grid_columnconfigure(1, weight=1)
        self.create_temporary_transfer_panel(transfer_page)
        transfer_hint = self.create_card(transfer_page, "连接步骤")
        tk.Label(
            transfer_hint,
            text="选择发送和接收目录，启动后把地址与访问码发给对方。对方在同一局域网内用浏览器即可收发文件。",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
            justify=tk.LEFT,
            anchor="w",
            wraplength=900,
        ).pack(fill=tk.X, padx=self.style.PADDING_NORMAL, pady=self.style.PADDING_NORMAL)

        # 服务配置与启停
        service_page = self._new_main_page("services")
        service_page.grid_columnconfigure(0, weight=1)
        service_page.grid_columnconfigure(1, minsize=360)
        service_page.grid_rowconfigure(0, weight=1)
        left_frame = tk.Frame(service_page, bg=self.style.BG_PRIMARY)
        left_frame.grid(
            row=0,
            column=0,
            sticky="new",
            padx=(self.style.PADDING_LARGE, self.style.PADDING_SMALL),
            pady=(self.style.PADDING_NORMAL, self.style.PADDING_LARGE),
        )
        right_frame = tk.Frame(service_page, bg=self.style.BG_PRIMARY)
        right_frame.grid(
            row=0,
            column=1,
            sticky="new",
            padx=(self.style.PADDING_SMALL, self.style.PADDING_LARGE),
            pady=(self.style.PADDING_NORMAL, self.style.PADDING_LARGE),
        )

        service_card = self.create_card(left_frame, "服务与端口")
        self.create_service_selection(service_card)

        config_card = self.create_card(left_frame, "账户、目录与权限")
        account_body = tk.Frame(config_card, bg=self.style.BG_CARD)
        account_body.pack(
            fill=tk.X, padx=self.style.PADDING_NORMAL, pady=self.style.PADDING_NORMAL
        )
        account_body.grid_columnconfigure(0, weight=1)
        account_body.grid_columnconfigure(1, minsize=220)
        cfg = tk.Frame(account_body, bg=self.style.BG_CARD)
        cfg.grid(row=0, column=0, sticky="new")
        cfg.grid_columnconfigure(0, weight=1)
        self.create_input_field(cfg, "账户名称", "qwe", 0, 0)
        self.create_input_field(cfg, "账户密码", "", 1, 0, show="*")
        self.create_input_field(cfg, "最大连接", "100", 2, 0)
        self.create_input_field(
            cfg, "访问目录", os.path.expanduser("~"), 3, 0, browse=True,
        )
        self.create_input_field(
            cfg, "欢迎信息", "欢迎访问 Slyar FTP Server!", 4, 0
        )
        self.create_input_field(cfg, "退出信息", "再见!", 5, 0)
        perm_panel = tk.Frame(account_body, bg=self.style.BG_CARD)
        perm_panel.grid(row=0, column=1, sticky="new", padx=(12, 0))
        self.create_permission_checkboxes(perm_panel)

        control_card = self.create_card(right_frame, "服务器控制")
        self.create_control_buttons(control_card)
        tk.Label(
            right_frame,
            text="FTP、SFTP 和 Web 服务使用上方的服务选项与访问目录配置。临时传输在独立菜单中管理。",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_PRIMARY,
            fg=self.style.TEXT_SECONDARY,
            justify=tk.LEFT,
            wraplength=330,
        ).pack(fill=tk.X, padx=4, pady=(4, self.style.PADDING_NORMAL))

        # 状态页面
        status_page = self._new_main_page("status")
        status_page.grid_columnconfigure(0, weight=1)
        status_card = self.create_card(status_page, "服务与网络状态")
        self.create_status_display(status_card)

        # 日志、帮助与退出入口
        tools_page = self._new_main_page("tools")
        tools_page.grid_columnconfigure(0, weight=1)
        tools_page.grid_columnconfigure(1, weight=1)
        log_card = self.create_card(tools_page, "运行日志")
        tk.Label(
            log_card,
            text="查看 FTP、SFTP、Web 和临时传输的启动、连接与错误记录。",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
            justify=tk.LEFT,
            wraplength=430,
        ).pack(fill=tk.X, padx=self.style.PADDING_NORMAL, pady=(8, 12))
        self.create_modern_button(
            log_card,
            "打开实时日志",
            self.show_log_window,
            self.style.BG_INFO,
            self.style.BG_INFO_HOVER,
        ).pack(fill=tk.X, padx=self.style.PADDING_NORMAL, pady=(0, 12))

        help_card = self.create_card(tools_page, "使用帮助")
        tk.Label(
            help_card,
            text="查看服务配置、客户端连接方式、防火墙排查和常见问题。",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
            justify=tk.LEFT,
            wraplength=430,
        ).pack(fill=tk.X, padx=self.style.PADDING_NORMAL, pady=(8, 12))
        self.create_modern_button(
            help_card,
            "打开使用帮助",
            self.show_help,
            self.style.BG_INFO,
            self.style.BG_INFO_HOVER,
        ).pack(fill=tk.X, padx=self.style.PADDING_NORMAL, pady=(0, 12))

        exit_card = self.create_card(tools_page, "退出程序")
        tk.Label(
            exit_card,
            text="退出前会停止正在运行的服务。关闭窗口会按托盘设置隐藏或退出。",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
            justify=tk.LEFT,
            wraplength=900,
        ).pack(fill=tk.X, padx=self.style.PADDING_NORMAL, pady=(8, 12))
        self.create_modern_button(
            exit_card,
            "退出程序",
            self.quit_program,
            self.style.BG_DANGER,
            self.style.BG_DANGER_HOVER,
        ).pack(anchor="w", padx=self.style.PADDING_NORMAL, pady=(0, 12))

    def show_main_page(self, key):
        """切换主页面并更新顶部菜单的选中状态。"""
        if key not in getattr(self, "main_pages", {}):
            self._pending_main_page = key
            return
        for page in self.main_pages.values():
            if page.winfo_manager() == "grid":
                page.grid_remove()
        self.main_pages[key].grid(
            row=0,
            column=0,
            sticky="new",
            padx=self.style.PADDING_LARGE,
            pady=(self.style.PADDING_NORMAL, self.style.PADDING_LARGE),
        )
        self.main_pages[key].tkraise()
        for item_key, (button, indicator) in self.main_nav_items.items():
            active = item_key == key
            button.configure(
                bg=self.style.BG_INFO if active else self.style.BG_CARD,
                fg=self.style.BG_BUTTON if active else self.style.TEXT_SECONDARY,
            )
            indicator.configure(bg=self.style.BG_BUTTON if active else self.style.BG_CARD)
        self.main_canvas.yview_moveto(0)
        self.root.after_idle(
            lambda: self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all"))
        )

    def create_temporary_transfer_panel(self, parent):
        """主界面的双向临时传输卡片。"""
        panel = tk.Frame(
            parent,
            bg=self.style.BG_CARD,
            highlightthickness=1,
            highlightbackground=self.style.BORDER,
        )
        panel.pack(
            fill=tk.X,
            padx=self.style.PADDING_LARGE,
            pady=(0, self.style.PADDING_NORMAL),
        )
        panel.grid_columnconfigure(0, weight=1)

        header = tk.Frame(panel, bg=self.style.BG_CARD)
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(15, 3))
        tk.Label(
            header,
            text="临时双向传输",
            font=(self.style.FONT_FAMILY, 13, "bold"),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_PRIMARY,
        ).pack(side=tk.LEFT)
        tk.Label(
            header,
            text="同一局域网内，用浏览器互传文件",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
        ).pack(side=tk.LEFT, padx=(12, 0))
        tk.Label(
            header,
            text="WEB · LAN",
            font=("Segoe UI", 9, "bold"),
            bg=self.style.BG_INFO,
            fg=self.style.BG_BUTTON,
            padx=9,
            pady=4,
        ).pack(side=tk.RIGHT)

        body = tk.Frame(panel, bg=self.style.BG_CARD)
        body.grid(row=1, column=0, sticky="ew", padx=18, pady=(10, 16))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, minsize=280)

        paths = tk.Frame(body, bg=self.style.BG_CARD)
        paths.grid(row=0, column=0, sticky="ew", padx=(0, 20))
        paths.grid_columnconfigure(0, weight=1)
        self.temp_send_dir_var = tk.StringVar(value="")
        self.temp_receive_dir_var = tk.StringVar(value="")

        for row, label, variable in (
            (0, "对方可下载", self.temp_send_dir_var),
            (1, "对方上传到", self.temp_receive_dir_var),
        ):
            tk.Label(
                paths,
                text=label,
                font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL, "bold"),
                bg=self.style.BG_CARD,
                fg=self.style.TEXT_SECONDARY,
                anchor="w",
            ).grid(row=row * 2, column=0, sticky="w", pady=(0, 4 if row == 0 else 0))
            entry = tk.Entry(
                paths,
                textvariable=variable,
                font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
                bg=self.style.BG_INPUT,
                fg=self.style.TEXT_PRIMARY,
                relief="flat",
                bd=0,
                highlightthickness=1,
                highlightbackground=self.style.BORDER,
                highlightcolor=self.style.BORDER_FOCUS,
            )
            entry.grid(row=row * 2 + 1, column=0, sticky="ew", ipady=7)
            if row == 0:
                self.temp_send_entry = entry
            else:
                self.temp_receive_entry = entry
            browse_button = self.create_modern_button(
                paths,
                "选择文件夹",
                lambda v=variable: self.browse_temporary_directory(v),
                self.style.BG_INFO,
                self.style.BG_INFO_HOVER,
                font_size=self.style.FONT_SIZE_SMALL,
            )
            browse_button.grid(row=row * 2 + 1, column=1, padx=(8, 0), ipady=2)
            if row == 0:
                self.temp_send_browse_button = browse_button
            else:
                self.temp_receive_browse_button = browse_button

        settings = tk.Frame(body, bg=self.style.BG_CARD)
        settings.grid(row=0, column=1, sticky="new")
        settings.grid_columnconfigure(0, weight=1)
        tk.Label(
            settings,
            text="网络接口",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL, "bold"),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.temp_refresh_network_button = tk.Button(
            settings,
            text="刷新",
            command=self.refresh_temp_network_choices,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_CARD,
            fg=self.style.BG_BUTTON,
            activebackground=self.style.BG_INFO,
            relief="flat",
            bd=0,
            cursor="hand2",
        )
        self.temp_refresh_network_button.grid(
            row=0, column=1, sticky="e", padx=(6, 0), pady=(0, 4)
        )
        self.temp_network_var = tk.StringVar(value="")
        self.temp_network_combo = ttk.Combobox(
            settings,
            textvariable=self.temp_network_var,
            state="readonly",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
        )
        self.temp_network_combo.grid(row=1, column=0, columnspan=2, sticky="ew", ipady=3)
        self.refresh_temp_network_choices()

        tk.Label(
            settings,
            text="自动关闭",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL, "bold"),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
            anchor="w",
        ).grid(row=2, column=0, sticky="w", pady=(10, 4))
        self.temp_duration_var = tk.StringVar(value="2 小时")
        self.temp_duration_combo = ttk.Combobox(
            settings,
            textvariable=self.temp_duration_var,
            state="readonly",
            values=("30 分钟", "1 小时", "2 小时", "4 小时"),
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
        )
        self.temp_duration_combo.grid(row=3, column=0, sticky="ew", ipady=3)

        actions = tk.Frame(settings, bg=self.style.BG_CARD)
        actions.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        actions.grid_columnconfigure(0, weight=1)
        self.temp_start_button = self.create_modern_button(
            actions,
            "开始临时传输",
            self.start_temporary_transfer,
            self.style.BG_SUCCESS,
            self.style.BG_SUCCESS_HOVER,
        )
        self.temp_start_button.grid(row=0, column=0, sticky="ew")
        self.temp_stop_button = self.create_modern_button(
            actions,
            "结束传输",
            self.stop_temporary_transfer,
            self.style.BG_DANGER,
            self.style.BG_DANGER_HOVER,
            state=tk.DISABLED,
        )
        self.temp_stop_button.grid(row=1, column=0, sticky="ew", pady=(7, 0))

        share = tk.Frame(body, bg=self.style.BG_PRIMARY)
        share.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        share.grid_columnconfigure(1, weight=1)
        self.temp_transfer_status = tk.Label(
            share,
            text="就绪 · 开启后会生成临时访问地址和访问码",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_PRIMARY,
            fg=self.style.TEXT_SECONDARY,
            anchor="w",
            padx=10,
            pady=7,
        )
        self.temp_transfer_status.grid(row=0, column=0, columnspan=4, sticky="ew")

        tk.Label(
            share,
            text="访问地址",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_PRIMARY,
            fg=self.style.TEXT_SECONDARY,
        ).grid(row=1, column=0, padx=(10, 7), pady=(9, 7))
        self.temp_transfer_url_var = tk.StringVar(value="")
        self.temp_transfer_url_entry = tk.Entry(
            share,
            textvariable=self.temp_transfer_url_var,
            state="readonly",
            readonlybackground=self.style.BG_CARD,
            font=("Consolas", self.style.FONT_SIZE_SMALL),
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.style.BORDER,
        )
        self.temp_transfer_url_entry.grid(row=1, column=1, sticky="ew", pady=(9, 7))
        self.temp_copy_link_button = self.create_modern_button(
            share, "复制地址", self.copy_temporary_link,
            self.style.BG_INFO, self.style.BG_INFO_HOVER,
            font_size=self.style.FONT_SIZE_SMALL, state=tk.DISABLED,
        )
        self.temp_copy_link_button.grid(row=1, column=2, padx=(8, 0), pady=(9, 7))
        self.temp_open_button = self.create_modern_button(
            share, "本机打开", self.open_temporary_transfer,
            self.style.BG_INFO, self.style.BG_INFO_HOVER,
            font_size=self.style.FONT_SIZE_SMALL, state=tk.DISABLED,
        )
        self.temp_open_button.grid(row=1, column=3, padx=(8, 10), pady=(9, 7))

        tk.Label(
            share,
            text="访问码",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_PRIMARY,
            fg=self.style.TEXT_SECONDARY,
        ).grid(row=2, column=0, padx=(10, 7), pady=(0, 9))
        self.temp_transfer_code_var = tk.StringVar(value="")
        self.temp_transfer_code_entry = tk.Entry(
            share,
            textvariable=self.temp_transfer_code_var,
            state="readonly",
            readonlybackground=self.style.BG_CARD,
            font=("Consolas", 12, "bold"),
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.style.BORDER,
            width=12,
        )
        self.temp_transfer_code_entry.grid(row=2, column=1, sticky="w", pady=(0, 9))
        self.temp_copy_code_button = self.create_modern_button(
            share, "复制访问码", self.copy_temporary_code,
            self.style.BG_INFO, self.style.BG_INFO_HOVER,
            font_size=self.style.FONT_SIZE_SMALL, state=tk.DISABLED,
        )
        self.temp_copy_code_button.grid(row=2, column=2, sticky="w", padx=(8, 0), pady=(0, 9))
        tk.Label(
            share,
            text="对方连不上时，先确认两台电脑在同一局域网，并检查防火墙是否允许本程序接收连接。",
            font=(self.style.FONT_FAMILY, 9),
            bg=self.style.BG_PRIMARY,
            fg=self.style.TEXT_MUTED,
            anchor="w",
        ).grid(row=3, column=0, columnspan=4, sticky="ew", padx=10, pady=(0, 9))

    def browse_temporary_directory(self, variable):
        initial = variable.get() or os.path.expanduser("~")
        directory = filedialog.askdirectory(initialdir=initial)
        if directory:
            variable.set(directory)

    def refresh_temp_network_choices(self):
        """将可绑定的 IPv4 网卡放入临时传输选择器。"""
        options = []
        mapping = {}
        for info in self.get_all_ip_addresses():
            if info["is_loopback"] or not info["is_up"]:
                continue
            label = f"{info['name']}  ·  {info['ip']}"
            options.append(label)
            mapping[label] = info["ip"]
        self._temp_network_mapping = mapping
        self.temp_network_combo.configure(values=options)
        current = self.temp_network_var.get()
        if current not in mapping:
            self.temp_network_var.set(options[0] if options else "没有可用的 IPv4 网卡")

    def _set_temporary_transfer_widgets(self, running):
        self.temp_start_button.config(state=tk.DISABLED if running else tk.NORMAL)
        self.temp_stop_button.config(state=tk.NORMAL if running else tk.DISABLED)
        self.temp_network_combo.config(state="disabled" if running else "readonly")
        self.temp_duration_combo.config(state="disabled" if running else "readonly")
        self.temp_refresh_network_button.config(state=tk.DISABLED if running else tk.NORMAL)
        self.temp_copy_link_button.config(state=tk.NORMAL if running else tk.DISABLED)
        self.temp_copy_code_button.config(state=tk.NORMAL if running else tk.DISABLED)
        self.temp_open_button.config(state=tk.NORMAL if running else tk.DISABLED)
        if hasattr(self, "temp_send_entry"):
            state = tk.DISABLED if running else tk.NORMAL
            self.temp_send_entry.config(state=state)
            self.temp_receive_entry.config(state=state)
            self.temp_send_browse_button.config(state=state)
            self.temp_receive_browse_button.config(state=state)

    def start_temporary_transfer(self):
        if self.temp_transfer_running:
            return
        send_directory = self.temp_send_dir_var.get().strip()
        receive_directory = self.temp_receive_dir_var.get().strip()
        if not os.path.isdir(send_directory) or not os.path.isdir(receive_directory):
            messagebox.showerror("目录无效", "请选择有效的发送目录和接收目录。")
            return

        interface_label = self.temp_network_var.get()
        bind_ip = getattr(self, "_temp_network_mapping", {}).get(interface_label)
        if not bind_ip:
            self.refresh_temp_network_choices()
            interface_label = self.temp_network_var.get()
            bind_ip = getattr(self, "_temp_network_mapping", {}).get(interface_label)
        if not bind_ip:
            messagebox.showerror("没有可用网卡", "请连接局域网后刷新网卡列表。")
            return

        durations = {"30 分钟": 30, "1 小时": 60, "2 小时": 120, "4 小时": 240}
        minutes = durations.get(self.temp_duration_var.get(), 120)
        deadline = time.time() + minutes * 60
        access_code = "".join(
            secrets.choice("23456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(8)
        )
        server = None
        try:
            handler = make_temporary_transfer_handler(
                send_directory, receive_directory, access_code, deadline
            )
            server = ThreadedWebServer((bind_ip, 0), handler)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
        except Exception as exc:
            if server is not None:
                server.server_close()
            messagebox.showerror("启动失败", f"无法开启临时传输：{exc}")
            return

        self.temp_transfer_server = server
        self.temp_transfer_thread = thread
        self.temp_transfer_running = True
        self.temp_transfer_deadline = deadline
        self.temp_transfer_code = access_code
        self.temp_transfer_url = f"http://{bind_ip}:{port}"
        self._service_ports["temp"] = port
        self.temp_transfer_url_var.set(self.temp_transfer_url)
        self.temp_transfer_code_var.set(access_code)
        self.temp_transfer_status.config(
            text=f"临时传输运行中 · {self.temp_duration_var.get()} 后自动关闭",
            fg=self.style.STATUS_ON,
        )
        self._set_temporary_transfer_widgets(True)
        self._update_temporary_transfer_countdown()
        self.log_info(f"临时双向传输已启动：{bind_ip}:{port}，{minutes} 分钟后自动关闭")
        self.statusbar_text.config(text="临时双向传输已开启；将地址和访问码发给对方")

    def _update_temporary_transfer_countdown(self):
        if not self.temp_transfer_running or self.temp_transfer_deadline is None:
            return
        remaining = max(0, int(self.temp_transfer_deadline - time.time()))
        if remaining <= 0:
            self.stop_temporary_transfer(expired=True)
            return
        hours, rest = divmod(remaining, 3600)
        minutes, seconds = divmod(rest, 60)
        self.temp_transfer_status.config(
            text=f"临时传输运行中 · 剩余 {hours:02d}:{minutes:02d}:{seconds:02d}",
            fg=self.style.STATUS_ON,
        )
        self._temp_transfer_after_id = self.root.after(
            1000, self._update_temporary_transfer_countdown
        )

    def stop_temporary_transfer(self, expired=False, notify=True):
        if self._temp_transfer_after_id:
            try:
                self.root.after_cancel(self._temp_transfer_after_id)
            except Exception:
                pass
            self._temp_transfer_after_id = None
        server = self.temp_transfer_server
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        thread = self.temp_transfer_thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        self.temp_transfer_server = None
        self.temp_transfer_thread = None
        was_running = self.temp_transfer_running
        self.temp_transfer_running = False
        self.temp_transfer_deadline = None
        self._service_ports.pop("temp", None)
        self.temp_transfer_url = ""
        self.temp_transfer_code = ""
        self.temp_transfer_url_var.set("")
        self.temp_transfer_code_var.set("")
        self.temp_transfer_status.config(
            text="已到期，传输服务关闭" if expired else "已结束 · 临时地址和访问码已失效",
            fg=self.style.TEXT_SECONDARY,
        )
        self._set_temporary_transfer_widgets(False)
        if was_running:
            self.log_info("临时双向传输已自动结束" if expired else "临时双向传输已手动结束")
        if notify and was_running and not expired:
            self.statusbar_text.config(text="临时传输已结束")

    def _copy_to_clipboard(self, value, label):
        if not value:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.statusbar_text.config(text=f"{label}已复制")

    def copy_temporary_link(self):
        self._copy_to_clipboard(self.temp_transfer_url, "访问地址")

    def copy_temporary_code(self):
        self._copy_to_clipboard(self.temp_transfer_code, "访问码")

    def open_temporary_transfer(self):
        if self.temp_transfer_url:
            webbrowser.open(self.temp_transfer_url)

    def create_card(self, parent, title):
        """创建现代化卡片"""
        card_frame = tk.Frame(
            parent, bg=self.style.BG_CARD, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=self.style.BORDER,
        )
        card_frame.pack(fill=tk.BOTH, expand=True, pady=self.style.PADDING_SMALL)

        # 卡片标题
        title_frame = tk.Frame(card_frame, bg=self.style.BG_CARD, height=44)
        title_frame.pack(fill=tk.X)
        title_frame.pack_propagate(False)
        tk.Frame(title_frame, bg=self.style.BG_BUTTON, width=4).pack(
            side=tk.LEFT, fill=tk.Y
        )

        title_label = tk.Label(
            title_frame,
            text=title,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL, "bold"),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_PRIMARY,
        )
        title_label.pack(
            side=tk.LEFT, padx=self.style.PADDING_NORMAL,
            pady=self.style.PADDING_TINY
        )

        # 内容区域
        content_frame = tk.Frame(card_frame, bg=self.style.BG_CARD)
        content_frame.pack(
            fill=tk.BOTH,
            expand=True,
            padx=self.style.PADDING_NORMAL,
            pady=(self.style.PADDING_TINY, self.style.PADDING_NORMAL),
        )

        return content_frame

    def create_service_selection(self, parent):
        """创建服务选择界面：每个服务一行，端口配置跟随对应服务"""
        service_frame = tk.Frame(parent, bg=self.style.BG_CARD)
        service_frame.pack(
            fill=tk.X, padx=self.style.PADDING_NORMAL, pady=self.style.PADDING_NORMAL
        )
        service_frame.grid_columnconfigure(0, weight=1)

        hint_label = tk.Label(
            service_frame,
            text="选择服务与监听端口 · 所有 IPv4 网卡均可连接",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
            anchor=tk.W,
        )
        hint_label.grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(0, self.style.PADDING_SMALL)
        )

        entry_kw = dict(
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_INPUT,
            fg=self.style.TEXT_PRIMARY,
            disabledbackground=self.style.BG_INPUT,
            disabledforeground=self.style.TEXT_MUTED,
            relief="flat",
            bd=0,
            justify="center",
            width=7,
            highlightthickness=1,
            highlightcolor=self.style.BORDER_FOCUS,
            highlightbackground=self.style.BORDER,
        )

        def port_label(parent_):
            return tk.Label(
                parent_,
                text="端口",
                font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
                bg=self.style.BG_CARD,
                fg=self.style.TEXT_SECONDARY,
            )

        # FTP服务
        ftp_row = tk.Frame(service_frame, bg=self.style.BG_CARD)
        ftp_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=7)
        ftp_row.grid_columnconfigure(0, weight=1)

        ftp_cb = tk.Checkbutton(
            ftp_row,
            text="FTP — 传统文件传输",
            variable=self.enable_ftp,
            command=self.update_service_port_states,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_PRIMARY,
            selectcolor=self.style.BG_INPUT,
            activebackground=self.style.BG_CARD,
            activeforeground=self.style.TEXT_PRIMARY,
            relief="flat",
            bd=0,
            anchor=tk.W,
        )
        ftp_cb.grid(row=0, column=0, sticky="ew")

        port_label(ftp_row).grid(row=0, column=1, padx=(0, self.style.PADDING_TINY))
        self.ftp_port_entry = tk.Entry(ftp_row, **entry_kw)
        self.ftp_port_entry.insert(0, "21")
        self.ftp_port_entry.grid(row=0, column=2)

        # SFTP服务（端口固定为 FTP端口+1）
        sftp_row = tk.Frame(service_frame, bg=self.style.BG_CARD)
        sftp_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=7)
        sftp_row.grid_columnconfigure(0, weight=1)

        sftp_status = "" if SFTP_AVAILABLE else "（需安装 paramiko）"
        sftp_cb = tk.Checkbutton(
            sftp_row,
            text=f"SFTP — 安全文件传输{ sftp_status }",
            variable=self.enable_sftp,
            command=self.update_service_port_states,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_PRIMARY if SFTP_AVAILABLE else self.style.TEXT_MUTED,
            selectcolor=self.style.BG_INPUT,
            activebackground=self.style.BG_CARD,
            activeforeground=(
                self.style.TEXT_PRIMARY if SFTP_AVAILABLE else self.style.TEXT_MUTED
            ),
            relief="flat",
            bd=0,
            state=tk.NORMAL if SFTP_AVAILABLE else tk.DISABLED,
            anchor=tk.W,
        )
        sftp_cb.grid(row=0, column=0, sticky="ew")

        sftp_port_note = tk.Label(
            sftp_row,
            text="端口 = FTP端口 + 1",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_MUTED,
        )
        sftp_port_note.grid(row=0, column=1, columnspan=2, sticky="e")

        # Web服务
        web_row = tk.Frame(service_frame, bg=self.style.BG_CARD)
        web_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=7)
        web_row.grid_columnconfigure(0, weight=1)

        web_cb = tk.Checkbutton(
            web_row,
            text="Web — 浏览器文件管理",
            variable=self.enable_web,
            command=self.update_service_port_states,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_PRIMARY,
            selectcolor=self.style.BG_INPUT,
            activebackground=self.style.BG_CARD,
            activeforeground=self.style.TEXT_PRIMARY,
            relief="flat",
            bd=0,
            anchor=tk.W,
        )
        web_cb.grid(row=0, column=0, sticky="ew")

        port_label(web_row).grid(row=0, column=1, padx=(0, self.style.PADDING_TINY))
        self.web_port_entry = tk.Entry(web_row, **entry_kw)
        self.web_port_entry.insert(0, "8080")
        self.web_port_entry.grid(row=0, column=2)

        # Web安全选项：访问密码 + 只读模式
        web_opts = tk.Frame(web_row, bg=self.style.BG_CARD)
        web_opts.grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(0, self.style.PADDING_TINY)
        )

        tk.Label(
            web_opts,
            text="访问密码（留空=免登录）",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
        ).pack(side=tk.LEFT)

        self.web_password_entry = tk.Entry(
            web_opts,
            show="•",
            width=14,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_INPUT,
            fg=self.style.TEXT_PRIMARY,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightcolor=self.style.BORDER_FOCUS,
            highlightbackground=self.style.BORDER,
        )
        self.web_password_entry.pack(side=tk.LEFT, padx=self.style.PADDING_TINY)

        self.web_readonly_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            web_opts,
            text="只读模式（禁止上传/管理）",
            variable=self.web_readonly_var,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
            selectcolor=self.style.BG_INPUT,
            activebackground=self.style.BG_CARD,
            activeforeground=self.style.TEXT_PRIMARY,
            relief="flat",
            bd=0,
            anchor=tk.W,
        ).pack(side=tk.LEFT, padx=self.style.PADDING_TINY)

        # WebDAV服务
        webdav_row = tk.Frame(service_frame, bg=self.style.BG_CARD)
        webdav_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=7)
        webdav_row.grid_columnconfigure(0, weight=1)

        webdav_cb = tk.Checkbutton(
            webdav_row,
            text="WebDAV — 资源管理器/网盘客户端映射",
            variable=self.enable_webdav,
            command=self.update_service_port_states,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_PRIMARY,
            selectcolor=self.style.BG_INPUT,
            activebackground=self.style.BG_CARD,
            activeforeground=self.style.TEXT_PRIMARY,
            relief="flat",
            bd=0,
            anchor=tk.W,
        )
        webdav_cb.grid(row=0, column=0, sticky="ew")

        port_label(webdav_row).grid(row=0, column=1, padx=(0, self.style.PADDING_TINY))
        self.webdav_port_entry = tk.Entry(webdav_row, **entry_kw)
        self.webdav_port_entry.insert(0, "5005")
        self.webdav_port_entry.grid(row=0, column=2)

    def update_service_port_states(self):
        """根据服务勾选状态启用/禁用对应端口输入框（SFTP端口依赖FTP端口，故一并启用）"""
        if not hasattr(self, "ftp_port_entry"):
            return
        ftp_enabled = self.enable_ftp.get() or self.enable_sftp.get()
        for entry, enabled in (
            (self.ftp_port_entry, ftp_enabled),
            (self.web_port_entry, self.enable_web.get()),
            (self.webdav_port_entry, self.enable_webdav.get()),
        ):
            entry.config(state=tk.NORMAL if enabled else tk.DISABLED)

    def create_input_field(
        self,
        parent,
        label_text,
        default_value,
        row,
        col,
        show=None,
        browse=False,
        columnspan=1,
    ):
        """创建表单行：左侧标签 + 右侧输入框（grid布局，随窗口宽度缩放）"""
        container = tk.Frame(parent, bg=self.style.BG_CARD)
        container.grid(
            row=row,
            column=col,
            columnspan=columnspan,
            sticky="ew",
            padx=self.style.PADDING_TINY,
            pady=self.style.PADDING_TINY,
        )
        container.grid_columnconfigure(1, weight=1)

        label = tk.Label(
            container,
            text=label_text,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
            anchor=tk.W,
        )
        label.grid(row=0, column=0, sticky="w", padx=(0, self.style.PADDING_SMALL))

        entry = tk.Entry(
            container,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_INPUT,
            fg=self.style.TEXT_PRIMARY,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightcolor=self.style.BORDER_FOCUS,
            highlightbackground=self.style.BORDER,
        )
        entry.insert(0, default_value)
        if show:
            entry.config(show=show)

        if browse:
            entry.grid(row=0, column=1, sticky="ew")
            browse_btn = tk.Button(
                container,
                text="📂",
                command=self.browse_directory,
                font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
                bg=self.style.BG_BUTTON,
                fg="white",
                relief="flat",
                bd=0,
                width=3,
                cursor="hand2",
            )
            browse_btn.grid(
                row=0, column=2, padx=(self.style.PADDING_TINY, 0)
            )
            self.directory_entry = entry
        else:
            entry.grid(row=0, column=1, sticky="ew")

        # 保存引用（保持与配置读写逻辑兼容）
        if "名称" in label_text:
            self.username_entry = entry
        elif "密码" in label_text:
            self.password_entry = entry
        elif "连接" in label_text:
            self.max_conn_entry = entry
        elif "欢迎" in label_text:
            self.welcome_entry = entry
        elif "退出" in label_text:
            self.goodbye_entry = entry

    def create_permission_checkboxes(self, parent):
        """创建权限复选框"""
        perms_frame = tk.Frame(parent, bg=self.style.BG_CARD)
        perms_frame.pack(
            fill=tk.X, padx=self.style.PADDING_NORMAL, pady=self.style.PADDING_NORMAL
        )

        self.perm_download = tk.BooleanVar(value=True)
        self.perm_upload = tk.BooleanVar(value=False)
        self.perm_rename = tk.BooleanVar(value=False)
        self.perm_delete = tk.BooleanVar(value=False)
        self.perm_mkdir = tk.BooleanVar(value=False)

        tk.Label(
            perms_frame,
            text="账户权限 · 默认只读",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        permissions = [
            ("下载文件", self.perm_download, 1, 0),
            ("上传文件", self.perm_upload, 1, 1),
            ("文件更名", self.perm_rename, 2, 0),
            ("删除文件", self.perm_delete, 2, 1),
            ("创建目录", self.perm_mkdir, 3, 0),
        ]

        for text, var, row, col in permissions:
            cb = tk.Checkbutton(
                perms_frame,
                text=text,
                variable=var,
                font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
                bg=self.style.BG_CARD,
                fg=self.style.TEXT_PRIMARY,
                selectcolor=self.style.BG_INPUT,
                activebackground=self.style.BG_CARD,
                activeforeground=self.style.TEXT_PRIMARY,
                relief="flat",
                bd=0,
            )
            cb.grid(
                row=row,
                column=col,
                sticky=tk.W,
                padx=self.style.PADDING_TINY,
                pady=5,
            )

    def create_control_buttons(self, parent):
        """创建常规服务控制按钮。"""
        button_frame = tk.Frame(parent, bg=self.style.BG_CARD)
        button_frame.pack(
            fill=tk.X, padx=self.style.PADDING_NORMAL, pady=8
        )
        button_frame.grid_columnconfigure(0, weight=1)

        # 主操作：启动 / 停止 / 打开Web
        self.start_button = self.create_modern_button(
            button_frame,
            "启动服务",
            self.start_all_services,
            self.style.BG_SUCCESS,
            self.style.BG_SUCCESS_HOVER,
        )
        self.start_button.grid(row=0, column=0, sticky="ew", pady=2)

        self.stop_button = self.create_modern_button(
            button_frame,
            "停止服务",
            self.stop_all_services,
            self.style.BG_DANGER,
            self.style.BG_DANGER_HOVER,
            state=tk.DISABLED,
        )
        self.stop_button.grid(row=1, column=0, sticky="ew", pady=2)

        self.web_button = self.create_modern_button(
            button_frame,
            "打开 Web 界面",
            self.open_web_interface,
            self.style.BG_INFO,
            self.style.BG_INFO_HOVER,
            state=tk.DISABLED,
        )
        self.web_button.grid(row=2, column=0, sticky="ew", pady=2)

    def create_modern_button(
        self,
        parent,
        text,
        command,
        bg_color,
        hover_color,
        state=tk.NORMAL,
        font_size=None,
    ):
        """创建现代化按钮"""
        foreground = (
            self.style.TEXT_DANGER if bg_color == self.style.BG_DANGER
            else self.style.TEXT_PRIMARY if bg_color == self.style.BG_SECONDARY
            else self.style.BG_BUTTON if bg_color == self.style.BG_INFO
            else self.style.TEXT_ON_DARK
        )
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            font=(self.style.FONT_FAMILY, font_size or self.style.FONT_SIZE_NORMAL, "bold"),
            bg=bg_color,
            fg=foreground,
            activeforeground=foreground,
            activebackground=hover_color,
            disabledforeground=self.style.TEXT_MUTED,
            highlightthickness=1,
            highlightbackground=(
                self.style.BORDER if foreground != self.style.TEXT_ON_DARK
                else bg_color
            ),
            relief="flat",
            bd=0,
            padx=self.style.PADDING_NORMAL,
            pady=5 if font_size is None else 4,
            cursor="hand2",
            state=state,
        )

        # 添加悬停效果
        def on_enter(e):
            if btn["state"] != tk.DISABLED:
                btn.config(bg=hover_color)

        def on_leave(e):
            if btn["state"] != tk.DISABLED:
                btn.config(bg=bg_color)

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)

        return btn

    def create_status_display(self, parent):
        """创建运行状态区：服务状态行 + 连接数 + 本机地址列表"""
        frame = tk.Frame(parent, bg=self.style.BG_CARD)
        frame.pack(
            fill=tk.BOTH,
            expand=True,
            padx=self.style.PADDING_NORMAL,
            pady=self.style.PADDING_NORMAL,
        )
        frame.grid_columnconfigure(1, weight=1)

        # 服务状态行：FTP / SFTP / Web / WebDAV / 临时传输
        self.service_status_labels = {}
        for i, (key, name) in enumerate(
            [
                ("ftp", "FTP"),
                ("sftp", "SFTP"),
                ("web", "Web"),
                ("webdav", "WebDAV"),
                ("temp", "临时传输"),
            ]
        ):
            dot = tk.Label(
                frame,
                text="●",
                font=(self.style.FONT_FAMILY, 11),
                bg=self.style.BG_CARD,
                fg=self.style.TEXT_MUTED,
                width=2,
            )
            dot.grid(row=i, column=0, sticky="w", pady=2)

            name_label = tk.Label(
                frame,
                text=name,
                font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL, "bold"),
                bg=self.style.BG_CARD,
                fg=self.style.TEXT_PRIMARY,
                width=5,
                anchor="w",
            )
            name_label.grid(row=i, column=1, sticky="w")

            info_label = tk.Label(
                frame,
                text="未启动",
                font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
                bg=self.style.BG_CARD,
                fg=self.style.TEXT_MUTED,
                anchor="e",
            )
            info_label.grid(row=i, column=2, sticky="e")

            self.service_status_labels[key] = (dot, info_label)

        # 连接数
        ttk.Separator(frame, orient="horizontal").grid(
            row=5, column=0, columnspan=3, sticky="ew", pady=3
        )
        conn_title = tk.Label(
            frame,
            text="当前连接数",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
            anchor="w",
        )
        conn_title.grid(row=6, column=0, columnspan=2, sticky="w")
        self.conn_count_label = tk.Label(
            frame,
            text="0",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL, "bold"),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_PRIMARY,
        )
        self.conn_count_label.grid(row=6, column=2, sticky="e")

        # 本机 IP 地址（供客户端连接）
        ttk.Separator(frame, orient="horizontal").grid(
            row=7, column=0, columnspan=3, sticky="ew", pady=3
        )
        ip_header = tk.Label(
            frame,
            text="📍 本机 IP 地址（客户端连接用）",
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_CARD,
            fg=self.style.TEXT_SECONDARY,
            anchor="w",
        )
        ip_header.grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(0, self.style.PADDING_TINY)
        )

        copy_btn = tk.Button(
            frame,
            text="复制",
            command=self.copy_primary_ip,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
            bg=self.style.BG_BUTTON,
            fg="white",
            relief="flat",
            bd=0,
            padx=8,
            cursor="hand2",
        )
        copy_btn.grid(row=8, column=2, sticky="e", pady=(0, self.style.PADDING_TINY))
        self.copy_ip_btn = copy_btn

        self.ip_info_frame = tk.Frame(frame, bg=self.style.BG_INPUT)
        self.ip_info_frame.grid(row=9, column=0, columnspan=3, sticky="ew")
        self.ip_info_frame.grid_columnconfigure(0, weight=1)

    def browse_directory(self):
        directory = filedialog.askdirectory()
        if directory:
            self.directory_entry.delete(0, tk.END)
            self.directory_entry.insert(0, directory)

    def get_all_ip_addresses(self):
        """获取所有网卡的IP地址和名称"""
        network_info = []

        try:
            # 获取主要IP（用于排序）
            primary_ip = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                primary_ip = s.getsockname()[0]
                s.close()
            except:
                pass

            # 使用psutil获取所有网卡信息
            interfaces = psutil.net_if_addrs()
            stats = psutil.net_if_stats()

            for interface_name, addresses in interfaces.items():
                # 检查网卡是否启用
                is_up = stats[interface_name].isup if interface_name in stats else False

                for addr in addresses:
                    # 只处理IPv4地址
                    if addr.family == socket.AF_INET:
                        ip = addr.address
                        # 过滤0.0.0.0和169.254开头的地址（APIPA地址）
                        if ip != "0.0.0.0" and not ip.startswith("169.254"):
                            # 判断是否为主要网卡
                            is_primary = ip == primary_ip
                            network_info.append(
                                {
                                    "name": interface_name,
                                    "ip": ip,
                                    "is_primary": is_primary,
                                    "is_up": is_up,
                                    "is_loopback": (ip == "127.0.0.1"),
                                }
                            )

            # 排序：主要网卡优先，然后是其他启用的网卡，最后是回环地址
            network_info.sort(
                key=lambda x: (
                    not x["is_primary"],  # 主要网卡排在最前
                    x["is_loopback"],  # 回环地址排在最后
                    not x["is_up"],  # 启用的网卡优先
                    x["name"],  # 按名称排序
                )
            )

        except Exception as e:
            # 如果psutil失败，使用备用方法
            try:
                hostname = socket.gethostname()
                local_ip = socket.gethostbyname(hostname)
                network_info.append(
                    {
                        "name": "默认网卡",
                        "ip": local_ip,
                        "is_primary": True,
                        "is_up": True,
                        "is_loopback": False,
                    }
                )
            except:
                pass

            # 添加回环地址
            network_info.append(
                {
                    "name": "Loopback",
                    "ip": "127.0.0.1",
                    "is_primary": False,
                    "is_up": True,
                    "is_loopback": True,
                }
            )

        return (
            network_info
            if network_info
            else [
                {
                    "name": "Loopback",
                    "ip": "127.0.0.1",
                    "is_primary": False,
                    "is_up": True,
                    "is_loopback": True,
                }
            ]
        )

    def update_local_ip(self):
        """更新本机IP列表显示（主要网卡置顶并高亮）"""
        network_info = self.get_all_ip_addresses()

        for child in self.ip_info_frame.winfo_children():
            child.destroy()

        for i, info in enumerate(network_info):
            name = info["name"]

            tag = ""
            if info["is_primary"]:
                tag = " ⭐"
            elif info["is_loopback"]:
                tag = " 🔄"

            name_label = tk.Label(
                self.ip_info_frame,
                text=f"{name}{tag}",
                font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_SMALL),
                bg=self.style.BG_INPUT,
                fg=self.style.TEXT_PRIMARY if info["is_primary"] else self.style.TEXT_MUTED,
                anchor="w",
                justify=tk.LEFT,
                wraplength=180,
            )
            name_label.grid(row=i, column=0, sticky="ew", padx=(8, 0), pady=3)

            ip_label = tk.Label(
                self.ip_info_frame,
                text=info["ip"],
                font=("Consolas", self.style.FONT_SIZE_SMALL),
                bg=self.style.BG_INPUT,
                fg=self.style.TEXT_PRIMARY if info["is_primary"] else self.style.TEXT_SECONDARY,
                anchor="e",
            )
            ip_label.grid(row=i, column=1, sticky="e", padx=(6, 8), pady=3)

    def copy_primary_ip(self):
        """复制主要IP地址到剪贴板"""
        ips = [
            i["ip"]
            for i in self.get_all_ip_addresses()
            if not i["is_loopback"]
        ]
        text = ips[0] if ips else "127.0.0.1"
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

        self.copy_ip_btn.config(text="✓ 已复制", bg=self.style.BG_SUCCESS)
        self.root.after(
            1500,
            lambda: self.copy_ip_btn.config(
                text="复制", bg=self.style.BG_BUTTON
            ),
        )

    def refresh_status(self):
        """定时刷新运行状态：服务徽标、端口、连接数（每2秒）"""
        try:
            states = {
                "ftp": self.ftp_running,
                "sftp": self.sftp_running,
                "web": self.web_running,
                "webdav": self.webdav_running,
                "temp": self.temp_transfer_running,
            }
            running_count = sum(1 for on in states.values() if on)

            for key, on in states.items():
                dot, info = self.service_status_labels[key]
                if on:
                    port = self._service_ports.get(key)
                    port_text = f"端口 {port}" if port else ""
                    dot.config(fg=self.style.STATUS_ON)
                    info.config(text=f"运行中 {port_text}".strip(), fg=self.style.TEXT_SECONDARY)
                else:
                    dot.config(fg=self.style.TEXT_MUTED)
                    info.config(text="未启动", fg=self.style.TEXT_MUTED)

            # 头部徽标与底部状态栏圆点
            if running_count:
                self.header_badge.config(
                    text=f"● 运行中 · {running_count} 项服务", fg=self.style.STATUS_ON
                )
                self.statusbar_dot.config(fg=self.style.STATUS_ON)
            else:
                self.header_badge.config(text="● 待机", fg=self.style.TEXT_MUTED)
                self.statusbar_dot.config(fg=self.style.TEXT_MUTED)

            # 连接数：统计到服务端口的已建立连接
            ports = {
                self._service_ports[key]
                for key in ("ftp", "sftp", "web", "webdav", "temp")
                if states.get(key) and key in self._service_ports
            }
            count_text = "—"
            if ports:
                try:
                    count_text = str(
                        sum(
                            1
                            for c in psutil.net_connections(kind="inet")
                            if c.status == psutil.CONN_ESTABLISHED
                            and c.laddr
                            and c.laddr.port in ports
                        )
                    )
                except Exception:
                    pass
            self.conn_count_label.config(text=count_text)
        except Exception:
            pass

        self.root.after(2000, self.refresh_status)

    def get_permissions(self):
        return ftp_permissions_from_config(self.get_permission_config())

    def get_permission_config(self):
        return {
            "download": self.perm_download.get(),
            "upload": self.perm_upload.get(),
            "rename": self.perm_rename.get(),
            "delete": self.perm_delete.get(),
            "mkdir": self.perm_mkdir.get(),
        }

    def is_admin(self):
        """检查是否具有管理员权限"""
        try:
            return ctypes.windll.shell32.IsUserAnAdmin()
        except:
            return False

    def show_help(self):
        """显示现代化使用帮助窗口"""
        help_window = tk.Toplevel(self.root)
        help_window.title("📚 使用帮助 - Slyar FTP Server")
        help_window.geometry("800x650")
        help_window.resizable(True, True)
        help_window.configure(bg=self.style.BG_PRIMARY)
        self._apply_icon(help_window)

        # 标题
        title_frame = tk.Frame(help_window, bg=self.style.BG_PRIMARY, height=60)
        title_frame.pack(
            fill=tk.X, padx=self.style.PADDING_LARGE, pady=(self.style.PADDING_LARGE, 0)
        )
        title_frame.pack_propagate(False)

        title_label = tk.Label(
            title_frame,
            text="📚 Slyar FTP Server 使用帮助",
            font=(self.style.FONT_FAMILY, 16, "bold"),
            bg=self.style.BG_PRIMARY,
            fg=self.style.TEXT_PRIMARY,
        )
        title_label.pack(
            side=tk.LEFT, padx=self.style.PADDING_NORMAL, pady=self.style.PADDING_NORMAL
        )

        # 创建现代化标签页
        notebook = ttk.Notebook(help_window)
        notebook.pack(
            fill=tk.BOTH,
            expand=True,
            padx=self.style.PADDING_LARGE,
            pady=self.style.PADDING_NORMAL,
        )

        # 配置标签页样式
        style = ttk.Style()
        style.configure("Help.TNotebook", background=self.style.BG_PRIMARY)
        style.configure(
            "Help.TNotebook.Tab",
            padding=[20, 10],
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
        )
        style.map("Help.TNotebook.Tab", background=[("selected", self.style.BG_CARD)])

        # 快速入门标签页
        quick_start_frame = tk.Frame(notebook, bg=self.style.BG_CARD)
        notebook.add(quick_start_frame, text="🚀 快速入门")

        quick_start_text = scrolledtext.ScrolledText(
            quick_start_frame,
            wrap=tk.WORD,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_INPUT,
            fg=self.style.TEXT_PRIMARY,
            relief="flat",
            bd=0,
        )
        quick_start_text.pack(
            fill=tk.BOTH,
            expand=True,
            padx=self.style.PADDING_NORMAL,
            pady=self.style.PADDING_NORMAL,
        )
        quick_start_text.insert(
            tk.END,
            """
🚀 快速入门指南

1️⃣ 选择服务类型
   在"服务选择"区域勾选要启用的服务：
   ✅ 🌐 FTP服务 - 传统文件传输协议
   ✅ 🔒 SFTP服务 - 安全文件传输协议（需要安装paramiko库）
   ✅ 🌍 Web服务 - 浏览器文件管理
   ✅ 🔗 WebDAV服务 - 映射为网络驱动器（资源管理器/WinSCP等客户端）

临时跨电脑互传可直接使用主窗口的「临时双向传输」卡片：
选择发送目录、接收目录和网卡，启动后把地址与访问码发给对方即可。
对方用浏览器访问；发送目录只读，接收目录仅用于上传。

2️⃣ 配置服务端口
   • 🌐 FTP端口：设置FTP服务端口（默认21）
   • 🌍 Web端口：设置Web服务端口（默认8080）
   • 🔗 WebDAV端口：设置WebDAV服务端口（默认5005）
   • SFTP端口：自动设置为FTP端口+1

3️⃣ 配置服务器
   • 🔗 最大连接：设置最大同时连接数（默认100）
   • 👤 账户信息：设置登录用户名和密码
   • 📁 访问目录：点击"浏览"选择要共享的文件夹

4️⃣ 设置权限
   勾选允许的操作：
   ✅ 📥 下载文件 - 允许客户端下载文件
   ✅ 📤 上传文件 - 允许客户端上传文件
   ✅ ✏️ 文件更名 - 允许重命名文件
   ✅ 🗑️ 删除文件 - 允许删除文件
   ✅ 📁 创建目录 - 允许创建新文件夹

5️⃣ 启动服务
   点击"🚀 启动所有服务"按钮，成功后会显示各服务状态

6️⃣ 连接服务
   🌐 FTP连接：
   • 📍 地址：程序底部显示的本机IP
   • 🔌 端口：您设置的FTP端口号
   • 👤 用户名：配置的账户名称
   • 🔐 密码：配置的账户密码

   🔒 SFTP连接：
   • 📍 地址：程序底部显示的本机IP
   • 🔌 端口：FTP端口+1
   • 👤 用户名：配置的账户名称
   • 🔐 密码：配置的账户密码

   🌍 Web连接：
   • 在浏览器中访问：http://IP地址:Web端口
   • 或点击"🌐 打开Web界面"按钮

   🔗 WebDAV连接：
   • 地址：http://IP地址:WebDAV端口/
   • 用户名/密码：配置的账户名称和密码
   • Windows资源管理器：右键"此电脑" → 映射网络驱动器
   • 或使用 WinSCP、RaiDrive、Cyberduck 等客户端连接

7️⃣ 停止服务
   点击"⏹️ 停止所有服务"按钮关闭所有服务
   临时传输可在卡片中单独结束，也会按所选时限自动关闭

💡 小贴士：
   • 首次使用建议FTP端口设置为2121，避免权限问题！
   • 可以同时启用多个服务，提供多种访问方式
   • Web服务提供直观的文件管理界面
   • SFTP服务提供加密传输，更安全
        """,
        )
        quick_start_text.config(state=tk.DISABLED)

        # 重要提示标签页
        tips_frame = tk.Frame(notebook, bg=self.style.BG_CARD)
        notebook.add(tips_frame, text="⚠️ 重要提示")

        tips_text = scrolledtext.ScrolledText(
            tips_frame,
            wrap=tk.WORD,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_INPUT,
            fg=self.style.TEXT_PRIMARY,
            relief="flat",
            bd=0,
        )
        tips_text.pack(
            fill=tk.BOTH,
            expand=True,
            padx=self.style.PADDING_NORMAL,
            pady=self.style.PADDING_NORMAL,
        )
        tips_text.insert(
            tk.END,
            """
⚠️ 重要提示和安全建议

🔌 端口权限问题
• 使用21端口需要管理员权限！
• 如果启动失败，建议使用2121等高端口
• 高端口（1024以上）不需要管理员权限

🔐 安全建议
• 使用强密码（建议8位以上，包含大小写字母、数字）
• 仅在可信网络环境下使用
• 定期更换密码
• 只授予必要的权限
• 不要共享系统敏感目录

🛡️ 防火墙设置
• 首次启动可能需要允许防火墙访问
• Windows防火墙可能需要手动添加规则
• 确保防火墙允许配置的端口通信

📁 目录权限
• 确保访问目录有相应的读写权限
• 建议不要使用系统目录（如C:\\Windows）
• 推荐使用专门的共享文件夹

💡 推荐配置
• 端口：2121（避免权限问题）
• 最大连接：100
• 密码：强密码
• 访问目录：专用共享文件夹

🚨 安全警告
• 请勿在公共网络中使用弱密码
• 定期检查访问日志
• 及时更新软件版本
        """,
        )
        tips_text.config(state=tk.DISABLED)

        # 连接方法标签页
        connect_frame = tk.Frame(notebook, bg=self.style.BG_CARD)
        notebook.add(connect_frame, text="🔗 连接方法")

        connect_text = scrolledtext.ScrolledText(
            connect_frame,
            wrap=tk.WORD,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_INPUT,
            fg=self.style.TEXT_PRIMARY,
            relief="flat",
            bd=0,
        )
        connect_text.pack(
            fill=tk.BOTH,
            expand=True,
            padx=self.style.PADDING_NORMAL,
            pady=self.style.PADDING_NORMAL,
        )
        connect_text.insert(
            tk.END,
            """
🔗 多服务连接方法

🌐 FTP连接方法

📱 Windows资源管理器
在地址栏输入：
ftp://用户名:密码@IP地址:端口

示例：
ftp://qwe:123@192.168.1.100:2121

💻 FileZilla客户端
• 主机：192.168.1.100
• 用户名：qwe
• 密码：123
• 端口：2121

⌨️ 命令行
Windows：
ftp IP地址 端口

Linux/Mac：
ftp IP地址 端口

🌐 浏览器
在地址栏输入：
ftp://IP地址:端口

然后输入用户名和密码

🔒 SFTP连接方法

💻 FileZilla客户端
• 协议：SFTP
• 主机：192.168.1.100
• 用户名：qwe
• 密码：123
• 端口：2122（FTP端口+1）

⌨️ 命令行
Windows（使用OpenSSH或PuTTY）：
sftp 用户名@IP地址:端口

Linux/Mac：
sftp -P 端口 用户名@IP地址

示例：
sftp -P 2122 qwe@192.168.1.100

🖥️ 第三方SFTP客户端
• FileZilla（全平台）
• WinSCP（Windows）
• Cyberduck（macOS）
• Transmit（macOS）

🌍 Web服务连接方法

🌐 浏览器访问
在浏览器地址栏输入：
http://IP地址:Web端口

示例：
http://192.168.1.100:8080

功能特性：
• 📁 文件浏览和下载
• 📤 文件上传（支持拖拽和选择文件）
• 📂 目录导航
• 📱 响应式设计，支持手机访问
• 🎨 现代化界面设计

📱 手机客户端推荐
• Android：推荐使用"FTP客户端"、"ES文件浏览器"
• iOS：推荐使用"FTP Manager"、"Documents"

💡 使用建议
• 🌐 FTP：适合快速文件传输，兼容性好
• 🔒 SFTP：适合敏感文件传输，加密安全
• 🌍 Web：适合不熟悉FTP的用户，界面友好，支持直接在浏览器中上传下载
• 🔄 多服务：可以同时使用多种方式，提高可靠性
        """,
        )
        connect_text.config(state=tk.DISABLED)

        # 常见问题标签页
        faq_frame = tk.Frame(notebook, bg=self.style.BG_CARD)
        notebook.add(faq_frame, text="❓ 常见问题")

        faq_text = scrolledtext.ScrolledText(
            faq_frame,
            wrap=tk.WORD,
            font=(self.style.FONT_FAMILY, self.style.FONT_SIZE_NORMAL),
            bg=self.style.BG_INPUT,
            fg=self.style.TEXT_PRIMARY,
            relief="flat",
            bd=0,
        )
        faq_text.pack(
            fill=tk.BOTH,
            expand=True,
            padx=self.style.PADDING_NORMAL,
            pady=self.style.PADDING_NORMAL,
        )
        faq_text.insert(
            tk.END,
            """
❓ 常见问题解答

Q1: 提示端口被占用怎么办？
A: 更换其他端口号（如2121、3021等），或关闭占用该端口的程序

Q2: 使用21端口提示权限不足？
A: 以管理员身份运行程序，或使用高端口号（1024以上）

Q3: SFTP服务显示不可用？
A: 需要安装paramiko库，运行命令：pip install paramiko

Q4: Web服务无法访问？
A:
• 检查Web端口是否被占用
• 确认防火墙允许Web端口通信
• 尝试使用127.0.0.1:端口本地测试

Q: 临时传输地址在另一台电脑上打不开？
A:
• 两台电脑连接同一局域网
• 选择实际连接该局域网的网卡
• 检查 Windows 防火墙是否允许本程序接收连接
• 临时传输使用 HTTP，建议仅在可信局域网中使用

Q5: 防火墙拦截连接？
A:
• Windows防火墙：添加入站规则允许对应端口
• 或临时关闭防火墙测试

Q6: 客户端连接超时？
A:
• 检查IP地址是否正确
• 确认端口号一致
• 检查网络连接
• 确认防火墙设置
• 确保对应服务已启动

Q7: 无法上传/下载文件？
A: 检查账户权限设置，确保勾选了相应的权限

Q8: 找不到共享的文件？
A: 检查"访问目录"设置是否正确

Q9: 如何以管理员身份运行？
A: 右键程序图标 → 选择"以管理员身份运行"

Q10: 配置会保存吗？
A: 会自动保存，下次启动时自动加载上次的配置

Q11: 支持多用户吗？
A: 当前版本支持单用户，后续版本会添加多用户支持

Q12: 如何查看连接日志？
A: 当前版本在状态区域显示连接信息，后续版本会添加详细日志功能

Q13: 哪种服务方式最推荐？
A:
• 🌐 FTP：兼容性最好，几乎所有设备都支持
• 🔒 SFTP：最安全，适合传输敏感文件
• 🌍 Web：最简单，适合不熟悉FTP的用户
• 🔄 多服务：建议同时启用，提供多种访问方式

Q14: 可以同时运行多个服务吗？
A: 可以！程序支持同时运行FTP、SFTP和Web服务，提供更可靠的文件访问

Q15: SFTP端口是什么？
A: SFTP端口自动设置为FTP端口+1，例如FTP使用2121，SFTP使用2122

Q16: WebDAV有什么用？
A: 把共享目录映射成"网络驱动器"，Windows资源管理器、WinSCP、RaiDrive、Cyberduck等客户端可以直接像本地磁盘一样读写文件。账户和权限与FTP共用。

Q17: WebDAV映射网络驱动器失败？
A:
• 账户密码必须非空（WebDAV不支持匿名）
• Windows资源管理器默认仅允许443端口的WebDAV，普通端口请使用WinSCP/RaiDrive等客户端
• 若坚持用资源管理器：需把WebDAV端口改为80，并以管理员运行注册表调整
  HKLM\\SYSTEM\\CurrentControlSet\\Services\\WebClient\\Parameters 下
  BasicAuthLevel=2、FileSizeLimitInBytes 改大（默认单文件上限约50MB）
• 防火墙需放行对应端口
        """,
        )
        faq_text.config(state=tk.DISABLED)

        # 关闭按钮
        button_frame = tk.Frame(help_window, bg=self.style.BG_PRIMARY)
        button_frame.pack(
            fill=tk.X, padx=self.style.PADDING_LARGE, pady=(0, self.style.PADDING_LARGE)
        )

        close_btn = self.create_modern_button(
            button_frame,
            "🚪 关闭帮助",
            help_window.destroy,
            self.style.BG_INFO,
            self.style.BG_INFO_HOVER,
        )
        close_btn.pack(side=tk.RIGHT)

    def start_all_services(self):
        """启动所有选中的服务"""
        try:
            # 获取基本配置
            username = self.username_entry.get()
            password = self.password_entry.get()
            directory = self.directory_entry.get()
            welcome_msg = self.welcome_entry.get()
            goodbye_msg = self.goodbye_entry.get()

            if (self.enable_ftp.get() or self.enable_sftp.get() or self.enable_webdav.get()) and (
                not username.strip() or not password or username.lower() == "anonymous"
            ):
                messagebox.showerror("错误", "请输入非 anonymous 的账户名称和密码！")
                return

            if not os.path.isdir(directory):
                messagebox.showerror("错误", "访问目录不存在或不是文件夹！")
                return

            # 检查是否至少选择了一个服务
            if not any(
                [
                    self.enable_ftp.get(),
                    self.enable_sftp.get(),
                    self.enable_web.get(),
                    self.enable_webdav.get(),
                ]
            ):
                messagebox.showerror("错误", "请至少选择一个服务类型！")
                return
            if self.enable_sftp.get() and not SFTP_AVAILABLE:
                messagebox.showerror("错误", "SFTP 服务不可用，请安装 paramiko")
                return

            # 校验参数（只校验已启用服务的配置项）
            max_conn = 100
            if self.enable_ftp.get():
                try:
                    max_conn = int(self.max_conn_entry.get())
                    if max_conn < 1:
                        raise ValueError
                except ValueError:
                    messagebox.showerror("错误", "最大连接数必须是正整数！")
                    return

            port = None
            if self.enable_ftp.get() or self.enable_sftp.get():
                try:
                    port = int(self.ftp_port_entry.get())
                    if not 0 < port < 65536:
                        raise ValueError
                except ValueError:
                    messagebox.showerror("错误", "FTP端口必须是 1-65535 之间的数字！")
                    return
                if self.enable_sftp.get() and port == 65535:
                    messagebox.showerror("错误", "SFTP 端口为 FTP 端口加 1，FTP 端口不能使用 65535")
                    return

            web_port = None
            if self.enable_web.get():
                try:
                    web_port = int(self.web_port_entry.get())
                    if not 0 < web_port < 65536:
                        raise ValueError
                except ValueError:
                    messagebox.showerror("错误", "Web端口必须是 1-65535 之间的数字！")
                    return

            webdav_port = None
            if self.enable_webdav.get():
                try:
                    webdav_port = int(self.webdav_port_entry.get())
                    if not 0 < webdav_port < 65536:
                        raise ValueError
                except ValueError:
                    messagebox.showerror("错误", "WebDAV端口必须是 1-65535 之间的数字！")
                    return

            # 检查端口权限
            if (
                port is not None
                and self.enable_ftp.get()
                and port < 1024
                and not self.is_admin()
            ):
                result = messagebox.askyesno(
                    "权限提示",
                    f"使用端口 {port} 需要管理员权限！\n\n"
                    "建议方案：\n"
                    '1. 点击"是"使用高端口（如2121）继续\n'
                    '2. 点击"否"取消，然后以管理员身份运行程序\n\n'
                    "是否自动更改为端口 2121？",
                )
                if result:
                    self.ftp_port_entry.config(state=tk.NORMAL)
                    self.ftp_port_entry.delete(0, tk.END)
                    self.ftp_port_entry.insert(0, "2121")
                    port = 2121
                else:
                    return

            temp_port = (
                self._service_ports.get("temp") if self.temp_transfer_running else None
            )
            self._service_ports = {"temp": temp_port} if temp_port else {}
            success_messages = []

            # 启动FTP服务
            if self.enable_ftp.get():
                try:
                    # 创建授权器
                    authorizer = DummyAuthorizer()
                    perms = self.get_permissions()
                    authorizer.add_user(username, password, directory, perm=perms)

                    # 创建处理器
                    handler = FTPHandler
                    handler.authorizer = authorizer
                    handler.banner = welcome_msg

                    # 创建服务器
                    self.ftp_server = FTPServer(("0.0.0.0", port), handler)
                    self.ftp_server.max_cons = max_conn
                    self.ftp_server.max_cons_per_ip = max_conn

                    # 在新线程中启动服务器
                    self.ftp_thread = threading.Thread(
                        target=self.ftp_server.serve_forever
                    )
                    self.ftp_thread.daemon = True
                    self.ftp_thread.start()

                    self.ftp_running = True
                    self._service_ports["ftp"] = port
                    success_messages.append(f"FTP服务已启动在端口 {port}")
                except Exception as e:
                    messagebox.showerror("错误", f"启动FTP服务失败：{str(e)}")
                    return

            # 启动SFTP服务
            if self.enable_sftp.get() and SFTP_AVAILABLE:
                try:
                    self.start_sftp_server(
                        port + 1, username, password, directory,
                        self.get_permission_config(),
                    )
                    self._service_ports["sftp"] = port + 1
                    success_messages.append(f"SFTP服务已启动在端口 {port + 1}")
                except Exception as e:
                    if self.ftp_running:
                        self.stop_all_services(notify=False)
                    messagebox.showerror("错误", f"启动SFTP服务失败：{str(e)}")
                    return

            # 启动Web服务
            if self.enable_web.get():
                try:
                    self.start_web_server(
                        web_port,
                        directory,
                        password=self.web_password_entry.get(),
                        readonly=self.web_readonly_var.get(),
                    )
                    self._service_ports["web"] = web_port
                    success_messages.append(f"Web服务已启动在端口 {web_port}")
                except Exception as e:
                    if self.ftp_running or self.sftp_running:
                        self.stop_all_services(notify=False)
                    messagebox.showerror("错误", f"启动Web服务失败：{str(e)}")
                    return

            # 启动WebDAV服务
            if self.enable_webdav.get():
                try:
                    self.start_webdav_server(
                        webdav_port,
                        username,
                        password,
                        directory,
                        self.get_permission_config(),
                    )
                    self._service_ports["webdav"] = webdav_port
                    success_messages.append(f"WebDAV服务已启动在端口 {webdav_port}")
                except Exception as e:
                    if self.ftp_running or self.sftp_running or self.web_running:
                        self.stop_all_services(notify=False)
                    messagebox.showerror("错误", f"启动WebDAV服务失败：{str(e)}")
                    return

            # 更新按钮状态
            self.start_button.config(state=tk.DISABLED)
            self.stop_button.config(state=tk.NORMAL)
            if self.enable_web.get():
                self.web_button.config(state=tk.NORMAL)

            # 显示成功消息
            messagebox.showinfo("成功", "\n".join(success_messages))
            for msg in success_messages:
                self.log_info(msg)

        except Exception as e:
            messagebox.showerror("错误", f"启动服务失败：{str(e)}")

    def stop_all_services(self, notify=True):
        """停止所有服务"""
        try:
            success_messages = []

            # 停止FTP服务
            if self.ftp_running and self.ftp_server:
                try:
                    self.ftp_server.close_all()
                    self.ftp_running = False
                    success_messages.append("FTP服务已停止")
                except Exception as e:
                    messagebox.showerror("错误", f"停止FTP服务失败：{str(e)}")

            # 停止SFTP服务
            if self.sftp_running and self.sftp_server:
                try:
                    self.sftp_server.stop()
                    self.sftp_running = False
                    self.sftp_server = None
                    success_messages.append("SFTP服务已停止")
                except Exception as e:
                    messagebox.showerror("错误", f"停止SFTP服务失败：{str(e)}")

            # 停止Web服务
            if self.web_running and self.web_server:
                try:
                    self.web_server.shutdown()
                    self.web_server.server_close()
                    self.web_running = False
                    success_messages.append("Web服务已停止")
                except Exception as e:
                    messagebox.showerror("错误", f"停止Web服务失败：{str(e)}")

            # 停止WebDAV服务
            if self.webdav_running and self.webdav_server:
                try:
                    self.webdav_server.stop()
                    self.webdav_running = False
                    self.webdav_server = None
                    success_messages.append("WebDAV服务已停止")
                except Exception as e:
                    messagebox.showerror("错误", f"停止WebDAV服务失败：{str(e)}")

            if self.temp_transfer_running:
                self.stop_temporary_transfer(notify=False)
                success_messages.append("临时传输已停止")

            self._service_ports = {}

            # 更新按钮状态
            self.start_button.config(state=tk.NORMAL)
            self.stop_button.config(state=tk.DISABLED)
            self.web_button.config(state=tk.DISABLED)

            # 显示成功消息
            if success_messages and notify:
                messagebox.showinfo("成功", "\n".join(success_messages))

        except Exception as e:
            messagebox.showerror("错误", f"停止服务失败：{str(e)}")

    def start_sftp_server(self, port, username, password, directory, permissions=None):
        """启动真实的 SFTP 子系统，监听全部 IPv4 网卡。"""
        if not SFTP_AVAILABLE:
            raise RuntimeError("SFTP 服务不可用，请安装 paramiko")
        service = SFTPService(port, username, password, directory, permissions)
        service.start()
        self.sftp_server = service
        self.sftp_thread = service.thread
        self.sftp_running = True

    def start_web_server(self, port, directory, password="", readonly=False):
        """启动Web文件服务器"""
        # 创建绑定共享目录的请求处理器
        handler = make_web_handler(directory, password=password, readonly=readonly)

        # 创建服务器
        try:
            self.web_server = ThreadedWebServer(("", port), handler)
        except OSError as e:
            if getattr(e, "winerror", None) == 10048:
                raise OSError(
                    f"端口 {port} 已被其他程序占用，请更换Web端口或关闭占用该端口的程序"
                )
            raise

        # 在新线程中启动服务器
        self.web_thread = threading.Thread(target=self.web_server.serve_forever)
        self.web_thread.daemon = True
        self.web_thread.start()
        self.web_running = True

    def start_webdav_server(self, port, username, password, directory, permissions=None):
        """启动 WebDAV 服务，监听全部 IPv4 网卡。"""
        service = WebDAVService(port, username, password, directory, permissions)
        service.start()
        self.webdav_server = service
        self.webdav_thread = service.thread
        self.webdav_running = True

    def open_web_interface(self):
        """打开Web管理界面"""
        if self.web_running:
            web_port = self.web_port_entry.get()
            webbrowser.open(f"http://127.0.0.1:{web_port}")
        else:
            messagebox.showerror("错误", "Web服务未启动！")

    def start_server(self):
        """保持向后兼容性的方法"""
        self.start_all_services()

    def stop_server(self):
        """保持向后兼容性的方法"""
        self.stop_all_services()

    def load_config(self):
        """加载配置"""
        config = self.config_manager.load_config()

        # 加载FTP端口配置
        self.ftp_port_entry.delete(0, tk.END)
        self.ftp_port_entry.insert(0, config.get("ftp_port", "21"))

        # 加载Web端口配置
        self.web_port_entry.delete(0, tk.END)
        self.web_port_entry.insert(0, config.get("web_port", "8080"))

        # 加载WebDAV端口配置
        self.webdav_port_entry.delete(0, tk.END)
        self.webdav_port_entry.insert(0, config.get("webdav_port", "5005"))

        # 加载Web安全配置
        self.web_password_entry.delete(0, tk.END)
        self.web_password_entry.insert(0, config.get("web_password", ""))
        self.web_readonly_var.set(bool(config.get("web_readonly", False)))

        # 加载服务选择配置
        self.enable_ftp.set(config.get("enable_ftp", True))
        self.enable_sftp.set(config.get("enable_sftp", False))
        self.enable_web.set(config.get("enable_web", False))
        self.enable_webdav.set(config.get("enable_webdav", False))

        # 加载其他配置（保持向后兼容）
        self.max_conn_entry.delete(0, tk.END)
        self.max_conn_entry.insert(0, config.get("max_connections", "100"))

        self.username_entry.delete(0, tk.END)
        self.username_entry.insert(0, config.get("username", "qwe"))

        self.password_entry.delete(0, tk.END)
        self.password_entry.insert(0, config.get("password", ""))

        self.welcome_entry.delete(0, tk.END)
        self.welcome_entry.insert(
            0, config.get("welcome_message", "欢迎访问 Slyar FTP Server!")
        )

        self.goodbye_entry.delete(0, tk.END)
        self.goodbye_entry.insert(0, config.get("goodbye_message", "再见!"))

        self.directory_entry.delete(0, tk.END)
        configured_directory = config.get("directory", os.path.expanduser("~"))
        self.directory_entry.insert(0, configured_directory)
        self.temp_send_dir_var.set(configured_directory)
        self.temp_receive_dir_var.set(configured_directory)
        self.refresh_temp_network_choices()

        perms = config.get("permissions", {})
        self.perm_download.set(perms.get("download", True))
        self.perm_upload.set(perms.get("upload", False))
        self.perm_rename.set(perms.get("rename", False))
        self.perm_delete.set(perms.get("delete", False))
        self.perm_mkdir.set(perms.get("mkdir", False))

        # 根据服务勾选状态刷新端口输入框可用性
        self.update_service_port_states()

    def save_config(self):
        """保存配置"""
        config = {
            # 新的多服务配置
            "ftp_port": self.ftp_port_entry.get(),
            "web_port": self.web_port_entry.get(),
            "webdav_port": self.webdav_port_entry.get(),
            "enable_ftp": self.enable_ftp.get(),
            "enable_sftp": self.enable_sftp.get(),
            "enable_web": self.enable_web.get(),
            "enable_webdav": self.enable_webdav.get(),
            # Web安全配置
            "web_password": self.web_password_entry.get(),
            "web_readonly": self.web_readonly_var.get(),
            # 保持向后兼容的配置
            "port": self.ftp_port_entry.get(),  # 为了向后兼容
            "max_connections": self.max_conn_entry.get(),
            "username": self.username_entry.get(),
            "password": self.password_entry.get(),
            "welcome_message": self.welcome_entry.get(),
            "goodbye_message": self.goodbye_entry.get(),
            "directory": self.directory_entry.get(),
            "permissions": self.get_permission_config(),
        }
        self.config_manager.save_config(config)

    def show_log_window(self):
        """显示日志窗口"""
        log_window = tk.Toplevel(self.root)
        log_window.title("📋 实时日志 - Slyar FTP Server")
        log_window.geometry("900x600")
        log_window.resizable(True, True)
        log_window.configure(bg=self.style.BG_PRIMARY)
        self._apply_icon(log_window)

        # 标题
        title_frame = tk.Frame(log_window, bg=self.style.BG_PRIMARY, height=60)
        title_frame.pack(
            fill=tk.X, padx=self.style.PADDING_LARGE, pady=(self.style.PADDING_LARGE, 0)
        )
        title_frame.pack_propagate(False)

        title_label = tk.Label(
            title_frame,
            text="📋 服务器实时日志",
            font=(self.style.FONT_FAMILY, 16, "bold"),
            bg=self.style.BG_PRIMARY,
            fg=self.style.TEXT_PRIMARY,
        )
        title_label.pack(
            side=tk.LEFT, padx=self.style.PADDING_NORMAL, pady=self.style.PADDING_NORMAL
        )

        # 控制按钮
        control_frame = tk.Frame(title_frame, bg=self.style.BG_PRIMARY)
        control_frame.pack(
            side=tk.RIGHT,
            padx=self.style.PADDING_NORMAL,
            pady=self.style.PADDING_NORMAL,
        )

        # 清空日志按钮
        clear_btn = self.create_modern_button(
            control_frame,
            "🗑️ 清空",
            lambda: self.clear_log_display(log_text),
            self.style.BG_DANGER,
            self.style.BG_DANGER_HOVER,
        )
        clear_btn.pack(side=tk.RIGHT, padx=(self.style.PADDING_TINY, 0))

        # 刷新按钮
        refresh_btn = self.create_modern_button(
            control_frame,
            "🔄 刷新",
            lambda: self.refresh_log_display(log_text),
            self.style.BG_INFO,
            self.style.BG_INFO_HOVER,
        )
        refresh_btn.pack(side=tk.RIGHT, padx=(self.style.PADDING_TINY, 0))

        # 日志显示区域
        log_frame = tk.Frame(log_window, bg=self.style.BG_CARD)
        log_frame.pack(
            fill=tk.BOTH,
            expand=True,
            padx=self.style.PADDING_LARGE,
            pady=self.style.PADDING_NORMAL,
        )

        # 创建滚动文本框
        log_text = scrolledtext.ScrolledText(
            log_frame,
            font=("Consolas", self.style.FONT_SIZE_SMALL),  # 使用等宽字体显示日志
            bg=self.style.BG_INPUT,
            fg=self.style.TEXT_PRIMARY,
            relief="flat",
            bd=0,
            wrap=tk.NONE,  # 不自动换行
            state=tk.DISABLED,
        )
        log_text.pack(
            fill=tk.BOTH,
            expand=True,
            padx=self.style.PADDING_SMALL,
            pady=self.style.PADDING_SMALL,
        )

        # 加载现有日志
        self.load_existing_logs(log_text)

        # 关闭按钮
        button_frame = tk.Frame(log_window, bg=self.style.BG_PRIMARY)
        button_frame.pack(
            fill=tk.X, padx=self.style.PADDING_LARGE, pady=(0, self.style.PADDING_LARGE)
        )

        close_btn = self.create_modern_button(
            button_frame,
            "🚪 关闭",
            log_window.destroy,
            self.style.BG_SECONDARY,
            self.style.BORDER,
        )
        close_btn.pack(side=tk.RIGHT)

        # 启动实时日志更新
        self.start_log_update(log_text, log_window)

    def load_existing_logs(self, log_text_widget):
        """加载现有的日志文件"""
        try:
            log_dir = "logs"
            if not os.path.exists(log_dir):
                return

            # 查找今天的日志文件
            today = datetime.datetime.now().strftime("%Y%m%d")
            log_file = os.path.join(log_dir, f"ftp_server_{today}.log")

            if os.path.exists(log_file):
                with open(log_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    log_text_widget.config(state=tk.NORMAL)
                    log_text_widget.delete(1.0, tk.END)
                    log_text_widget.insert(tk.END, content)
                    log_text_widget.config(state=tk.DISABLED)
                    # 滚动到底部
                    log_text_widget.see(tk.END)
        except Exception as e:
            self.log_error(f"加载日志文件失败: {str(e)}")

    def clear_log_display(self, log_text_widget):
        """清空日志显示"""
        log_text_widget.config(state=tk.NORMAL)
        log_text_widget.delete(1.0, tk.END)
        log_text_widget.config(state=tk.DISABLED)

    def refresh_log_display(self, log_text_widget):
        """刷新日志显示"""
        self.load_existing_logs(log_text_widget)

    def start_log_update(self, log_text_widget, log_window):
        """启动实时日志更新"""

        def update_logs():
            try:
                while not self.log_queue.empty():
                    log_entry = self.log_queue.get_nowait()
                    log_text_widget.config(state=tk.NORMAL)
                    log_text_widget.insert(tk.END, log_entry + "\n")
                    log_text_widget.config(state=tk.DISABLED)
                    # 滚动到底部
                    log_text_widget.see(tk.END)
            except:
                pass

            # 如果窗口还存在，继续更新
            if log_window.winfo_exists():
                log_window.after(500, update_logs)

        # 启动更新循环
        update_logs()

    def hide_to_tray(self):
        """点击窗口关闭按钮：隐藏到系统托盘，服务保持后台运行"""
        if not self.tray_available:
            self.quit_program()
            return
        self.root.withdraw()
        self.log_info("窗口已关闭，程序驻留系统托盘；双击托盘图标可重新打开，右键可退出")
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.notify(
                "程序仍在后台运行\n双击托盘图标打开主窗口，右键托盘图标可退出"
            )

    def show_window(self):
        """从托盘恢复主窗口（托盘线程调用，转发到tk主线程执行）"""
        try:
            self.root.after(0, self._show_window)
        except Exception:
            pass

    def _show_window(self):
        self.root.deiconify()
        try:
            self.root.state("normal")
        except Exception:
            pass
        self.root.lift()
        self.root.focus_force()

    def request_quit(self):
        """托盘菜单触发退出（托盘线程调用，转发到tk主线程执行）"""
        try:
            self.root.after(0, self.quit_program)
        except Exception:
            pass

    def quit_program(self):
        """退出程序"""
        # 移除系统托盘图标
        if getattr(self, "tray", None):
            self.tray.stop()

        # 停止所有服务
        if any(
            [self.ftp_running, self.sftp_running, self.web_running, self.temp_transfer_running]
        ):
            self.stop_all_services(notify=False)

        # 保存配置
        self.save_config()

        # 退出程序
        self.root.quit()


class HeadlessServer:
    """无界面服务器类，用于命令行模式"""

    def __init__(self):
        # 配置管理器
        self.config_manager = ConfigManager()

        # 服务器相关变量
        self.ftp_server = None
        self.sftp_server = None
        self.web_server = None
        self.webdav_server = None
        self.ftp_thread = None
        self.sftp_thread = None
        self.web_thread = None
        self.webdav_thread = None

        # 服务状态
        self.ftp_running = False
        self.sftp_running = False
        self.web_running = False
        self.webdav_running = False

        # 设置日志
        self.setup_logging()

    def setup_logging(self):
        """设置日志系统"""
        # 创建日志目录
        log_dir = "logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        # 配置日志文件
        log_file = os.path.join(
            log_dir, f"ftp_server_{datetime.datetime.now().strftime('%Y%m%d')}.log"
        )

        # 配置logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.FileHandler(log_file, encoding="utf-8"),
                logging.StreamHandler(),
            ],
        )

        # 记录启动日志
        self.logger = logging.getLogger("FTPServer")
        self.logger.info("Slyar FTP Server 启动")
        self.logger.info(f"日志文件: {log_file}")

    def log_info(self, message):
        """记录信息日志"""
        self.logger.info(message)

    def log_error(self, message):
        """记录错误日志"""
        self.logger.error(message)

    def load_config(self):
        """加载配置"""
        return self.config_manager.load_config()

    def start_ftp_service(
        self,
        port=21,
        max_conn=100,
        username="qwe",
        password="",
        directory=os.path.expanduser("~"),
        welcome_msg="欢迎访问 Slyar FTP Server!",
        permissions=None,
    ):
        """启动FTP服务"""
        try:
            if not username or not password or username.lower() == "anonymous":
                raise ValueError("FTP 需要非 anonymous 的用户名和密码")
            # 创建授权器
            authorizer = DummyAuthorizer()
            perms = ftp_permissions_from_config(permissions)
            authorizer.add_user(username, password, directory, perm=perms)

            # 创建处理器
            handler = FTPHandler
            handler.authorizer = authorizer
            handler.banner = welcome_msg

            # 创建服务器
            self.ftp_server = FTPServer(("0.0.0.0", port), handler)
            self.ftp_server.max_cons = max_conn
            self.ftp_server.max_cons_per_ip = max_conn

            # 在新线程中启动服务器
            self.ftp_thread = threading.Thread(target=self.ftp_server.serve_forever)
            self.ftp_thread.daemon = True
            self.ftp_thread.start()

            self.ftp_running = True
            self.log_info(f"FTP服务已启动在端口 {port}")
            return True
        except Exception as e:
            self.log_error(f"启动FTP服务失败：{str(e)}")
            return False

    def start_sftp_service(
        self, port=22, username="qwe", password="",
        directory=os.path.expanduser("~"), permissions=None,
    ):
        """启动 SFTP 服务，启动失败时不报告成功。"""
        if not SFTP_AVAILABLE:
            self.log_error("SFTP 服务不可用，请安装 paramiko")
            return False
        try:
            service = SFTPService(port, username, password, directory, permissions)
            service.start()
            self.sftp_server = service
            self.sftp_thread = service.thread
            self.sftp_running = True
            self.log_info(f"SFTP 服务已启动在端口 {port}")
            return True
        except Exception as e:
            self.log_error(f"启动 SFTP 服务失败：{e}")
            return False

    def start_web_service(
        self,
        port=8080,
        directory=os.path.expanduser("~"),
        password="",
        readonly=False,
    ):
        """启动Web服务"""
        try:
            # 创建绑定共享目录的请求处理器
            handler = make_web_handler(directory, password=password, readonly=readonly)

            # 创建服务器
            self.web_server = ThreadedWebServer(("", port), handler)

            # 在新线程中启动服务器
            self.web_thread = threading.Thread(target=self.web_server.serve_forever)
            self.web_thread.daemon = True
            self.web_thread.start()
            self.web_running = True
            self.log_info(f"Web服务已启动在端口 {port}")
            return True
        except Exception as e:
            self.log_error(f"启动Web服务失败：{str(e)}")
            return False

    def start_webdav_service(
        self,
        port=5005,
        username="qwe",
        password="",
        directory=os.path.expanduser("~"),
        permissions=None,
    ):
        """启动 WebDAV 服务，启动失败时不报告成功。"""
        try:
            service = WebDAVService(port, username, password, directory, permissions)
            service.start()
            self.webdav_server = service
            self.webdav_thread = service.thread
            self.webdav_running = True
            self.log_info(f"WebDAV服务已启动在端口 {service.port}")
            return True
        except Exception as e:
            self.log_error(f"启动WebDAV服务失败：{e}")
            return False

    def stop_all_services(self):
        """停止所有服务"""
        if self.ftp_running and self.ftp_server:
            try:
                self.ftp_server.close_all()
                self.ftp_running = False
                self.log_info("FTP服务已停止")
            except Exception as e:
                self.log_error(f"停止FTP服务失败：{str(e)}")

        if self.sftp_running and self.sftp_server:
            try:
                self.sftp_server.stop()
                self.sftp_running = False
                self.sftp_server = None
                self.log_info("SFTP服务已停止")
            except Exception as e:
                self.log_error(f"停止SFTP服务失败：{str(e)}")

        if self.web_running and self.web_server:
            try:
                self.web_server.shutdown()
                self.web_server.server_close()
                self.web_running = False
                self.log_info("Web服务已停止")
            except Exception as e:
                self.log_error(f"停止Web服务失败：{str(e)}")

        if self.webdav_running and self.webdav_server:
            try:
                self.webdav_server.stop()
                self.webdav_running = False
                self.webdav_server = None
                self.log_info("WebDAV服务已停止")
            except Exception as e:
                self.log_error(f"停止WebDAV服务失败：{str(e)}")

    def get_local_ip(self):
        """获取本机IP地址"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"


def _acquire_single_instance():
    """Windows下用命名互斥体防止重复启动（后台常驻+开机自启动场景容易重复打开）"""
    if sys.platform != "win32":
        return True
    try:
        ctypes.windll.kernel32.CreateMutexW(
            None, False, "SlyarFTPServer_SingleInstance"
        )
        return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _warn_already_running():
    message = (
        "Slyar FTP Server 已在运行中。\n\n"
        "请查看任务栏右下角的托盘图标：\n"
        "双击图标即可打开主窗口。"
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "Slyar FTP Server", 0x40)
    except Exception:
        print("程序已在运行中，请查看系统托盘图标")


def main():
    """主函数，支持命令行参数"""
    # 打包运行时切换到exe所在目录，保证配置、日志路径与开机自启动时一致
    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))

    parser = argparse.ArgumentParser(
        description="Slyar FTP Server - 现代化FTP服务器管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python ftp_server_gui.py              # 启动GUI界面
  python ftp_server_gui.py --hidden     # 后台模式启动（驻留系统托盘）
  python ftp_server_gui.py --ftp        # 仅启动FTP服务
  python ftp_server_gui.py --sftp       # 仅启动SFTP服务
  python ftp_server_gui.py --web        # 仅启动Web服务
  python ftp_server_gui.py --webdav     # 仅启动WebDAV服务
  python ftp_server_gui.py --all        # 启动所有服务
  python ftp_server_gui.py --ftp --web  # 启动FTP和Web服务
        """,
    )

    parser.add_argument("--ftp", action="store_true", help="启动FTP服务")
    parser.add_argument("--sftp", action="store_true", help="启动SFTP服务")
    parser.add_argument("--web", action="store_true", help="启动Web服务")
    parser.add_argument("--webdav", action="store_true", help="启动WebDAV服务")
    parser.add_argument("--all", action="store_true", help="启动所有服务")
    parser.add_argument(
        "--hidden",
        action="store_true",
        help="启动后隐藏主窗口，驻留系统托盘（开机自启动使用）",
    )

    args = parser.parse_args()

    # 如果没有指定任何参数，启动GUI
    if not any([args.ftp, args.sftp, args.web, args.webdav, args.all]):
        if not _acquire_single_instance():
            _warn_already_running()
            return
        root = tk.Tk()
        if args.hidden:
            root.withdraw()
        app = FTPServerGUI(root, start_hidden=args.hidden)
        root.mainloop()
        return

    # 命令行模式
    server = HeadlessServer()

    # 加载配置
    config = server.load_config()

    # 获取配置参数
    ftp_port = int(config.get("ftp_port", 21))
    web_port = int(config.get("web_port", 8080))
    webdav_port = int(config.get("webdav_port", 5005))
    max_conn = int(config.get("max_connections", 100))
    username = config.get("username", "qwe")
    password = config.get("password", "")
    directory = config.get("directory", os.path.expanduser("~"))
    welcome_msg = config.get("welcome_message", "欢迎访问 Slyar FTP Server!")

    # 确定要启动的服务
    start_ftp = args.all or args.ftp
    start_sftp = args.all or args.sftp
    start_web = args.all or args.web
    start_webdav = args.all or args.webdav

    success_count = 0

    # 启动服务
    if start_ftp:
        if server.start_ftp_service(
            ftp_port, max_conn, username, password, directory, welcome_msg,
            config.get("permissions"),
        ):
            success_count += 1

    if start_sftp:
        if server.start_sftp_service(
            ftp_port + 1, username, password, directory,
            config.get("permissions"),
        ):
            success_count += 1

    if start_web:
        if server.start_web_service(
            web_port,
            directory,
            password=str(config.get("web_password", "")),
            readonly=bool(config.get("web_readonly", False)),
        ):
            success_count += 1

    if start_webdav:
        if server.start_webdav_service(
            webdav_port,
            username,
            password,
            directory,
            config.get("permissions"),
        ):
            success_count += 1

    if success_count == 0:
        print("没有成功启动任何服务")
        return

    # 显示服务信息
    ip = server.get_local_ip()
    print(f"\n服务已启动，访问信息：")
    print(f"本机IP: {ip}")

    if server.ftp_running:
        print(f"FTP服务: ftp://{username}@{ip}:{ftp_port}")

    if server.sftp_running:
        print(f"SFTP服务: sftp://{username}@{ip}:{ftp_port + 1}")

    if server.web_running:
        print(f"Web服务: http://{ip}:{web_port}")

    if server.webdav_running:
        print(f"WebDAV服务: http://{ip}:{webdav_port}/（用户名 {username}）")

    print("\n按 Ctrl+C 停止服务")

    try:
        # 保持程序运行
        while True:
            import time

            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止服务...")
        server.stop_all_services()
        print("服务已停止")


if __name__ == "__main__":
    main()
