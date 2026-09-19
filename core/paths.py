# -*- coding: utf-8 -*-
"""程序路径的**唯一出处**：把「只读资源」和「用户数据」分成两个目录。

## 为什么非要分开（2026-09-19 打包实测的教训）

源码模式下 `core/` 的上一层就是仓库根，`prompts/` 和 `papers/` 都躺在那里，
一个 `ROOT` 走天下没毛病。**打包之后这两者就分家了**：

    · PyInstaller 把代码解到 `_internal`（`sys._MEIPASS`）—— **只读资源**在那儿；
    · 用户的论文和 API Key **必须落在 exe 旁边**，否则重装 / 升级就全没了。

当时 `app.py` 只改了 `server.PAPERS` 和 `adapters.KEYS` 两个全局变量，
`core/*.py` 里那条 `ROOT/papers` **完全不知情** —— 结果打包版一划词就
`FileNotFoundError: ...\\_internal\\papers\\<id>\\notebook.json`，
自动标注、一键分析同样读不到数据。界面上完全看不出异常，
只有真去用（划个词）才炸。

## 规矩

    RES  ——  只读的程序资源。**只准读**，谁都不许往这里写。
    DATA ——  用户的数据。**凡是要写盘的东西，路径必须从 DATA 出发。**

打包后往 RES 里写不会报错（解包目录在磁盘上是可写的），
但文件会在下次启动、下次重装时**无声无息地消失** —— 所以宁可定死规矩。
"""
import os
import sys

#: 是否运行在 PyInstaller 打出来的包里
FROZEN = bool(getattr(sys, 'frozen', False))

#: `core/` 的上一层。源码模式 = 仓库根；打包后 = `_internal`（没用，别拿它当数据目录）
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 程序所在目录。源码模式 = 仓库根；打包后 = **exe 所在目录**（用户选的那个安装位置）
BASE = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else _HERE

#: 只读资源：`prompts/`、`P0/`、`paperide.config.json`
RES = getattr(sys, '_MEIPASS', _HERE)

#: 用户数据**唯一**的根：`papers/`、`.keys.json`
DATA = BASE


def res(*parts):
    """拼一个只读资源路径（`res('prompts', 'extract.md')`）。"""
    return os.path.join(RES, *parts)


def data(*parts):
    """拼一个用户数据路径（`data('papers', pid, 'graph.json')`）。"""
    return os.path.join(DATA, *parts)


def paper_dir(pid):
    """一篇论文的工作区目录。**唯一的写法**，别在别处再拼一遍 `papers/<id>`。"""
    return data('papers', pid)
