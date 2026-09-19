# -*- coding: utf-8 -*-
"""标题回归检查：把 papers/ 下每篇的 meta.json 标题，和"用当前 server.py 的标题逻辑
重新对 paper.pdf 猜一遍"的结果逐篇对齐。

为什么用 extract_meta() 而不是直接调 _title_by_fontsize()：
    平台上显示的标题是 extract_meta() 的产物——它先信 PDF 内嵌元数据，元数据是垃圾值
    才退回"字号法 → 位置法"。（wang2025 就是靠位置法猜对的：它的标题和正文同字号，
    字号法本来就返回空。）所以"重新猜的标题"取 extract_meta()，字号法的单独结果另列一列
    供排查。

用法：
    python tools/check_titles.py                  # 查 papers/ 下全部已收录论文
    python tools/check_titles.py <pdf> [<pdf>...]  # 对任意 PDF 单独猜（验收用，如 Majumdar2024）
"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import server  # noqa: E402


def check_library():
    rows = []
    for pdir in sorted(glob.glob(os.path.join(server.PAPERS, '*'))):
        pdf = os.path.join(pdir, 'paper.pdf')
        mj = os.path.join(pdir, 'meta.json')
        if not (os.path.exists(pdf) and os.path.exists(mj)):
            continue
        stored = (json.load(open(mj, encoding='utf-8')).get('title') or '').strip()
        meta = server.extract_meta(pdf)                    # 管线口径（平台实际显示的）
        reguess = (meta.get('title') or '').strip()
        # 字号法单独跑，只看它自己给什么（wang2025 这类同字号版式本来就为空）
        import fitz
        doc = fitz.open(pdf)
        fs = ''
        try:
            fs = server._title_by_fontsize(doc)
        finally:
            doc.close()
        rows.append({
            'dir': os.path.basename(pdir),
            'stored': stored,
            'reguess': reguess,
            'fontsize': fs,
            'ok': stored == reguess,
        })

    print('=' * 110)
    print('论文目录 | meta.json 标题 | 重新猜的标题（extract_meta 管线口径） | 字号法单独 | 是否一致')
    print('=' * 110)
    for r in rows:
        print('%-48s | %s | %s | %s | %s' % (
            r['dir'][:48], r['stored'][:58], r['reguess'][:58],
            (r['fontsize'] or '(空)')[:42], '一致' if r['ok'] else '★不一致★'))
    bad = [r for r in rows if not r['ok']]
    print('-' * 110)
    print('总计 %d 篇，一致 %d 篇，不一致 %d 篇' % (len(rows), len(rows) - len(bad), len(bad)))
    for r in bad:
        print('  ★ %s\n      meta : %s\n      重猜 : %s' % (r['dir'], r['stored'], r['reguess']))
    if bad:
        print('''
不一致的两种情况，先看清是哪一种再动手：
  (1) 标题逻辑退化了 —— 重猜的结果**不如** meta.json，该改代码；
  (2) 库里的 meta.json 是**旧版本逻辑写下的**（当时就错）—— 重猜才是对的，该重新拖一次
      PDF 入库（会生成新 id），或按新标题手改 meta.json + papers/index.json。
      已知 majumdar2024 属于第 (2) 种：2026-09-18 第 5 轮验收前它被旧逻辑写成了小节标题。''')
    return 0 if not bad else 1


def guess_files(paths):
    import fitz
    for p in paths:
        doc = fitz.open(p)
        try:
            fs = server._title_by_fontsize(doc)
            pos = server._title_by_position(doc)
        finally:
            doc.close()
        meta = server.extract_meta(p)
        print('%s' % p)
        print('   字号法   : %s' % (fs or '(空)'))
        print('   位置法   : %s' % (pos or '(空)'))
        print('   管线口径 : %s   [%s]' % (meta['title'], meta['titleSource']))
    return 0


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    sys.exit(guess_files(args) if args else check_library())
