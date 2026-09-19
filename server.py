# -*- coding: utf-8 -*-
"""
PaperIDE · 本地服务（P3-a）

替代原来的 `python -m http.server`（纯静态、跑不了任务）。两件事：

  ① 静态托管前端（P0/index.html 那套三栏 + 四种图型，**前端不重写**，只换数据来源）
  ② 跑长任务：拖 PDF 进来 → 存进项目目录 → 抽元数据 → 自动分节 → 落盘
     前端轮询 /api/jobs/<id> 看进度（分节现在是秒级，但接口按长任务设计，
     P3-b 的"逐节抽取"要跑几十次模型调用，必须能看进度、能单节重跑）

目录约定（一篇论文一个目录，见 01-接续文档 §2.2⑦）：
    papers/<id>/paper.pdf          原样存
    papers/<id>/meta.json          标题/作者/页数/收录时间
    papers/<id>/sections.json      自动分节结果（含 sourceText）
    papers/<id>/graph.json         右栏渲染用的主产物（P3-b 产出，现在从 P0 迁过来）
    papers/<id>/notebook.json      翻译缓存（P4-a 产出）
    papers/<id>/annotations.json   标注层（P3-c 产出）

用法：
    python server.py [--port 8777]
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core import paths                     # noqa: E402  （路径的唯一出处，见 core/paths.py）

_STARTED = time.time()
# ★ 数据与资源**分成两个目录**：打包后 `papers/` 在 exe 旁边（用户找得着、拷得走），
#   而 `P0/`(前端) 在 PyInstaller 的解包目录里（只读的程序资源）。
#   以前这里是 `os.path.join(HERE, ...)` 两条 —— 打包后 HERE = `_internal`，
#   于是数据也被指到了 `_internal\papers`（一个重装就没的地方）。
PAPERS = paths.data('papers')
STATIC = paths.res('P0')                   # 前端与它的依赖（d3/pdf.js）都在这
MANIFEST = os.path.join(PAPERS, 'index.json')


# ==================================================================== 论文库
def _load_manifest():
    if os.path.exists(MANIFEST):
        return json.load(open(MANIFEST, encoding='utf-8'))
    return {'papers': []}


def _save_manifest(m):
    os.makedirs(PAPERS, exist_ok=True)
    json.dump(m, open(MANIFEST, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def _count_sections(d):
    """节数**现算**：`sections.json` 是唯一真源。

    ⚠ 别读 `meta.json` 里那份 —— 同一个事实写在两处就一定会漂
    （shi2025 曾长期在论文库列表里显示「78 节」，而它实际只有 14 节）。
    """
    p = os.path.join(d, 'sections.json')
    if not os.path.exists(p):
        return 0
    try:
        return len(json.load(open(p, encoding='utf-8')).get('sections', []))
    except Exception:
        return 0


def paper_dir(pid):
    # id 的白名单里含 `.`，所以 `..` 也匹配得上——虽然文件名是写死的白名单、
    # 且服务只绑 127.0.0.1（拿不到任意文件），但不该留这个口子。
    if not pid or '..' in pid or pid.startswith('.'):
        raise ValueError('非法论文 id：%r' % pid)
    return os.path.join(PAPERS, pid)


# ---------------------------------------------------------------- 论文库管理（P5）
# 这一组只服务于 `P0/library.html`（论文库管理页）的三个写接口：改名 / 排序 / 删除。
# **删除是破坏性的**（`shutil.rmtree` 一删就是整篇的全部产物），所以校验写得比别处严。
_ID_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,119}$')


def valid_paper_id(pid):
    """论文 id 白名单：字母/数字/下划线开头，后面只准 `字母 数字 _ . -`。

    比 `paper_dir()` 那层更严，理由是**调用点的性质不同**：`paper_dir()` 服务的是"读"，
    这里服务的是"删"。多一道 `^[A-Za-z0-9_]` 开头把 `.` 开头的（隐藏目录、`.` `..`）
    全挡在外面，再配一条 `'..' in pid` 防"中间夹着点点"（`a..b` 这种）。
    路径分隔符 `/` `\\` 和 `:` 本来就不在白名单里，所以"往上跳一层"的输入连正则都过不了。"""
    if not pid or len(pid) > 120:
        return False
    if not _ID_RE.match(pid):
        return False
    if '..' in pid or pid.startswith('.'):
        return False
    return True


def safe_paper_dir(pid):
    """把 id 落成**确定就在 `papers/` 下、且是它的直接子目录**的绝对路径；非法返回 None。

    为什么光有白名单还不够：白名单管的是"字符串长得对不对"，管不了"这个目录名在磁盘上
    其实是个符号链接/junction，指向 papers/ 外面"。所以这里再走一遍 realpath：

        papers/ 的真实路径            → base
        realpath(base + id)           → d
        要求 dirname(d) == base       → 即 d 必须是 base 的**直接**子目录

    两者都过，`rmtree(d)` 才可能被调用。这条同时也是"只删 papers/<id>/ 这一层"的保证：
    函数返回的永远是 `papers/` 的直系子目录，不可能更深也不会更浅。"""
    if not valid_paper_id(pid):
        return None
    base = os.path.realpath(PAPERS)
    d = os.path.realpath(os.path.join(base, pid))
    if os.path.dirname(d) != base:
        return None
    return d


def _force_remove(func, path, _exc):
    """rmtree 的回调：删不动就先去只读位再删一次。

    Windows 实测会挡路的正是只读属性——`graph.json.bak` / `annotations.json.bak`
    这类备份文件常被编辑器或 git 留成只读，`rmtree` 到它们就抛 PermissionError，
    整篇论文删一半停住（目录剩半截、索引也没摘），比不删还糟。"""
    try:
        os.chmod(path, 0o700)
        func(path)
    except Exception:
        traceback.print_exc()


def rmtree_force(path):
    """`shutil.rmtree` 的兼容壳：3.12 把回调参数从 `onerror` 改名成了 `onexc`
    且旧名已标废弃，两套都认一下，免得升级 Python 时这里先炸。"""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_remove)
    else:
        shutil.rmtree(path, onerror=_force_remove)


def _manifest_has(pid):
    """在索引里吗——**删除前必须过这一关**：不在索引里的 id 一律不碰磁盘，
    免得一个手滑的请求把 `papers/` 下某个还没登记（或名字打错）的目录带走。"""
    return any(p.get('id') == pid for p in _load_manifest().get('papers', []))


def slugify(t, maxlen=48):
    s = re.sub(r'[^a-z0-9]+', '-', (t or '').lower()).strip('-')
    return s[:maxlen].strip('-') or 'paper'


def unique_id(base):
    m = _load_manifest()
    have = {p['id'] for p in m['papers']}
    if base not in have:
        return base
    i = 2
    while '%s-%d' % (base, i) in have:
        i += 1
    return '%s-%d' % (base, i)


def _plain_line(l):
    """是不是"人话"行：至少两个 ≥3 字母的词，且不像排版残留码。

    为什么要这道：实测猜到过 `MOLCEL9701_grabs 1..1`（Elsevier 的排版残留）、
    `this version posted October 5, 2025. The copyright holder for this preprint`
    （bioRxiv 左侧**竖排**的版权声明）——都不该当标题。"""
    import re as _re
    t = ''.join(s['text'] for s in l['spans']).strip()
    if len(t) < 12:
        return False
    if _re.match(r'^[\w]{4,}\d*_\w+', t):                  # MOLCEL9701_grabs 这种
        return False
    words = _re.findall(r'[A-Za-z]{3,}', t)
    return len(words) >= 2


def _in_main_column(l, page):
    """在正文栏里吗：横排（dir≈(1,0)）且不在左右边距。

    为什么：bioRxiv 的版权声明是**竖排**贴在左边距的，按 y 排序会排到最前面，
    于是"取最上面几行"就取到了它。"""
    d = l.get('dir') or (1.0, 0.0)
    if abs(d[0] - 1.0) > 0.05 or abs(d[1]) > 0.05:
        return False                                   # 竖排（bioRxiv 的版权声明就是竖排贴边距的）
    # 左边界给到 5%：Molecular Cell 的正文栏从 x=53 起（612 页宽的 8.7%），
    # 卡 10% 会把**真标题**也滤掉（实测踩过）。
    return l['bbox'][0] >= page.rect.width * 0.05 and l['bbox'][2] <= page.rect.width * 0.97


def _looks_numbered_heading(t):
    """像带编号的小节标题吗：`2.1 Construction…` / `3.2.1 Effects…` / `1 Introduction`。

    为什么单列这一条：Frontiers / MDPI 这类版式的**二级标题带编号**（"2.1 …"），
    字号只比正文大一点、跨行时行数也够，光看"字号大 + 行数够"就会被当成论文标题
    （Majumdar2024 实测踩过，见 _title_by_fontsize 的注释）。
    已核对不会误伤真标题：`3D …` / `5-HT …` / `2024 …` / `2.5-fold …` 都匹配不上
    （数字后面必须紧跟 `.数字` 组或空白 + 一个非空白字符，且数字最多两位）。"""
    import re as _re
    return bool(_re.match(r'^\s*\d{1,2}(\.\d{1,3})*\.?\s+\S', t))


def _title_by_fontsize(doc):
    """版式一：**第 1 页**里字号明显更大的那几行 = 标题（Cell / Nature 系都是这样）。

    ⚠ 页序必须是 (0, 1)——**先第 1 页**，第 2 页只做兜底。
      原来这里是 (1, 0)（先看第 2 页），2026-09-18 第 5 轮验收抓到的 Majumdar2024 就栽在这：
      那篇是 Frontiers 版式，**第 2 页**最大字号 12.0pt 恰好有 2 行——正是跨行的二级标题
      `2.1 Construction of ITAM restricted murine anti-CD19 CARs`（而且长在页面**底部** y≈84%）。
      旧条件 `len(lines) >= 2 and k >= 12` 当场成立 → 直接 return，
      **第 1 页 21.0pt × 3 行的真标题根本没被看到** → 顶栏 h1 / 浏览器标签 / 论文库三处全错。
      改回 (0, 1) 后再跑 9 篇已收录论文，答案一字不变（tools/check_titles.py 可复跑）。

    候选取值一共 4 道闸，阈值都有实测依据（本库 9 篇 + 验收 7 篇的实测值写在各自注释里）：
      ① 字号 ≥ 12pt 且**至少 2 行**（原有）
      ② 组顶落在页面**上部** ③ 字号**明显大于**本页正文 ④ 文本不像编号小节标题
    """
    import re as _re
    for pno in (0, 1):
        if pno >= doc.page_count:
            continue
        page = doc[pno]
        sizes = {}
        for b in page.get_text('dict')['blocks']:
            if b.get('type') != 0:
                continue
            prev = None                    # 同一 block 里的上一行 (字号, y0, x0)
            for l in b['lines']:
                solid = [s for s in l['spans'] if s['text'].strip()]
                if not solid or not _in_main_column(l, page):
                    prev = None
                    continue
                if l['bbox'][1] < page.rect.height * 0.06:     # 页眉带（doi/版权条）
                    prev = None
                    continue
                s = max(solid, key=lambda s: len(s['text']))
                k = round(s['size'], 1)
                # 标题折行的**末行**常常只有一个短词，_plain_line 会把它当排版残留滤掉，
                # 标题就少一截——实测 shen2026 第 1 页标题共 3 行（18.9pt，行距 20.9pt =
                # 1.11×字号，左端都在 x=53.1），末行只是 "potency"（7 字）→ 被滤掉后
                # 标题成了 "…and antitumor"。所以补一条：紧贴在**同字号上一行**下面、
                # 左端对齐的续行，即使本身很短也收进来。
                cont = (prev is not None and prev[0] == k
                        and 0 < l['bbox'][1] - prev[1] < s['size'] * 2.2
                        and abs(l['bbox'][0] - prev[2]) < 3)
                if not _plain_line(l) and not cont:
                    prev = None
                    continue
                sizes.setdefault(k, []).append(l)
                prev = (k, l['bbox'][1], l['bbox'][0])
        for k in sorted(sizes, reverse=True):
            lines = sizes[k]
            # ① "字号够大 + 行数够"只是入场券：光靠这两条，Majumdar 第 2 页那个 2 行的
            #    二级标题就能通关，所以后面 ③ 还会要求它**明显大于正文**——
            #    "勉强凑够 2 行"的组过不了 ③。
            if len(lines) < 2 or k < 12:
                continue
            # ② 标题在页面**上部**：实测 16 处正确答案的组顶都在页高的 19% 以内
            #    （最靠下的 Majumdar 第 1 页真标题 y=16%，最靠上的 lunger 9.2%）；
            #    而 Majumdar 第 2 页那个误判的 "2.1 …" 在 84%（贴着页面底部）→ 出局。
            #    0.45 这个界沿用兄弟函数 _title_by_position 的"首页上半部分"口径。
            if min(l['bbox'][1] for l in lines) > page.rect.height * 0.45:
                continue
            # ③ 字号要**明显**大于本页正文（正文 = 比候选小、行数最多的那一档）：
            #    实测真标题是正文的 1.64–4.1 倍（最小的一处 = bioRxiv 18.0/11.0 = 1.64），
            #    而像标题的节标题只有 1.30–1.33 倍（12.0/9.0、10.8/8.3）。
            #    两簇的几何中点 √(1.33×1.64) ≈ 1.48 → 阈值取 1.5。
            #    本页只有一档字号时（如 wang2025 的 9.5pt 满页），没有正文可比 → 不启用这条。
            smaller = [k2 for k2 in sizes if k2 < k]
            if smaller:
                body = max(smaller, key=lambda k2: (len(sizes[k2]), k2))
                if k < body * 1.5:
                    continue
            # 跨行拼接：行尾是连字符且下一行以小写字母开头 → **直接粘上**，不插空格。
            #   实测 yue2026 的标题在第 1 页被断成 "…of granulocyte-" / "monocyte …"，
            #   插空格会拼出 "granulocyte- monocyte"（论文库里的真值是 "granulocyte-monocyte"）。
            parts = []
            for l in lines:
                t = ''.join(s['text'] for s in l['spans']).strip()
                if not t:
                    continue
                if parts and parts[-1].endswith('-') and t[:1].islower():
                    parts[-1] += t
                else:
                    parts.append(t)
            txt = ' '.join(parts)
            # ④ 不像编号小节标题（"2.1 …" / "1 Introduction" / "3.2.1 …"）
            if _looks_numbered_heading(txt):
                continue
            if len(txt.strip()) >= 12:
                return _re.sub(r'\s+', ' ', txt).strip()
    return ''


def _title_by_position(doc):
    """版式二：**标题和正文一样大**的投稿版式（bioRxiv 常见）——字号法失效，
    改成按位置取：首页上半部分、**作者行之前**的那几行。

    怎么认出作者行：作者的单位上标让名字后面紧跟数字（`Yuxin Wang1#, Shiman Zuo1#`），
    而标题行不会有 `字母+数字` 这种粘连。"""
    import re as _re
    p = doc[0]
    rows = []
    for b in p.get_text('dict')['blocks']:
        if b.get('type') != 0:
            continue
        for l in b['lines']:
            t = ''.join(s['text'] for s in l['spans']).strip()
            if not t or l['bbox'][1] >= p.rect.height * 0.45:
                continue
            if not _in_main_column(l, p):          # 竖排的版权声明贴在边距，别当标题
                continue
            rows.append((l['bbox'][1], l['bbox'][0], t))
    rows.sort()
    out = []
    for y, _x, t in rows:
        if len(t) < 4:
            continue
        # ⚠ 页顶那条"页眉带"要先跳过：bioRxiv 的 doi/版权声明在 **y≈3**，
        #   比标题（y≈79）还靠上，按 y 取最上面几行必然先取到它。
        if y < p.rect.height * 0.06:
            continue
        if _re.search(r'[A-Za-z]\d', t):
            break                                    # 作者行（带上标）→ 标题到此为止
        if _re.match(r'^(https?://|doi|biorxiv|posted|cc-by|not certified|preprint|'
                     r'this version posted|the copyright holder|available under|'
                     r'creative commons|all rights reserved)', t, _re.I):
            continue
        if _re.search(r'@|\b(university|institute|department|laboratory)\b', t, _re.I):
            continue
        # 版式标签（Molecular Cell 首页的 "Article" / "Authors" / "Correspondence"）不是标题
        if _re.match(r'^(article|authors?|correspondence|research article|original article|'
                     r'review|editorial|letter|report|graphical abstract|highlights)\b', t, _re.I):
            continue
        out.append(t)
        # ⚠ 上限不能卡太紧：标题换行时**每一行都要收**。
        #   卡 60 字会把 "…for next-generation CAR-M" 之后的 "immunotherapies"（第二行）丢掉
        #   （第 4 轮验收抓到的）。真正的刹车是上面的作者行判断，这里只防失控。
        if sum(len(a) for a in out) >= 220 or len(out) >= 4:
            break
    txt = ' '.join(out)
    return _re.sub(r'\s+', ' ', txt).strip() if len(txt.strip()) >= 15 else ''


def extract_meta(pdf_path):
    """元数据：先信 PDF 自带的，没有就从首屏文字里猜（Cell 这类期刊的 XMP 常年是空的）。"""
    import fitz
    doc = fitz.open(pdf_path)
    md = doc.metadata or {}
    title = (md.get('title') or '').strip()
    author = (md.get('author') or '').strip()
    # ⚠ PDF 内嵌的 Title/Author 经常是**垃圾值**：第 3 轮验收那篇 bioRxiv 的 Title 就是 "87447064"、
    #   Author 是 "Administrator"（投稿系统/LaTeX 模板留下的），直接采信的话全站标题就是那串数字。
    #   所以垃圾值要判掉，退回"按版式从首屏文字里猜"。
    import re as _re
    def _junk(s):
        s = (s or '').strip()
        if not s or len(s) < 6:
            return True
        if s.lower().endswith('.pdf'):
            return True
        if _re.fullmatch(r'[\d\s\W_]+', s):        # 纯数字/纯符号：87447064 这种
            return True
        if _re.match(r'^[\w]{4,}\d*_\w+', s):      # MOLCEL9701_grabs 这种排版残留码
            return True
        if s.lower() in ('administrator', 'user', 'unknown', 'untitled', 'microsoft word', 'latex'):
            return True
        # 得**像个人话**：至少两个 ≥3 字母的词（"1..1" "87447064" 这类都过不了）
        if len(_re.findall(r'[A-Za-z]{3,}', s)) < 2:
            return True
        return False
    guess = ''
    if _junk(title):
        guess = _title_by_fontsize(doc) or _title_by_position(doc)
        title = guess or title or os.path.splitext(os.path.basename(pdf_path))[0]
    return {
        'title': re.sub(r'\s+', ' ', title).strip(),
        'authors': author,
        'pdfPages': doc.page_count,
        'titleSource': 'pdf-meta' if (md.get('title') or '').strip() else 'layout-guess',
    }


# ==================================================================== 任务
JOBS = {}
JOBS_LOCK = threading.Lock()


_MOD_MTIME = {}

# 作业开跑前要检查的模块（顺序有讲究：P0 的 validator 要先于 pipeline_ai）
_CORE_MODULES = ['pipeline', 'core.pdftext', 'core.quotefix', 'core.adapters',
                 'core.locate', 'core.layout', 'core.sectioner',
                 'core.pipeline_ai', 'core.annotate_ai', 'core.translate_ai']

# 指纹表里除内核模块之外**还要盯**的文件：它们不是 import 进来的模块（既不能热重载、
# 也不该混进 _CORE_MODULES），但"改了没生效"一样会让验收报告验错代码——验收 02 / 04
# 都栽过这种"服务跑的是旧代码"。这里按**显式路径**写，别按 core 的前缀去拼。
_WATCH_FILES = {
    'server': os.path.abspath(__file__),               # 后端主文件（进程内 import 一次，改了必须重启）
    'P0/index.html': os.path.join(STATIC, 'index.html'),   # 前端**全部**界面逻辑都在这一个文件里
    'P0/library.html': os.path.join(STATIC, 'library.html'),  # 论文库管理页（独立单页）
}


def preload_core():
    """启动时就把内核模块导进来。

    两个用处：① 版本指纹表有内容可显示（懒加载的话启动时是空的）
    ② 第一次请求不用现等导入。"""
    import importlib
    sys.path.insert(0, os.path.join(HERE, 'P0', '_extract'))   # P0 的校验器住这儿
    for n in _CORE_MODULES:
        try:
            importlib.import_module(n)
        except Exception as e:
            print('[preload] %s 导入失败：%s' % (n, e))


def reload_changed_modules():
    """把**改过**的内核模块重新载入，再让作业用上新代码。

    为什么必须做：`server.py` 是进程内 import 一次、没有 reload 的。
    改了 `core/*.py` 而没重启服务，跑的仍然是旧代码——第 2 轮验收就抓到了这个：
    服务 02:19:39 启动、代码 02:22:03 改的，验收者验的其实是**改之前**的代码，
    整份报告的可信度都受影响。这层自动重载把这类"改了没生效"消掉。

    安全前提：这些模块里只有函数和常量，没有需要保活的运行态。
    """
    import importlib
    changed = []
    for name in _CORE_MODULES:
        m = sys.modules.get(name)
        if m is None:
            continue
        f = getattr(m, '__file__', None)
        if not f or not os.path.exists(f):
            continue
        mt = os.path.getmtime(f)
        if _MOD_MTIME.get(name) == mt:
            continue
        try:
            importlib.reload(m)
            _MOD_MTIME[name] = mt
            changed.append(name)
        except Exception as e:
            print('[reload] %s 重载失败（继续用旧的）：%s' % (name, e))
    if changed:
        print('[reload] 已重新载入：%s' % '、'.join(changed))
    return changed


def code_version():
    """给验收的人一个**可核对**的版本指纹：进程什么时候起的、每个核心模块什么时候改的。
    改了代码没重启，这里一眼就看得出（源码 mtime 晚于进程启动时间 = 跑的是旧代码）。

    除了 `_CORE_MODULES`（可热重载的内核模块），还要报 `_WATCH_FILES` 里那两个
    **不是模块**的文件：`server.py`（后端主文件，不重载）和 `P0/index.html`（前端全部
    界面逻辑）。否则"服务跑的是不是旧代码"这条哨兵对它们就是瞎的。"""
    mods = {}
    for name in _CORE_MODULES:
        m = sys.modules.get(name)
        f = getattr(m, '__file__', None) if m else None
        if f and os.path.exists(f):
            mods[name] = time.strftime('%H:%M:%S', time.localtime(os.path.getmtime(f)))
    for name, f in _WATCH_FILES.items():
        if os.path.exists(f):
            mods[name] = time.strftime('%H:%M:%S', time.localtime(os.path.getmtime(f)))
    return {'startedAt': time.strftime('%H:%M:%S', time.localtime(_STARTED)),
            'now': time.strftime('%H:%M:%S'),
            'moduleMtimes': mods,
            'adapter': (json.load(open(os.path.join(HERE, 'paperide.config.json'),
                                       encoding='utf-8')).get('adapter')
                        if os.path.exists(os.path.join(HERE, 'paperide.config.json'))
                        else 'claude-code')}


def skeleton(pid):
    """还没一键分析时的**骨架**：只有节标题和原文，没有图和导读。

    为什么需要它：新论文刚拖进来，"分节"完成了但"抽取"还没做——这时候如果 /graph 404，
    前端就白屏了，用户只会看到"读取数据失败"。改成吐骨架，界面照常出（节 tab、PDF、
    原文都在），只是图区空着并提示去点一键分析。"""
    d = paper_dir(pid)
    sp = os.path.join(d, 'sections.json')
    if not os.path.exists(sp):
        return None
    sec = json.load(open(sp, encoding='utf-8'))
    meta = {}
    mp = os.path.join(d, 'meta.json')
    if os.path.exists(mp):
        meta = json.load(open(mp, encoding='utf-8'))
    return {
        'schemaVersion': 'p3a-skeleton',
        'generatedBy': '自动分节（尚未一键分析）',
        'paper': {'id': pid, 'title': meta.get('title', pid),
                  'citation': meta.get('authors', ''), 'pdf': 'paper.pdf'},
        # ⚠ `pdfPages` 必须跟着走：`pageRange` 的口径不一致（期刊页码 / PDF 页码混着来），
        #   前端显示"第几页"要优先用 `pdfPages`（真实 PDF 页号）。
        #   这是**第四处**组装 section 的地方（另三处在 core/pipeline_ai.py），别只改一处。
        'sections': [{'id': s['id'], 'title': s['title'], 'pageRange': s['pageRange'],
                      'pdfPages': s.get('pdfPages'),
                      'sourceText': s['sourceText'], 'lead': None, 'views': []}
                     for s in sec['sections']],
        'viewsSummary': {}, 'relVocabulary': {'used': []},
        'needsAnalysis': True,
    }


def job_new(kind, paper_id):
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = {'id': jid, 'kind': kind, 'paperId': paper_id, 'state': 'running',
                     'steps': [], 'log': [], 'error': None,
                     'startedAt': time.time(), 'endedAt': None}
    return jid


def job_log(jid, msg):
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if j is not None:
            j['log'].append('%s  %s' % (time.strftime('%H:%M:%S'), msg))
            j['log'] = j['log'][-60:]


def job_set(jid):
    """更新"当前在干什么"：写日志 + 把详情贴到**正在跑的那一步**上。

    ⚠ 老写法是给每条日志都 `job_step(name, 'running')` —— 而 name 是从日志文本切出来的，
    每节都不一样，于是**每一步永远停在"运行中"，打不上勾**（第 3 轮验收实测：
    45 个 step 只有 3 个 done）。正确做法：步骤由 `run_analyze` 显式声明开始/结束，
    这里只负责刷详情。"""
    def _p(msg):
        job_log(jid, msg)
        with JOBS_LOCK:
            j = JOBS.get(jid)
            if not j:
                return
            for s in reversed(j['steps']):
                if s['state'] == 'running':
                    s['detail'] = msg[:64]
                    return
    return _p


def job_step(jid, name, state='done', detail=''):
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if not j:
            return
        if state == 'running':
            j['steps'].append({'name': name, 'state': 'running', 'detail': detail})
        else:
            for s in reversed(j['steps']):
                if s['name'] == name and s['state'] == 'running':
                    s['state'] = state
                    s['detail'] = detail
                    return
            j['steps'].append({'name': name, 'state': state, 'detail': detail})


def job_done(jid, state='done', error=None):
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if j:
            j['state'] = state
            j['error'] = error
            j['endedAt'] = time.time()


def run_ingest(jid, pid, pdf_path):
    reload_changed_modules()
    """拖进来之后跑的活：元数据 → 自动分节 → 落盘。

    **失败就什么都不留。** 这是 2026-09-19 定下来的口径，原因是实测：

      · 以前两步裹在**同一个** try 里、写索引在最后 —— 分节一抛异常，索引不写，
        可 `papers/<id>/` 目录已经建好了：**论文既不在库里、界面上也看不见、还删不掉**。
      · 试着改成"分节失败也入库（0 节）"，把前端拉起来一看：阅读器**不会崩**，
        但中间栏和右栏都是空的（PDF 占位页本来就是按节算出来的），
        等于给用户留了一个"点得开、但里面什么都没有"的条目，比报错还费解。

    所以现在：**分节失败 = 入库失败**，并且把目录清掉（不留半截）。
    用户看到的是明确的失败信息，重拖一次即可 —— 上传的那份原件他本来就有。"""
    from core import sectioner
    d = paper_dir(pid)
    meta = None

    try:
        job_step(jid, '读取元数据', 'running')
        meta = extract_meta(pdf_path)
        meta['id'] = pid
        meta['pdf'] = 'paper.pdf'
        meta['addedAt'] = time.strftime('%Y-%m-%d %H:%M:%S')
        job_step(jid, '读取元数据', 'done', meta['title'][:60])
    except Exception as e:
        traceback.print_exc()
        # 失败也要把**正在跑的那一步**标成 failed —— 否则进度卡上那个圈会一直转
        # （实测：job 都 failed 了，步骤列表里"读取元数据"还挂着 ◌）
        job_step(jid, '读取元数据', 'failed', str(e)[:120])
        job_done(jid, 'failed', '%s: %s' % (type(e).__name__, e))
        return

    meta['sections'] = 0
    try:
        job_step(jid, '自动分节', 'running')
        r = sectioner.sectionize(pdf_path, paper_id=pid)
        json.dump(r, open(os.path.join(d, 'sections.json'), 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
        meta['sections'] = len(r['sections'])
        meta['doc'] = r['doc']
        job_step(jid, '自动分节', 'done', '%d 节' % len(r['sections']))
    except Exception as e:
        traceback.print_exc()
        job_step(jid, '自动分节', 'failed', str(e)[:120])
        # 明确失败：**把目录清掉**。留着一个没进索引的 `papers/<id>/`，
        # 界面上看不见、也删不掉，只会悄悄攒着。
        shutil.rmtree(d, ignore_errors=True)
        job_done(jid, 'failed', '分节失败：%s' % str(e)[:140])
        return

    try:
        json.dump(meta, open(os.path.join(d, 'meta.json'), 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
        m = _load_manifest()
        m['papers'] = [p for p in m['papers'] if p['id'] != pid]
        m['papers'].append({'id': pid, 'title': meta['title'], 'pdf': 'paper.pdf',
                            'pdfPages': meta.get('pdfPages'), 'sections': meta['sections'],
                            'addedAt': meta['addedAt']})
        _save_manifest(m)
        job_done(jid)
    except Exception as e:
        traceback.print_exc()
        job_done(jid, 'failed', '%s: %s' % (type(e).__name__, e))


#: 内容哈希缓存：`路径 → (size, mtime, sha256)`。
#: 为什么要缓存：`find_paper_by_content` 每打开一个 `.ano` 就要跟全库比一遍，
#: 全库 82 MB 的话每次重算 sha256 白等半秒。文件没动（size+mtime 都一样）就直接复用。
_HASH_CACHE = {}


def file_sha256(path):
    """算文件的 sha256（带 size+mtime 缓存）。读不动就返回 None。"""
    import hashlib
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_size, int(st.st_mtime))
    hit = _HASH_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
    except OSError:
        return None
    d = h.hexdigest()
    _HASH_CACHE[path] = (key, d)
    return d


def find_paper_by_content(path, size=None):
    """这个文件是不是**已经在库里**了？是就返回它那篇的 id，否则 None。

    两个判据**都要**，缺一不可：

      · **路径**：用户双击 `papers/<id>/paper.ano` 是最常见的用法 —— 一眼就能认出来，
        而且能认出"库里那篇的 `paper.pdf`"（它和 `.ano` 内容不同，哈希必然对不上）。
      · **内容**：用户手上那份也可能是同一篇的**另一份拷贝**（U 盘、下载目录、
        把库整个拷到别处 —— 绿色版就是拷走就能带走）。这种只能靠内容认。

    比内容时**先比大小**：大小不同就绝不可能同内容，能省掉绝大多数哈希。
    """
    try:
        m = _load_manifest()
    except Exception:
        return None
    known = {p.get('id') for p in m.get('papers', [])}

    # ① 先看**路径**：文件本身就住在 `papers/<id>/` 里 → 就是那一篇。
    #
    #    为什么光有内容哈希不够（2026-09-19 实测）：库里的 `paper.pdf` 和
    #    `paper.ano` **不是同一串字节** —— `.ano` 是拿 PyMuPDF 把底座重写过的，
    #    哈希对不上。于是双击库里的 `paper.pdf` 会**又导入一份**（实测生成了
    #    一个叫 `paper` 的新条目，而它其实只是 shi2025 的原文）。
    #    路径判据在这里既准又便宜。
    rp = os.path.realpath(path)
    base = os.path.realpath(PAPERS)
    parent = os.path.dirname(rp)
    if os.path.dirname(parent) == base:
        pid = os.path.basename(parent)
        if pid in known:
            return pid

    want = file_sha256(path)
    if not want:
        return None
    from core import ano_pack
    for p in m.get('papers', []):
        pid = p.get('id')
        try:
            d = paper_dir(pid)
        except ValueError:
            continue
        if not os.path.isdir(d):
            continue
        # ⚠ 要跟**两份**比，不能只比 `paper_file()` 那一份：
        #   · `paper.ano` 是平台的成品，但它的 PDF 底座被 PyMuPDF **重写过** ——
        #     跟用户手上那份原始 PDF **不是同一串字节**（实测差 1.5 MB）；
        #   · 而入库时保留的 `paper.pdf` 才是**原封不动**那份，用户手上的拷贝
        #     通常和它逐字节相同。
        #   只比 `.ano` 的话，"拖进一篇库里已有论文的原始 PDF"会被判成新论文、
        #   又导入一份（实测③：库外那份拷贝生成了 `a-review-of-morphing-aircraft-2`）。
        cands = []
        try:
            cands.append(ano_pack.paper_file(d))
        except Exception:
            pass
        cands.append(os.path.join(d, 'paper.pdf'))
        for cand in cands:
            if not cand or not os.path.isfile(cand):
                continue
            if size is not None and os.path.getsize(cand) != size:
                continue
            if file_sha256(cand) == want:
                return pid
    return None


def ingest_bytes(data, name):
    """**入库的唯一入口**（上传和"打开本机文件"都走这儿）。

    返回 `(响应对象, HTTP 状态码)` —— 调用点只管 `self._json(*ingest_bytes(...))`。

    两种输入：
      · `.ano` 包（**按内容判定**，不看扩展名）→ 解包；包里带着分析产物的
        **跳过分析、秒级入库**，没有产物的当普通 PDF 走一遍；
      · 普通 PDF → 存盘 + 起一个后台 job 抽元数据/分节。
    """
    # ⚠ 魔数校验 —— 以前连这个都没验（只认扩展名）。PDF 与 .ano 的文件头都是 %PDF-
    if b'%PDF-' not in data[:1024]:
        return {'error': '这个文件不是 PDF（文件头不对）'}, 400

    # ★ 再**真的打开一次**。魔数只看头 4 个字节 —— 一个 `%PDF-1.7` + 4KB 随机字节的
    #   文件照样能过，然后 "入库" 成功、后台抽元数据时才炸，用户看到的是
    #   `FileDataError: Failed to open file '…\\paper.pdf'` 这种英文异常，
    #   而且**留下一个 papers/<id>/ 垃圾目录**（它没进索引，界面上看不见也删不掉）。
    #   （2026-09-19 实测：造一个坏 `.ano` 喂进去，就是这样。）
    #   在这儿拦掉：只花几十毫秒，换来"目录不落盘 + 报错是人话"。
    try:
        import fitz
        _doc = fitz.open(stream=data, filetype='pdf')
        _n = _doc.page_count
        _broken = _doc.is_repaired or not _doc.is_pdf
        _doc.close()
        if _n < 1:
            raise ValueError('0 页')
    except Exception as e:
        return {'error': '这个文件打不开，可能下载不完整或已经损坏（%s）' % str(e)[:60]}, 400

    base = slugify(os.path.splitext(os.path.basename(name))[0])
    pid = unique_id(base)
    d = paper_dir(pid)
    os.makedirs(d, exist_ok=True)

    # ★ 是 Ano 包吗？**不押在扩展名上** —— .pdf 改个名就成 .ano，反之亦然，
    #   判定靠内容（扫载荷标记，不解包）。
    #   ⚠ **必须全文件扫**：名字树被 PyMuPDF 排在文件后部（实测 wu2020 那份在 5.63 MB 处），
    #     只扫前几 MB 会漏判 —— 第一版就是栽在这（.ano 被当成普通 PDF 存成了 paper.pdf，
    #     包里的数据全丢、还要重新跑一遍分析）。全扫 8 MB 实测 0.0 ms（bytes 的 in 是 C 实现），
    #     200 MB 上限也就几十毫秒，不值得为这点时间冒漏判的风险。
    if b'Ano.00-manifest.json' in data:
        from core import ano_pack
        ano_path = os.path.join(d, 'paper.ano')
        with open(ano_path, 'wb') as f:
            f.write(data)
        ur = ano_pack.unpack(ano_path, d)
        if not ur['ok']:
            shutil.rmtree(d, ignore_errors=True)        # **明确失败**：不留半截目录
            return {'error': '这个 .ano 读不了：' + '；'.join(ur['errors'])}, 400
        if os.path.exists(os.path.join(d, 'graph.json')):
            # 包里带着全部分析产物 → **跳过分析，秒级入库**（不重跑、不花钱）
            mp = os.path.join(d, 'meta.json')
            try:
                meta = json.load(open(mp, encoding='utf-8'))
            except Exception:
                meta = {}
            meta.update({'id': pid, 'pdf': 'paper.ano',
                         'addedAt': time.strftime('%Y-%m-%d %H:%M:%S')})
            json.dump(meta, open(mp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            m = _load_manifest()
            m['papers'] = [x for x in m['papers'] if x['id'] != pid]
            m['papers'].append({
                'id': pid, 'title': meta.get('title') or base, 'pdf': 'paper.ano',
                'pdfPages': meta.get('pdfPages') or (ur['manifest'].get('pdf') or {}).get('pages'),
                'sections': _count_sections(d), 'addedAt': meta['addedAt']})
            _save_manifest(m)
            # ⚠ 这里**故意不带 `job` 字段**：活已经干完了，没有任何东西要轮询。
            #   前端必须认这一点（`!r.job` = 已经好了）—— 以前前端不管三七二十一
            #   去轮询 `/api/jobs/undefined`，白转 5 分钟才报"等太久了"。
            return {'id': pid, 'ok': True, 'ano': True, 'bytes': len(data),
                    'meta': {'title': meta.get('title')},
                    'note': '数据都在包里，已跳过分析'}, 202
        # 包里没有分析产物（这篇没分析过）→ 走正常流程（.ano 本身就是 PDF，直接当 PDF 用）
        jid = job_new('ingest', pid)
        threading.Thread(target=run_ingest, args=(jid, pid, ano_path), daemon=True).start()
        return {'id': pid, 'job': jid, 'ano': True, 'bytes': len(data),
                'meta': {'title': base},
                'note': '这个 .ano 里没有分析成果，当普通 PDF 入库'}, 202

    # —— 普通 PDF（原流程，一字未改）——
    pdf_path = os.path.join(d, 'paper.pdf')
    with open(pdf_path, 'wb') as f:
        f.write(data)

    # 同一目录下的历史产物（换 PDF 重来时别留旧数据）
    for f in ('sections.json', 'graph.json', 'meta.json'):
        p = os.path.join(d, f)
        if os.path.exists(p):
            os.remove(p)

    jid = job_new('ingest', pid)
    threading.Thread(target=run_ingest, args=(jid, pid, pdf_path), daemon=True).start()
    return {'id': pid, 'job': jid, 'bytes': len(data),
            'meta': {'title': os.path.splitext(name)[0]}}, 202


def run_resection(jid, pid):
    reload_changed_modules()
    """只用新版分节器重切一遍（改进分节器之后不用重新拖 PDF 进来）。"""
    from core import sectioner, ano_pack
    try:
        d = paper_dir(pid)
        job_log(jid, '重新分节…')
        # 「先找 paper.ano 再找 paper.pdf」—— .ano 自己就是合法 PDF（兼容层），
        # 直接交给分节器即可。见 调研/Ano格式-设计方案.md §8.2 第 6 条。
        r = sectioner.sectionize(ano_pack.paper_file(d), paper_id=pid)
        json.dump(r, open(os.path.join(d, 'sections.json'), 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
        # ⚠ meta.json 也要一起更新：以前这里只改了索引，meta['sections'] 就永远停在上一次
        #   入库时的数字（shi2025 曾长期显示「78 节」，而它实际只有 14 节——论文库列表读的
        #   就是这个数，用户直接看得见）。**同一个事实写在两处，就一定会漂。**
        mp = os.path.join(d, 'meta.json')
        if os.path.exists(mp):
            try:
                meta2 = json.load(open(mp, encoding='utf-8'))
                meta2['sections'] = len(r['sections'])
                meta2['pdfPages'] = (r.get('doc') or {}).get('pdfPages') or meta2.get('pdfPages')
                json.dump(meta2, open(mp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            except Exception:
                traceback.print_exc()
        m = _load_manifest()
        for p in m['papers']:
            if p['id'] == pid:
                p['sections'] = len(r['sections'])
        _save_manifest(m)
        job_step(jid, '自动分节', 'done', '%d 节' % len(r['sections']))
        job_log(jid, '重切完成：%d 节' % len(r['sections']))
        job_done(jid)
    except Exception as e:
        traceback.print_exc()
        job_done(jid, 'failed', '%s: %s' % (type(e).__name__, e))


def run_analyze(jid, pid):
    reload_changed_modules()
    """一键分析（P3-b/c + P4-a 入口 A）：抽取 → 校验重试 → 标注层 → 翻译预热。
    每一步单独 try——**一步挂了不该把前面的成果丢掉**（那些已经落盘了）。"""
    from core import pipeline_ai, annotate_ai, translate_ai
    pdir = paper_dir(pid)
    failed_sections = []
    try:
        job_log(jid, '开始一键分析')
        job_step(jid, '逐节抽取思想图', 'running', '准备中…')
        r = pipeline_ai.analyze(pid, workers=3, progress=job_set(jid))
        job_step(jid, '逐节抽取思想图', 'done' if not r['failed'] else 'failed',
                 '%d/%d 节通过' % (r['ok'], r['total']))
        failed_sections = sorted(r['failed'].keys())
        if r['failed']:
            # 顶层 job 仍是 done（活干完了），但**必须让用户看见失败**——
            # 第一轮验收就抓到这个："9 节里 3 节交白卷，界面却报成功"。
            job_log(jid, '⚠ %d 节没过校验，它们不会出图：%s'
                    % (len(r['failed']), '、'.join(failed_sections)))
            with JOBS_LOCK:
                if JOBS.get(jid) is not None:
                    JOBS[jid]['summary'] = {
                        'sections': r['total'], 'ok': r['ok'],
                        'failed': len(failed_sections), 'failedIds': failed_sections,
                    }
    except Exception as e:
        traceback.print_exc()
        job_log(jid, '抽取阶段出错：%s' % e)
        job_step(jid, '逐节抽取思想图', 'failed', str(e)[:120])
    try:
        job_log(jid, '开始生成标注层…')
        job_step(jid, '生成标注层', 'running', '逐节挑表达…')
        ra = annotate_ai.annotate(pid, progress=job_set(jid))
        job_step(jid, '生成标注层', 'done', '%d 条表达' % ra['count'])
    except Exception as e:
        traceback.print_exc()
        job_log(jid, '标注层出错：%s' % e)
        job_step(jid, '生成标注层', 'failed', str(e)[:120])
    try:
        job_log(jid, '预热翻译缓存…')
        job_step(jid, '预热翻译缓存', 'running', '整句翻译…')
        rt = translate_ai.prewarm(pid, progress=job_set(jid))
        job_step(jid, '预热翻译缓存', 'done', '%d 句整句译文' % rt.get('ok', 0))
    except Exception as e:
        traceback.print_exc()
        job_log(jid, '翻译预热出错：%s' % e)
        job_step(jid, '预热翻译缓存', 'failed', str(e)[:120])

    # ★ 产出 .ano —— **平台的存储格式就是它**（主人 2026-09-19 定的流程：
    #   "入库 PDF 作为裸文章进行智能分析，然后再产出 ano 文件"）。
    #   已有 .ano 的走 solidify（增量、保住外部阅读器打的注释）；
    #   没有的走 pack（首次打包，把 PDF 底座 + 载荷 + 高亮封成一个文件）。
    #   ⚠ **打包失败不算分析失败** —— 分析成果早就落盘了，这里只是"封装"，
    #     失败了用户照样能读能改，下次再跑一遍就好。
    try:
        job_log(jid, '打包成 .ano…')
        job_step(jid, '打包成 .ano', 'running', '把分析成果封进一个文件…')
        from core import ano_pack, ano_solidify
        dest = os.path.join(pdir, 'paper.ano')
        if os.path.exists(dest):
            rep = ano_solidify.solidify(dest, pdir)
            via = '固化'
        else:
            rep = ano_pack.pack(ano_pack.paper_file(pdir), pdir, dest, pid=pid)
            via = '首次打包'
        if rep['ok']:
            job_step(jid, '打包成 .ano', 'done',
                     '%s · %.1f MB · %d 条标注'
                     % (via, rep['bytes'] / 1048576, rep.get('wrote_ai', 0) + rep.get('wrote_marks', 0)))
        else:
            job_step(jid, '打包成 .ano', 'failed', '；'.join(rep['errors'])[:120])
            job_log(jid, '⚠ .ano 没打成（不影响已落盘的分析成果）：%s' % '；'.join(rep['errors'])[:160])
    except Exception as e:
        traceback.print_exc()
        job_log(jid, '打包 .ano 出错（不影响分析成果）：%s' % e)
        job_step(jid, '打包成 .ano', 'failed', str(e)[:120])
    job_done(jid)


# ==================================================================== HTTP
class Handler(SimpleHTTPRequestHandler):
    server_version = 'PaperIDE/0.3'

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), fmt % args))

    # -------------------------------------------------- 小工具
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype=None):
        if not os.path.exists(path):
            return self._json({'error': '文件不在了', 'path': path}, 404)
        data = open(path, 'rb').read()
        self.send_response(200)
        self.send_header('Content-Type', ctype or _ctype(path))
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _pdir(self, pid):
        """非法 id 返回一个**一定不存在的路径**（而不是 None）——
        调用点都是 `os.path.isdir(self._pdir(pid))`，给 None 会抛 TypeError 变成 500。"""
        try:
            return paper_dir(pid)
        except ValueError:
            return os.path.join(PAPERS, '__invalid__')

    # -------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        if path == '/api/version':
            return self._json(code_version())
        # ★ 设置界面用：只关心"AI 有没有配 key"，**不回传 key 本身**
        #   （没必要让它在网络上多走一趟；也给个打码提示让用户认出自己填的是哪个）
        if path == '/api/settings':
            from core import adapters
            k = adapters.load_key('deepseek')
            cfg = {}
            try:
                cfg = json.load(open(adapters.CONFIG, encoding='utf-8'))
            except Exception:
                pass
            return self._json({
                'adapter': cfg.get('adapter') or 'claude-code',
                'hasKey': bool(k),
                'keyHint': (k[:7] + '…' + k[-4:]) if len(k) > 14 else ('已配置' if k else ''),
            })
        if path == '/api/papers':
            m = _load_manifest()
            for p in m['papers']:          # "分析过没有"按磁盘上的 graph.json 现算，别存（会漂移）
                p['analyzed'] = os.path.exists(os.path.join(paper_dir(p['id']), 'graph.json'))
            return self._json(m)
        if path == '/api/library':
            # 论文库管理页专用：`/api/papers` 的**只读加料版**（标题/节数/页数/入库时间
            # 一样，另加 `authors` 和 `analyzed`）。为什么另开一条而不是改 /api/papers：
            # 阅读器的论文库弹窗正吃着那条接口，动它就有连带风险；这条是纯新增，老接口
            # 一个字节没变。`authors` 是**现读 meta.json**（不是抄一份存进索引）——
            # 索引里再存一份作者，改标题时就有第二个源要同步，迟早漂。
            m = _load_manifest()
            items = []
            for p in m.get('papers', []):
                try:
                    d = paper_dir(p['id'])
                except ValueError:
                    continue
                meta = {}
                mp = os.path.join(d, 'meta.json')
                if os.path.exists(mp):
                    try:
                        meta = json.load(open(mp, encoding='utf-8'))
                    except Exception:
                        traceback.print_exc()
                items.append({
                    'id': p['id'],
                    'title': meta.get('title') or p.get('title') or p['id'],
                    'authors': meta.get('authors') or '',
                    'sections': p.get('sections', 0),
                    'pdfPages': p.get('pdfPages') or meta.get('pdfPages'),
                    'addedAt': p.get('addedAt') or meta.get('addedAt') or '',
                    # 「分析过没有」按磁盘上的 graph.json 现算，别存（会漂）——同 /api/papers
                    'analyzed': os.path.exists(os.path.join(d, 'graph.json')),
                    'dirExists': os.path.isdir(d),
                })
            return self._json({'papers': items})
        if path == '/api/jobs':
            with JOBS_LOCK:
                return self._json({'jobs': list(JOBS.values())})
        m = re.match(r'^/api/jobs/([0-9a-f]+)$', path)
        if m:
            with JOBS_LOCK:
                j = JOBS.get(m.group(1))
            return self._json(j or {'error': '这个任务已经结束了'}, 200 if j else 404)
        m = re.match(r'^/api/papers/([\w\-.]+)/(\w+)$', path)
        if m:
            pid, what = m.group(1), m.group(2)
            d = self._pdir(pid)
            if not os.path.isdir(d):
                return self._json({'error': '找不到这一篇：%s' % pid}, 404)
            if what == 'graph':
                gp = os.path.join(d, 'graph.json')
                if not os.path.exists(gp):          # 还没分析 → 给骨架，别让前端白屏
                    sk = skeleton(pid)
                    if sk:
                        return self._json(sk)
                # ⚠ graph.json 里的 paper.title 是**生成那一刻**抄进去的副本。
                #   meta.json 后来纠正了标题（垃圾元数据判定），graph 不会跟着变——
                #   于是界面顶上还是 `87447064`（第 4 轮验收抓到的就是这个：改了 meta，
                #   前端读的却是 graph）。**出口处统一以 meta.json 为准**，从根上消掉双源不一致。
                try:
                    g = json.load(open(gp, encoding='utf-8'))
                    mp = os.path.join(d, 'meta.json')
                    if os.path.exists(mp):
                        meta = json.load(open(mp, encoding='utf-8'))
                        if meta.get('title') and g.get('paper', {}).get('title') != meta['title']:
                            g.setdefault('paper', {})['title'] = meta['title']
                            g['paper']['citation'] = meta.get('authors', '') or g['paper'].get('citation', '')
                    return self._json(g)
                except Exception:
                    traceback.print_exc()
                    return self._file(gp)
            if what == 'sections':
                return self._file(os.path.join(d, 'sections.json'))
            if what == 'meta':
                return self._file(os.path.join(d, 'meta.json'))
            # 还没分析过的论文没有这两份文件。**返回空结构而不是 404**——
            # 404 会在浏览器控制台留两条红字，用户看着像坏了（验收报告缺陷 8）。
            # 前端拿到空结构会安静地不显示对应入口，这才是想要的行为。
            if what == 'notebook':
                p = os.path.join(d, 'notebook.json')
                if not os.path.exists(p):
                    return self._json({'paperId': pid, 'glossary': [], 'sentences': [],
                                       'excerpts': [], 'empty': True})
                return self._file(p)
            if what == 'annotations':
                p = os.path.join(d, 'annotations.json')
                if not os.path.exists(p):
                    return self._json({'paper': pid, 'count': 0, 'items': [], 'empty': True})
                return self._file(p)
            if what == 'pdf':
                # 前端取 PDF 走这条。`.ano` 本身就是 PDF，直接给它 —— 磁盘上不必再留一份
                from core import ano_pack
                return self._file(ano_pack.paper_file(d), 'application/pdf')
            return self._json({'error': '没有这个资源：%s' % what}, 404)
        if path in ('/', '/index.html'):
            return self._file(os.path.join(STATIC, 'index.html'), 'text/html; charset=utf-8')
        # 论文库管理页。裸 `/library` 也认（和 `/` 认 `/index.html` 一个口径）；
        # 剩下的静态文件（d3.min.js / pdf.min.js 这些）继续走 SimpleHTTPRequestHandler。
        if path in ('/library', '/library.html'):
            return self._file(os.path.join(STATIC, 'library.html'), 'text/html; charset=utf-8')
        return super().do_GET()

    # -------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        # 存 API Key（设置界面用）。传空值 = 清除。
        if path == '/api/settings/apikey':
            n = int(self.headers.get('Content-Length') or 0)
            try:
                body = json.loads(self.rfile.read(n).decode('utf-8'))
            except Exception as e:
                return self._json({'error': '不是合法 JSON: %s' % e}, 400)
            from core import adapters
            v = adapters.save_key('deepseek', body.get('key') or '')
            return self._json({'ok': True, 'hasKey': bool(v),
                               'keyHint': (v[:7] + '…' + v[-4:]) if len(v) > 14 else ('已配置' if v else '')})

        # 一键分析
        m = re.match(r'^/api/papers/([\w\-.]+)/analyze$', path)
        if m:
            pid = m.group(1)
            if not os.path.isdir(self._pdir(pid)):
                return self._json({'error': '找不到这一篇：%s' % pid}, 404)
            # 同一篇正在跑就复用那个 job——用户手快点了两下不该跑两遍
            # （两遍会互相覆盖 ai/<sec>.json，还会白烧一倍的钱）
            with JOBS_LOCK:
                for j in JOBS.values():
                    if j['paperId'] == pid and j['kind'] == 'analyze' and j['state'] == 'running':
                        return self._json({'id': pid, 'job': j['id'], 'reused': True}, 200)
            jid = job_new('analyze', pid)
            threading.Thread(target=run_analyze, args=(jid, pid), daemon=True).start()
            return self._json({'id': pid, 'job': jid}, 202)

        # 重新分节（分节器改进后不用重新上传 PDF）
        m = re.match(r'^/api/papers/([\w\-.]+)/resection$', path)
        if m:
            pid = m.group(1)
            d = self._pdir(pid)
            if not os.path.isdir(d):
                return self._json({'error': '找不到这一篇：%s' % pid}, 404)
            jid = job_new('resection', pid)
            threading.Thread(target=run_resection, args=(jid, pid), daemon=True).start()
            return self._json({'id': pid, 'job': jid}, 202)

        # 阅读时的即时翻译（P4-a 入口 B）
        m = re.match(r'^/api/papers/([\w\-.]+)/translate$', path)
        if m:
            pid = m.group(1)
            n = int(self.headers.get('Content-Length') or 0)
            try:
                body = json.loads(self.rfile.read(n).decode('utf-8'))
            except Exception as e:
                return self._json({'error': '不是合法 JSON: %s' % e}, 400)
            text = (body.get('text') or '').strip()
            if not text:
                return self._json({'error': 'text 不能为空'}, 400)
            from core import translate_ai
            try:
                r = translate_ai.translate(pid, text, body.get('sentence'),
                                           body.get('page'), body.get('sectionId'))
            except Exception as e:
                traceback.print_exc()
                return self._json({'error': '%s: %s' % (type(e).__name__, e)}, 502)
            return self._json(r)

        # 改标题（论文库管理页）。写两处：`papers/<id>/meta.json` + `papers/index.json`。
        # 为什么必须两处都写：meta.json 是**真源**（阅读器的 h1 / 浏览器标签 /
        # `/graph` 出口都从它取），index.json 是列表页读的那份——只改一处，
        # 列表和阅读器就会显示两个标题（第 4 轮验收抓到的双源漂移）。
        m = re.match(r'^/api/papers/([^/]+)/rename$', path)
        if m:
            pid = unquote(m.group(1))
            d = safe_paper_dir(pid)
            if d is None:
                return self._json({'error': '这一篇的编号不合法'}, 400)
            if not _manifest_has(pid):
                return self._json({'error': '论文不在索引里：%s' % pid}, 404)
            n = int(self.headers.get('Content-Length') or 0)
            try:
                body = json.loads(self.rfile.read(n).decode('utf-8'))
            except Exception as e:
                return self._json({'error': '不是合法 JSON: %s' % e}, 400)
            # 标题清洗：去控制字符、压空白、限长。空标题没意义（列表会变成一片空白）
            # 所以直接退回 400，让前端把输入框留在原地报错，而不是写进去一个空串。
            title = re.sub(r'[\x00-\x1f\x7f]+', ' ', str(body.get('title') or ''))
            title = re.sub(r'\s+', ' ', title).strip()
            if not title:
                return self._json({'error': '标题不能为空'}, 400)
            if len(title) > 300:
                title = title[:300]
            # meta.json 先读后写（保留 doc 这类别的字段），不存在就建一份最小的
            mp = os.path.join(d, 'meta.json')
            meta = {}
            if os.path.exists(mp):
                try:
                    meta = json.load(open(mp, encoding='utf-8'))
                except Exception as e:
                    return self._json({'error': '这一篇的标题信息读不出来，没敢改：%s' % e}, 500)
            old = meta.get('title') or ''
            meta['title'] = title
            meta.setdefault('id', pid)
            json.dump(meta, open(mp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            mm = _load_manifest()
            hit = False
            for p in mm.get('papers', []):
                if p.get('id') == pid:
                    p['title'] = title
                    hit = True
            if hit:
                _save_manifest(mm)
            return self._json({'ok': True, 'id': pid, 'title': title,
                               'oldTitle': old, 'indexUpdated': hit})

        # 拖拽排序的落盘口。顺序就存在 `papers/index.json` 的 `papers` **数组次序**里
        # （数组顺序即自定义顺序，单一真源，不再另写一个 order 字段——写了就是双源，
        #  迟早和数组顺序对不上）。
        if path == '/api/papers/order':
            n = int(self.headers.get('Content-Length') or 0)
            try:
                body = json.loads(self.rfile.read(n).decode('utf-8'))
            except Exception as e:
                return self._json({'error': '不是合法 JSON: %s' % e}, 400)
            want = body.get('order')
            if not isinstance(want, list) or not all(isinstance(x, str) for x in want):
                return self._json({'error': 'order 必须是 id 字符串数组'}, 400)
            mm = _load_manifest()
            cur = [p['id'] for p in mm.get('papers', [])]
            known = set(cur)
            # 容错口径：认得的 id 按给的次序排；**没提到的**（比如期间别人新导入了一篇）
            # 按原相对次序接在后面，而不是丢掉——排序请求和并发导入撞上时不会吞数据。
            # 不认得的 id 只记下来回报，不入库。
            ordered, seen = [], set()
            for x in want:
                if x in known and x not in seen:
                    ordered.append(x)
                    seen.add(x)
            ignored = [x for x in want if x not in known]
            rest = [x for x in cur if x not in seen]
            final = ordered + rest
            if cur and not ordered:
                return self._json({'error': 'order 里没有一个 id 是库里的', 'known': cur}, 400)
            by = {p['id']: p for p in mm.get('papers', [])}
            mm['papers'] = [by[x] for x in final]
            _save_manifest(mm)
            return self._json({'ok': True, 'order': final,
                               'appended': rest, 'ignored': ignored})

        # ★ 打开本机的一个文件（双击 `.ano` 时由启动器带 `?file=` 过来）。
        #   和 `/api/papers` 的**区别只在字节从哪来**（那边是请求体，这边是磁盘），
        #   入库那一段是同一个函数 `ingest_bytes()` —— 免得两处各写一套、
        #   改了一处忘了另一处（`.ano` 要全文件扫、判 graph.json 这些细节很容易漏）。
        if path == '/api/import-local':
            n = int(self.headers.get('Content-Length') or 0)
            try:
                body = json.loads(self.rfile.read(n).decode('utf-8'))
            except Exception as e:
                return self._json({'error': '不是合法 JSON: %s' % e}, 400)
            p = (body.get('path') or '').strip()
            if not p:
                return self._json({'error': 'path 不能为空'}, 400)
            p = os.path.abspath(p)
            low = p.lower()
            if not (low.endswith('.pdf') or low.endswith('.ano')):
                return self._json({'error': '只认 .pdf / .ano 文件'}, 400)
            if not os.path.isfile(p):
                return self._json({'error': '找不到这个文件：%s' % p}, 404)
            size = os.path.getsize(p)
            if size <= 0 or size > 200 * 1024 * 1024:
                return self._json({'error': '空文件或超过 200MB'}, 400)
            # ★ 已经在库里了？→ 直接打开那一篇，**不要再导入一份**。
            #   双击库里的 `papers/<id>/paper.ano` 是最常见的用法，
            #   要是每次都复制一份，用户点三下就有三篇一模一样的论文。
            hit = find_paper_by_content(p, size)
            if hit:
                return self._json({'id': hit, 'opened': True,
                                   'note': '这篇已经在库里，直接打开'})
            try:
                with open(p, 'rb') as f:
                    data = f.read()
            except OSError as e:
                return self._json({'error': '读不了这个文件：%s' % e}, 400)
            return self._json(*ingest_bytes(data, os.path.basename(p)))

        if path != '/api/papers':
            return self._json({'error': '没有这个接口'}, 404)
        name = unquote((q.get('filename') or ['paper.pdf'])[0])
        low = name.lower()
        if not (low.endswith('.pdf') or low.endswith('.ano')):
            return self._json({'error': '只收 PDF 或 .ano 文件'}, 400)
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > 200 * 1024 * 1024:
            return self._json({'error': '空文件或超过 200MB'}, 400)
        data = self.rfile.read(n)
        return self._json(*ingest_bytes(data, name))

    # -------------------------------------------------- DELETE（论文库：删一篇，不可恢复）
    def do_DELETE(self):
        """删掉 `papers/<id>/` 整个目录 + 从索引里摘掉。**破坏性、不可恢复。**

        四道闸，缺一不可（口径见 `valid_paper_id` / `safe_paper_dir`）：

          ① id 必须过白名单 —— 路径分隔符 / `..` / 点开头，正则那关就过不了
          ② realpath 之后必须是 `papers/` 的**直接**子目录 —— 挡住符号链接/junction
             把删除目标引到库外面去；同时也保证了"只删这一层"，不会连坐父目录或更深
          ③ id 必须**已经在索引里** —— 名字打错、或目录还没登记的，一律不动磁盘
          ④ 这篇正在跑任务（一键分析/重新分节）就拒掉 —— 否则 rmtree 之后后台线程
             还在往那个目录写，会凭空长出一个半截目录出来
        """
        u = urlparse(self.path)
        m = re.match(r'^/api/papers/([^/]+)$', u.path)
        if not m:
            return self._json({'error': '没有这个接口'}, 404)
        pid = unquote(m.group(1))
        d = safe_paper_dir(pid)                                    # ① + ②
        if d is None:
            return self._json({'error': '非法论文 id：%r' % pid}, 400)
        if not _manifest_has(pid):                                 # ③
            return self._json({'error': '论文不在索引里，没动任何文件：%s' % pid}, 404)
        with JOBS_LOCK:                                            # ④
            busy = [j['id'] for j in JOBS.values()
                    if j.get('paperId') == pid and j.get('state') == 'running']
        if busy:
            return self._json({'error': '这篇还有任务在跑（%s），等它跑完再删'
                                        % '、'.join(busy)}, 409)
        # 删之前先量一下"要丢掉多少东西"，删完就没得量了——回给前端做提示/回执
        files = total = 0
        if os.path.isdir(d):
            for root, _dirs, fns in os.walk(d):
                for fn in fns:
                    files += 1
                    try:
                        total += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        pass
        existed = os.path.isdir(d)
        if existed:
            try:
                rmtree_force(d)
            except Exception as e:
                traceback.print_exc()
                return self._json({'error': '删目录失败：%s: %s' % (type(e).__name__, e)}, 500)
        # 目录真删掉了才动索引。**顺序不能反**：先摘索引再删目录的话，rmtree 一失败
        # 就成了"索引里没有、磁盘上还在"的孤儿目录，下次导入会撞名。
        mm = _load_manifest()
        mm['papers'] = [p for p in mm.get('papers', []) if p.get('id') != pid]
        _save_manifest(mm)
        sys.stderr.write('[del] %s（%d 个文件 / %.1f MB）\n' % (pid, files, total / 1048576.0))
        return self._json({'ok': True, 'id': pid, 'deletedDir': existed,
                           'files': files, 'bytes': total})

    # -------------------------------------------------- PUT/PATCH（人工改分节，P3-a 只做覆盖写）
    def do_PUT(self):
        u = urlparse(self.path)
        m = re.match(r'^/api/papers/([\w\-.]+)/(\w+)$', u.path)
        if not m:
            return self._json({'error': '没有这个接口'}, 404)
        pid, what = m.group(1), m.group(2)
        d = self._pdir(pid)
        if not os.path.isdir(d):
            return self._json({'error': '找不到这一篇'}, 404)
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n)
        try:
            json.loads(raw.decode('utf-8'))          # 只收合法 JSON，别把坏数据写进库里
        except Exception as e:
            return self._json({'error': '不是合法 JSON: %s' % e}, 400)
        if what not in ('sections', 'graph', 'notebook', 'annotations'):
            return self._json({'error': '不可写 %s' % what}, 400)
        with open(os.path.join(d, what + '.json'), 'wb') as f:
            f.write(raw)
        return self._json({'ok': True, 'wrote': what + '.json', 'bytes': len(raw)})


def _ctype(p):
    e = os.path.splitext(p)[1].lower()
    return {'.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
            '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
            '.pdf': 'application/pdf', '.png': 'image/png', '.svg': 'image/svg+xml',
            '.cmd': 'text/plain; charset=utf-8', '.md': 'text/plain; charset=utf-8'}.get(e,
            'application/octet-stream')


def main():
    port = 8777
    if '--port' in sys.argv:
        port = int(sys.argv[sys.argv.index('--port') + 1])
    os.makedirs(PAPERS, exist_ok=True)
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print('PaperIDE 服务已起：http://127.0.0.1:%d/' % port)
    print('  前端   : %s' % STATIC)
    print('  论文库 : %s' % PAPERS)
    print('  拖一个 PDF 到页面上就能入库分节。Ctrl+C 停。')
    preload_core()             # 先把内核导进来，版本指纹才有内容
    reload_changed_modules()
    print('  内核版本：%s' % code_version()['moduleMtimes'])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n停了。')


if __name__ == '__main__':
    main()
