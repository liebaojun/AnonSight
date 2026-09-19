@echo off
REM ============================================================
REM  AnonSight 一键打包（程序 + 安装程序）
REM
REM  产出：
REM    dist\AnonSight\                       程序本体（文件夹模式，启动 1-2 秒）
REM    installer\AnonSight-0.1.0-Setup-preview.exe  发给别人的就是这一个
REM
REM  ⚠ 打包前确认没把个人数据带进去 —— 白名单在 app.spec，
REM     打完之后 exe 启动时还会自检（混进 .keys.json / papers/ 会拒绝启动）。
REM ============================================================
setlocal
cd /d "%~dp0"

echo [1/3] 清理上次的产物…
if exist build     rmdir /s /q build
if exist dist      rmdir /s /q dist
if exist installer rmdir /s /q installer

echo [2/3] 打包程序（文件夹模式）…
python -m PyInstaller app.spec --noconfirm --clean
if errorlevel 1 goto fail
if not exist dist\AnonSight\AnonSight.exe (
  echo ✗ 没产出 dist\AnonSight\AnonSight.exe
  goto fail
)

echo [3/3] 生成安装程序…
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=iscc"
"%ISCC%" installer.iss
if errorlevel 1 goto fail

echo.
echo ============================================
echo  ✓ 全部完成
echo ============================================
echo  程序本体: dist\AnonSight\
echo  安装程序: installer\
dir /b installer
echo.
echo  发给别人只需要 installer 里那一个 exe。
echo  自己验证装一遍：双击它 → 选个目录 → 装完看开始菜单和 .ano 关联。
endlocal
exit /b 0

:fail
echo.
echo ✗ 打包失败，看上面的报错。
endlocal
exit /b 1
