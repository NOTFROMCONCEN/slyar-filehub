@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 打包FTP服务器为EXE

echo ========================================
echo    FTP服务器 - 打包工具
echo ========================================
echo.

python packtoexe.py

echo.
pause