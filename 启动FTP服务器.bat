@echo off
chcp 65001 >nul
cd /d "%~dp0"
title FTP服务器管理工具

echo ========================================
echo    FTP服务器管理工具
echo ========================================
echo.

REM 检查Python是否安装
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到Python，请先安装Python 3.10或更高版本
    echo.
    pause
    exit /b 1
)

echo [信息] 检查依赖库...
REM 主程序顶层无条件导入 pyftpdlib 与 psutil，缺任一都会直接 ModuleNotFoundError
python -c "import pyftpdlib, psutil" >nul 2>&1
if errorlevel 1 (
    echo [信息] 正在按 requirements.txt 安装依赖库...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 安装失败，请手动运行: pip install -r requirements.txt
        pause
        exit /b 1
    )
)

echo [信息] 启动FTP服务器管理工具...
echo.
python ftp_server_gui.py

if errorlevel 1 (
    echo.
    echo [错误] 程序运行出错
    pause
)