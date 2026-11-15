@echo off
chcp 936 >nul
cd /d "%~dp0"
title 安装运行库

echo ============================================================
echo               安装运行库 / 检查环境
echo ============================================================
echo.
echo 检测 python ...
python --version
if errorlevel 1 (
    echo.
    echo [X] 没找到 python。
    echo     请先安装 Python 3.8 以上版本，安装时务必勾选 Add Python to PATH。
    echo     下载： https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
echo.
echo [1/3] 升级 pip ...
python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
echo [2/3] 安装 numpy 和 pillow ...
python -m pip install numpy pillow -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo.
    echo 镜像源没装上，改用官方源再试一次 ...
    python -m pip install numpy pillow
)
echo.
echo [3/3] 复查 ...
python dvc_deps.py
echo.
echo 结束。按任意键关闭。
pause >nul
