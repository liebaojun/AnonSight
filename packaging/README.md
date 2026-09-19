# AnonSight 打包说明

> 这份文件**不进安装包**（它在 `packaging/`，而打包白名单只管 `app.spec` 里列的东西）。

## 怎么打

双击 `build.cmd` —— 一条命令做两件事：打程序 + 生成安装程序。

| 产物 | 路径 |
|---|---|
| 程序本体（文件夹模式） | `dist\AnonSight\AnonSight.exe` |
| **安装程序**（发给别人的） | `installer\AnonSight-0.1.0-安装程序.exe` |

用户不需要装 Python、不需要开浏览器。

## ⚠️ 铁律：打包版绝不能带开发者的个人数据

主人 2026-09-19 原话：「**打包版本一定是要清空一切属于我的 API，让用户用自己的**」。

这类东西漏进去是**最坏的一种 bug** —— 它不报错、界面看不出异常，
而你的 API Key / 论文已经跟着发布版躺在别人机器上了。

所以这里是**三层防线**：

| # | 防线 | 在哪 |
|---|---|---|
| 1 | **白名单**（只放确认要的，不是"排除我知道的"） | `app.spec` 的 `P0_KEEP` + `datas` |
| 2 | **启动自检**：包里若有 `.keys.json` / `papers/` → **拒绝启动并报错** | `app.py` 的 `check_no_personal_data()` |
| 3 | **用户数据落在 exe 旁边**，与开发者完全隔离 | `core/paths.py` 的 `RES` / `DATA` 分离 |

### 资源目录 / 数据目录（2026-09-19 修，别再踩）

打包后 `core/` 跑在 PyInstaller 的解包目录里，而**用户的论文必须落在 exe 旁边**
（重装才不丢）。这两件事以前是靠 `app.py` 猴补丁几个全局变量凑的，结果**只治了
`server.py` 和 `adapters.py` 两个文件** —— `core/translate_ai.py` 里那条
`ROOT/papers/<id>/notebook.json` 毫不知情，于是**打包版一划词就 `FileNotFoundError`**，
自动标注、一键分析同样读不到数据。界面上完全看不出异常，只有真去用才炸。

现在统一由 `core/paths.py` 提供：

| 常量 | 是什么 | 打包后 |
|---|---|---|
| `RES` | 只读程序资源（`prompts/`、`P0/`、出厂配置） | `sys._MEIPASS`（即 `_internal`） |
| `DATA` | 用户数据（`papers/`、`.keys.json`） | **exe 所在目录** |

**规矩：凡是要写盘的东西，路径必须从 `DATA` 出发。** 往 `RES` 写不会当场报错
（解包目录在磁盘上确实可写），但下次启动、下次重装就没了 —— 所以定死，
别再回到"在 `app.py` 里补一个全局变量"那种做法。

**进包的**：`server.py` · `core/` · `prompts/` · `P0` 的 11 个运行时文件 ·
**`P0/_extract/pipeline.py`**（见下，**必须有**）· `paperide.config.json`

**不进包的**：`papers/`（全部论文）· `.keys.json`（API Key）· `调研/` `验收/` `*.md`（规划与交接文档）·
`P0/_backup/` `P0/shot-*.png` `P0/*.py` `P0/*.json` `P0/wu2020.pdf`（截图/脚本/样例数据）·
`P0/_extract/sections/` `P0/_extract/source/`（Wu2020 的样例数据）· `tools/` · `__pycache__/`

> ⚠ **`P0/_extract/pipeline.py` 是个例外，它必须进包。** 它是「一键分析」的校验器
> （`core/pipeline_ai.py` 在**模块顶层** `import pipeline`）。这个目录一度被整体
> 当成"开发脚手架"排除，后果是打包版**一键分析整个是死的**：一按就
> `ModuleNotFoundError`，而且死在 `run_analyze` 第一行（在 `try` 外面），
> 后台线程静默死掉、进度条卡五分钟。
> PyInstaller 其实报了 `missing module named pipeline - imported by core.pipeline_ai`
> （在 `build\app\warn-app.txt` 里），但那**只是一条警告**，构建照样"成功"。
> 平台只用到它的 `check()` / `vocab_json()`，两个都只吃传进来的对象，
> 不碰 `sections/` `source/` 那些样例数据 —— 所以只带这一个文件。

**打完自查一遍**（比信白名单更实在）：

```bash
python packaging/check-export.py                 # 静态：清单 + 字节级搜 key/路径
python packaging/smoke-test.py                   # 实跑：真启动、调接口、再关掉
```

## 用户第一次打开会看到什么

- `papers/` 是**空的**（还没有论文）→ 拖一篇 `.ano` 或 PDF 进去就行
- 顶栏「设置」→ 填自己的 DeepSeek API Key（**不填也能当 `.ano` 阅读器用**）
- 如果用户装了 Claude Code，也可以走本机 CC 那条路（改 `paperide.config.json` 的 `adapter`）

## 还没做的（下一步）

- ~~`.ano` 文件关联~~ → **已做**：安装时写 HKCU（`installer.iss` 的 `[Registry]` 段），
  卸载时 `uninsdeletekey` 干净删掉。运行时的兜底在 `app.py:register_ano_association()`（幂等）。
  ⚠ **写完必须通知 shell**（`SHChangeNotify(SHCNE_ASSOCCHANGED)`）——
  两处都补上了（安装器靠 `ChangesAssociations=yes`，程序靠 `notify_shell_assoc_changed()`）。
  少这一句的症状是**图标永远空白**，而注册表和 `.ico` 文件全是对的，极难查 —— 见下面那张坑表。
- **双击 `.ano` 已经能打开那一篇**：启动器把路径拼成 `?file=`，前端弹确认条 →
  `POST /api/import-local` → 库里已有就**直接打开**（按路径或内容哈希判），没有才导入。
- **代码签名**：没签名的话 Windows SmartScreen 会拦一下（"未知发布者"）。要发给大家用就得买证书。
- **自动更新**：现在没有。
- **多个实例**：已经在运行时再双击一个 `.ano`，会**另起一个窗口**（另一个端口），
  不会复用已有窗口。功能上没问题（两个窗口共享同一个 `papers/`），但用户会觉得乱。
  要做成"单实例 + 把文件转交给已有窗口"得加一层 IPC。

## 打包踩过的坑（都实测过，别重踩）

| 坑 | 症状 | 正解 |
|---|---|---|
| 把 `numpy` / `scipy` 加进排除 | **exe 打得出来、双击也起得来**，但一用「语义布局」就 ModuleNotFoundError | **不能排** —— `core/layout.py` 的 TF-IDF + SVD + MDS 在用它们 |
| 把 `distutils` / `setuptools` 加进排除 | 打包**中途** `ValueError: Target module "distutils" already imported as ExcludedModule` 中断 | **不能排** —— PyInstaller 打包过程自己要用 |
| 用退出码判断成败 | `python -m PyInstaller ... \| tail` 返回的是 **tail 的** 退出码，永远是 0 | **看日志尾部有没有 `Build complete!`**，别只看 exit code |
| 只在源码里改、忘了重打 | 改了 `app.py`（比如加自检）但 exe 还是旧的 | 每次改完都要重打；**打出来的 exe 不会自动跟着源码变** |
| **少一个数据文件** | 打得出来、双击也起得来，但某个功能一用就崩（PyInstaller 只在 `warn-app.txt` 里报一句 `missing module named xxx`，**构建照样"成功"**） | 加进 `app.spec` 的 `datas`；**打完在 `warn-app.txt` 里搜 `missing module`** |
| 写文件关联后不通知 shell | 注册表全对、`.ico` 也正常，但**资源管理器里图标永远是空白**（等多久都不好） | 加 `ChangesAssociations=yes` / 调 `SHChangeNotify(SHCNE_ASSOCCHANGED)`。**换图标工具、清 IconCache.db 都不对症** |
| 自查脚本自己过期 | `check-export.py` 还在找 `dist\AnonSight.exe`（单文件时代的路径），改成 onedir 后**第一步就退出**，"打完必做的第一步"静默失效 | 工具跟着改动一起更新；跑之前先看它有没有正常输出 |
| `\| tail` 吃掉退出码 | 同上（打包失败也显示成功） | 看日志尾部的 `Build complete!` |

**怎么发现"排错了模块"**：把平台实际 import 的库列出来逐个对 ——

```bash
grep -rhn "^import \|^from " server.py core/*.py packaging/app.py \
  | grep -oP "(?<=import )[a-zA-Z_][a-zA-Z0-9_.]*|(?<=from )[a-zA-Z_][a-zA-Z0-9_.]*" \
  | cut -d. -f1 | sort -u
```

现在的依赖就这些：`fitz`(PyMuPDF) · `numpy` · `sklearn` · `webview` + 标准库。

## 打完必做的两步

```bash
python packaging/check-export.py    # ① 静态：包里有没有混进个人数据（清单 + 字节级搜 key/路径）
python packaging/smoke-test.py      # ② 实跑：真启动它，等本地服务起来、调一次接口，再关掉
```

**② 不能省**：打包最常见的翻车就是"打得出来、双击就崩"（缺 hiddenimport / 缺数据文件 /
排错模块），而这些在静态清单里看不出来。

## 为什么不用 Tauri / Electron

考虑过，选了 **PyWebView + PyInstaller**：

- 后端是 Python，**PyInstaller 能把 Python 运行时一起打包进去** → 用户什么都不用装
- Tauri/Electron 套壳的话，还得**另外解决"Python 怎么带上"**（要么让用户装 Python，要么再嵌一个运行时），复杂度翻倍
- 代价是体积大（程序本体 664 MB / 安装包 171 MB，大头是 sklearn+scipy+numpy），换来"什么都不用装"

（Windows 上用系统自带的 WebView2 渲染，Win10/11 都有，不用额外装。）

## 安装器（Inno Setup）

`installer.iss` → 产出 `installer\AnonSight-0.1.0-安装程序.exe`（171 MB）

管四件"运行时做不好"的事：
① **让用户选安装位置** ② 建开始菜单 / 桌面快捷方式
③ **装的时候就写好 `.ano` 关联**（卸载时能干净删掉）
④ 提供卸载

**为什么需要它**：程序用**文件夹模式**（onedir）打包，启动 **1.3 秒**；
如果打单文件（onefile），每次启动都要解压 245 MB —— 那个才是真的慢。
代价是产物变成文件夹，所以配个安装器把它"装"到用户选的位置。

**装 .ano 关联时写的是 HKCU**（当前用户），不需要额外提权；
但装到 `Program Files` 本身要管理员，所以**会弹一次 UAC**。

## ⚠️ 又一个"测量假象"（2026-09-19，主人当场看出来的）

我把文件夹模式的启动量成 **21.2 秒**，正准备去"优化"。主人说：「我看着几乎是秒开啊」。

真因在 `smoke-test.py` 自己身上：第一版用 `urlopen(..., timeout=1.0)` **挨个试 20 个端口**——
光探测就花掉 20 秒，而 exe 其实 **1.3 秒**就绪了。
改法：裸 socket、连接超时 **0.05 秒**、只探 5 个端口、循环间隔 **0.1 秒**。

> **教训与今天前几则同源**：**量出来的数字也可能是假的。**
> 当"测量结果"和"人的直接观察"打架时，**先信人**，回头查测量方法 —— 别急着按假数字去改代码。
