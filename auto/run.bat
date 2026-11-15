@echo off
chcp 936 >nul
cd /d "%~dp0"
title 三角洲 · 坐标自动化点击

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
echo                    三角洲 · 坐标自动化点击
echo ============================================================
echo   1   自检（截图 / 坐标 / OCR / 找图，全程不点击）
echo   2   读一次余额，看 OCR 准不准
echo   3   补采「余额」字模（认错时才用）
echo   4   补采「价格」字模（认错时才用）
echo   5   实时看 OCR（30 秒）
echo   6   在仓库区找「空白存储」物品图
echo   ------------------------------------------------------
echo  11   从现成的截图学字模（不方便切窗口时用这个）
echo  12   安装 / 修复运行库（提示缺库时用这个）
echo   ------------------------------------------------------
echo   7   空跑整个闭环（只打印，不点击）
echo   8   实盘运行（真正点击，F9 急停）
echo   9   只跑 A：卖货补余额
echo  10   只跑 B：刷价抢购
echo   0   退出
echo ============================================================
set "ch="
set /p "ch=选择: "

if /i "%ch%"=="1"  call :run --selftest
if /i "%ch%"=="2"  call :run --probe --live
if /i "%ch%"=="3"  call :run --learn balance
if /i "%ch%"=="4"  call :run --learn price
if /i "%ch%"=="5"  call :run --ocr-live
if /i "%ch%"=="6"  call :run --find
if /i "%ch%"=="7"  call :run
if /i "%ch%"=="8"  call :run --live
if /i "%ch%"=="9"  call :run --only a --live
if /i "%ch%"=="10" call :run --only b --live
if /i "%ch%"=="11" goto learnfrom
if /i "%ch%"=="12" goto installdeps
if /i "%ch%"=="0"  exit /b 0
goto menu

:installdeps
cls
echo 调用 装依赖.bat ...
echo.
call "%~dp0装依赖.bat"
goto menu

:learnfrom
cls
echo 从一张现成的 png 截图里学数字字模。
echo 截图必须是「只截了那块数字区域」的图，和 --dump 存出来的格式一致。
echo.
set "pf="
set /p "pf=截图 png 完整路径: "
if not exist "%pf%" (
    echo.
    echo 文件不存在: %pf%
    pause
    goto menu
)
set "dg="
set /p "dg=图上显示的数字（不要逗号，看不出 K 就写 K）: "
if "%dg%"=="" (
    echo.
    echo 数字不能为空。
    pause
    goto menu
)
echo.
%PYEXE% dvc_auto.py --learn-from "%pf%" "%dg%"
echo.
echo ------------------------------------------------------------
pause
goto menu

:run
cls
echo ^> %PYEXE% dvc_auto.py %*
echo.
%PYEXE% dvc_auto.py %*
echo.
echo ------------------------------------------------------------
pause
exit /b 0
