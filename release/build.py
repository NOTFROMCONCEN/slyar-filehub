#!/usr/bin/env python3
"""跨平台发版构建（Windows / Linux 共用一份脚本）。

产出 5 个资产，文件名统一为 <APP>-<VERSION>-<平台>-amd64[-portable].<ext>：
  windows : .exe（PyInstaller 单文件）、-portable.zip（免安装目录）
  linux   : .deb、.rpm、-portable.tar.gz

用法：
  python release/build.py --target windows --output release/dist
  python release/build.py --target linux   --output release/dist --version 1.2.0

Linux 的 deb/rpm 依赖 fpm（CI 中安装）；本机缺少时只跳过这两个包，
其余资产仍会正常生成，不会让整次构建失败。
"""

import argparse
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
RELEASE = pathlib.Path(__file__).resolve().parent
BUILD = RELEASE / "_build"
DEFAULT_OUTPUT = RELEASE / "dist"

APP = "slyar-filehub"
DISPLAY_NAME = "Slyar FileHub"
ENTRY = "ftp_server_gui.py"
MAINTAINER = "Huameitang <nyanon@vip.qq.com>"
DESCRIPTION = "LAN multi-protocol file transfer toolbox (FTP/SFTP/Web/WebDAV)"
HOMEPAGE = "https://github.com/NOTFROMCONCEN/slyar-filehub"
DATA_FILES = ("app_icon.ico", "ftp_config.example.json", "LICENSE", "README.md")
EXTRA_DOCS = ("使用指南.md", "新手使用教程.md")
SEP = ";" if os.name == "nt" else ":"


def read_version():
    return (RELEASE / "VERSION").read_text(encoding="utf-8").strip()


def run(cmd):
    print("+ " + " ".join(str(part) for part in cmd), flush=True)
    subprocess.check_call([str(part) for part in cmd], cwd=str(ROOT))


def pyinstaller_cmd(mode, workdir, target):
    is_windows = target == "windows"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP,
        f"--{mode}",
        "--distpath", workdir / "dist",
        "--workpath", workdir / "work",
        "--specpath", workdir,
    ]
    # --specpath 会把相对路径基准改到 spec 目录，所有源路径必须用绝对路径
    for name in DATA_FILES:
        cmd += ["--add-data", f"{ROOT / name}{SEP}."]
    if is_windows:
        for hidden in ("pystray._win32", "pystray._util.win32"):
            cmd += ["--hidden-import", hidden]
        cmd += ["--windowed", f"--icon={ROOT / 'app_icon.ico'}"]
    cmd.append(str(ROOT / ENTRY))
    return cmd


def build_payload(target, version):
    """跑两次 PyInstaller，返回 (单文件可执行文件路径, onedir 目录)。"""
    work = BUILD / target
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    for mode in ("onefile", "onedir"):
        workdir = work / mode
        workdir.mkdir(parents=True)
        run(pyinstaller_cmd(mode, workdir, target))

    exe_name = f"{APP}.exe" if target == "windows" else APP
    single = work / "onefile" / "dist" / exe_name
    folder = work / "onedir" / "dist" / APP
    if not single.is_file():
        raise FileNotFoundError(f"未生成单文件产物: {single}")
    if not folder.is_dir():
        raise FileNotFoundError(f"未生成目录产物: {folder}")
    return single, folder


def stage_portable(folder, target, version):
    """把 onedir 产物连同文档组装成便携版目录。"""
    staging = BUILD / target / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    dest = staging / f"{APP}-{version}"
    shutil.copytree(folder, dest)
    # --add-data 的内容在 PyInstaller 6 的 onedir 中位于 _internal/，
    # 用户不易发现；把文档与配置模板再复制一份到解压根目录。
    for name in EXTRA_DOCS + ("README.md", "LICENSE", "ftp_config.example.json"):
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, dest / name)
    if target == "linux":
        launcher = dest / APP
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest


def make_zip(source_dir, output):
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source_dir.rglob("*")):
            archive.write(path, path.relative_to(source_dir.parent).as_posix())
    return output


def make_tarball(source_dir, output):
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(source_dir.rglob("*")):
            archive.add(path, arcname=path.relative_to(source_dir.parent).as_posix(),
                        recursive=False)
    return output


def build_linux_packages(portable_dir, output, version):
    """用 fpm 从同一份目录树生成 deb 与 rpm。"""
    fpm = shutil.which("fpm")
    if not fpm:
        print("SKIP: 未找到 fpm，跳过 deb/rpm（Linux 便携版仍会生成）", flush=True)
        return []

    pkgroot = BUILD / "linux" / "pkgroot"
    if pkgroot.exists():
        shutil.rmtree(pkgroot)
    opt_dir = pkgroot / "opt" / APP
    opt_dir.parent.mkdir(parents=True)
    shutil.copytree(portable_dir, opt_dir)
    (pkgroot / "usr" / "bin").mkdir(parents=True)
    os.symlink(f"/opt/{APP}/{APP}", pkgroot / "usr" / "bin" / APP)
    apps = pkgroot / "usr" / "share" / "applications"
    apps.mkdir(parents=True)
    shutil.copy2(RELEASE / "packaging" / f"{APP}.desktop", apps / f"{APP}.desktop")
    docs = pkgroot / "usr" / "share" / "doc" / APP
    docs.mkdir(parents=True)
    shutil.copy2(ROOT / "LICENSE", docs / "copyright")

    # ico 在 Linux 桌面上不能作为图标，转成 PNG 供桌面启动项使用
    icons = pkgroot / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps"
    icons.mkdir(parents=True)
    try:
        from PIL import Image

        with Image.open(ROOT / "app_icon.ico") as image:
            image.convert("RGBA").save(icons / f"{APP}.png", format="PNG")
    except Exception as exc:
        print(f"WARN: 图标转换失败（不影响安装）: {exc}", flush=True)

    produced = []
    for kind, arch, ext in (("deb", "amd64", "deb"), ("rpm", "x86_64", "rpm")):
        package = output / f"{APP}-{version}-linux-amd64.{ext}"
        run([
            fpm, "-s", "dir", "-t", kind,
            "-n", APP, "-v", version, "-a", arch,
            "--license", "MIT", "--maintainer", MAINTAINER,
            "--description", DESCRIPTION, "--url", HOMEPAGE,
            "--force", "-C", pkgroot, "-p", package, ".",
        ])
        if not package.is_file():
            raise FileNotFoundError(f"未生成 {ext} 包: {package}")
        produced.append(package)
    return produced


def main():
    parser = argparse.ArgumentParser(description="构建 Slyar FileHub 发版资产")
    parser.add_argument("--target", choices=("windows", "linux"), required=True)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--version", default=None, help="覆盖 release/VERSION")
    args = parser.parse_args()

    version = (args.version or read_version()).strip().lstrip("v")
    output = pathlib.Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    print(f"== {DISPLAY_NAME} {version} / {args.target} ==", flush=True)
    single, folder = build_payload(args.target, version)
    portable = stage_portable(folder, args.target, version)

    produced = []
    if args.target == "windows":
        exe = output / f"{APP}-{version}-windows-amd64.exe"
        shutil.copy2(single, exe)
        produced.append(exe)
        archive = output / f"{APP}-{version}-windows-amd64-portable.zip"
        produced.append(make_zip(portable, archive))
    else:
        archive = output / f"{APP}-{version}-linux-amd64-portable.tar.gz"
        produced.append(make_tarball(portable, archive))
        produced.extend(build_linux_packages(portable, output, version))

    print("\n== 产出 ==", flush=True)
    for path in produced:
        print(f"  {path.name}  {path.stat().st_size / 1024 / 1024:.1f} MiB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
