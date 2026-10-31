# -*- coding: utf-8 -*-
"""
PaperIDE · LLM 语义分节（2026-10-25）

主人 2026-10-25 定的口径：
    「分节器不能只依赖程序……不如让模型在分析全文的时候顺便给出分节信息。
      大模型直接理解语义，不太可能弄错；自动程序可以用来辅助大模型。」

所以这一层的分工是：

    程序（core/sectioner.py）                 模型（本文件 + prompts/structure.md）
    ─────────────────────────                 ───────────────────────────────────
    · 从 PDF 提行、字号/字体/坐标              · 通读全文，判断"这篇论文到底分几节"
    · 找候选标题（可能有假、可能漏）           · 定层级 / 父子关系 / 哪些是后置内容
    · 给每行一个稳定的 L 行号                  · 给出每节的标题原文 + 锚点行
    · 按模型给的锚点切正文、算页码             · 标题改名、少一节、字体变了都不怕
    · 模型失败 / 没配 Key → 自动退回版式分节

★ 为什么不让模型直接吐"第几行到第几行"：
  模型对长数字的复制不如对文字稳。让它同时给 `anchorLine` 和 `anchor`（原文），
  程序在 L 行号附近按文本相似度**再对一次**——两边都错才定位失败。
  实测模型偶尔把 L 号差 1/差几行，这一步能自动纠回来。

★ 为什么正文仍由程序切：
  · `sourceText` 必须和 PDF 行一一对应，下游标注/高亮/引文校验全靠它；
  · 模型只负责"结构语义"，不负责"字节搬运"——这是它最不该干的活。

★ 失败策略：**绝不因为 LLM 挂了让论文进不了库**。
  退回 `sectioner.sectionize()` 的版式分节，并在 sections.json 里记
  `structureSource: "heuristic"` / `structureError`，界面上仍能读、能分节。
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import fitz                                        # noqa: E402

from core import adapters, pdftext, paths, sectioner   # noqa: E402

MAX_SECTIONS = 90            # 模型最多给多少节（防跑飞；正常论文 10–40）
MAX_TEXT_CHARS = 240000      # 全文超过这个长度改用"候选附近"压缩版，防上下文/费用失控
MIN_BODY_CHARS = 80          # 短于此的节：有子节 → 容器；没子节 → 并进下一节
MIN_COVER = 0.30             # 正文覆盖率低于此 → 认为模型结构不可用，退回版式分节
WARN_COVER = 0.55            # 低于此只告警（仍采用）


class StructureError(RuntimeError):
    """模型结构不可用（JSON 残缺 / 锚点全对不上 / 覆盖率太低）。"""


def load_prompt(name):
    return open(paths.res('prompts', name + '.md'), encoding='utf-8').read()


def _norm(s):
    """归一化：与分节器/引文校验同一套清洗（连字、希腊字母、不可见字符）。"""
    return re.sub(r'\s+', ' ', pdftext.clean_text(s or '')).strip().lower()


def _is_end_head(text):
    """是不是「正文之后」的标题（Methods / References / 致谢……）。"""
    nu = sectioner._norm_head(text)
    for e in sectioner.END_HEADINGS:
        if nu == e or nu.startswith(e + ' '):
            return True
    return False


def _is_hard_end(text):
    """真正代表正文结束的标题（References / 致谢 / 补充材料），不含 Methods。

    为什么单独一张表：J Immunol 这类版式把 Methods 放在 Results **前面**，
    拿 Methods 当"正文终点"会把后面的 Results/Discussion 全切掉（3-C 的根因之一）。"""
    nu = sectioner._norm_head(text)
    for e in sectioner.HARD_END_HEADINGS:
        if nu == e or nu.startswith(e + ' '):
            return True
    return False


# ==================================================================== 给模型的材料
def build_payload(lines, heads):
    """把"候选标题 + 带 L 行号的全文"拼成模型输入。

    返回 (payload, mode)；mode = 'full' | 'compact'。
    compact 只用于超长综述：保留每页开头 + 每个候选标题附近几行，仍带真实 L 号。
    """
    pos = {id(l): i for i, l in enumerate(lines)}
    cand = []
    for h in heads[:240]:
        i = pos.get(id(h['lines'][0]))
        if i is None:
            continue
        cand.append('- L%d p%d | %s | %s %.1fpt | 程序猜 L%d'
                    % (i, h.get('page') or lines[i].page, h['text'],
                       (h.get('font') or '')[:24], float(h.get('size') or 0),
                       h.get('level', 2)))
    cand_block = '\n'.join(cand) if cand else '（程序没找到任何候选标题）'

    def render(keep=None):
        parts, last_page = [], None
        for i, l in enumerate(lines):
            if keep is not None and i not in keep:
                continue
            if l.role not in ('head', 'prose', 'small'):
                continue
            t = pdftext.clean_text(l.text)
            if not t:
                continue
            if l.page != last_page:
                parts.append('\n[PDF 第 %d 页]' % l.page)
                last_page = l.page
            parts.append('L%d| %s' % (i, t))
        return '\n'.join(parts)

    body = render()
    mode = 'full'
    if len(body) > MAX_TEXT_CHARS:
        mode = 'compact'
        keep = set(range(min(200, len(lines))))              # 标题/摘要通常在最前面
        for h in heads:
            gi = pos.get(id(h['lines'][0]))
            if gi is None:
                continue
            keep.update(range(gi, min(len(lines), gi + 3)))  # 标题 + 后两行
        keep.update(range(max(0, len(lines) - 60), len(lines)))   # 文末 References 附近
        body = render(keep)

    payload = ('## 一、程序检测到的候选标题\n'
               '（可能有假标题，也可能漏；`程序猜 Lx` 只是版式线索，**不要信**）\n\n'
               + cand_block +
               '\n\n## 二、论文全文（按 PDF 页；每行左侧 L 是定位行号）\n'
               + ('> ⚠ 原文过长，这里是压缩版（每页开头 + 每个候选标题附近几行）；\n'
                  '> 结构判断仍按你读到的内容来，anchorLine 必须用**真实 L 号**。\n'
                  if mode == 'compact' else '')
               + body)
    return payload, mode


# ==================================================================== 锚点定位
def _find_line(item, lines, ntexts):
    """把模型给的 (anchorLine, anchor, title, page) 对回真实行号。

    评分优先级：原文精确 > 前缀 > 包含；标题命中再加分；标题行本身 +25；
    页码对上 +15；离 anchorLine 越近加分越多。**不追求一步到位，追求对不上就认输**。"""
    anchor = _norm(item.get('anchor') or '')
    # 模型可能把 `L123| ` 前缀一起抄回来；也可能用省略号截断
    if anchor.startswith('l') and '|' in anchor:
        anchor = anchor.split('|', 1)[1].strip()
    anchor = re.split(r'…|\.\.\.', anchor)[0].strip()
    title = _norm(item.get('title') or '')
    page = item.get('page')
    try:
        al = int(item.get('anchorLine'))
    except (TypeError, ValueError):
        al = None

    best_i, best_s = None, 0
    for i, t in enumerate(ntexts):
        s = 0
        if anchor:
            if t == anchor:
                s += 120
            elif t.startswith(anchor):
                s += 85
            elif anchor in t:
                s += 60
            elif len(t) >= 4 and t in anchor:
                s += 40
        if title:
            if t == title:
                s += 100
            elif t.startswith(title):
                s += 70
            elif title in t:
                s += 40
        if lines[i].role == 'head':
            s += 25
        if page and lines[i].page == page:
            s += 15
        if al is not None:
            d = abs(i - al)
            if d == 0:
                s += 30
            elif d <= 3:
                s += 20
            elif d <= 40:
                s += 10
        if s > best_s:
            best_s, best_i = s, i
    return best_i if best_s >= 45 else None


# ==================================================================== 组装
def _join_body(body, body_size):
    """把一节的行拼成正文；顺带愈合**首字下沉**。

    J Immunol 的 Introduction 第一个词渲染成两行：`C`（41.8pt）+ `D8+ T lymphocytes…`。
    直接按空格拼会得到 "C D8+…"——人读得懂，但模型引用 `CD8+…` 时在 sourceText 里
    永远搜不到，校验器会把好引文判成"不是原文子串"（溯源铁律不允许这么干）。
    判据收紧到"第一节行、1–2 个字母、字号明显大于正文"才直接黏合，避免误伤图注字母。

    另外剥掉行内摘要标签（`ABSTRACT: …` / `Summary: …`）：标签不是正文，
    但同一行后面的摘要第一句必须保留。"""
    texts = [l.text for _, l in body]
    if texts:
        texts[0] = re.sub(r'^(?:abstract|summary)\s*[:：]\s*', '', texts[0].strip(),
                          flags=re.I).strip()
    if len(texts) >= 2:
        a, b = texts[0].strip(), texts[1].strip()
        first = body[0][1]
        if (1 <= len(a) <= 2 and a.isalpha() and b[:1].isalnum()
                and getattr(first, 'size', 0) > body_size * 1.5):
            texts = [a + b] + texts[2:]
    return pdftext.join_lines(texts)


def assemble(doc, lines, heads, raw, paper_id, llm_meta, body_group, debug=False):
    """模型的章节树 + 程序的行数据 → sections.json 同款结构。

    ⚠ 输出格式必须和 `sectioner.sectionize()` 完全一致（下游一行都不用改），
      只**多**几个字段：structureSource / llm / warnings / 每节的 level+parent。
    """
    warnings = []
    if not isinstance(raw, dict):
        raise StructureError('模型返回的不是 JSON 对象')
    items = raw.get('sections')
    if not isinstance(items, list) or len(items) < 2:
        raise StructureError('模型只给了 %s 个正文节（至少要 2 个）'
                             % (len(items) if isinstance(items, list) else 0))

    ntexts = [_norm(l.text) for l in lines]
    pos = {id(l): i for i, l in enumerate(lines)}

    # ---------- 1. 正文节：解析 + 定位 + 按文档顺序排 ----------
    resolved, dropped = [], []
    for it in items[:MAX_SECTIONS]:
        if not isinstance(it, dict):
            continue
        title = re.sub(r'\s+', ' ', str(it.get('title') or '')).strip()
        if not title:
            continue
        gi = _find_line(it, lines, ntexts)
        if gi is None:
            dropped.append(title)
            continue
        try:
            level = int(it.get('level') or 2)
        except (TypeError, ValueError):
            level = 2
        resolved.append({
            'title': title,
            'level': max(1, min(3, level)),
            'parentTitle': str(it.get('parentTitle') or '').strip(),
            'kind': 'implicit' if str(it.get('kind') or '').lower() == 'implicit'
                    else 'heading',
            'page': int(it.get('page') or lines[gi].page),
            '_gi': gi,
        })
    resolved.sort(key=lambda r: r['_gi'])

    dedup = []
    for r in resolved:
        if dedup and r['_gi'] == dedup[-1]['_gi']:
            dropped.append(r['title'])
            continue
        dedup.append(r)
    resolved = dedup
    if len(resolved) < 2:
        raise StructureError('模型给的节里只有 %d 个能在原文里定位（至少 2 个）'
                             % len(resolved))
    if dropped:
        warnings.append('模型给的 %d 个节在原文里没定位到，已忽略：%s'
                        % (len(dropped), '、'.join(dropped[:6])))

    # ---------- 1a. 摘要兜底 ----------
    # (a) 结构化摘要：模型常把 "Introduction: …" 那一栏当成 Introduction，
    #     后面又出现真正的 "1 Introduction" → 两个同名 L1，第一个其实是摘要。
    for k in range(len(resolved) - 1):
        a, b = resolved[k], resolved[k + 1]
        na = sectioner._norm_head(a['title'])
        nb = sectioner._norm_head(b['title'])
        if (a['kind'] == 'implicit' and a['level'] == b['level']
                and na == nb and na == 'INTRODUCTION'):
            a['title'] = 'Abstract'
            warnings.append('检测到结构化摘要：第一个 "Introduction" 已改判为 Abstract')

    # (b) 模型完全漏掉 ABSTRACT 行（有的期刊摘要用小字号，模型容易漏）→ 程序补一条。
    #     这是"程序辅助模型"的一处：模型判语义，程序兜住版式上的显式标记。
    has_abs = any(_norm(r['title']).startswith(('abstract', 'summary'))
                  for r in resolved)
    if not has_abs and resolved:
        pat_abs = re.compile(r'^\s*(abstract|summary)\b\s*[:：]?', re.I)
        for i in range(min(200, len(lines))):
            l = lines[i]
            if l.role not in ('prose', 'small', 'head'):
                continue
            t = (l.text or '').strip()
            m = pat_abs.match(t)
            if not m or l.page > 3:             # 只在前 3 页找摘要
                continue
            if i >= resolved[0]['_gi'] - 1:     # 第一节已经从这里/更早开始，不重复
                break
            rest = t[m.end():].strip()
            resolved.insert(0, {
                'title': 'Abstract', 'level': 1, 'parentTitle': '',
                'kind': 'heading' if (l.role == 'head' and len(rest) < 20) else 'implicit',
                'page': l.page, '_gi': i,
            })
            warnings.append('模型漏掉摘要，已按原文 ABSTRACT 行补一节')
            break
    resolved.sort(key=lambda r: r['_gi'])

    # ---------- 1b. 首字下沉（drop cap）吸回本节 ----------
    # J Immunol 2014 这篇的 Introduction 第一个词是首字下沉："C" 独占一行（41.8pt），
    # 下一行才是 "D8+ T lymphocytes…"。模型按语义把 Introduction 指到 "D8+…" 那行，
    # 如果不管，那个 "C" 会被留在上一节（Abstract）末尾，两节各断一半。
    # 判据收紧：① 上一行只有 1–2 个字母；② 同一页、垂直紧邻；③ 是正文行（不是图注标签）。
    for r in resolved:
        if r['kind'] != 'implicit':
            continue
        i = r['_gi']
        if i <= 0:
            continue
        p, cur = lines[i - 1], lines[i]
        t = (p.text or '').strip()
        if (p.page == cur.page and 1 <= len(t) <= 2 and t.isalpha()
                and p.role in ('prose', 'symbol')
                and abs(cur.y0 - p.y0) <= 90):
            r['_gi'] = i - 1

    # ---------- 2. 父子关系 ----------
    by_title = {}
    for r in resolved:
        by_title.setdefault(_norm(r['title']), r)
    last_l1 = None
    for r in resolved:
        if r['level'] <= 1:
            r['parent'] = ''
            last_l1 = r['title']
            continue
        pn = _norm(r['parentTitle'])
        p = by_title.get(pn) if pn else None
        if p is not None and p is not r and p['level'] < r['level']:
            r['parent'] = p['title']
        elif r['parentTitle']:
            r['parent'] = r['parentTitle']
        else:
            r['parent'] = last_l1 or ''

    # 有子节 = 紧跟着的下一个节层级更深（文档顺序已经排好，这比 parentTitle
    # 字符串匹配稳：模型偶尔把父节标题抄错一个词，层级关系仍然是对的）
    for k, r in enumerate(resolved):
        r['_has_children'] = (k + 1 < len(resolved)
                              and resolved[k + 1]['level'] > r['level'])

    # ---------- 3. 后置内容（excluded）：模型给的 + 程序兜底 ----------
    excl, seen_e = [], set()
    for e in (raw.get('excluded') or [])[:MAX_SECTIONS]:
        if not isinstance(e, dict):
            continue
        t = re.sub(r'\s+', ' ', str(e.get('title') or '')).strip()
        if not t:
            continue
        gi = _find_line(e, lines, ntexts)
        if gi is None:
            warnings.append('后置标题没定位到：%s' % t)
            continue
        try:
            lv = int(e.get('level') or 1)
        except (TypeError, ValueError):
            lv = 1
        key = (gi, _norm(t))
        if key in seen_e:
            continue
        seen_e.add(key)
        excl.append({'title': t, 'page': lines[gi].page, 'level': lv, '_gi': gi})

    first_gi = resolved[0]['_gi']
    have_gi = {e['_gi'] for e in excl} | {r['_gi'] for r in resolved}
    for h in heads:
        gi = pos.get(id(h['lines'][0]))
        if gi is None or gi <= first_gi or gi in have_gi:
            continue
        if _is_end_head(h['text']):
            excl.append({'title': h['text'], 'page': h['page'],
                         'level': h.get('level', 1), '_gi': gi})
            have_gi.add(gi)

    # ---------- 4. 边界：任何"节/后置标题"都切断上一节的正文 ----------
    bounds = sorted(set([r['_gi'] for r in resolved] + [e['_gi'] for e in excl]))
    hard = len(lines)
    for h in heads:
        gi = pos.get(id(h['lines'][0]))
        if gi is not None and gi > resolved[-1]['_gi'] and _is_end_head(h['text']):
            hard = min(hard, gi)
    late = [e['_gi'] for e in excl if e['_gi'] > resolved[-1]['_gi']]
    if late:
        hard = min(hard, min(late))

    head_line_ids = set()
    for h in heads:
        for l in h['lines']:
            head_line_ids.add(id(l))

    # ---------- 5. 切正文 ----------
    kept, pending, pending_pages = [], '', []
    for r in resolved:
        start = r['_gi']
        nxt = [b for b in bounds if b > start]
        end = nxt[0] if nxt else hard
        i0 = start if r['kind'] == 'implicit' else start + 1
        body = []
        # 摘要/Summary 可能整段用比正文小的字号排版（Nature Biotech、部分综述），
        # 这些行在 font_roles 里是 'small'。对摘要节放行 small，其余节仍只收正文，
        # 免得把图注混进 sourceText。若正文行太少（< MIN_BODY_CHARS），也退一步收 small。
        tn = _norm(r['title'])
        allow_small = (tn == 'abstract' or tn == 'summary'
                       or tn.startswith('abstract ') or tn.startswith('summary '))

        # 隐式节（摘要/无标题开头）的第一行即使被版式判成 head，也是**正文的一部分**。
        # 典型：Frontiers 结构化摘要的 "Introduction: Chimeric antigen receptor…" 被
        # 判成标题行；模型正确地把它当摘要首行，程序不能按"标题行"把它剔掉——
        # 否则摘要第一句会从 "…have shown" 直接跳到 "great promise…"，丢半句。
        implicit_body_ids = set()
        if r['kind'] == 'implicit':
            implicit_body_ids.add(start)
            for h in heads:
                if h['lines'] and id(h['lines'][0]) == id(lines[start]):
                    implicit_body_ids.update(pos[id(x)] for x in h['lines']
                                             if id(x) in pos)
                    break

        def collect(include_small):
            out = []
            for i in range(i0, max(i0, end)):
                l = lines[i]
                if id(l) in head_line_ids and i not in implicit_body_ids:
                    continue
                if (allow_small and
                        re.match(r'^\s*(key\s*words?|keywords)\s*[:：]', l.text or '', re.I)):
                    break                       # 摘要到此为止，后面是关键词
                if (l.role in ('prose', 'symbol') or i in implicit_body_ids
                        or (include_small and l.role == 'small')):
                    out.append((i, l))
            return out

        body = collect(False)
        text = _join_body(body, body_group[1]) if body else ''
        if len(text) < MIN_BODY_CHARS:
            body2 = collect(True)
            text2 = _join_body(body2, body_group[1]) if body2 else ''
            if len(text2) > len(text):
                body, text = body2, text2
        pages = [l.page for _, l in body] or [r['page']]
        r['_body_idx'] = [i for i, _ in body]
        r['_pages'] = pages

        if len(text) < MIN_BODY_CHARS:
            if text:
                pending = (pending + ' ' + text).strip()
                pending_pages.extend(pages)
            if r['_has_children']:
                r['_container'] = True
                continue
            r['_skip'] = True
            warnings.append('节太短，已并入下一节：%s（%d 字）' % (r['title'], len(text)))
            continue

        if pending:
            text = (pending + ' ' + text).strip()
            pages = list(set(pages) | set(pending_pages))
            pending, pending_pages = '', []
        r['_body_text'] = text
        r['_pages'] = pages
        kept.append(r)

    if pending and kept:                     # 文末的零头并回上一节
        kept[-1]['_body_text'] = (kept[-1]['_body_text'] + ' ' + pending).strip()
        kept[-1]['_pages'] = list(set(kept[-1]['_pages']) | set(pending_pages))

    # ---------- 5b. Methods 不进正文分析 ----------
    # 模型偶尔会把 Methods 及其子节也列进 sections（2024 Peng 那篇实测 19 节里有 8 节是
    # Methods）。下游会逐节花 AI 钱做思想图，而方法学不是"论文论据"——平台从 P0 起
    # 就只分析正文。这里**程序强制**：Methods 顶节及其整棵子树从 sections 移走，
    # 保留在 toc / excluded 里（结构信息不丢，只是不分析）。
    METHOD_HEADS = ('METHODS', 'METHOD', 'MATERIALS AND METHODS',
                    'EXPERIMENTAL PROCEDURES', 'STAR METHODS', 'ONLINE METHODS',
                    'METHOD DETAILS', 'EXPERIMENTAL MODEL AND SUBJECT DETAILS')

    def _is_methods_title(t):
        nu = sectioner._norm_head(t)
        return any(nu == m or nu.startswith(m + ' ') for m in METHOD_HEADS)

    drop_methods = set()
    for k, r in enumerate(resolved):
        if not _is_methods_title(r['title']):
            continue
        drop_methods.add(r['_gi'])
        j = k + 1
        while j < len(resolved) and resolved[j]['level'] > r['level']:
            drop_methods.add(resolved[j]['_gi'])
            j += 1
    if drop_methods:
        keep2 = [r for r in kept if r['_gi'] not in drop_methods]
        if len(keep2) >= 2:
            for r in resolved:
                if r['_gi'] in drop_methods and _is_methods_title(r['title']):
                    excl.append({'title': r['title'], 'page': r['page'],
                                 'level': r['level'], '_gi': r['_gi']})
            warnings.append('Methods 及其子节已移出正文分析（保留在目录/后置区）')
            kept = keep2

    if not kept:
        raise StructureError('模型给的节切不出正文（全部短于 %d 字）' % MIN_BODY_CHARS)

    # ---------- 6. 覆盖率验收 ----------
    covered = set()
    for r in resolved:
        covered.update(r.get('_body_idx') or [])
    lo = resolved[0]['_gi']
    hi = min(hard, len(lines))
    prose = [i for i in range(lo, hi) if lines[i].role == 'prose']
    cover = (len(covered & set(prose)) / float(len(prose))) if prose else 1.0
    if cover < MIN_COVER:
        raise StructureError('模型分节只覆盖了正文 %.0f%%（阈值 %.0f%%），退回版式分节'
                             % (cover * 100, MIN_COVER * 100))
    if cover < WARN_COVER:
        warnings.append('正文覆盖率 %.0f%%（偏低，建议人工看一眼节边界）' % (cover * 100))

    # ---------- 7. 输出 sections ----------
    jmap = sectioner.journal_map(lines)
    out, seen_ids = [], {}
    for r in kept:
        pages = r['_pages'] or [r['page']]
        jpages = [jmap.get(p, p) for p in pages]
        jhead = jmap.get(r['page'], r['page'])
        pdf_lo = min(pages + [r['page']])
        pdf_hi = max(pages + [r['page']])
        base = sectioner.slugify(r['title'])
        seen_ids[base] = seen_ids.get(base, 0) + 1
        sid = base if seen_ids[base] == 1 else '%s-%d' % (base, seen_ids[base])
        out.append({
            'id': sid,
            'title': r['title'],
            'rawTitle': r['title'],
            'level': r['level'],
            'parent': r['parent'],
            'pageRange': [min(jpages + [jhead]), max(jpages + [jhead])],
            'pdfPages': [pdf_lo, pdf_hi],
            'sourceText': r['_body_text'],
            'chars': len(r['_body_text']),
        })

    # 模型有时会把同一标题的目录条目也列进来；同一标题只留正文多的那个
    best = {}
    for s in out:
        k = _norm(s['title'])
        if k not in best or s['chars'] > best[k]['chars']:
            best[k] = s
    out = [s for s in out if best[_norm(s['title'])] is s]

    # 摘要置顶：有些期刊（JCI 等）的摘要在 PDF 里被提取到 Introduction 之后
    # （版面在首页右栏，PyMuPDF 按左栏优先吐文本）。内容切得对，但数组顺序会变成
    # Introduction → Abstract；前端 tab / 下游都按数组顺序走，所以这里按标题置顶。
    # 只对**首页**的 Abstract/Summary 生效，避免把正文中间的同名小节误提上来。
    def _is_abstract_title(t):
        n = _norm(t)
        return (n in ('abstract', 'summary')
                or n.startswith('abstract ') or n.startswith('summary '))

    out.sort(key=lambda s: 0 if (_is_abstract_title(s['title'])
                                  and (s.get('pdfPages') or [99])[0] <= 1) else 1)
    if len(out) < 2:
        raise StructureError('去重后只剩 %d 节，退回版式分节' % len(out))

    # ---------- 8. toc / excluded / 结果 ----------
    toc = [{'title': r['title'], 'level': r['level'],
            'page': jmap.get(r['page'], r['page']),
            'container': bool(r.get('_has_children'))} for r in resolved]
    toc.sort(key=lambda x: 0 if (_is_abstract_title(x['title'])
                                  and x.get('page', 99) <= 1) else 1)
    excluded, seen = [], set()
    for e in sorted(excl, key=lambda x: x['_gi']):
        k = _norm(e['title'])
        if k in seen:
            continue
        seen.add(k)
        excluded.append({'title': e['title'],
                         'page': jmap.get(e['page'], e['page']),
                         'level': e['level']})

    if debug:
        print('---- LLM 分节：%d 节（模型给 %d，定位失败 %d）----'
              % (len(out), len(items), len(dropped)))
        for s in out:
            print('  L%d %-52s p%-6s %6d 字  parent=%s'
                  % (s['level'], s['title'][:52], '%d-%d' % tuple(s['pageRange']),
                     s['chars'], s['parent'] or '-'))
        print('  覆盖率 %.0f%% · 后置标题 %d 条 · warnings %d'
              % (cover * 100, len(excluded), len(warnings)))

    return {
        'schemaVersion': 'p4-llm-1',
        'paperId': paper_id,
        'engine': 'core/structure_ai.py + core/sectioner.py',
        'structureSource': 'llm',
        'doc': {
            'pdfPages': doc.page_count,
            'bodyFont': body_group[0],
            'bodySize': body_group[1],
            'pageMap': {str(k): v for k, v in sorted(jmap.items())},
        },
        'sections': out,
        'excluded': excluded,
        'toc': toc,
        'llm': llm_meta,
        'warnings': warnings,
    }


# ==================================================================== 入口
def build(pdf_path, paper_id=None, progress=None, debug=False):
    """PDF → LLM 分节结果（sections.json 同款 dict）。

    抛 StructureError 表示"模型这条链不可用"；调用方（sectioner.sectionize_smart）
    负责退回版式分节。**任何异常都不该让论文入库失败。**"""
    progress = progress or (lambda m: None)
    pid = paper_id or os.path.splitext(os.path.basename(pdf_path))[0]
    doc = fitz.open(pdf_path)
    try:
        lines = sectioner.extract_lines(doc)
        if not lines:
            raise StructureError('PDF 里没有可提取的文字层（扫描件？需要先 OCR）')
        roles, body_group, body_size, leading, prevs = sectioner.font_roles(lines)
        heads = sectioner.heading_blocks(lines, roles, leading=leading)
        progress('已提取 %d 行、%d 个候选标题；让模型通读全文判结构…'
                 % (len(lines), len(heads)))
        payload, mode = build_payload(lines, heads)
        prompt = load_prompt('structure') + '\n\n---\n\n' + payload
        ad = adapters.get_adapter(timeout=360, temperature=0.1,
                                  json_mode=True, max_tokens=16384)
        t0 = time.time()
        raw = ad.json(prompt)
        elapsed = time.time() - t0
        meta = {
            'preset': getattr(ad, 'preset', ''),
            'model': getattr(ad, 'model', ''),
            'elapsedS': round(elapsed, 1),
            'usage': getattr(ad, 'last_usage', None),
            'mode': mode,
            'candidateCount': len(heads),
            'lineCount': len(lines),
            'createdAt': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        r = assemble(doc, lines, heads, raw, pid, meta, body_group, debug=debug)
        progress('AI 分节完成：%d 节（%s，用时 %.0f 秒）'
                 % (len(r['sections']),
                    '压缩版' if mode == 'compact' else '全文', elapsed))
        return r
    finally:
        doc.close()
