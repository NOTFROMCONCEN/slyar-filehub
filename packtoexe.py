#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FTP服务器管理工具 - 自动打包脚本
使用PyInstaller将程序打包成exe可执行文件
"""

import os
import sys
import subprocess
import shutil


def check_pyinstaller():
    """检查PyInstaller是否已安装"""
    try:
        import PyInstaller

        print("✓ PyInstaller 已安装")
        return True
    except ImportError:
        print("✗ PyInstaller 未安装")
        return False


def install_pyinstaller():
    """安装PyInstaller"""
    print("\n正在安装 PyInstaller...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
        print("✓ PyInstaller 安装成功")
        return True
    except subprocess.CalledProcessError:
        print("✗ PyInstaller 安装失败")
        return False


def clean_build():
    """清理之前的构建文件"""
    dirs_to_remove = ["build", "dist", "__pycache__"]
    files_to_remove = ["ftp_server_gui.spec"]

    print("\n清理旧的构建文件...")
    for dir_name in dirs_to_remove:
        if os.path.exists(dir_name):
            shutil.rmtree(dir_name)
            print(f"  已删除: {dir_name}/")

    for file_name in files_to_remove:
        if os.path.exists(file_name):
            os.remove(file_name)
            print(f"  已删除: {file_name}")


def create_version_file():
    """创建版本信息文件"""
    version_info = """# UTF-8
#
# 版本信息文件

VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 1, 0, 0),
    prodvers=(1, 1, 0, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'080404b0',
        [StringStruct(u'CompanyName', u'Claude Code'),
        StringStruct(u'FileDescription', u'FTP服务器管理工具'),
        StringStruct(u'FileVersion', u'1.1.0.0'),
        StringStruct(u'InternalName', u'FTP Server Manager'),
        StringStruct(u'LegalCopyright', u'Copyright © 2025'),
        StringStruct(u'OriginalFilename', u'FTP服务器.exe'),
        StringStruct(u'ProductName', u'Slyar FTPserver'),
        StringStruct(u'ProductVersion', u'1.1.0.0')])
      ]), 
    VarFileInfo([VarStruct(u'Translation', [2052, 1200])])
  ]
)
"""
    with open("version.txt", "w", encoding="utf-8") as f:
        f.write(version_info)
    print("✓ 版本信息文件已创建")


def pack_onefile():
    """打包为单文件exe"""
    print("\n" + "=" * 60)
    print("开始打包为单文件 EXE...")
    print("=" * 60 + "\n")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",  # 用当前python环境调用，避免PATH上其他环境的pyinstaller
        "--onefile",  # 打包成单个文件
        "--windowed",  # 不显示控制台窗口
        "--name=FTP服务器",  # 输出文件名
        "--clean",  # 清理临时文件
        "--noconfirm",  # 不确认覆盖
        "--add-data",
        "config_manager.py;.",  # 包含配置管理模块
        "--add-data",
        "tray_manager.py;.",  # 包含托盘与自启动管理模块
        "--add-data",
        "app_icon.ico;.",  # 应用图标（窗口标题栏使用）
        "--icon=app_icon.ico",  # EXE文件图标
        "--hidden-import",
        "pystray._win32",  # pystray动态加载的Windows后端
        "--hidden-import",
        "pystray._util.win32",
        "ftp_server_gui.py",  # 主程序
    ]

    try:
        subprocess.check_call(cmd)
        print("\n✓ 单文件打包成功！")
        print(f"✓ 输出位置: {os.path.abspath('dist/FTP服务器.exe')}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n✗ 打包失败: {e}")
        return False


def pack_onedir():
    """打包为单目录exe"""
    print("\n" + "=" * 60)
    print("开始打包为单目录 EXE...")
    print("=" * 60 + "\n")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",  # 用当前python环境调用，避免PATH上其他环境的pyinstaller
        "--onedir",  # 打包成目录
        "--windowed",  # 不显示控制台窗口
        "--name=FTP服务器",  # 输出文件名
        "--clean",  # 清理临时文件
        "--noconfirm",  # 不确认覆盖
        "--add-data",
        "config_manager.py;.",  # 包含配置管理模块
        "--add-data",
        "tray_manager.py;.",  # 包含托盘与自启动管理模块
        "--add-data",
        "app_icon.ico;.",  # 应用图标（窗口标题栏使用）
        "--icon=app_icon.ico",  # EXE文件图标
        "--hidden-import",
        "pystray._win32",  # pystray动态加载的Windows后端
        "--hidden-import",
        "pystray._util.win32",
        "ftp_server_gui.py",  # 主程序
    ]

    try:
        subprocess.check_call(cmd)
        print("\n✓ 单目录打包成功！")
        print(f"✓ 输出位置: {os.path.abspath('dist/FTP服务器/')}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n✗ 打包失败: {e}")
        return False


def show_menu():
    """显示菜单"""
    print("\n" + "=" * 60)
    print("FTP服务器管理工具 - 打包工具")
    print("=" * 60)
    print("\n请选择打包方式：")
    print("  1. 单文件打包 (推荐) - 生成单个exe文件，便于分发")
    print("  2. 单目录打包 - 生成文件夹，启动更快")
    print("  3. 两种方式都打包")
    print("  0. 退出")
    print("=" * 60)


def main():
    """主函数"""
    print("FTP服务器管理工具 - 自动打包脚本\n")

    # 检查并安装PyInstaller
    if not check_pyinstaller():
        choice = input("\n是否安装 PyInstaller? (Y/n): ").strip().lower()
        if choice != "n":
            if not install_pyinstaller():
                print("\n打包中止。")
                return
        else:
            print("\n打包中止。")
            return

    # 清理旧文件
    clean_build()

    # 显示菜单
    while True:
        show_menu()
        choice = input("\n请输入选项 (0-3): ").strip()

        if choice == "1":
            pack_onefile()
            break
        elif choice == "2":
            pack_onedir()
            break
        elif choice == "3":
            pack_onefile()
            print("\n" + "-" * 60 + "\n")
            pack_onedir()
            break
        elif choice == "0":
            print("\n已取消打包。")
            break
        else:
            print("\n无效选项，请重新选择！")

    print("\n" + "=" * 60)
    print("打包完成！")
    print("=" * 60)

    if os.path.exists("dist"):
        print("\n生成的文件位于 dist 目录：")
        for item in os.listdir("dist"):
            print(f"  - {item}")

    print("\n提示：")
    print("  • 首次运行exe可能需要防火墙授权")
    print("  • 使用21端口需要管理员权限")
    print("  • 建议使用2121等高端口避免权限问题")
    print("  • 关闭窗口后程序在系统托盘后台常驻，右键托盘图标可开关开机自启动或退出")
    print("  • 分发时可以附带使用指南.md文件\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n用户中断，打包已取消。")
    except Exception as e:
        print(f"\n发生错误: {e}")
        import traceback

        traceback.print_exc()
