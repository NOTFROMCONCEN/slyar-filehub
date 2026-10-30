@echo off
chcp 65001 >nul
title FTP服务器管理工具

echo ========================================
echo    FTP服务器管理工具
echo ========================================
echo.

REM 检查Python是否安装
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到Python，请先安装Python 3.6或更高版本
    echo.
    pause
    exit /b 1
)

echo [信息] 检查依赖库...
pip show pyftpdlib >nul 2>&1
if errorlevel 1 (
    echo [信息] 正在安装pyftpdlib库...
    pip install pyftpdlib
    if errorlevel 1 (
        echo [错误] 安装失败，请手动运行: pip install pyftpdlib
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