# -*- coding: utf-8 -*-
"""
PaperIDE · P3-c 标注层自动生成

    逐节原文 ──→ 模型挑表达（text/type/zh/tip/quote）
                   ──→ 定位器算 rects（core/locate.py，主人那台 113/113 的机器）
                   ──→ annotations.json（格式与 P0 的 annotations-wu2020.json 一致，
                                       **前端不用改一行**）

格式（对齐 P0）：
    {"paper", "source", "note", "count", "items":[
      {"id","pdfPage","journalPage","text","zh","type","source_ref","tip","rects"}]}
    rects = 归一化坐标（0–1，除以该页宽高），渲染时乘 canvas 尺寸即可。
"""
import json
import math
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import fitz                                       # noqa: E402

from core import adapters, locate, ano_pack, paths  # noqa: E402

TYPES = ('术语', '搭配', '动词', '逻辑', '句式')
MAX_ATTEMPT = 4

# 标注配额（提示词里写死的"8–18 条"）是**按一次调用**给的，不是按字数给的。
# 分节一粗，配额就塌：第 07 轮验收里 14 622 字的 Results 也只拿 18 条，
# 正文第 3–9 页一片白（"37 条 · 全程悬停"名不副实）。
# 修法：**超长节按句子边界切块、分次送模型**，配额随块数自然放大（块数 × 8–18 条），
# 数量与覆盖就跟着正文长度走了，而不是跟着"节"这个容器走。
#
# 为什么切在句子边界而不是"段落边界"：
#   `sectioner` 存下来的 `sourceText` 是**整条没有换行的字符串**（实查全库 118 节，
#   含 \n 的 **0 节**）——段落标记在这一层根本不存在了，拿不到。句子是数据里还剩的
#   最自然的语义边界，切在句末永远不会把一句话拦腰截断（主人要的"按自然语义分隔"）。
# 两个阈值怎么定的（依据 = 全库 118 节的字数分布）：
#   节字数 min 972 / p25 1781 / **p50 2994** / p75 4564 / **p90 6559** / p95 9553 / max 14622
#   · CHUNK_CUT=6000 → 只有 p90 往上、"明显偏长"的那约一成（13/118 节）才切；
#     2–4k 的正常节、乃至 5.9k 的节**仍然只走一次调用**，存量论文不受影响。
#   · CHUNK_TARGET=4000 → 切出来的每块落在 2000–4000 字，正是 p50–p75 这段
#     "一节一块、跑得好好的"的区间。
CHUNK_CUT = 6000
CHUNK_TARGET = 4000

# 句末：标点(+可选收尾引号/括号) + 空白 + 下一个大写字母/数字/( —— 下一个词小写就不算句末
_SENT_END = re.compile(r'[.!?]["\')\]]?\s+(?=[A-Z0-9(])')
# 标点前是这些词 → 是缩写或编号，不是句末（et al. / Fig. / e.g. / vs.）
_ABBR = {'al', 'eg', 'ie', 'vs', 'fig', 'figs', 'eq', 'no', 'cf', 'approx', 'ref', 'refs',
         'dr', 'mr', 'mrs', 'ms', 'prof', 'st', 'etc', 'vol', 'sec', 'suppl', 'ca', 'resp'}


def load_prompt(name):
    return open(paths.res('prompts', name + '.md'), encoding='utf-8').read()


def _is_sentence_end(head):
    """`head`：从上一句末到**候选**句末标点为止的一段。判断这个候选是不是真句末。

    只挡两类最常见的误判（挡不住也无所谓——模型拿到的只是"块"的边界，
    不像 `clean_items` 那样会牵连定位）：缩写（et al. / Fig. / e.g.）和引文括注（Smith et al., 2003）。
    """
    pre = head.strip().rstrip('"\')]').rstrip()
    if not pre or pre[-1] not in '.!?':
        return False
    pre = pre[:-1].rstrip().rstrip('"\')]').rstrip()
    if re.search(r'[,;]\s*(19|20)\d{2}$', pre):        # (… et al., 2003). → 引文括注
        return False
    m = re.search(r'([A-Za-z0-9.]+)$', pre)
    w = (m.group(1) if m else '').lower().replace('.', '')
    if not w:
        return False
    if w in _ABBR:
        return False
    if len(w) == 1 and w.isalpha():                    # 人名缩写 J. / 编号 A.
        return False
    return True


def split_sentences(text):
    """按句末切句。切不准不影响正确性（块边界只影响模型看到的范围），
    所以宁可保守——**拿不准就当不是句末**，绝不在一句话中间下刀。"""
    out, start = [], 0
    for m in _SENT_END.finditer(text):
        if not _is_sentence_end(text[start:m.end()]):
            continue
        out.append(text[start:m.end()])
        start = m.end()
    if start < len(text):
        out.append(text[start:])
    return [s for s in out if s.strip()] or [text]


def chunk_text(text, cut=CHUNK_CUT, target=CHUNK_TARGET):
    """超长节 → 若干块；正常节原样返回一块（**保证它仍然只走一次模型调用**）。

    块边界只落在句末；块数 = ceil(字数 / target)，每块因此落在 2000–4000 字。
    末尾不足半块的零头并回上一块，免得最后一块只剩一两句话、白跑一次调用。
    """
    if len(text) <= cut:
        return [text]
    n = max(2, int(math.ceil(len(text) / float(target))))
    want = len(text) / float(n)
    chunks, cur = [], ''
    for s in split_sentences(text):
        cur += s
        if len(cur) >= want and len(chunks) < n - 1:
            chunks.append(cur)
            cur = ''
    if cur:
        if chunks and len(cur) < want * 0.5:           # 零头太小 → 并回上一块
            chunks[-1] += cur
        else:
            chunks.append(cur)
    return chunks or [text]


def clean_items(raw, source_text, why=None):
    """模型给的条目做硬过滤——**不要让"长得像但不是原文子串"的东西混进去**，
    否则定位必失败、高亮打不中，用户看到的就是"标了却没画出来"（P0 踩过的坑）。

    `why`：传一个 dict 进来，会把**每一条被丢了的原因**记进去。
    为什么要它：第 3 轮验收遇到"整节挑出 0 条"，日志只报个数、看不出为什么——
    没有原因就只能瞎猜（是模型没给？还是类型非法？还是子串对不上？）。"""
    out, seen = [], set()
    stats = {'模型给的': len(raw.get('items') or []), '长度/类型不合规': 0,
             '不是原文子串': 0, '重复': 0}
    flat = ' '.join(source_text.split())
    for it in (raw.get('items') or []):
        t = (it.get('text') or '').strip()
        ty = (it.get('type') or '').strip()
        if not (3 <= len(t) <= 60) or ty not in TYPES:
            stats['长度/类型不合规'] += 1
            continue
        if t not in source_text and ' '.join(t.split()) not in flat:
            stats['不是原文子串'] += 1
            continue
        k = t.lower()
        if k in seen:
            stats['重复'] += 1
            continue
        seen.add(k)
        out.append({'text': t, 'type': ty, 'zh': (it.get('zh') or '').strip(),
                    'tip': (it.get('tip') or '').strip(),
                    'quote': (it.get('quote') or '').strip()})
    if why is not None:
        why.update(stats)
        why['通过'] = len(out)
    return out


def _annotate_chunk(sec, text, adapter, nth=None, nchunks=None):
    """**只做模型调用**，不碰 PDF——PyMuPDF 的 Document 不是线程安全的，
    多线程共用一个 doc 会随机崩。定位统一放主线程串行做（那一步很快）。

    `nth/nchunks`：本块是第几块/共几块。要告诉模型，否则它会以为这 4 千字
    就是"这一节的全部"，然后把块内的条目也挤在开头。"""
    head = '## 本节\n标题：%s\n页码范围：%d–%d\n' % (sec['title'], sec['pageRange'][0],
                                               sec['pageRange'][1])
    if nchunks and nchunks > 1:
        head += ('（本节原文较长，被切成 %d 块分批给你——**这是第 %d 块**。'
                 '上面的页码范围是**整节**的范围，本块只含其中连续的一段。）\n'
                 % (nchunks, nth))
    prompt = (load_prompt('annotate') + '\n\n---\n\n' + head +
              '\n## 本节原文\n%s\n' % text)
    items, why = [], {}
    for attempt in range(1, MAX_ATTEMPT + 1):
        why = {}
        try:
            raw = adapter.json(prompt)
        except adapters.AIError as e:
            if attempt == MAX_ATTEMPT:
                return [], '模型调用失败：%s' % str(e)[:150]
            continue
        items = clean_items(raw, text, why)
        for it in items:
            it['_sec'] = sec['id']
        if items:
            break
        # 把**丢弃原因**回灌给模型，比笼统说"重新挑"有用得多
        reason = '；'.join('%s %d 条' % (k, v) for k, v in why.items() if k != '通过' and v)
        prompt += ('\n\n---\n\n⚠ 上一版**一条都没通过**，丢弃原因：%s。\n'
                   '最常见的是 `text` 不是上面原文里的**原样子串**（一个字都不能改，'
                   '包括标点和空格）。请**直接复制原文里的片段**，重新挑，只输出 JSON。\n' % reason)
    return items, ('全部被丢弃：%s' % why) if not items and why else ''


def annotate_section(sec, adapter):
    """**超长节切块 → 逐块挑 → 合并去重**。

    切块是这次修复的核心：配额（8–18 条）是**按调用**给的，块数一多，
    长节自然就拿到"块数 × 8–18 条"，覆盖也就跟着正文长度走。
    正常节（≤ `CHUNK_CUT`）走的还是原来那条路——只有一次调用，行为和以前逐字一致。"""
    chunks = chunk_text(sec['sourceText'])
    n = len(chunks)
    items, warns, dup = [], [], 0
    for i, ch in enumerate(chunks, 1):
        got, warn = _annotate_chunk(sec, ch, adapter, i if n > 1 else None, n if n > 1 else None)
        items.extend(got)
        if warn:
            warns.append(('第 %d/%d 块：%s' % (i, n, warn)) if n > 1 else warn)
    # 合并去重：相邻两块的交界处，同一句话可能被两块都挑中（都在各自的原文里）。
    # 保留**先出现的那条**（块序 = 正文序），别让同一个表达在 PDF 上画出两层高亮。
    seen, merged = set(), []
    for it in items:
        k = ' '.join(it['text'].lower().split())
        if k in seen:
            dup += 1
            continue
        seen.add(k)
        merged.append(it)
    if n > 1:
        warns.insert(0, '%d 字切 %d 块合并（去重 %d 条）' % (len(sec['sourceText']), n, dup))
    return merged, '；'.join(warns)


def locate_items(sec, items, doc, jmap):
    """把表达定位到 PDF 坐标。主线程串行（doc 不跨线程）。"""
    pages = list(range(sec['pdfPages'][0], sec['pdfPages'][1] + 1))
    out, lost = [], 0
    for it in items:
        hits = locate.locate(doc, it['text'], pages=pages)
        if not hits:
            lost += 1
            continue
        h = hits[0]
        jp = jmap.get(h['pdfPage'], sec['pageRange'][0])
        out.append({
            'pdfPage': h['pdfPage'], 'journalPage': jp,
            'text': it['text'], 'zh': it['zh'], 'type': it['type'],
            'tip': it['tip'], 'quote': it['quote'], 'sectionId': sec['id'],
            # ⚠ 出处行**只能用真 PDF 页号 `h['pdfPage']`**（前端悬停卡直接印这串）。
            #   以前写的是 `jp`（期刊页码），于是悬停卡显示 "SUMMARY p.855" 而详情面板
            #   证据卡显示"第 2 页"，同屏打架——同一个表达两个页号。
            #   `sec['pageRange'][0]` 兜底也是错的：两代数据口径不同（早期手工样本
            #   wu2020/shi2025 是内部页 id 855/298，自动产线才是真 PDF 页号），
            #   拿它当兜底等于往错答案上再压一层。`pdfPage` 永远准。
            'source_ref': '%s p.%d' % (sec['title'], h['pdfPage']),
            'rects': h['rects'],
        })
    return out, lost


def annotate(pid, workers=3, progress=None, force=False):
    progress = progress or (lambda m: None)
    pdir = paths.paper_dir(pid)
    sections = json.load(open(os.path.join(pdir, 'sections.json'), encoding='utf-8'))['sections']
    out_path = os.path.join(pdir, 'annotations.json')
    cached = {}
    if os.path.exists(out_path) and not force:
        for it in json.load(open(out_path, encoding='utf-8')).get('items', []):
            cached.setdefault(it.get('sectionId'), []).append(it)

    # 「先找 paper.ano 再找 paper.pdf」（.ano 就是 PDF）—— 见 Ano 方案 §8.2 第 6 条
    doc = fitz.open(ano_pack.paper_file(pdir))
    jmap = locate.journal_pages(doc)
    adapter = adapters.get_adapter()
    all_items, lock = [], threading.Lock()
    done = 0

    import concurrent.futures as cf
    todo = [s for s in sections if s['id'] not in cached]
    progress('标注层：%d 节，%d 节已有缓存' % (len(sections), len(cached)))
    for sid, its in cached.items():
        all_items.extend(its)
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(annotate_section, s, adapter): s for s in todo}
        for f in cf.as_completed(futs):
            s = futs[f]
            items, warn = f.result()
            done += 1
            with lock:
                all_items.extend(items)
            progress('[%d/%d] %s — 挑出 %d 条%s' % (done, len(todo), s['id'], len(items),
                                                  ('（%s）' % warn) if warn else ''))
    # 定位放主线程：doc 不是线程安全的
    for x in all_items:
        x.setdefault('_sec', x.get('sectionId'))
    located, total_lost = [], 0
    for s in sections:
        its = [x for x in all_items if x.get('_sec') == s['id']]
        if not its:
            continue
        got, lost = locate_items(s, its, doc, jmap)
        total_lost += lost
        located.extend(got)
        progress('%s 定位：%d/%d 成功' % (s['id'], len(got), len(its)))
    all_items = located
    doc.close()
    if total_lost:
        progress('⚠ 有 %d 条表达在 PDF 里没定位到（已丢弃，宁可不画也不画错位置）' % total_lost)

    # 同一个表达在好几节里都出现（比如 "immunoreceptor tyrosine-based activation motif"
    # 摘要和引言各提一次）——**只留最先出现的那条**，否则 PDF 上会出现重影高亮。
    all_items.sort(key=lambda x: (x['pdfPage'], x['rects'][0][1] if x['rects'] else 0))
    seen, uniq = set(), []
    for it in all_items:
        k = ' '.join(it['text'].lower().split())
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    all_items = uniq
    for k, it in enumerate(all_items, 1):
        it['id'] = 'an-%03d' % k
    data = {
        'paper': pid,
        'source': 'PaperIDE 自动标注（core/annotate_ai.py + %s 适配器）' % adapter.name,
        'note': '自动从原文挑出的表达：text=原样片段，rects=归一化坐标（0–1），'
                '渲染时乘 canvas 尺寸。只画高亮、不画常驻气泡，信息走 hover。',
        'count': len(all_items),
        'items': all_items,
    }
    json.dump(data, open(out_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return {'count': len(all_items), 'sections': len(sections)}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        print(__doc__)
        return
    r = annotate(args[0], force='--force' in sys.argv,
                 progress=lambda m: print(m, flush=True))
    print('\n标注层完成：%d 条' % r['count'])


if __name__ == '__main__':
    main()
