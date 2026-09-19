# -*- coding: utf-8 -*-
"""
PaperIDE · 引文吸附（把"差不多对"的引文修回原文的真实片段）

**为什么需要**：模型的引文十次里有八次不是"原样复制"，而是**记忆式复述**——
空白多一个少一个、破折号写成连字符、弯引号变直引号、首字母大小写变了、
偶尔中间少一个词。校验器判它"找不到"，于是整份打回重试。
第一轮验收里 **每一节都要重试一次**才过，就是这么来的——很贵，而且没必要。

**修法**：不是放宽校验（那会让溯源失真），而是**把引文吸附回原文**——
在原文里找到它对应那一段的**精确下标区间**，把引文替换成那段真文本。
这样：
  · 溯源依然是真的（引文 = 原文里的原样片段，一个字没编）
  · 不用重试，也不用降级

做法：把原文做一次"归一化 + 下标映射"（折叠空白、统一引号/破折号），
在归一化空间里找位置，再用映射换回**原文里的真实起止下标**。
"""
import re

# 归一化时要"看成一个东西"的字符
FOLD = {
    '‘': "'", '’': "'", '‚': "'", '‛': "'",
    '“': '"', '”': '"', '„': '"',
    '‐': '-', '‑': '-', '‒': '-', '–': '-', '—': '-',
    '―': '-', '−': '-', 'ﬀ': 'ff', 'ﬁ': 'fi', 'ﬂ': 'fl',
    'ﬃ': 'ffi', 'ﬄ': 'ffl',
}
_CTRL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f­​-‏⁠﻿]')
MIN_LEN = 10          # 短于此的引文不猜（"ITAM" 这种到处都有，猜出来是害人）
MIN_SNAP = 40         # 只吸附"能对上的那一段"时，至少要有这么长（太短没意义）

# **宽松空间**（只用来"在原文里找位置"，不用来产出）：大小写不分 + 希腊字母按形近拉丁字母算。
# 为什么需要：模型抄原文时**希腊字母经常写成拉丁字母**——实测 eLife 那篇，
#   原文 `FcRγ` 被抄成 `FcRV`、`αCD22` 被抄成 `aCD22`，引文于是"找不到"，
#   14622 字的 Results 连挂 3 次、掉进"最简版"兜底只出 1 张图（原 3 张）。
# 折成同一类之后头尾就能对上；**吸附回去的仍然是原文里的真片段**（见 _span），溯源不失真。
_CONF = {
    'γ': 'v', 'Γ': 'v', 'α': 'a', 'Α': 'a', 'β': 'b', 'Β': 'b', 'δ': 'd', 'Δ': 'd',
    'ε': 'e', 'Ε': 'e', 'ζ': 'z', 'Ζ': 'z', 'η': 'n', 'Η': 'n', 'θ': 't', 'Θ': 't',
    'ι': 'i', 'Ι': 'i', 'κ': 'k', 'Κ': 'k', 'λ': 'l', 'Λ': 'l', 'μ': 'u', 'Μ': 'u',
    'ν': 'v', 'Ν': 'n', 'ξ': 'x', 'Ξ': 'x', 'π': 'p', 'Π': 'p', 'ρ': 'p', 'Ρ': 'p',
    'σ': 's', 'Σ': 's', 'ς': 's', 'τ': 't', 'Τ': 't', 'υ': 'u', 'Υ': 'y', 'φ': 'f',
    'Φ': 'f', 'χ': 'x', 'Χ': 'x', 'ψ': 'y', 'Ψ': 'y', 'ω': 'w', 'Ω': 'w',
}


def _fold_map(s, loose=False):
    """归一化 + 保留映射：返回 (归一化串, [归一化第 i 个字符在原串里的下标])

    ⚠ 下标表**逐字符**展开：`ﬀ` 这类连字折叠后是 2 个字符（"ff"），
      只记一个下标会让后面所有下标错位（老版本就是这个 bug，遇连字必错位）。
    """
    out, idx = [], []
    prev_sp = False
    for i, ch in enumerate(s):
        if _CTRL.match(ch):
            continue
        c = FOLD.get(ch, ch)
        if loose:
            c = ''.join(_CONF.get(x, _CONF.get(x.lower(), x.lower())) for x in c)
        if c.isspace():
            if prev_sp:
                continue
            c, prev_sp = ' ', True
        else:
            prev_sp = False
        out.append(c)
        idx.extend([i] * len(c))
    return ''.join(out), idx


def _span(source, idx, j0, j1):
    """归一化空间的下标区间 [j0, j1) → **原文里的真实片段**（引文永远来自原文）"""
    if j0 < 0 or j0 >= len(idx) or j1 <= j0:
        return None
    j1 = min(j1, len(idx))
    return source[idx[j0]:idx[j1 - 1] + 1]


def _anchor(source, ns, idx, nq, max_span):
    """头 40 字 + 尾 30 字各定一端，中间允许被复述改掉"""
    if len(nq) < MIN_LEN:
        return None
    head, tail = nq[:40], nq[-30:]
    j0 = ns.find(head)
    if j0 < 0:
        return None
    k = ns.find(tail, j0 + len(head))
    if k < 0:
        return None
    end = k + len(tail)
    if end - j0 > max_span:
        return None                      # 拼出来太长，多半是跨句误拼
    return _span(source, idx, j0, end)


def _longest_run(source, ns, idx, nq):
    """**最长能对上的那一段**：模型把引文一头写坏了（多写/串行/写错）时，
    从头尽量往后对、从尾尽量往前对，取更长的那段，返回的仍是**原文**片段
    （不是模型的文字）——所以即使只吸附到半句，也是原文里真有的半句。"""
    if len(nq) < MIN_SNAP:
        return None
    best = None
    j0 = ns.find(nq[:MIN_SNAP])                      # 从开头往后对
    if j0 >= 0:
        k = MIN_SNAP
        while j0 + k < len(ns) and k < len(nq) and ns[j0 + k] == nq[k]:
            k += 1
        best = (j0, j0 + k)
    t = nq[-MIN_SNAP:]                               # 从尾巴往前对
    j1 = ns.find(t)
    if j1 >= 0:
        a = j1
        k = 1
        while a - k >= 0 and k <= len(nq) and ns[a - k] == nq[len(nq) - 1 - k]:
            k += 1
        cand = (a - k + 1, j1 + len(t))
        if best is None or (cand[1] - cand[0]) > (best[1] - best[0]):
            best = cand
    if not best:
        return None
    a, b = best
    if b - a < MIN_SNAP:
        return None
    a, b = _trim_words(ns, a, b)          # 别把半个单词截出来当引文
    if b - a < MIN_SNAP * 0.6:
        return None
    seg = _span(source, idx, a, b)
    return seg.strip() if seg else None


def _trim_words(ns, a, b):
    """把 [a, b) 收到完整单词边界上（只收**真截在词中间**的那一端）

    ⚠ 判据是"端点旁边的那个字符是不是字母/数字"，不是"端点是不是空格"——
      对到 `…Bai1, and MerTK` 这种**正好停在完整单词后面**的，就别再往回退了
      （退回去会把一个本来挺好的长引文砍短）。
    """
    if a > 0 and a < len(ns) and ns[a - 1].isalnum():          # 左端落在词中间
        k = ns.find(' ', a)
        if k < 0 or k >= b:
            return a, b
        a = k + 1
    if b > 0 and b < len(ns) and ns[b - 1].isalnum() and ns[b].isalnum():   # 右端落在词中间
        k = ns.rfind(' ', a, b)
        if k > a:
            b = k
    return a, b


def repair(quote, source):
    """返回 (吸附后的引文, 是否改过)。改不了就原样回，交给校验器判。"""
    q = (quote or '').strip()
    if not q:
        return quote, False
    if q in source:                                   # 本来就中，不动
        return q, False
    nq, _ = _fold_map(q)
    nq = nq.strip()
    if len(nq) < MIN_LEN:
        return quote, False
    ns, idx = _fold_map(source)

    # ① 整个引文（归一化后）在原文里 → 直接换回真实下标区间
    j = ns.find(nq)
    if j < 0 and len(ns) == len(ns.lower()):
        j = ns.lower().find(nq.lower())               # 大小写差异
    if j >= 0 and j + len(nq) <= len(idx):
        return _span(source, idx, j, j + len(nq)), True

    # ② 中间被复述改掉了 → 用**头**定位起点、**尾**定位终点（只信两端）
    #    例：模型写 "…inhibitory Csk kinase to attenuate"，原文是
    #        "…inhibitory Csk kinase to attenuate TCR signaling"，尾对不上就退回①
    got = _anchor(source, ns, idx, nq, len(nq) * 1.6)
    if got:
        return got, True

    # ③ 同样是头尾定位，但换到**宽松空间**里找：大小写不分、希腊字母按形近拉丁算
    #    （FcRγ/FcRV、αCD22/aCD22 这类"字形抄错"，只有这一步能救）
    lq, _ = _fold_map(q, loose=True)
    lq = lq.strip()
    ls = lidx = None
    if len(lq) >= MIN_LEN:
        ls, lidx = _fold_map(source, loose=True)
        j = ls.find(lq)
        if j >= 0:
            return _span(source, lidx, j, j + len(lq)), True
        got = _anchor(source, ls, lidx, lq, len(lq) * 1.6)
        if got:
            return got, True

    # ④ 最后一招：只吸附到**能对上的那一段**（模型多抄了 / 抄串了半句时的兜底）。
    #    产出的仍是原文片段，只是短一点；比"整节打回、掉进最简版兜底"划算得多。
    got = _longest_run(source, ns, idx, nq)
    if not got and len(lq) >= MIN_LEN:
        got = _longest_run(source, ls, lidx, lq)
    if got:
        return got, True
    return quote, False


def repair_view(view):
    """把一个视图里所有 evidence 的引文都过一遍。返回改了几条。"""
    n = 0

    def do(evs):
        nonlocal n
        for ev in (evs or []):
            if isinstance(ev, dict) and ev.get('quote'):
                new, changed = repair(ev['quote'], SOURCE[0])
                if changed:
                    ev['quote'] = new
                    n += 1

    for node in view.get('nodes', []):
        do(node.get('evidence'))
    for edge in view.get('edges', []):
        do(edge.get('evidence'))
    dom = view.get('domain') or {}
    for t in dom.get('tracks', []):
        do(t.get('evidence'))
        for s in t.get('segments', []):
            do(s.get('evidence'))
    mat = view.get('matrix') or {}
    for r in mat.get('rows', []):
        for cell in (r.get('cells') or {}).values():
            do(cell.get('evidence'))
    return n


SOURCE = [None]        # 当前节的正文（模块级传参，省得每层都塞一个参数）


def repair_section(sec_obj):
    """把一整节里所有引文吸附回本节正文。返回改了几条。"""
    SOURCE[0] = sec_obj.get('sourceText', '')
    n = 0
    for v in sec_obj.get('views', []):
        n += repair_view(v)
    return n
