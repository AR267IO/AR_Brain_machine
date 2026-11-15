@echo off
chcp 936 >nul
title 格拉默脑机
cd /d "%~dp0"

rem 双击这个文件启动。pythonw 启动时不留黑色控制台窗口。
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   没在 PATH 里找到 python。
  echo   请先装 Python 3.9 以上，安装时记得勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

where pythonw >nul 2>nul
if errorlevel 1 goto useplain
start "" pythonw "server\main.py"
goto done

:useplain
start "" python "server\main.py"

:done
