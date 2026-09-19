@echo off
chcp 65001 >nul
title AnonSight - 本地服务（关掉这个窗口 = 停服务）
cd /d "%~dp0"

REM ---------- 找 Python ----------
REM 优先用 py 启动器（Windows 上最可靠），退到 PATH 里的 python
set "PY=py -3"
where py >nul 2>nul
if errorlevel 1 (
  set "PY=python"
  where python >nul 2>nul
  if errorlevel 1 (
    echo.
    echo   ✗ 没找到 Python。请先装 Python 3.9 以上版本：
    echo     https://www.python.org/downloads/
    echo     （安装时记得勾选 "Add Python to PATH"）
    echo.
    pause
    exit /b 1
  )
)

REM ---------- 检查依赖 ----------
%PY% -c "import fitz, numpy, sklearn" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   检测到缺少依赖，正在安装（第一次运行需要一会儿）...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   ✗ 依赖安装失败。可以手动试试：
    echo     %PY% -m pip install PyMuPDF numpy scikit-learn
    echo.
    pause
    exit /b 1
  )
)

echo ============================================================
echo  AnonSight 正在启动...
echo.
echo   平台地址： http://127.0.0.1:8777/
echo   论文库  ： %~dp0papers\
echo.
echo   用法：把 PDF 拖到页面上，自动入库并分节
echo         顶栏「一键分析」自动出思想图
echo         （没有 PDF 可先下载 Release 里的示范文件 .ano 拖进来）
echo.
echo   ★ 关掉这个窗口就等于停服务
echo ============================================================
echo.

start "" "http://127.0.0.1:8777/"
%PY% server.py --port 8777
pause
