@echo off
chcp 936 >nul
cd /d "%~dp0"
title 三角洲自动化 · 控制台

set "PYEXE=python"
where python >nul 2>nul
if errorlevel 1 (
    echo 没在 PATH 里找到 python。
    echo 先双击「装依赖.bat」，或者把本文件里的 PYEXE 改成 python 的完整路径。
    pause
    exit /b 1
)

%PYEXE% gui.py
if errorlevel 1 (
    echo.
    echo 控制台异常退出，上面有报错信息。
    pause
)