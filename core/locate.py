# -*- coding: utf-8 -*-
"""
PaperIDE · 文字 → PDF 坐标定位（P3-c）

标注层要画在 PDF 上，就得把"一个表达"变成"页面上几个矩形"。这件事主人那边
**已经有实测 113/113 命中的机器**（`E:\\CAR-T重编程\\表达库\\_annotate_pdf.py`），
规划 §3.4 说它可直接复用为 `evidence.quote → PDF 位置` 的定位引擎。这里就是它。

三个真坑（主人的机器都已经处理过，照搬）：
  1. **连字**：PDF 里 `quantiﬁed` 的 ﬁ 是单个字符，跟 "fi" 不是一回事
  2. **跨行断词**：`elec-trostatically` 在文本层是两个词，要合并后再匹配
  3. **大小写与标点**：比对两边都要归一化，不能只归一化一边
"""
import re

import fitz

LIGS = [('\ufb01', 'fi'), ('\ufb02', 'fl'), ('\ufb00', 'ff'),
        ('\ufb03', 'ffi'), ('\ufb04', 'ffl'), ('\u2019', "'")]
_SPLIT = re.compile(r"[^a-z0-9\-+/ ]")


def norm(s):
    """归一化：小写 + 连字展开 + 只留字母数字/-+/ 和空格"""
    s = s.lower()
    for a, b in LIGS:
        s = s.replace(a, b)
    return _SPLIT.sub('', s).strip()


def build_tokens(page):
    """词表 → [(归一化词, [rect...], 原始词)]，**跨行断词已合并**"""
    raw = [(norm(w[4]).strip('-'), fitz.Rect(w[:4]), w[4]) for w in page.get_text('words')]
    raw = [t for t in raw if t[0]]
    out, i = [], 0
    while i < len(raw):
        w, r, orig = raw[i]
        if orig.rstrip().endswith('-') and i + 1 < len(raw):
            w2, r2, _ = raw[i + 1]
            out.append((w + w2, [r, r2], orig + raw[i + 1][2]))
            i += 2
        else:
            out.append((w, [r], orig))
            i += 1
    return out


def find_in_tokens(toks, need):
    """在词表里找连续匹配，返回 [(rects, 起点下标)]"""
    n, hits = len(need), []
    if n == 0:
        return hits
    for i in range(len(toks) - n + 1):
        ok = True
        for j in range(n):
            a, b = toks[i + j][0], need[j]
            if a != b and not (len(b) > 4 and a.startswith(b)) and not (len(a) > 4 and b.startswith(a)):
                ok = False
                break
        if ok:
            rects = []
            for j in range(n):
                rects += toks[i + j][1]
            hits.append((rects, i))
    return hits


def _candidates(text):
    """字面 → 候选检索词序列（先全串，再退化到去掉首尾虚词/标点的版本）"""
    c = [text.strip()]
    c.append(re.sub(r'^[^A-Za-z0-9]+|[^A-Za-z0-9]+$', '', text))
    c.append(re.sub(r'^(the|a|an|of|in|to|and|or)\s+', '', text.strip(), flags=re.I))
    out, seen = [], set()
    for x in c:
        k = norm(x)
        if k and k not in seen:
            seen.add(k)
            out.append((norm(x).split(), x))
    return out


def locate(doc, text, pages=None, max_hits=3):
    """把一个表达定位到 PDF 坐标。

    pages: 只在这些（1-based）PDF 页里找——节内定位能少扫一大半，也避免同形词串页。
    返回 [{'pdfPage', 'rects'(归一化 0–1)}]；找不到返回 []。
    """
    if not text or len(text.strip()) < 2:
        return []
    rng = pages or range(1, doc.page_count + 1)
    out = []
    for cand_words, _ in _candidates(text):
        if not cand_words:
            continue
        for pno in rng:
            if pno < 1 or pno > doc.page_count:
                continue
            if any(o['pdfPage'] == pno for o in out):
                continue
            page = doc[pno - 1]
            W, H = page.rect.width, page.rect.height        # ⚠ 这是**显示**尺寸（已含旋转）
            # ⚠ 旋转页坐标（2026-09-19 修）：`get_text('words')` 给的是**未旋转**坐标系里的
            #    rect，而 `page.rect` 是**显示**尺寸 —— 直接相除在 `/Rotate≠0` 的 PDF 上会算错
            #    （实测 /Rotate=90 时：未旋转 400×600、显示 600×400，坐标不换算就整个错位）。
            #    `rotation_matrix` 正是"未旋转 → 显示"的映射。
            #    **`/Rotate=0` 时它是单位矩阵**，所以这次修对库里现有 11 篇（全是 0 度）
            #    **逐位无变化** —— 属于"只为将来正确、不改变现状"的修法。
            rm = page.rotation_matrix
            for rects, _i in find_in_tokens(build_tokens(page), cand_words)[:max_hits]:
                # 逐行合并碎片矩形：同一行的相邻小块接起来，否则高亮是一段段断的
                rows = {}
                for r in rects:
                    rows.setdefault(round(r.y0, 1), []).append(r)
                merged = []
                for y in sorted(rows):
                    rs = sorted(rows[y], key=lambda r: r.x0)
                    cur = rs[0]
                    for r in rs[1:]:
                        if r.x0 - cur.x1 <= 2.5:
                            cur = fitz.Rect(cur.x0, min(cur.y0, r.y0), r.x1, max(cur.y1, r.y1))
                        else:
                            merged.append(cur)
                            cur = r
                    merged.append(cur)
                norm = []
                for r in merged:
                    rr = r * rm                             # 未旋转 → 显示（rot=0 时原样）
                    norm.append([round(rr.x0 / W, 4), round(rr.y0 / H, 4),
                                 round(rr.x1 / W, 4), round(rr.y1 / H, 4)])
                out.append({'pdfPage': pno, 'rects': norm})
        if out:
            break
    return out


def journal_pages(doc):
    """PDF 页 → 期刊页码。没有就退回 PDF 页码（返回 {} 里的缺项）。

    ⚠ 两个坑都踩过（卡片上显示成 "p.2020"）：
      ① 页码在页脚那一行的**最右侧**，而同一行还有 "Cell 182, 855–871, August 20, 2020"
         ——按词匹配会把**年份**当页码。所以只认**整行纯数字**的行（"2020" 不是独立行）。
      ② 页面左右边缘才是页码位（页码要么在最左要么在最右），中间的纯数字多半是别的。
    """
    m = {}
    for pno in range(doc.page_count):
        page = doc[pno]
        H, W = page.rect.height, page.rect.width        # 显示尺寸（已含旋转）
        rm = page.rotation_matrix                        # 未旋转 → 显示（rot=0 时单位矩阵）
        best = None
        for b in page.get_text('dict')['blocks']:
            if b.get('type') != 0:
                continue
            for l in b['lines']:
                t = ''.join(s['text'] for s in l['spans']).strip()
                if not re.fullmatch(r'\d{1,4}', t):
                    continue
                # ⚠ 同 locate()：bbox 是未旋转坐标，先映射到显示坐标再套"贴边 + 靠下"这套判据
                bb = fitz.Rect(l['bbox']) * rm
                x0, y0, x1 = bb.x0, bb.y0, bb.x1
                if y0 < H * 0.90:                       # 页码在最底下那一带
                    continue
                edge = min(x0, W - x1)                  # 离左右边缘有多近
                if edge > W * 0.12:
                    continue
                score = (edge, -y0)                     # 先看贴不贴边，再看靠不靠下
                if best is None or score < best[0]:
                    best = (score, int(t))
        if best:
            m[pno + 1] = best[1]
    # 页码必须随 PDF 页递增；跳变的一律作废（宁可退回 PDF 页码，也不给错的）
    seq = sorted(m.items())
    for i in range(1, len(seq)):
        (pa, va), (pb, vb) = seq[i - 1], seq[i]
        if pb > pa and vb < va:
            m.pop(pb, None)
    return m
