# -*- coding: utf-8 -*-
"""回填"论文有多少节"这个数（meta.json + papers/index.json）。

## 为什么需要
`meta.json` 的 `sections` 和 `papers/index.json` 里每篇的 `sections`，
本该等于 `sections.json` 的节数，但它们**会被写在不同的时候**：
  · 入库（run_ingest）：三处一起写 ✓
  · 重切（run_resection）：以前只写 sections.json + 索引，**漏了 meta** ✗
  · 换了新版分节器之后重切过一轮的老论文：数字就永远停在上一次入库时的值
实测过的后果：`shi2025-nri-charge-based` 长期显示「78 节」（实际 14 节），
`yue2026` 显示 11（实际 12），`natbiotech-human-car-macrophages` 显示 5（实际 9）。
论文库列表读的就是这个数 —— **用户直接看得见**。

## 真源
`sections.json` 的 `sections` 数组长度。这个脚本只做**派生数据的回填**，
不碰 sections.json / graph.json / annotations.json。

## 用法
    python tools/backfill_section_counts.py            # 回填
    python tools/backfill_section_counts.py --dry-run  # 只看会改什么
"""
import json
import os
import sys
import io

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPERS = os.path.join(ROOT, 'papers')
MANIFEST = os.path.join(PAPERS, 'index.json')

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


def truth(pid):
    """这一篇到底有多少节 —— 以 sections.json 为准。"""
    p = os.path.join(PAPERS, pid, 'sections.json')
    if not os.path.exists(p):
        return None
    try:
        return len(json.load(open(p, encoding='utf-8')).get('sections', []))
    except Exception:
        return None


def main():
    dry = '--dry-run' in sys.argv
    try:
        man = json.load(open(MANIFEST, encoding='utf-8'))
    except Exception:
        man = {'papers': []}
    by_id = {p['id']: p for p in man.get('papers', [])}

    fixed_meta = fixed_idx = skipped = 0
    for pid in sorted(os.listdir(PAPERS)):
        d = os.path.join(PAPERS, pid)
        if not os.path.isdir(d):
            continue
        n = truth(pid)
        if n is None:
            skipped += 1
            print('－ %-40s 跳过（没有 sections.json）' % pid[:40])
            continue
        notes = []

        mp = os.path.join(d, 'meta.json')
        if os.path.exists(mp):
            try:
                meta = json.load(open(mp, encoding='utf-8'))
            except Exception as e:
                print('✗ %-40s meta.json 读不了：%s' % (pid[:40], e))
                continue
            if meta.get('sections') != n:
                notes.append('meta %s→%d' % (meta.get('sections'), n))
                if not dry:
                    meta['sections'] = n
                    with open(mp, 'w', encoding='utf-8') as f:
                        json.dump(meta, f, ensure_ascii=False, indent=1)
                fixed_meta += 1

        e = by_id.get(pid)
        if e is not None and e.get('sections') != n:
            notes.append('索引 %s→%d' % (e.get('sections'), n))
            if not dry:
                e['sections'] = n
            fixed_idx += 1

        print('%s %-40s %d 节%s' % ('○' if notes else '✓', pid[:40], n,
                                    ('  ' + '；'.join(notes)) if notes else '  已是最新'))

    if not dry and fixed_idx:
        with open(MANIFEST, 'w', encoding='utf-8') as f:
            json.dump(man, f, ensure_ascii=False, indent=1)

    print('=' * 72)
    print('合计：meta 改了 %d 篇、索引改了 %d 篇、跳过 %d 篇%s'
          % (fixed_meta, fixed_idx, skipped, '（--dry-run，未落盘）' if dry else ''))


if __name__ == '__main__':
    main()
