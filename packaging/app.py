# -*- coding: utf-8 -*-
"""AnonSight 桌面版 · 启动器

打包成 exe 之后：**双击 → 起本地服务 → 开一个原生窗口**，
不需要用户装 Python、不需要开浏览器、任务栏上就是它自己的图标。

设计上的三个要点：

1. **数据目录跟着 exe 走，不跟着程序走**。
   PyInstaller 会把代码解到临时目录（`sys._MEIPASS`），往那儿写文件等于丢。
   所以打包版把 `papers/` 放在 **exe 旁边** —— 用户找得着、拷走也能带走
   （绿色版的做法）。源码模式下则维持原样（就在仓库里）。

2. **不用固定端口**。前端用的是同源相对路径，端口是多少它不关心；
   而固定端口会被"用户开了两个实例""别的软件占了 8777"这类事撞上。
   优先试 8777（便于调试），占用就往后找。

3. **双击 `.ano` 能把路径带进来**（Windows 文件关联会把它作为命令行参数传给我们），
   窗口起来后由前端提示"要不要导入这一篇"。
"""
import os
import socket
import sys
import threading

#: 是否运行在 PyInstaller 打出来的包里
FROZEN = bool(getattr(sys, 'frozen', False))
#: 程序所在目录（打包后 = exe 所在目录；源码模式 = 本文件所在目录）
BASE = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
#: 资源目录（打包后是 PyInstaller 的解包目录；源码模式同 BASE）
RES = getattr(sys, '_MEIPASS', BASE)
#: 仓库根（源码模式下比 BASE 高一层）
SRC_ROOT = os.path.dirname(BASE) if not FROZEN else BASE


def notify_shell_assoc_changed():
    """告诉资源管理器「文件关联变了，别再拿旧的图标糊弄人」。

    主人 2026-09-19 报的那个 bug 就是少这一句：注册表写得**完全正确**、
    `.ico` 文件也**好好的**（实测 Windows 能解析它全部 6 层），
    但资源管理器显示的**依然是空白图标**。

    2026-09-19 在本机实测定位（复现脚本 `tools/_tmp/notify_test.py`）：

        · 注册好关联后**不通知** → 图标不变，**等 5 秒也不变**
        · 补一次 SHChangeNotify(ASSOCCHANGED) → **立刻**变成自定义图标

    也就是说 shell 把".ano = 未知类型"这个结论缓存在自己进程里，
    **不会**因为我们改了注册表就自动重读，也不会自己过期。
    写关联的程序必须显式喊这一嗓子。

    ⚠ 这也是为什么"ico 文件换成 BMP 层 / 清 IconCache.db"这类偏方在这里都没用
      —— 那些治的是另外两种病（文件坏、缓存文件脏），不是这一种。
    """
    try:
        import ctypes
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
        return True
    except Exception:
        return False


def register_ano_association():
    """把 `.ano` 关联到本程序 —— 双击 `.ano` 就用 AnonSight 打开。

    主人 2026-09-19：「只要安装这个软件就自动关联啊」。
    绿色版没有安装程序，所以**首次运行时自己注册**。

    四个刻意的选择：
      · 只写 **HKCU**（当前用户）—— 不需要管理员权限，不弹 UAC，也不污染别的用户；
      · **幂等**：每次启动比一下现有值，一样就不写（exe 换位置了会自动更新）；
      · **失败不拦启动** —— 关联不上只是"双击不好使"，不该让整个程序起不来
        （比如某些受管电脑锁了注册表）；
      · 改完**一定通知 shell**（见 `notify_shell_assoc_changed`）——
        少了这句就是主人遇到的那个"图标永远空白"。
    """
    if not FROZEN:
        return 'skip'                      # 源码模式不动注册表
    try:
        import winreg
    except ImportError:
        return 'skip'
    exe = sys.executable
    # 图标取 **exe 旁边**那份：安装器 `installer.iss` 会把 `ano-file.ico` 单独复制到
    # `{app}` 根目录，指过去既规范（不在 `_internal` 那种程序内部结构里），
    # 也少一个"解包目录万一不在"的失败面。取不到再退回包里那份。
    icon_src = ''
    for cand in (os.path.join(BASE, 'ano-file.ico'),
                 os.path.join(RES, 'P0', 'ano-file.ico')):
        if os.path.exists(cand):
            icon_src = cand
            break
    if not icon_src:
        icon_src = exe                       # 实在没有就用 exe 自己的图标，总比没有强
    want = {
        (r'Software\Classes\.ano', ''): 'AnonSight.ano',
        (r'Software\Classes\AnonSight.ano', ''): 'Ano 文档（AnonSight 项目文件）',
        (r'Software\Classes\AnonSight.ano\DefaultIcon', ''): icon_src + ',0',
        (r'Software\Classes\AnonSight.ano\shell\open\command', ''): '"%s" "%%1"' % exe,
    }
    changed = 0
    for (path, name), val in want.items():
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as k:
                try:
                    cur, _ = winreg.QueryValueEx(k, name)
                except FileNotFoundError:
                    cur = None
                if cur != val:
                    winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
                    changed += 1
        except Exception:
            return 'error'
    if changed:
        # 只在**真改了东西**的时候喊 —— 每次启动都广播一遍 ASSOCCHANGED 会让
        # 资源管理器白刷一遍图标缓存（用户看到桌面闪一下），没必要。
        notify_shell_assoc_changed()
    return 'updated(%d)' % changed if changed else 'ok'


def free_port(prefer=8777, tries=20):
    for p in [prefer] + list(range(8800, 8800 + tries)):
        with socket.socket() as s:
            try:
                s.bind(('127.0.0.1', p))
                return p
            except OSError:
                continue
    raise RuntimeError('找不到可用端口（试过 %d 和 8800-%d）' % (prefer, 8799 + tries))


def check_no_personal_data():
    """打包版启动时自检：包里**绝不该有**开发者的个人数据。

    为什么值得专门写一段：这类东西一旦漏进去，是"悄悄跟着发布版发出去"的 ——
    用户在界面上看不出任何异常，而开发者的 API Key / 论文已经躺在别人机器上了。
    宁可启动时大声报错，也不要静默带出去。
    """
    bad = []
    for rel in ('.keys.json', 'papers', 'papers/index.json'):
        p = os.path.join(RES, rel)
        if os.path.exists(p):
            bad.append(rel)
    # 解包目录里出现任何一篇论文目录也算
    pd = os.path.join(RES, 'papers')
    if os.path.isdir(pd) and os.listdir(pd):
        bad.append('papers/（%d 项）' % len(os.listdir(pd)))
    if bad:
        raise SystemExit(
            '✗ 这个安装包里混进了开发者的个人数据，已拒绝启动：%s'
            ' —— 请重新打包（打包白名单见 packaging/app.spec）。'
            % '、'.join(sorted(set(bad))))


def main():
    # 让 `import server` 能找到后端代码
    sys.path.insert(0, RES if FROZEN else SRC_ROOT)
    import server as S

    if FROZEN:
        check_no_personal_data()
        # ★ 这里**以前是一串猴补丁**（改 S.PAPERS / S.STATIC / _A.KEYS 三个全局变量）。
        #   它的毛病是只治了 server 和 adapters 两个文件：`core/translate_ai.py` 里
        #   那条 `ROOT/papers/<id>/notebook.json` 毫不知情，于是打包版一划词就
        #   FileNotFoundError（2026-09-19 主人实测）。
        #   现在路径统一由 `core/paths.py` 提供（RES = 只读资源 / DATA = 用户数据），
        #   **没有全局变量要补**：server.py 和每个 core 模块都是自己从 paths 取的。
        #   留这段注释是为了让下一个想在这里加猴补丁的人先读一读。
        from core import paths as _P
        assert _P.DATA == BASE, '_MEIPASS 之外的数据目录必须是 exe 所在目录'
    os.makedirs(S.PAPERS, exist_ok=True)

    port = free_port()
    S.preload_core()
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(('127.0.0.1', port), S.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 双击 .ano 时，文件路径会作为参数进来（Windows 文件关联就是这么传的）
    argv_file = ''
    for a in sys.argv[1:]:
        if a.lower().endswith('.ano') and os.path.exists(a):
            argv_file = os.path.abspath(a)
            break

    url = 'http://127.0.0.1:%d/' % port
    if argv_file:
        from urllib.parse import quote
        url += '?file=' + quote(argv_file)

    # 双击 .ano 的关联：幂等、只写 HKCU、失败不拦启动
    try:
        print('[assoc]', register_ano_association())
    except Exception as _e:
        print('[assoc] 跳过:', _e)

    import webview
    win = webview.create_window(
        'AnonSight', url,
        width=1440, height=940, min_size=(920, 620),
        text_select=True,
    )
    webview.start()          # 阻塞在这里直到窗口关闭
    try:
        srv.shutdown()
    except Exception:
        pass


if __name__ == '__main__':
    main()
