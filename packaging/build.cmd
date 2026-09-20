@echo off
REM  ⚠ 这个文件**必须是 CRLF 行尾**（2026-09-20 踩到，血泪）：
REM    cmd.exe 执行批处理时是按字节块读文件再跳转的，**LF-only 的文件在长到一定程度后
REM    会从行中间断开**，表现为把 "echo" 当成 "ho"、"exit /b 1" 当成 "1" 去执行：
REM        '--clean' is not recognized as an internal or external command
REM    （同目录的 打开平台.cmd 也是 LF 却能跑，是因为它短 —— 这类 bug 会随文件变长突然出现，
REM      而且报的错跟真正的原因毫无关系，极难查。）
REM    改这个文件请用能保留 CRLF 的编辑器；git 这边没配 .gitattributes，不做自动转换。
chcp 65001 >nul
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
REM  ★ 出厂配置（2026-09-20 起）：**不用再手动改**。
REM      以前这里有一条"打完把 _internal\paperide.config.json 的 adapter 改成
REM      claude-code"的手工步骤 —— 那种"打完包再记得改一个文件"的做法本身就是隐患：
REM      忘了 = 发出去的包默认行为跟你以为的不一样，而且**没人会发现**（2026-09-19
REM      就是这么把一份带 claude-code 的包发出去的，事后全靠人记得）。
REM      现在出厂配置跟仓库根那份**一模一样**（`{"adapter":"openai","ai":{"preset":"deepseek"}}`），
REM      由 app.spec 直接拷进包。用户换哪家在**设置界面**里选（下拉 + 三个输入框），
REM      不再依赖配置文件里写死某一家 —— 所以打包流程里没有任何"手改配置"的步骤了。
REM ============================================================
setlocal
cd /d "%~dp0"

REM  ★ 用哪个 python：**优先 `py -3`（Python 启动器）**。
REM    原因（2026-09-20 实测）：本机 PATH 里 Microsoft Store 的**假别名**
REM    （`...\WindowsApps\python.exe`）排在真 Python 前面，直接敲 `python`
REM    会得到 "Python was not found; run without arguments to install from the
REM    Microsoft Store" —— 而 PATH 里明明装着 Python 3.12。`py` 没有这个别名问题。
set "PY=py -3"
where py >nul 2>nul || set "PY=python"

echo [1/4] 清理上次的产物…
if exist build     rmdir /s /q build
if exist dist      rmdir /s /q dist
if exist installer rmdir /s /q installer

echo [2/4] 打包程序（文件夹模式）…
%PY% -m PyInstaller app.spec --noconfirm --clean
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
%PY% check-export.py
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
