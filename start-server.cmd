@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 本地下载加速服务（别关这个窗口）
if exist "%~dp0dl.exe" (
    "%~dp0dl.exe" --serve --port 6800
) else (
    python "%~dp0dl.py" --serve --port 6800
)
echo.
echo 服务已退出。按任意键关闭。
pause >nul
