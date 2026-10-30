import json
import os


class ConfigManager:
    def __init__(self, config_file="ftp_config.json"):
        self.config_file = config_file
        self.default_config = {
            "port": "21",
            "max_connections": "100",
            "username": "qwe",
            "password": "",
            "welcome_message": "欢迎访问 Slyar FTPserver!",
            "goodbye_message": "再见!",
            "directory": os.path.expanduser("~"),
            "web_password": "",
            "web_readonly": False,
            "webdav_port": "5005",
            "enable_webdav": False,
            "permissions": {
                "download": True,
                "upload": False,
                "rename": False,
                "delete": False,
                "mkdir": False,
            },
        }

    def load_config(self):
        """加载配置文件"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    # 合并默认配置和加载的配置
                    return {**self.default_config, **config}
            except Exception as e:
                print(f"加载配置失败: {e}")
                return self.default_config
        return self.default_config

    def save_config(self, config):
        """保存配置文件"""
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存配置失败: {e}")
            return False


def ftp_permissions_from_config(permissions):
    """GUI 和命令行共用的 FTP 权限映射；未配置写权限时只读。"""
    permissions = permissions or {}
    result = "el"  # 进入目录、列出目录
    for name, flag in (
        ("download", "r"),
        ("upload", "w"),
        ("mkdir", "m"),
        ("delete", "d"),
        ("rename", "f"),
    ):
        if permissions.get(name, name == "download"):
            result += flag
    return result
