# -*- mode: python ; coding: utf-8 -*-
"""AnonSight 打包配置（PyInstaller）

## 只打"跑起来必需的东西"

**用白名单，不用黑名单** —— 黑名单是"排除你知道的"，白名单是"只放你确认的"。
这个仓库里有大量跟发布无关的东西（规划/交接/调研 md、11 篇论文、验收截图、
开发脚本、样例数据），黑名单漏一条就会把不该发的发出去。

**明确不进包**（主人 2026-09-19 点名）：
  · `papers/`            —— 已分析的全部论文（个人数据）
  · `.keys.json`         —— API Key
  · `调研/` `验收/` `*.md` —— 规划、交接、调研报告
  · `P0/_backup/` `P0/_extract/` `P0/shot-*.png` `P0/*.py` `P0/*.json` `P0/wu2020.pdf`
                          —— 回滚点、截图、开发脚本、样例数据
  · `tools/` `__pycache__/`

**进包**：server.py · core/ · prompts/ · P0 的 11 个运行时文件 · paperide.config.json
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)))

#: `P0/` 里**真正运行时要用**的文件。其余一律不进包。
P0_KEEP = [
    'index.html', 'library.html',                    # 前端两个页面
    'd3.min.js', 'pdf.min.js', 'pdf.worker.min.js',  # 图形与 PDF 渲染
    'anime.min.js',                                  # 动效
    'favicon.ico', 'logo.png', 'logo-ano.png',       # 品牌图标
    'apple-touch-icon.png', 'ano-file.ico',
]

# AI 提示词：缺了分析就跑不了（core 走 hiddenimports，不重复带）
datas = [(os.path.join(ROOT, 'prompts'), 'prompts')]
for _f in P0_KEEP:
    _p = os.path.join(ROOT, 'P0', _f)
    if os.path.exists(_p):
        datas.append((_p, 'P0'))
_cfg = os.path.join(ROOT, 'paperide.config.json')
if os.path.exists(_cfg):
    datas.append((_cfg, '.'))

# ★ 质量闸门 `P0/_extract/pipeline.py` —— **必须进包**（2026-09-19 修）。
#
#   它是「一键分析」的校验器，`core/pipeline_ai.py` 在**模块顶层** `import pipeline`
#   （见那里第 44 行的注释）。这个目录一度被当成"开发脚手架"排除在包外，
#   后果是打包版**一键分析整个是死的**：一键分析一按，`run_analyze` 第一行
#   `from core import pipeline_ai` 就 ModuleNotFoundError —— 而那行在 try 外面，
#   后台线程静默死掉，进度条卡在"运行中"五分钟才报"等太久了"。
#
#   PyInstaller 其实报了 `missing module named pipeline - imported by core.pipeline_ai`
#   （在 packaging/build/app/warn-app.txt 里），但**那只是条警告**，构建照样"成功"。
#
#   ⚠ 只带 `pipeline.py` 一个文件：它给平台用的两个函数 `check()` / `vocab_json()`
#     都只吃传进来的对象，不碰 `_extract/sections/` `_extract/source/` 那些
#     样例数据（那是 Wu2020 的，属于开发材料，不该跟着发布版出去）。
_pchk = os.path.join(ROOT, 'P0', '_extract', 'pipeline.py')
assert os.path.exists(_pchk), \
    '✗ 找不到 P0/_extract/pipeline.py —— 没有它，一键分析在打包版里会直接崩'
datas.append((_pchk, os.path.join('P0', '_extract')))

#: `server.py` 里是**函数内 import**（`from core import sectioner` 这种），
#: 静态分析看不见 → 必须显式告诉 PyInstaller，否则运行时 ModuleNotFoundError。
HIDDEN = [
    'core.adapters', 'core.annotate_ai', 'core.layout', 'core.locate',
    'core.pdftext', 'core.pipeline_ai', 'core.quotefix', 'core.sectioner',
    'core.translate_ai', 'core.ano_pack', 'core.ano_solidify', 'core.paths',
]

#: 明确踢掉的大块头（**确认平台没用到的**，带着只会让 exe 变大）
#:
#: ⚠ 别把 numpy / scipy / sklearn 往这儿加 —— `core/layout.py` 的语义布局
#:   （TfidfVectorizer + TruncatedSVD + MDS）真的在用它们，排掉之后 exe 一跑就
#:   ModuleNotFoundError。**第一版就写错了这一条**，是靠"把平台实际 import 的库
#:   列出来逐一对"才发现的。exe 现在 204 MB，大头就是这三个，但那是功能要的。
#: ⚠ 只放**确认能排**的。`distutils`/`setuptools` 看着像废物，但 PyInstaller
#:   打包过程自己要用 —— 排掉会直接 ValueError 中断（踩过一次：日志里报
#:   'Target module "distutils" already imported as ExcludedModule'，
#:   而外层 exit code 是 0，看退出码根本发现不了）。
EXCLUDES = ['tkinter', 'matplotlib', 'pandas', 'PyQt5', 'PySide2', 'PySide6',
            'IPython', 'pytest', 'notebook', 'jupyter']

a = Analysis(
    ['app.py'],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

# ⚠ 用 **onedir（文件夹模式），不用 onefile**：
#   onefile 每次启动都要把 245 MB 解压到临时目录 —— 实测 **21 秒**才出窗口；
#   onedir 不压缩、直接跑，实测 1-2 秒。代价是产物是个文件夹而不是单个 exe，
#   所以**配一个安装器**（见 installer.iss）把它装到用户选的位置。
#   主人 2026-09-19 定的：「做个安装器，打开安装器一键安装，选择安装位置那种」。
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='AnonSight',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX 压缩会让某些杀软误报，得不偿失
    console=False,             # 不带黑框（出错时想看日志就把这行改成 True）
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, 'P0', 'favicon.ico'),
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name='AnonSight',
)
