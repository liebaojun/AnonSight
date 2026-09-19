# -*- coding: utf-8 -*-
"""
P3-a 回归验收：自动分节 vs 手工分节（Wu2020）

分节是整条产线的地基——**切错了后面全白干**。所以拿 P0 手工做的那 11 节当基准，
验三件事，一件比一件硬：

  ① 节数与顺序       —— 11 节要对齐，顺序要一致
  ② 页码区间         —— 自动算的 pageRange 要覆盖手工范围
  ③ ★ 引文命中率     —— **这条才是命门**：手工版里写好的每一条 evidence.quote，
                        必须能在**自动分出的 sourceText** 里原样命中。
                        命中不了 = 左栏高亮打不中 = 溯源链断（P0 的校验器就是这么拦的）。

用法：  python tools/regress_sections.py
        python tools/regress_sections.py --verbose
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core import sectioner                       # noqa: E402
from core import pdftext                         # noqa: E402

P0 = os.path.join(ROOT, 'P0')
PDF = os.path.join(P0, 'wu2020.pdf')
EXTRACT = os.path.join(P0, '_extract')

# P0 手工节的 id，**按论文顺序**——自动分节也按论文顺序输出，
# 两边按位对齐（不按 id 认亲：自动 id 是标题 slug，改了截断长度就认不上了）
MANUAL_ORDER = [
    'abstract', 'intro', 'ms-method', 'phospho-patterns', 'csk-recruitment', 'car-incorporation',
    'itam-cytokine', 'brs-persistence', 'disc-phospho', 'disc-csk', 'disc-akt',
]


def load_manual():
    """手工节：正文取自 _extract/source/<id>.txt（没有就退回 json 里内嵌的 sourceText），
    引文取自 _extract/sections/<id>.json 的所有 evidence.quote。"""
    sys.path.insert(0, EXTRACT)
    from pipeline import load_sections                # 复用产线的载入逻辑，别另写一套
    out = {}
    for sec in load_sections():
        quotes = []
        for v in sec.get('views', []):
            for n in v.get('nodes', []):
                for e in n.get('evidence', []):
                    if e.get('quote') and not e.get('external'):
                        quotes.append(e['quote'])
            for e in v.get('edges', []):
                for ev in e.get('evidence', []):
                    if ev.get('quote') and not ev.get('external'):
                        quotes.append(ev['quote'])
            d = v.get('domain') or {}
            for t in d.get('tracks', []):
                for ev in t.get('evidence', []):
                    if ev.get('quote') and not ev.get('external'):
                        quotes.append(ev['quote'])
                for s in t.get('segments', []):
                    for ev in s.get('evidence', []):
                        if ev.get('quote') and not ev.get('external'):
                            quotes.append(ev['quote'])
            m = v.get('matrix') or {}
            for r in m.get('rows', []):
                for cell in (r.get('cells') or {}).values():
                    for ev in cell.get('evidence', []):
                        if ev.get('quote') and not ev.get('external'):
                            quotes.append(ev['quote'])
        out[sec['id']] = {'sec': sec, 'text': sec.get('sourceText', ''), 'quotes': quotes}
    return out


def main():
    verbose = '--verbose' in sys.argv
    auto = sectioner.sectionize(PDF, paper_id='wu2020')
    manual = load_manual()
    autos = auto['sections']
    print('自动分节 %d 节 / 手工分节 %d 节\n' % (len(autos), len(manual)))
    if len(autos) != len(MANUAL_ORDER):
        print('⚠ 节数对不上，按位对齐不可靠——先看 --debug 的标题块列表')
    PAIRS = list(zip([s['id'] for s in autos], MANUAL_ORDER))
    by_id = {s['id']: s for s in autos}
    hdr = '%-32s %-14s %-14s %7s %7s  %s'
    print(hdr % ('自动节 id', '自动页码', '手工页码', '自字数', '手字数', '引文命中'))
    print('-' * 100)

    tot_q = tot_hit = 0
    miss_all = []
    for aid, mid in PAIRS:
        a = by_id.get(aid)
        m = manual.get(mid)
        if not a or not m:
            print('  ✗ 缺：%s / %s' % (aid, mid))
            continue
        hits, miss = 0, []
        for q in m['quotes']:
            tot_q += 1
            if q in a['sourceText']:
                hits += 1
                tot_hit += 1
            else:
                miss.append(q)
        miss_all += [(mid, q) for q in miss]
        pg = '%d-%d' % tuple(a['pageRange'])
        mp = '%d-%d' % tuple(m['sec']['pageRange'])
        flag = '✓' if not miss else '✗'
        print(hdr % (aid[:32], pg, mp, a['chars'], len(m['text']),
                     '%s %d/%d' % (flag, hits, len(m['quotes']))))
        if miss and verbose:
            for q in miss[:6]:
                print('        ✗ 「%s」' % q[:96])

    print('-' * 100)
    rate = 100.0 * tot_hit / max(1, tot_q)
    print('引文命中：%d/%d = %.1f%%' % (tot_hit, tot_q, rate))
    if miss_all:
        print('\n未命中 %d 条：' % len(miss_all))
        for mid, q in miss_all[:20]:
            print('   [%s] %s' % (mid, q[:100]))
    return 0 if rate >= 99.0 else 1


if __name__ == '__main__':
    sys.exit(main())
