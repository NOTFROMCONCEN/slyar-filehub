import copy
import json
import os


DEFAULT_PERMISSIONS = {
    "download": True,
    "upload": False,
    "rename": False,
    "delete": False,
    "mkdir": False,
}

# 端口/连接数配置项缺省值（与 GUI 原有的 .get() 回退值保持一致）
DEFAULT_PORTS = {
    "ftp_port": 21,
    "web_port": 8080,
    "webdav_port": 5005,
    "max_connections": 100,
}


class ConfigManager:
    def __init__(self, config_file="ftp_config.json"):
        self.config_file = config_file
        self.default_config = {
            "port": "21",
            "ftp_port": "21",
            "web_port": "8080",
            "webdav_port": "5005",
            "max_connections": "100",
            "username": "qwe",
            "password": "",
            "welcome_message": "欢迎访问 Slyar FTPserver!",
            "goodbye_message": "再见!",
            "directory": os.path.expanduser("~"),
            "web_password": "",
            "web_readonly": False,
            "enable_ftp": True,
            "enable_sftp": False,
            "enable_web": False,
            "enable_webdav": False,
            # 可选加固项，空字符串 = 沿用原有的「监听全部网卡」行为
            "bind_address": "",
            "permissions": copy.deepcopy(DEFAULT_PERMISSIONS),
        }

    @staticmethod
    def _merge(defaults, loaded):
        """浅合并顶层键，并单独深合并 permissions，避免部分配置覆盖整组默认值。"""
        merged = copy.deepcopy(defaults)
        for key, value in (loaded or {}).items():
            if key == "permissions" and isinstance(value, dict):
                merged["permissions"] = {**DEFAULT_PERMISSIONS, **value}
            else:
                merged[key] = value
        return merged

    def load_config(self):
        """加载配置文件"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    if not isinstance(config, dict):
                        raise ValueError("配置文件根节点必须是对象")
                    return self._merge(self.default_config, config)
            except Exception as e:
                print(f"加载配置失败: {e}")
                return copy.deepcopy(self.default_config)
        # 返回副本，避免调用方修改污染共享的默认配置对象
        return copy.deepcopy(self.default_config)

    def save_config(self, config):
        """保存配置文件"""
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存配置失败: {e}")
            return False


def ftp_permissions_from_config(permissions, allow_append=False):
    """GUI 和命令行共用的 FTP 权限映射；未配置写权限时只读。

    allow_append 是可选开关，默认关闭以保持既有权限串完全不变；
    开启后才为可写账户补上 pyftpdlib 的 'a'（APPE）权限。
    """
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
    if allow_append and permissions.get("upload", False) and "a" not in result:
        result += "a"
    return result


def coerce_int(value, default):
    """把配置值安全转为整数，非法值回退到缺省值而不是抛异常。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_ports(config):
    """返回统一的整数端口/连接数映射，不修改传入配置。"""
    config = config or {}
    result = {
        key: coerce_int(config.get(key), fallback)
        for key, fallback in DEFAULT_PORTS.items()
    }
    # 兼容旧配置中只写了 "port" 的情况
    if "ftp_port" not in config and "port" in config:
        result["ftp_port"] = coerce_int(config.get("port"), DEFAULT_PORTS["ftp_port"])
    return result


def _port_error(name, value):
    if not 1 <= value <= 65535:
        return f"{name} 必须是 1-65535 之间的数字（当前 {value}）"
    return None


def validate_service_config(config, services=None, check_directory=True):
    """校验启动参数，返回错误信息列表（空列表表示通过）。纯函数，无副作用。

    services 为 None 时从配置的 enable_* 读取；显式传入可覆盖（命令行模式使用）。
    """
    config = config or {}
    if services is None:
        services = {
            "ftp": bool(config.get("enable_ftp", True)),
            "sftp": bool(config.get("enable_sftp", False)),
            "web": bool(config.get("enable_web", False)),
            "webdav": bool(config.get("enable_webdav", False)),
        }
    ports = normalize_ports(config)
    errors = []
    used = {}

    def reserve(label, value):
        error = _port_error(label, value)
        if error:
            errors.append(error)
            return
        if value in used:
            errors.append(f"{label} 与 {used[value]} 端口冲突（均为 {value}）")
        else:
            used[value] = label

    if services.get("ftp"):
        reserve("FTP 端口", ports["ftp_port"])
    if services.get("sftp"):
        sftp_port = ports["ftp_port"] + 1
        if sftp_port > 65535:
            errors.append("SFTP 端口为 FTP 端口加 1，FTP 端口不能使用 65535")
        else:
            reserve("SFTP 端口", sftp_port)
    if services.get("web"):
        reserve("Web 端口", ports["web_port"])
    if services.get("webdav"):
        reserve("WebDAV 端口", ports["webdav_port"])

    if services.get("ftp") and ports["max_connections"] < 1:
        errors.append("最大连接数必须是正整数")

    if any(services.get(name) for name in ("ftp", "sftp", "webdav")):
        username = str(config.get("username", "") or "").strip()
        if not username or not config.get("password") or username.lower() == "anonymous":
            errors.append("请输入非 anonymous 的账户名称和密码")

    if check_directory:
        directory = config.get("directory") or ""
        if not os.path.isdir(directory):
            errors.append("访问目录不存在或不是文件夹")

    return errors


def security_warnings(config, services=None):
    """返回不改变行为的运行期安全提示，供 GUI 启动时写入日志。"""
    config = config or {}
    if services is None:
        services = {"web": bool(config.get("enable_web", False))}
    warnings = []
    bind_address = str(config.get("bind_address", "") or "").strip()
    if bind_address in ("", "0.0.0.0", "::"):
        warnings.append("服务监听全部网卡；如需限制访问范围可设置 bind_address")
    if services.get("web"):
        if not config.get("web_password"):
            warnings.append("Web 服务未设置访问密码，任何可访问该端口的主机都能进入")
            if not config.get("web_readonly"):
                warnings.append("Web 服务同时处于可写模式，未认证访问者可上传/改名/删除文件")
    return warnings
