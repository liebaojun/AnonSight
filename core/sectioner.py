# -*- coding: utf-8 -*-
"""
PaperIDE · P3-a 自动分节引擎

把一个 PDF 变成「节列表」——每节带 id / 标题 / 页码 / 正文（sourceText），
直接喂给现有的抽取产线（pipeline.py 校验器）与渲染器。

    PDF ──(PyMuPDF 版式特征)──→ 行(带字号/字体/坐标) ──→ 标题块 ──→ 节
                                     │
                                     └─ 字体角色分类（正文/标题/符号/小字/页眉页脚）

★ 为什么不按"字号变大"找标题：
  这篇 Cell 的**标题和正文同字号（8.5pt）**，靠的是**换字体家族**（AdvPSA183→AdvPSHN-H）。
  而**摘要正文**又用了另一套字体（AdvPSHN-M, 10pt）。所以只认"字体≠正文"会把摘要正文
  整段当成标题；只认"字号变大"会一条标题都找不到。**两个都不够，得判"这行是不是片段"**。

★ 判据（fragment ratio，本文件的核心）：
  同一个 (字体,字号) 组里，如果大部分行的**下一行是它的续行**（紧邻、同栏、同组），
  这组就是**会跨行续写的正文**；反之每行自成一体 → **标题**。
  实测：AdvPSHN-H 组 48 行里只有换行标题的首行是片段 → ratio≈0.4 → 标题 ✓
        AdvPSHN-M 组（摘要正文）几乎每行都是片段 → ratio≈0.9 → 正文 ✓

用法：
    python core/sectioner.py <pdf> [--debug] [--json out.json]
"""
import json
import os
import re
import sys

import fitz  # PyMuPDF

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import pdftext  # noqa: E402

# 正文章节区：从这里开始（摘要/引言），到"后置内容"为止
START_HEADINGS = ('SUMMARY', 'ABSTRACT', 'INTRODUCTION')
# 后置内容：正文到此为止（这些之后是方法/参考文献/补充材料，不是论文论据）
END_HEADINGS = (
    'STAR+METHODS', 'STAR METHODS', 'EXPERIMENTAL PROCEDURES', 'METHODS',
    'REFERENCES', 'ACKNOWLEDGMENTS', 'ACKNOWLEDGEMENTS', 'SUPPLEMENTAL INFORMATION',
    'AUTHOR CONTRIBUTIONS', 'DECLARATION OF INTERESTS', 'RESOURCE AVAILABILITY',
    'KEY RESOURCES TABLE', 'LEAD CONTACT', 'EXPERIMENTAL MODEL AND SUBJECT DETAILS',
    'METHOD DETAILS', 'QUANTIFICATION AND STATISTICAL ANALYSIS', 'ONLINE METHODS',
    'MATERIALS AND METHODS', 'SUPPLEMENTARY INFORMATION', 'SUPPLEMENTAL FIGURES',
    'SUPPLEMENTAL TABLES', 'FIGURE LEGENDS',
)

SIZE_TOLERANCE = 0.45       # 同一字体的字号抖动容差：8.2/8.3/8.4 其实是同一个字号
SMALL_SIZE_DELTA = 0.8      # 比正文小这么多 = 图注/表注/脚注，不收进正文
REPEAT_MAX_CHARS = 220      # 重复到多少字还算"页眉页脚"（Cell 的 "in press" 页眉能有 150+ 字）
# 不是章节标题的东西：DOI / 网址 / 邮箱 / 期刊自己的小标签
JUNK_HEAD = re.compile(
    r'^(https?://|www\.|doi:|check for updates$|download|share|cite this article|'
    r'correspondence|reprints|permissions|article$|review article$|perspective$)|'
    r'[\w.\-]+@[\w.\-]+\.\w+|'
    # 图表标题不是章节标题（"Table 1 | …" / "Fig. 2 | …" / "Box 1"）
    r'^(fig|figure|table|box|scheme|extended data|supplementary)\s*\.?\s*\d|'
    # 期刊的"连续页"页眉：Cell 的 "Please cite this article in press as: Yue et al., …"
    # 又长又每页重复，实测被当成过节标题（还吞掉 4000 多字正文）
    r'^please cite this article|^this article is protected|^downloaded from|'
    r'^provided by|^see discussions|^article in press', re.I)
FRAGMENT_RATIO = 0.55       # 组内"续行"占比高于此 = 会跨行续写的正文，不是标题
WS_RATIO = 1.8              # 行上方留白是正文行距的几倍 = 有"段前空"
ADJACENT_LEADING = 1.45     # 标题换行合并的紧邻阈值（同一标题内部 dy≈1.0 行距；两条标题之间 dy≈2.0）
COL_OVERLAP = 0.4           # 水平重叠多少算"同一栏"
MIN_HEAD_CHARS = 4          # 短于此的"标题"多半是符号/缩写，不要
STUB_CHARS = 900            # 正文短于此的"节"多半是父级壳，并进下一节（见 sectionize 末尾）

# ---- 负判据：把"假标题"从正文区里剔掉（2026-09-18 第 7 轮，eLife 版式）----
# 上面那两个正判据（片段比 / 段前留白）在 eLife 上会**同时**失效，详见 drop_fake_heads 的说明。
LABEL_CPL = 10.0            # 字体级 chars/line 低于此 = 图内标签字体（刻度 / 条件 / 显著性星号）
# 标题**不可能**用这些字符开头：`(` 是句子中段（"(FcRV), Bai1, and MerTK…"）、`*` 是图里的显著性
# 标记（`***`）、`,` `;` 是折行的续行。实测：9 篇已收录 + 4 篇验收里，被 body_head 命中的
# **真标题一条都不以这些字符开头**（只有 Morrissey 的碎片命中）。
HEAD_PUNCT = '([{*†‡§¶,;:)]}-–—/\\|=+%$#@~^&<>'
DIG_LOWER = re.compile(r'^\d+[a-z]')   # `2a).` —— 数字后**没有**空白就接小写字母
# ⚠ 这条是 NUM_PREFIX 的补集：NUM_PREFIX 要求 `\d[.:)]?\s+` 后面才是标题词（`2 Results`），
#   而 `2a).` / `4b)` 这种是正文里的"图 2a"被 PyMuPDF 从中间切开的一半，不是编号标题。


# ==================================================================== 行抽取
class Line(object):
    __slots__ = ('page', 'idx', 'text', 'size', 'font', 'x0', 'y0', 'x1', 'y1',
                 'group', 'role', 'journal', 'block', 'pageH')

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def key(self):
        return self.group

    def __repr__(self):
        return '<%s p%d %s %r>' % (self.role, self.page, self.group[1], self.text[:40])


def drop_line_numbers(lines):
    """剔掉**投稿版式的行号栏**（bioRxiv / 预印本常见）。

    实测（Lunger2026）：行号在左边距 x≈35，正文在 x≈72，**同一 y**——
    PyMuPDF 当成两行分别吐出来，拼接后落进句子中间：
        "…increased CD40, CD80, and PDL1 expression 140 (Fig. 1e). In contrast… 141 increased…"
    模型按自然语序复述引文（当然不会带那个 140）→ 引文永远对不上 →
    **9/10 节要重试**（第 2 轮验收就是这个数字）。引文吸附也救不了——中间多出来的数字它不敢动。

    判据（三條同时成立才算行号栏，免得误伤页码）：
      ① 整行只有数字
      ② 比正文栏更靠左（≥6pt）
      ③ 同一页里 ≥5 个，且大部分逐行 +1~+3 递增
    另外排除页面底部 12%（页码住在那儿）。"""
    by_page = {}
    for l in lines:
        by_page.setdefault(l.page, []).append(l)
    drop = set()
    for pno, ls in by_page.items():
        bare = [l for l in ls if re.fullmatch(r'\d{1,4}', l.text.strip())]
        if len(bare) < 5:
            continue
        H = bare[0].pageH or 792
        bare = [l for l in bare if l.y0 < H * 0.88]
        if len(bare) < 5:
            continue
        others = [l.x0 for l in ls if not re.fullmatch(r'\d{1,4}', l.text.strip())]
        if not others:
            continue
        body_x = min(others)
        if min(l.x0 for l in bare) > body_x - 6:
            continue                                   # 不比正文更靠左 → 不是行号栏
        bare.sort(key=lambda l: l.y0)
        vals = [int(l.text.strip()) for l in bare]
        steps = [b - a for a, b in zip(vals, vals[1:])]
        good = sum(1 for s in steps if 0 < s <= 3)
        if steps and good < len(steps) * 0.6:
            continue                                   # 不递增 → 不是行号
        for l in bare:
            drop.add(id(l))
    return [l for l in lines if id(l) not in drop]


def assign_groups(lines):
    """把「字体 + 聚类后的字号」当作组键。

    ⚠ 为什么必须聚类：同一段正文在不同行会渲染出 **8.2 / 8.3 / 8.4** 三种字号
    （亚像素抖动，Nature Reviews 那篇就是这样）。按 (字体, 原始字号) 分组会把
    一段连续正文劈成三组，于是——
      · 片段比失准（"下一行"跳到了别组，续行判不出来）
      · 段前留白失准
    结果整篇 78 节、一半是正文句子被当成标题。聚类后才是"一个字号一组"。"""
    weights = {}
    for l in lines:
        if l.role == 'pagenum':
            continue
        weights.setdefault(l.font, {})
        weights[l.font][l.size] = weights[l.font].get(l.size, 0) + len(l.text.strip())
    rep = {}
    for font, sizes in weights.items():
        uniq = sorted(sizes)
        groups, cur = [], []
        for s in uniq:
            if cur and s - cur[-1] > SIZE_TOLERANCE:
                groups.append(cur)
                cur = []
            cur.append(s)
        if cur:
            groups.append(cur)
        for g in groups:
            tot = sum(sizes[s] for s in g) or 1
            mean = sum(s * sizes[s] for s in g) / tot          # 按字符数加权的代表字号
            for s in g:
                rep[(font, s)] = round(mean, 1)
    for l in lines:
        l.group = (l.font, rep.get((l.font, l.size), l.size))


def extract_lines(doc):
    """PDF → 行列表（保留字号/字体/坐标；**span 文本原样拼接**，
    PyMuPDF 的 span 自带前后空格，直接接就是完整句，见 core/pdftext 的说明）"""
    lines = []
    for pno in range(doc.page_count):
        page = doc[pno]
        H, W = page.rect.height, page.rect.width
        li = 0
        for bi, b in enumerate(page.get_text('dict')['blocks']):
            if b.get('type') != 0:
                continue
            for l in b['lines']:
                # ⚠ 拼接时**不能过滤空白 span**：这篇 PDF 的空格是**独立的 span**
                #   （x 位置能对上，但文本里是 ' '），滤掉就变成
                #   "contributionsofdifferentCD3chains" ——引文一条都命不中
                spans = l['spans']
                solid = [s for s in spans if s['text'].strip()]
                if not solid:
                    continue
                text = ''.join(s['text'] for s in spans)
                dom = max(solid, key=lambda s: len(s['text'].strip()))
                x0, y0, x1, y1 = l['bbox']
                lines.append(Line(
                    page=pno + 1, idx=li, text=text, size=round(dom['size'], 1),
                    font=dom['font'], x0=x0, y0=y0, x1=x1, y1=y1,
                    group=(dom['font'], round(dom['size'], 1)), role=None,
                    journal=None, block=bi, pageH=H,
                ))
                li += 1
        # 页号：**页面最底端**的纯数字行。
        # ⚠ 阈值不能松：bioRxiv 那种投稿格式**左边距有行号**（1…300 每行一个数字），
        #   松一点就把行号当页码，整篇页码变成 87/137/188 这种鬼东西。
        cand = [l for l in lines if l.page == pno + 1 and re.match(r'^\d{1,4}$', l.text.strip())
                and l.y0 > H * 0.93]
        if cand:
            l = min(cand, key=lambda x: -x.y0)      # 最靠下的那个
            l.role = 'pagenum'
            l.journal = int(l.text.strip())
    lines = drop_line_numbers(lines)   # 投稿版式的行号栏要先剔，否则会混进句子中间
    assign_groups(lines)
    return lines


# ==================================================================== 字体角色
EDGE_TOP, EDGE_BOTTOM = 0.12, 0.85     # 页眉在页面上 12%，页脚在下 15%——只有这些地方才可能有页眉页脚


def _near_edge(l):
    """页眉页脚只可能长在页面上下边缘。

    ⚠ 这条判据是必须的：正文里**偶尔也会有一句话重复**（比如每节末尾的固定表述），
    只按"重复"判会把它们误当页眉页脚从正文里删掉。加上位置限制就稳了。"""
    if not l.pageH:
        return False
    return l.y0 < l.pageH * EDGE_TOP or l.y0 > l.pageH * EDGE_BOTTOM


def _reading_neighbours(lines):
    """给每一行找"同栏上一行"——判段前留白用。
    跨栏、跨页、跨图都不算邻居（否则图文混排处会算出一个巨大的假留白）。"""
    prevs = {}
    last = None
    for l in lines:
        if l.role == 'pagenum':
            continue
        prevs[id(l)] = last
        last = l
    return prevs


def font_roles(lines, debug=False):
    """给每一行定角色：prose（正文）/ head（标题）/ small / symbol / running / pagenum

    · **符号字体**（希腊字母 ε δ γ ζ、数学符号）是被内联进正文的，按字体整体判定
    · **页眉页脚**按"整页重复的同一句话"判（"Article" 40 页都有）——**逐行判**，
      不能整组判：一句话碰巧重复，不该把整组正文连坐
    · 正文 vs 标题用**两票**：
        A. 片段比 —— 这行是不是"下一行的前半句"（会跨行续写 = 正文）
        B. 段前留白 —— 行上方空白是正文行距的几倍（标题有段前空）
      两个信号都指向标题才判标题；只看片段比会被斜体的行内词（in vitro）骗到
    """
    stats = {}
    for l in lines:
        if l.role == 'pagenum':
            continue
        s = stats.setdefault(l.group, {'chars': 0, 'lines': 0})
        s['chars'] += len(l.text.strip())
        s['lines'] += 1
    body_group = max(stats.items(), key=lambda kv: kv[1]['chars'])[0]
    body_size = body_group[1]

    # 符号字体：**按字体整体**看（同一字体各字号合起来），平均每行不到 4 个字
    by_font = {}
    for (font, size), s in stats.items():
        f = by_font.setdefault(font, [0, 0])
        f[0] += s['chars']
        f[1] += s['lines']
    sym_fonts = {f for f, (c, n) in by_font.items() if n and c / n < 4.0}

    # 页眉页脚
    # ⚠ 不能按**原文**精确匹配：期刊页脚在奇偶页不一样（"3696 Molecular Cell…" vs "Molecular Cell…"），
    #   精确匹配一条都认不出来，页脚就被当成章节标题了（Li2025 那篇多出 4 个假节）。
    #   办法：把**数字抹成 #** 再比——模板一样就算同一句页脚。
    pages_of = {}
    for l in lines:
        if l.role == 'pagenum' or not _near_edge(l):
            continue
        t = l.text.strip()
        if 0 < len(t) <= REPEAT_MAX_CHARS:
            pages_of.setdefault(re.sub(r'\d+', '#', t), set()).add(l.page)
    n_pages = max((l.page for l in lines), default=1)
    thr = max(3, min(n_pages, 25) * 0.25)
    repeated_norm = {k for k, ps in pages_of.items() if len(ps) >= thr}

    # 逐行定"明显的"角色
    for l in lines:
        if l.role == 'pagenum':
            continue
        t = l.text.strip()
        if not t:
            l.role = 'symbol'
        elif (_near_edge(l) and len(t) <= REPEAT_MAX_CHARS
              and re.sub(r'\d+', '#', t) in repeated_norm):
            l.role = 'running'
        elif l.font in sym_fonts:
            # 图里的分栏字母（A B C…）也走符号字体，但它们**不是正文**——
            # 混进来会把句子拦腰截断：「…condition, A C D F G H J I E B the phosphorylation level…」
            # 于是引文一条都命不中。短到这个程度的符号行判为图注标签，正文不收。
            l.role = 'panel' if len(t) <= 3 else 'symbol'
        elif l.size < body_size - SMALL_SIZE_DELTA:
            l.role = 'small'
        elif len(t) <= 120 and JUNK_HEAD.search(t):
            l.role = 'junk'                 # DOI / 网址 / 邮箱 / "Check for updates"：不是标题也不是正文
        else:
            l.role = None

    # 正文行距 + 正文段前留白（都从正文组量，作为标尺）
    prevs = _reading_neighbours(lines)
    leads, wss = [], []
    for l in lines:
        if l.group != body_group or l.role is not None:
            continue
        p = prevs.get(id(l))
        if p is None or p.page != l.page:
            continue
        leads.append(l.y0 - p.y0)
        wss.append(max(0.0, l.y0 - p.y1))
    leading = _median(leads) or 11.0
    body_ws = _median(wss) or 2.5

    # 分组的正文/标题判定
    groups = {}
    for l in lines:
        if l.role is None:
            groups.setdefault(l.group, []).append(l)

    frag, wsr, roles = {}, {}, dict()
    for g, ls in groups.items():
        n_frag = 0
        for k, l in enumerate(ls):
            if k + 1 >= len(ls):
                continue
            nxt = ls[k + 1]
            if nxt.page != l.page or (nxt.y0 - l.y0) >= leading * ADJACENT_LEADING:
                continue
            ov = min(l.x1, nxt.x1) - max(l.x0, nxt.x0)
            if ov < (l.x1 - l.x0) * COL_OVERLAP:
                continue
            if l.text.rstrip().endswith(('.', '!', '?', ':')):
                continue
            n_frag += 1
        frag[g] = n_frag / max(1, len(ls))
        n_ws = 0
        for l in ls:
            p = prevs.get(id(l))
            if p is None or p.page != l.page or p.group != g:
                n_ws += 1                      # 栏顶第一行 / 新组首行：当成有段前空
            elif (l.y0 - p.y1) > body_ws + leading * (WS_RATIO - 1.0) * 0.5:
                n_ws += 1
        wsr[g] = n_ws / max(1, len(ls))
        is_head = (frag[g] < FRAGMENT_RATIO and wsr[g] > 0.5
                   and max(len(x.text.strip()) for x in ls) >= MIN_HEAD_CHARS)
        roles[g] = 'head' if is_head else 'prose'

    for l in lines:
        if l.role is None:
            l.role = roles.get(l.group, 'prose')

    if debug:
        print('---- 字体角色 ----（正文组 %s %s · 行距 %.1f · 段前留白 %.1f）'
              % (body_group[0], body_group[1], leading, body_ws))
        for g in sorted(stats, key=lambda g: -stats[g]['chars']):
            ls = groups.get(g)
            if ls is None:
                continue
            print('  %-26s sz%-5s chars%-6d lines%-5d frag%.2f ws%.2f → %s'
                  % (g[0], g[1], stats[g]['chars'], len(ls), frag[g], wsr[g], roles[g]))
    return roles, body_group, body_size, leading, prevs


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


# ==================================================================== 标题块
# ---- 编号标题（Frontiers / MDPI / PLOS 这类"首字母大写 + 数字编号"版式）----
# 实测 Majumdar2024（Frontiers in Immunology 15:1509980）：31 条标题块**全认出来了**，
# 但 `1 Introduction` / `2.1 …` 是**首字母大写**，过不了下面 `upper`（要求全大写）那条判据
# → 全被判成 level 2 → 一条 level-1 都没有 → sectionize 找不到正文区起点 → 整篇退化成
# 「按页切 7 块 + fallback: no-headings」（验收 05 的第 2 条缺陷）。影响一整类期刊。
#
# 两条正则各管一件事：
#   ① NUM_PREFIX（heading_blocks 里）→ **定级**：单个数字 = 一级，带点的（2.1 / 2.1.3）= 二级
#   ② NUM_PREFIX + ROMAN_PREFIX（_norm_head 里）→ **比较前剥掉编号**，
#      让 '1 INTRODUCTION' / 'I. INTRODUCTION' 跟 START/END 表里的 'INTRODUCTION' 全等对上
# ⚠ 两条都**必须**带分隔符 + 后接字母/左括号——否则会切掉正文词的头：
#    '2D cultures' / '3D printing' / '1.5-fold' / 'IL-2 signaling'（数字后不是空白，不匹配）
#    / '4 °C'（后面不是字母，不匹配）/ shi2025 第 1 页被误判成标题的 '2 & Chenqi Xu'（同上）
#    字符类末尾的 `一-鿿` 就是 `一-鿿`——给中文标题（'1 引言'）留的门，别删
NUM_PREFIX = re.compile(r'^(\d{1,2}(?:\.\d{1,2}){0,3})[.:\)]?\s+(?=[A-Za-z(一-鿿])')
# 罗马数字编号只做**归一化**（剥前缀），**不用来定级**：
#   'C. elegans culture' / 'V. cholerae' 这类细菌学写法会被误判成一级标题，
#   而定级错了代价很大（一级标题会被当成"有子标题的容器"、自己的正文被吞掉）。
#   反正 '_norm_head' 剥完之后 START_HEADINGS 的全等判据照样能兜住 'I. Introduction'。
ROMAN_PREFIX = re.compile(r'^([IVXLC]{1,6})[.:\)]\s+(?=[A-Za-z(一-鿿])')


def _num_level(text):
    """编号标题的级别：'2 Results' → 1；'2.1 …' / '2.1.3 …' → 2；没编号 → 0（不定级）。"""
    m = NUM_PREFIX.match(text)
    if not m:
        return 0
    return 1 if '.' not in m.group(1) else 2


def heading_blocks(lines, roles, leading=11.0, debug=False):
    """把标题行聚成**标题块**（标题换行要合成一条），并按编号/全大写/首字母大写定级别。

    关键：只在**同栏紧邻**时合并——这篇论文里两个标题可能上下紧挨着
    （左栏末一条 + 右栏首一条），字段一合就把两条标题粘成一条。"""
    heads = [l for l in lines if l.role == 'head']
    blocks = []
    for l in heads:
        text = l.text.strip()
        if not text:
            continue
        if blocks:
            prev, plines = blocks[-1]
            last = plines[-1]
            if last.page == l.page:
                dy = l.y0 - last.y0
                ov = min(last.x1, l.x1) - max(last.x0, l.x0)
                narrow = min(last.x1 - last.x0, l.x1 - l.x0)
                same_font = l.group == last.group
                # 两个阈值都是踩过坑改的：
                # ① 行距用**全文行距**，不用本行高度（bbox 高度随字体浮动，临界点上会随机合不上）
                # ② 重叠量按**较窄那行**算——换行标题的第二行本身短，拿上一行的宽度当分母
                #    永远够不上阈值（"Cover All CD3 ITAMs" 88pt / 上一行 233pt = 0.38 < 0.4）
                if (same_font and 0 < dy < leading * ADJACENT_LEADING
                        and ov > narrow * COL_OVERLAP):
                    plines.append(l)
                    continue
        blocks.append((None, [l]))
    out = []
    for _, plines in blocks:
        text = ' '.join(x.text.strip() for x in plines)
        text = re.sub(r'\s+', ' ', text).strip()
        letters = [c for c in text if c.isalpha()]
        upper = letters and all(c.isupper() for c in letters)
        # 级别两个信号，**编号优先**：
        #   ① 编号（显式的版式结构信息，'2 Results' → 1 / '2.1 …' → 2）
        #   ② 全大写（老版式：Cell / wu2020 的 'SUMMARY' / 'INTRODUCTION'）
        # ⚠ 编号优先于全大写是有意的：全大写期刊里 '2.1 ITAM RESTRICTED CARS…' 若被判成
        #   一级，而它的兄弟 '2.2 Something'（首字母大写）判成二级，2.1 就会被当成
        #   "有子标题的容器"、**自己的正文被吞掉**。编号是显式的，比大小写可靠。
        lvl = _num_level(text)
        out.append({
            'text': text,
            'level': lvl or (1 if upper else 2),
            'font': plines[0].group[0],
            'size': plines[0].group[1],
            'page': plines[0].page,
            'y': plines[0].y0,
            'lines': plines,
        })
    if debug:
        print('---- 标题块 %d 条 ----' % len(out))
        for h in out:
            print('  L%d p%-3d %-58s %s' % (h['level'], h['page'], h['text'][:58], h['font'][:20]))
    return out


# ==================================================================== 假标题（负判据）
# ★ 2026-09-18 第 7 轮：eLife 版式（Morrissey2018）8 节里 6 个标题是正文碎片、toc 37 条里 35 条不是标题。
#
# 根因**不在**编号判据（把那三处临时回退，跑同一批 PDF，Morrissey 的输出一字不变），
# 而在 font_roles 的那两票在这类版式上会**同时**失效：
#
#   ① 片段比 frag —— 它按"**同组**下一行"判续行。eLife 把**行内引用**（"Lacerna et al., 1988;
#      Andreesen et al., 1990"）画在另一支字体子集 AdvPA5BD 里，于是这一组 13 行**每一行都夹在
#      AdvPA5CF 正文行中间**——组内相邻两行永远不在同一页的同一处 → n_frag 恒为 0 → frag=0.00，
#      看着"每行自成一体"，正是标题的样子。
#   ② 段前留白 wsr —— 判据里有一条 `p.group != g` 就当"有段前空"，而这些行的上一行**必然**
#      是别的组（正文）→ wsr 恒为 1.00。
#   两票全中 → 'head'。同理，只在图里出现的 ArialMT / Helvetica / Arial-BoldMT（轴标签、条件名、
#   `***` 显著性标记）也都是"孤立短行"，同样两票全中。
#
# 所以补三条**负判据**（都只作用于**正文区**的标题块，见 sectionize；不动 excluded、
# 不改任何行的字体角色分组，因此对其余论文是零影响——13 篇逐字节实测见报告）：
#   A. 图内标签字体 —— 字体全文平均每行不到 LABEL_CPL 个字符
#   B. 行首形态     —— 小写字母 / HEAD_PUNCT 标点 / `2a).` 这种数字接小写字母
#   C. 折行保护     —— 上一行也是标题行且满足折行几何条件时，B 不适用
#                      （eLife/Yue2026 的标题折行会换字体子集，如斜体的 "in vivo"，
#                        单看小写开头会被当成碎片）
def label_fonts(lines):
    """**图内标签字体**：全文平均每行不到 LABEL_CPL 个字符的字体。

    这是 font_roles 里 sym_fonts（< 4.0 个/行 = 符号字体）那一招的**第二档**，复用同一套
    "字体级 chars/line" 统计，但**只用来否定标题，不改任何行的角色**——sym_fonts 一改就会
    牵动 symbol/panel 的角色分配、正文文字，进而让所有论文的 sourceText 变样。

    实测（13 篇，被 body_head 命中的字体）：
        真标题字体最低 18.2 字/行（Yue2026 的 HelveticaNeue-HeavyItalic，就是 "in vivo" 那个斜体）
        Morrissey 的三支图内字体：ArialMT 6.7 / Arial-BoldMT 5.3 / Helvetica 7.2
    两簇之间空着 7.2→18.2 一整档，阈值取中间的 10.0。
    （图内标签必然是"几个字的刻度/条件名"，标题是词、正文是句，这是稳定的版式事实。）"""
    by_font = {}
    for l in lines:
        if l.role == 'pagenum':
            continue
        f = by_font.setdefault(l.font, [0, 0])
        f[0] += len(l.text.strip())
        f[1] += 1
    return {f for f, (c, n) in by_font.items() if n and c / n < LABEL_CPL}


def _is_wrapped_head(l0, prevs, leading):
    """本行是不是**上一行（标题行）的折行**——是的话，小写开头就不算"正文碎片"。

    eLife / Yue2026 这类版式，标题折行时会换字体（"…restore myeloid function" + 斜体的
    "in vivo"），于是折行那半截另起一个字体组、被单独判成一条标题块。它小写开头、看着像碎片，
    但它上面**紧挨着的也是标题行**——这是它与"句子中段"唯一的、也是决定性的区别。
    几何条件与 heading_blocks 合并折行时用的完全一致（同页 / 紧邻 / 同栏），别另起一套。"""
    p = prevs.get(id(l0))
    if p is None or p.page != l0.page or p.role != 'head':
        return False
    dy = l0.y0 - p.y0
    if not (0.5 < dy < leading * ADJACENT_LEADING):
        return False
    ov = min(p.x1, l0.x1) - max(p.x0, l0.x0)
    narrow = min(p.x1 - p.x0, l0.x1 - l0.x0)
    return ov > narrow * COL_OVERLAP


def drop_fake_heads(lines, heads, prevs, leading, debug=False):
    """剔掉正文区里的**假标题**，返回 (留下的标题块, 被剔的记录)。

    被剔的两类，处理方式不同（不能一律丢）：
      · 'inline'（正文碎片，如 eLife 的行内引用半句）——它**本来就是正文**，把行角色改回
        'prose'，让它作为正文回到所在节里。丢出去只会让正文出现"neg- / ative"这种断词。
      · 'label'（图内文字，如 `***` / "Condition:" / 轴标签）——它不是正文也不是标题，
        改判 'panel'（这个角色本仓库已有，build_sections 本来就不收它）。"""
    lf = label_fonts(lines)
    keep, dropped = [], []
    for h in heads:
        l0 = h['lines'][0]
        t = l0.text.strip()
        if h['font'] in lf:
            why = 'label'                       # 图内标签字体（轴/条件/显著性星号）
        elif not re.search(r'[A-Za-z0-9]', t):
            why = 'label'                       # 纯符号行（`***` / `††`）：不是正文也不是标题
        elif _is_wrapped_head(l0, prevs, leading):
            why = None                          # 标题折行，放行
        elif t[:1].islower() or t[:1] in HEAD_PUNCT or DIG_LOWER.match(t):
            why = 'inline'                      # 句子中段截出来的一半
        else:
            why = None
        if why is None:
            keep.append(h)
            continue
        for l in h['lines']:
            l.role = 'prose' if why == 'inline' else 'panel'
        dropped.append((why, h))
    if debug and dropped:
        print('---- 剔除假标题 %d 条（inline %d / label %d）----'
              % (len(dropped), sum(1 for w, _ in dropped if w == 'inline'),
                 sum(1 for w, _ in dropped if w == 'label')))
        for w, h in dropped:
            print('  [%s] p%-3d %-20s %r' % (w, h['lines'][0].page, h['font'][:18], h['text'][:56]))
    return keep, dropped


# ==================================================================== 组节
def _norm_head(t):
    """标题归一化：压缩空白 + 大写 + **剥掉前导编号** + 去掉尾部句点/冒号。

    ⚠ 剥编号这一步是必须的（验收 05 的根因②）：START_HEADINGS 里写的是 'INTRODUCTION'，
      而 Frontiers 版式的标题块是 '1 Introduction' → 归一化成 '1 INTRODUCTION' →
      **全等比不中** → 正文区起点 start_i 一找不着，body_heads 就空 → 覆盖度 0% → 退化成按页切。
      END_HEADINGS 同理认不出 '4 Materials and methods'（方法学会混进 Discussion 节）。

    调用点一共 4 处，**都只吃标题文本**（不是正文行，所以不存在"把正文里的 '3 只小鼠' 也算上"）：
      · build_sections 里父子比较：剥不剥都不变（'2 RESULTS'≠'CONSTRUCTION'，'RESULTS'也≠）
        —— 顺带的好处是 '2 Results' 和它自己的子节不再需要靠"字面不等"来区分
      · sectionize 的正文区起止（START/END）：**这一步就是修复本身**
      · 末尾目录去重（两处）：**只有"同一标题的两种写法并存"时行为才会变**——期刊首页那个
        "Sections" 目录框抄 'Introduction'、正文里那条写 '1 Introduction'，剥完归一到同一个键。
        本仓库 9 篇里没有这种样本，所以这一条只有逻辑论证、没有实测样本；把编号剥掉之后剩下的
        都是标题词本身，只会让**同一个**标题更早合并，不会把两个不同标题并到一起。"""
    t = re.sub(r'\s+', ' ', t).strip().upper()
    t = NUM_PREFIX.sub('', t, count=1)
    t = ROMAN_PREFIX.sub('', t, count=1)
    return t.rstrip('.:')


def _split_by_pages(lines, body_group, end_line=None, pages_per_section=3, min_chars=800):
    """**没有章节标题**时的降级方案：把正文按页分块，每块合成一节。

    产物跟正常节一模一样（有 id/title/pageRange/sourceText），下游（抽取/标注/渲染）
    完全不用改——只是"节"的边界是页而不是标题。

    为什么值得做：Letters 格式（Nature Biotechnology 等）整篇没有一个标题，
    不降级的话平台对这类论文直接交白卷（0 节，没有图、没有导读）。"""
    tail = len(lines) if end_line is None else end_line
    # ⚠ 这里**不按 body_group 挑**：降级本来就是因为"版式没看懂"，
    #   而这恰恰是最容易把正文字体也认错的时候（实测那篇 Letters：正文 9.3pt、
    #   参考文献 7.5pt，按字符数挑出来的是参考文献那一组，结果只覆盖了最后 3 页）。
    #   退一步：所有被判为 prose 的行都算正文。
    body = [l for l in lines[:tail] if l.role == 'prose']
    if len(' '.join(l.text for l in body)) < min_chars:
        return []
    jp = journal_map(lines)
    out, cur, cur_pages = [], [], []
    for l in body:
        cur.append(l)
        cur_pages.append(l.page)
        if len(cur_pages) >= pages_per_section and l.page != cur_pages[-2]:
            out.append(_mk_page_section(cur, cur_pages, jp, len(out) + 1))
            cur, cur_pages = [], []
    if len(' '.join(x.text for x in cur)) >= min_chars:
        out.append(_mk_page_section(cur, cur_pages, jp, len(out) + 1))
    return [s for s in out if s]


def _mk_page_section(ls, pages, jp, n):
    """造一个"长得像标题块"的对象，好让下游的组装逻辑原样吃下去。

    ⚠ 这一档**直接用 PDF 页码**，不查期刊页码表：降级本来就是"版式认不出来"的情形，
    而期刊页码表恰恰也是最不可靠的时候（实测那篇 Nature Biotech 会冒出 953 和 8 混在一起，
    拼出"第 953–8 页"这种鬼东西）。PDF 页码永远准确、无歧义。"""
    if len(ls) < 3:
        return None
    p_lo, p_hi = min(pages), max(pages)
    return {
        'text': '第 %d–%d 页' % (p_lo, p_hi),
        'lines': ls, '_body': ls, 'level': 2, 'parent': '',
        'page': p_lo, 'id': 'part-%d-p%d-%d' % (n, p_lo, p_hi),
        'fallback': 'no-headings', 'pdfOnly': True,
    }


def build_sections(lines, heads, body_group, end_line=None, debug=False):
    """标题表 → 节列表。

    规则：**有子标题的一级标题不单独成节**（RESULTS 自己没正文，它的六条二级标题才是节），
    没有子标题的一级标题自己成节（SUMMARY / INTRODUCTION）。"""
    n = len(heads)
    sections = []
    parent = None
    for i, h in enumerate(heads):
        if h['level'] == 1:
            parent = h['text']             # 记住最近的一级标题，给下面的小节当地域标签
        if h['level'] == 1 and i + 1 < n and heads[i + 1]['level'] > 1:
            h['container'] = True          # 有子标题的一级标题（RESULTS）自己不收正文
            continue
        h['container'] = False
        # 所属大节（RESULTS / DISCUSSION）——AI 写中文节标题时要用：
        # 不然它分不清"CD3 磷酸化格局"该叫「结果 ·」还是「讨论 ·」
        h['parent'] = parent if parent and _norm_head(parent) != _norm_head(h['text']) else ''
        sections.append(h)

    # 行号索引：节正文 = 本标题之后，到下一条标题（或正文区结束）之前的正文行
    at = {id(l): i for i, l in enumerate(lines)}
    head_line_ids = {id(x) for h in heads for x in h['lines']}
    bounds = [at[id(h['lines'][0])] for h in heads]
    # 末节的下界 = 正文区结束（第一个"后置内容"标题的行号）；不给它就会一路吃到文末
    tail = len(lines) if end_line is None else end_line

    for h in sections:
        hi = heads.index(h)
        start_i = at[id(h['lines'][-1])] + 1        # 标题块之后
        end_i = bounds[hi + 1] if hi + 1 < len(heads) else tail
        body = [l for l in lines[start_i:end_i]
                if id(l) not in head_line_ids and l.role in ('prose', 'symbol')]
        h['_body'] = body
    return sections


# ==================================================================== 主体
def journal_map(lines):
    """PDF 页 → 期刊页码。**不可靠就整个丢掉**（退回 PDF 页码）。

    ⚠ 单条纠错不够：有些版式页脚混着好几种数字（Nature Biotech 那篇就冒出过
    "953" 和 "8" 混在一起），逐条删反而删成"953–8"这种鬼页号。
    所以做**全局判据**：相邻页的页码绝大多数得递增，否则整张表不可信。"""
    m = {}
    for l in lines:
        if l.role == 'pagenum' and l.journal:
            m.setdefault(l.page, l.journal)
    if len(m) < 3:
        return m
    seq = sorted(m.items())
    steps = [b - a for (_p1, a), (_p2, b) in zip(seq, seq[1:])]
    inc = sum(1 for s in steps if 0 < s <= 5)
    if steps and inc < len(steps) * 0.8:
        return {}                      # 不可信 → 全部退回 PDF 页码
    return m


def slugify(t, maxlen=48):
    s = t.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s[:maxlen].strip('-') or 'sec'


def sectionize(pdf_path, paper_id=None, debug=False):
    doc = fitz.open(pdf_path)
    lines = extract_lines(doc)
    if not lines:
        raise RuntimeError('PDF 里没有可提取的文字层（扫描件？需要先 OCR）')
    roles, body_group, body_size, leading, prevs = font_roles(lines, debug=debug)
    heads = heading_blocks(lines, roles, leading=leading, debug=debug)
    jmap = journal_map(lines)

    def jp(pno):
        return jmap.get(pno, pno)

    # 正文区：START 到 END
    start_i, end_i = None, len(heads)
    for i, h in enumerate(heads):
        nu = _norm_head(h['text'])
        if start_i is None and (nu in START_HEADINGS or h['level'] == 1):
            start_i = i
            break
    for i in range(start_i or 0, len(heads)):
        nu = _norm_head(heads[i]['text'])
        # ⚠ 不能只认"完全等于"：投稿版式常把一级标题和它的第一个子标题并成一行
        #（bioRxiv 的 "Methods" + "Cell lines" → 标题块拼成 "Methods Cell lines"）。
        # 只认全等的话 Methods 之后的方法学会被当成正文，一节一节全收进来（实测 Wang2025 多了 14 节）。
        if nu in END_HEADINGS or any(nu.startswith(e + ' ') for e in END_HEADINGS):
            end_i = i
            break
    body_heads = heads[start_i:end_i] if start_i is not None else []
    excluded = heads[end_i:] if end_i < len(heads) else []
    body_end_line = None
    if excluded:
        body_end_line = next(i for i, l in enumerate(lines) if l is excluded[0]['lines'][0])

    # ★ 假标题过滤：**只**作用于正文区里那些要当"节标题"的块（见 drop_fake_heads）。
    # ⚠ 位置是有意的——放在 start_i/end_i 切分**之后**：
    #   ① excluded 是"正文区之后未收"的清单，跟节无关，不该被负判据牵动（改了它的条数 = 改了别的论文）
    #   ② body_end_line 由 excluded[0] 定，必须先算完
    body_heads, dropped_heads = drop_fake_heads(lines, body_heads, prevs, leading, debug=debug)
    if dropped_heads:
        print('[分节] 剔除假标题 %d 条（正文碎片 %d → 回归正文 / 图内文字 %d → 判为图注）'
              % (len(dropped_heads),
                 sum(1 for w, _ in dropped_heads if w == 'inline'),
                 sum(1 for w, _ in dropped_heads if w == 'label')))

    secs = build_sections(lines, body_heads, body_group, end_line=body_end_line, debug=debug)

    # ── 降级：版式里**根本没有章节标题**的论文 ──
    # Letters / 连续散文体（实测 Nature Biotechnology 的 Letters、以及某些预印本）
    # 通篇没有一个标题，上面的流程会切出 0~1 节 —— 平台就交白卷了。
    # 但"每篇论文都能出图"是硬要求，所以退一步：**按页切**，每 3 页一节。
    # ⚠ 判据是**覆盖度**，不是节数：Nature Biotechnology 的 Letters 那篇能切出 5 个
    #   （全是方法学小节），看着"有节"，但**正文一点没覆盖到**——只看节数会漏判。
    _tail = body_end_line if body_end_line is not None else len(lines)
    body_prose = [l for l in lines[:_tail] if l.role == 'prose' and l.group == body_group]
    covered = {id(l) for s in secs for l in (s.get('_body') or [])}
    cover = (sum(1 for l in body_prose if id(l) in covered) / len(body_prose)) if body_prose else 1.0
    if cover < 0.5:
        fb = _split_by_pages(lines, body_group, body_end_line)
        if fb:
            print('[分节] 正文覆盖度只有 %.0f%%（版式里没有章节标题），退化为按页切分：%d 节'
                  % (cover * 100, len(fb)))
            secs = fb

    out, seen = [], {}
    for h in secs:
        body_lines = h.get('_body') or []
        if not body_lines:
            continue
        text = pdftext.join_lines([l.text for l in body_lines])
        if len(text) < 120:              # 太短的"节"多半是误判的标题（如页眉残留）
            continue
        base = slugify(h['text'])
        sid = h.get('id') or base
        if sid in seen:
            seen[sid] += 1
            sid = '%s-%d' % (sid, seen[sid])
        else:
            seen[sid] = 0
        pl = [l.page for l in body_lines]
        # 降级节用 PDF 页码（见 _mk_page_section 的说明），其余走期刊页码表
        pages = pl if h.get('pdfOnly') else [jp(p) for p in pl]
        hp = (h.get('page') or (pages[0] if pages else 0)) if h.get('pdfOnly') \
            else (jp(h['page']) if h.get('page') else (pages[0] if pages else 0))
        out.append({
            'id': sid,
            'title': h['text'],
            'rawTitle': h.get('rawTitle', h['text']),
            'level': h.get('level', 2),
            'parent': h.get('parent', ''),
            'pageRange': [min(pages + [hp]), max(pages + [hp])],
            'pdfPages': [min(pl + [h.get('page') or pl[0]]), max(pl + [h.get('page') or pl[0]])],
            'sourceText': text,
            'chars': len(text),
            **( {'fallback': h['fallback']} if h.get('fallback') else {} ),
        })

    # 期刊首页常有个 "Sections" 目录框，把全文标题又列了一遍。同一标题出现两次时
    # **留正文多的那个**（正文里那条），目录条目丢掉——否则会多出七八个空壳节。
    best = {}
    for s in out:
        k = _norm_head(s['title'])
        if k not in best or s['chars'] > best[k]['chars']:
            best[k] = s
    out = [s for s in out if best[_norm_head(s['title'])] is s]

    # "父级小节"不是节：像 Nature Reviews 那种排版，"Immunoreceptor BRS signalling mechanisms"
    # 底下只有一两句引子，正文全在它的子小节里。这种 200–700 字的壳留在列表里，
    # AI 从那么点字里做不出图（实测连试 3 次都过不了校验），用户还会看到"这一节没图"。
    # 处理：**并进下一节**（不是删掉——那几句原文是有用的），并从列表里去掉。
    merged, i = [], 0
    while i < len(out):
        s = out[i]
        if s['chars'] < STUB_CHARS and i + 1 < len(out):
            nxt = out[i + 1]
            nxt['sourceText'] = (s['sourceText'] + ' ' + nxt['sourceText']).strip()
            nxt['chars'] = len(nxt['sourceText'])
            nxt['pageRange'][0] = min(nxt['pageRange'][0], s['pageRange'][0])
            nxt['pdfPages'][0] = min(nxt['pdfPages'][0], s['pdfPages'][0])
            nxt['mergedFrom'] = (nxt.get('mergedFrom') or []) + [s['title']]
            i += 1
            continue
        merged.append(s)
        i += 1
    out = merged
    seen = {}
    for s in out:                       # 去重之后再定 id，别留下 "-1" 这种尾巴
        base = slugify(s['title'])
        seen[base] = seen.get(base, 0) + 1
        s['id'] = base if seen[base] == 1 else '%s-%d' % (base, seen[base])

    return {
        'schemaVersion': 'p3a-1',
        'paperId': paper_id or os.path.splitext(os.path.basename(pdf_path))[0],
        'engine': 'core/sectioner.py',
        'doc': {
            'pdfPages': doc.page_count,
            'bodyFont': body_group[0],
            'bodySize': body_group[1],
            'pageMap': {str(k): v for k, v in sorted(jmap.items())},
        },
        'sections': out,
        'excluded': [{'title': h['text'], 'page': jp(h['page']), 'level': h['level']}
                     for h in excluded],
        'toc': [{'title': h['text'], 'level': h['level'], 'page': jp(h['page']),
                 'container': h.get('container', False)} for h in body_heads],
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    debug = '--debug' in sys.argv
    if not args:
        print(__doc__)
        return
    r = sectionize(args[0], debug=debug)
    print('\n===== 分节结果：%d 节 =====' % len(r['sections']))
    for s in r['sections']:
        print('  %-46s p.%-9s %6d 字  %s'
              % (s['id'][:46], '%d-%d' % tuple(s['pageRange']), s['chars'], s['title'][:44]))
    if r['excluded']:
        print('\n-- 正文区之后（未收）--')
        for e in r['excluded'][:12]:
            print('   L%d p%-4s %s' % (e['level'], e['page'], e['title'][:60]))
        if len(r['excluded']) > 12:
            print('   … 另有 %d 条' % (len(r['excluded']) - 12))
    if '--json' in sys.argv:
        p = sys.argv[sys.argv.index('--json') + 1]
        json.dump(r, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('\n写出 %s' % p)


if __name__ == '__main__':
    main()
