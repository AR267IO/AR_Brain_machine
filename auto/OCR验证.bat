@echo off
chcp 936 >nul
cd /d "%~dp0"
title OCR 验证

set "PYEXE=python"
where python >nul 2>nul
if errorlevel 1 (
    echo 没在 PATH 里找到 python。
    echo 装一个 Python 3.8+ 并勾选 "Add to PATH"，或者把本文件里的 PYEXE 改成 python 的完整路径。
    pause
    exit /b 1
)

:menu
cls
echo ============================================================
echo                          OCR 验证
echo ============================================================
echo   1   拖框选区域（自己在屏幕上圈一块）
echo   2   直接盯「余额」区域（config 里的 ocr_balance）
echo   3   直接盯「价格」区域（config 里的 ocr_price）
echo   0   退出
echo ============================================================
echo   识别中：F9 重新框选   F8 存当前画面   ESC 退出
echo ============================================================
set "ch="
set /p "ch=选择: "

if /i "%ch%"=="1" (
    %PYEXE% ocr_probe.py
    goto menu
)
if /i "%ch%"=="2" (
    %PYEXE% ocr_probe.py --region ocr_balance
    goto menu
)
if /i "%ch%"=="3" (
    %PYEXE% ocr_probe.py --region ocr_price
    goto menu
)
if /i "%ch%"=="0" exit /b 0
goto menu
