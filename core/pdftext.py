# -*- coding: utf-8 -*-
"""
PaperIDE · 文本规范化（唯一真源）

P0 的 `_extract/make_source.py` 里有一套清洗规则，校验器（pipeline.py）和渲染器
（index.html 的 highlightQuotes）都依赖它——**源文本和引文必须用同一套规则改**，
否则"校验过不了 / 左栏高亮打不中"（P0 那两节就栽在这）。

现在分节从"手写行号"换成了"读 PDF"，清洗规则必须原样搬过来，所以抽成这个模块：
    make_source.py（旧）  ─┐
    自动分节（新）        ─┴─→ 本文件 clean() ─→ sourceText ─→ 校验器 / 渲染器
旧脚本仍然独立可跑（P0 回归基准不能动），但新代码一律从这里 import。
"""
import re

# PDF 提取会把希腊字母丢成 ASCII（CD3ε→CD3e、CD3ζ→CD3z、IFN-γ→IFN-g…），
# 照着念别扭、照抄引用还会对不上。**源文本和引文要用同一张表改**。
GREEK = [
    ('CD3e', 'CD3ε'), ('CD3d', 'CD3δ'), ('CD3g', 'CD3γ'), ('CD3z', 'CD3ζ'),
    ('IFN-g', 'IFN-γ'), ('TNF-a', 'TNF-α'), ('TGF-b', 'TGF-β'), ('IL2', 'IL-2'),
    ('TCRab', 'TCRαβ'), ('a-hCD3', 'α-hCD3'), ('a-CD3', 'α-CD3'),
    ('PLCg1', 'PLCγ1'), ('Plcg1', 'Plcγ1'),
    ('εd,', 'εδ,'), ('εg,', 'εγ,'), ('and zz', 'and ζζ'),
]

# 行内引文括号： " (Au-Yeung et al., 2018)" → ""
CITE = re.compile(r'\s*\(([^()]*?(?:19|20)\d\d[a-z]?(?:;[^()]*)?)\)')


def normalize_greek(t):
    """CD3 家族的多字符替换——与 make_source.py 逐字一致（含 'and zz' → 'and ζζ'）。

    注意：单字符的 CD3e/CD3d… 已经在 GREEK 里，这里不再扩，避免把
    "CD3 expression" 里的 'e' 也替掉这种误伤。"""
    for a, b in GREEK:
        t = t.replace(a, b)
    return t


def clean_text(t):
    """段落级清洗：连字/引号/破折号/断词/斜杠断行 → 单空格折叠 + 希腊化。

    与 make_source.py 的 clean() 的**后半段**等价（前半段的"按行删图注/页码"在
    新管线里改成按版式特征做，见 sectioner.py）。"""
    t = t.replace('ﬁ', 'fi').replace('ﬂ', 'fl')
    t = t.replace('’', "'").replace('‘', "'")
    t = t.replace('“', '"').replace('”', '"')
    t = t.replace('–', '-').replace('—', '--')
    t = t.replace('´', "'").replace('́', '')
    # 断词连字符（行尾 '-' + 换行）
    t = re.sub(r'-\s*\n\s*', '', t)
    # 斜杠处的换行：PDF 会在 "BRS/" 后断行，留着会变成 "BRS/ PRS"（引用永远对不上）
    t = re.sub(r'/\s*\n\s*', '/', t)
    # 引文括号
    t = CITE.sub('', t)
    # ⚠ PDF 里会混进**控制字符**（如 \x01——某些字体的特殊空格标记）。
    #   它不是 Python 的 \s，`re.sub(r'\s+')` 收不走，会原样留在正文里：
    #   原文变成 "efficiencies were \x0150%"，而人（和模型）引用时写的是普通空格
    #   → **引文永远对不上**（P3-b 实测在这栽了一次，10/11 就卡在这条）。
    t = re.sub(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]', ' ', t)
    # **不可见字符**：软连字符 U+00AD、零宽空格 U+200B、方向标记、BOM、word joiner。
    # 它们在排版里不显示，但**实实在在占着一个字符位**——模型（和人）引用时都不会打，
    # 于是引文永远对不上（Shen2026 那篇因此一节报了 37 条错）。
    # 软连字符直接删（它是断行提示，不是内容）；其余不可见的换成空格。
    t = t.replace('­', '')
    t = re.sub(r'[​-‏  ‪-‮⁠﻿]', ' ', t)
    # 各种"看起来像空格"的空白
    t = t.replace(' ', ' ').replace(' ', ' ').replace(' ', ' ')
    # 数学减号 U+2212 → ASCII '-'：模型打的是 '-'，留着就永久对不上
    t = t.replace('−', '-')
    t = re.sub(r'\s+', ' ', t).strip()
    return normalize_greek(t)


def join_lines(lines):
    """把一节的若干物理行接成正文：
    1) 行尾连字符断词要愈合（"phosphor-" + "ylation" → "phosphorylation"）
    2) 其余按空格接（PDF 的硬换行不是语义换行）
    3) 再走一遍 clean_text
    """
    buf = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        buf.append(s)
    return clean_text('\n'.join(buf))


# ------------------------------------------------------------------ 切句
ABBR = re.compile(r'(?:^|[\s(])(?:Fig|Figs|Table|Tables|Suppl|Supp|Ref|Refs|No|vs|approx|'
                  r'et al|e\.g|i\.e|Dr|Mr|Ms|St|Sec|Eq)\.$', re.I)


def sentences(text):
    """与 P0/_extract/pipeline.py 和 index.html splitSentences 同款切句。
    **切句规则三处必须一致**（校验器 / 渲染器 / 这里），否则校验过了高亮还是打不中。
    点后面跟数字不算句末（CD19.28Z / 0.98），常见缩写和单字母缩写也不算。"""
    out, last = [], 0
    for m in re.finditer(r'[.!?]+(?=\s|$)', text):
        at = m.start()
        after = text[at + 1:at + 2]
        head = text[last:at]
        if after and after.isdigit():
            continue
        # ⚠ 判缩写的片段必须**把句号本身也算进来**（seg 而不是 head）：
        #   ABBR 的正则以 `\.$` 结尾，而句号在 m 里、不在 head 里——拿 head 去比，
        #   "Fig." 永远判不出来，于是 "…(Fig. 2b). Conversely…" 被劈成
        #   "…(Fig." + "2b). " 两个碎片句，跨句的引文一条都对不上。
        seg = text[last:m.end()]
        if ABBR.search(seg):
            continue
        if re.search(r'(?:^|\s)[A-Z]\.$', seg):
            continue
        end = m.end()
        while end < len(text) and text[end].isspace():
            end += 1
        out.append(text[last:end])
        last = end
    if last < len(text):
        out.append(text[last:])
    return out or [text]
