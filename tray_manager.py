"""系统托盘图标与开机自启动管理（Windows 托盘，其他平台自动降级）"""

import os
import sys

try:
    import pystray
    from PIL import Image, ImageDraw

    TRAY_AVAILABLE = True
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None
    TRAY_AVAILABLE = False

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import winreg

APP_TITLE = "Slyar FTP Server"
AUTOSTART_VALUE_NAME = "SlyarFTPServer"


class AutoStartManager:
    """通过 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 管理开机自启动"""

    RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def __init__(self, value_name=AUTOSTART_VALUE_NAME):
        self.value_name = value_name
        self.available = IS_WINDOWS

    def get_launch_command(self):
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}" --hidden'
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        python_exe = pythonw if os.path.exists(pythonw) else sys.executable
        script = os.path.abspath(sys.argv[0])
        return f'"{python_exe}" "{script}" --hidden'

    def is_enabled(self):
        if not self.available:
            return False
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self.RUN_KEY_PATH, 0, winreg.KEY_READ
            ) as key:
                winreg.QueryValueEx(key, self.value_name)
            return True
        except OSError:
            return False

    def enable(self):
        if not self.available:
            return False
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self.RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(
                    key, self.value_name, 0, winreg.REG_SZ, self.get_launch_command()
                )
            return True
        except OSError as e:
            print(f"设置开机自启动失败: {e}")
            return False

    def disable(self):
        if not self.available:
            return False
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self.RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, self.value_name)
            return True
        except FileNotFoundError:
            return True
        except OSError as e:
            print(f"关闭开机自启动失败: {e}")
            return False

    def toggle(self):
        if self.is_enabled():
            return self.disable()
        return self.enable()


class TrayManager:
    """系统托盘图标：双击显示主窗口，右键菜单可切换开机自启动和退出"""

    def __init__(self, app):
        self.app = app
        self.autostart = AutoStartManager()
        self.icon = None

    def start(self):
        """启动托盘图标（独立线程，不阻塞tk主循环）"""
        if not TRAY_AVAILABLE:
            self.app.log_warning(
                "未安装 pystray/Pillow，系统托盘功能不可用（pip install pystray pillow）"
            )
            return False
        try:
            self.icon = pystray.Icon(
                "SlyarFTPServer",
                self._load_icon_image(),
                f"{APP_TITLE} - 托盘常驻",
                self._build_menu(),
            )
            self.icon.run_detached()
            return True
        except Exception as e:
            self.app.log_error(f"启动系统托盘失败: {e}")
            self.icon = None
            return False

    def stop(self):
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None

    def notify(self, message, title=APP_TITLE):
        """显示托盘气泡通知"""
        if self.icon is None:
            return
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    def _load_icon_image(self):
        icon_path = getattr(self.app, "_icon_path", None)
        if icon_path and os.path.exists(icon_path):
            try:
                return Image.open(icon_path).convert("RGBA")
            except Exception:
                pass
        image = Image.new("RGBA", (64, 64), (37, 99, 235, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((16, 22, 48, 42), fill=(255, 255, 255, 255))
        draw.rectangle((20, 27, 26, 32), fill=(37, 99, 235, 255))
        draw.rectangle((20, 36, 26, 41), fill=(37, 99, 235, 255))
        return image

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("显示主窗口", self._on_show_window, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "开机自启动",
                self._on_toggle_autostart,
                checked=lambda item: self.autostart.is_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出程序", self._on_quit),
        )

    def _on_show_window(self, icon, item):
        self.app.show_window()

    def _on_toggle_autostart(self, icon, item):
        if self.autostart.toggle():
            state = "已开启" if self.autostart.is_enabled() else "已关闭"
            self.notify(f"开机自启动{state}")
        else:
            self.notify("开机自启动设置失败")
        icon.update_menu()

    def _on_quit(self, icon, item):
        self.app.request_quit()
