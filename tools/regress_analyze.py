# -*- coding: utf-8 -*-
"""验 `build_graph()` 那条路（就是独立验收抓到的 `NameError: name 'pdir'`）。

怎么做到**不花一分钱**：把某一篇的 `sections.json` / `meta.json` / `ai/` 整份拷到临时目录，
再把 `paths.DATA` 指到那儿，然后跑**完整**的 `analyze()`。
因为每一节的成果都在缓存里，`todo` 是空的 → **一次模型调用都不会发**，
流程会直接走到最后那步 `build_graph()` —— 而出错正是发生在那里。

这样测的好处：走的是**真函数、真流程**（不是单测里手搭参数），
但没有任何 API 开销。刷新后的 `graph.json` 落在临时目录，不碰主人的论文。

用法： python tools/regress_analyze.py
"""
import io
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = os.path.join(ROOT, 'tools', '_tmp', 'graph_test')
SRC_ID = 'wu2020'


def main():
    from core import paths, pipeline_ai

    src = os.path.join(ROOT, 'papers', SRC_ID)
    if not os.path.isdir(src):
        print('✗ 找不到样本论文', src); return 1

    shutil.rmtree(TMP, ignore_errors=True)
    dst = os.path.join(TMP, 'papers', SRC_ID)
    os.makedirs(dst)
    for f in ('sections.json', 'meta.json'):
        p = os.path.join(src, f)
        if os.path.exists(p):
            shutil.copy(p, dst)
    if os.path.isdir(os.path.join(src, 'ai')):
        shutil.copytree(os.path.join(src, 'ai'), os.path.join(dst, 'ai'))
    n_ai = len([f for f in os.listdir(os.path.join(dst, 'ai')) if f.endswith('.json')])
    print('拷了 %d 份节的成果到临时目录' % n_ai)

    # ★ 把数据目录指到临时区 —— 这就是"不动主人的论文"的全部秘密
    paths.DATA = TMP
    assert paths.paper_dir(SRC_ID).startswith(TMP), 'DATA 没指过去，别往下跑'
    print('数据目录 →', paths.DATA)

    lines = []
    try:
        r = (pipeline_ai.analyze(SRC_ID, workers=1, progress=lambda m: lines.append(str(m))))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print('\n✗ analyze() 抛了：%s' % e)
        print('  最后几行进度:')
        for l in lines[-6:]:
            print('    ', l)
        return 1

    gp = os.path.join(dst, 'graph.json')
    print('\n最后几行进度:')
    for l in lines[-5:]:
        print('   ', l)
    print()
    if not os.path.exists(gp):
        print('✗ analyze() 跑完了，但 **graph.json 没产出** —— 就是验收报的那个症状')
        return 1
    g = json.load(io.open(gp, encoding='utf-8'))
    secs = g.get('sections', [])
    with_views = [s for s in secs if s.get('views')]
    print('✅ graph.json 产出了：%.0f KB，%d 节，其中 %d 节有图'
          % (os.path.getsize(gp) / 1024, len(secs), len(with_views)))
    print('   paper.title =', (g.get('paper') or {}).get('title'))
    print('   pageMap 有 %d 项 | relVocabulary 有 %d 个词'
          % (len(g.get('pageMap') or {}), len((g.get('relVocabulary') or {}).get('used') or [])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
