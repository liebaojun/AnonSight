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
REM
REM  ★ 开源发布版还多一步（2026-09-19 定的，别忘）：
REM      `dist\AnonSight\_internal\paperide.config.json` 的 adapter 要写成
REM      **claude-code**（仓库里那份是开发机自用的 deepseek，快 17 倍）。
REM      理由：发出去的包面向"装了 Claude Code 的人"，默认 claude-code 才开箱能用；
REM      默认 deepseek 的话，没填 Key 的人一按分析就报"还没配 DeepSeek API Key"，
REM      而界面上**没有切换适配器的开关**，他会卡在那儿。
REM      这一步要放在 ISCC 之前。改完记得重跑一次 ISCC（不用重跑 PyInstaller）。
REM ============================================================
setlocal
cd /d "%~dp0"

echo [1/4] 清理上次的产物…
if exist build     rmdir /s /q build
if exist dist      rmdir /s /q dist
if exist installer rmdir /s /q installer

echo [2/4] 打包程序（文件夹模式）…
python -m PyInstaller app.spec --noconfirm --clean
if errorlevel 1 goto fail
if not exist dist\AnonSight\AnonSight.exe (
  echo ✗ 没产出 dist\AnonSight\AnonSight.exe
  goto fail
)

echo [3/4] 自查：产物里有没有夹带个人数据…
REM ★ 这一步是**闸门**，不是"打完再跑的建议" —— 2026-09-19 开源发布前实测踩到：
REM   `paths.DATA = exe 所在目录`，跑过一次 dist 里的打包版就会在那儿生成 `papers\`；
REM   而安装器原本用通配符打包，**再打一次就把开发者的论文一起发出去了**。
REM   所以自查必须放在"生成安装程序"**之前**，不过就停。
python check-export.py
if errorlevel 1 goto fail

echo [4/4] 生成安装程序…
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
