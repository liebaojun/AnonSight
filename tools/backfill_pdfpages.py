# -*- coding: utf-8 -*-
"""
回填 `pdfPages` 到已有的 graph.json（**纯数据修补，绝不重跑 AI 分析**）。

背景
----
graph.json 每一节的 `pageRange` **口径不一致**：
  · `wu2020` / `shi2025-nri-charge-based` → 期刊页码（855、856… / 298、299…），
    前端直接显示就成了「第 855–855 页」「p.855」这种鬼页号；
  · 其余篇（含降级按页切分的 natbiotech 系）→ 真实 PDF 页码（2、3…）。

而 `sections.json` 里每一节都有口径**统一且正确**的 `pdfPages`（真实 PDF 页号，9 篇全一致），
只是 `core/pipeline_ai.py` 组装 graph.json 时把它丢了（已修，见 build_graph）。

这个脚本负责把**已经生成好的** graph.json 按 sections.json 补上 `pdfPages`。
不调模型、不花钱、不动 sourceText/views/lead 等任何其他字段。

用法
----
    python tools/backfill_pdfpages.py            # 全部论文
    python tools/backfill_pdfpages.py <pid> ...  # 只处理点名的几篇
    python tools/backfill_pdfpages.py --dry-run  # 只看会改什么，不落盘

保证
----
· **幂等**：跑第二遍不会有任何改动（第二节起会报"已是最新"，也不重写文件，mtime 不变）。
· **备份**：第一次要改某篇时，先把原 graph.json 存成 `graph.json.bak`。
  `.bak` **只写一次**（已存在就不覆盖）——否则第二遍跑就把"真·原始文件"冲掉了，
  备份也就失去意义。
· 匹配按 section `id`；对不上的 id 会**打出来**，不静默吞掉。
"""
import json
import os
import shutil
import sys

# ⚠ Windows 控制台默认 GBK，日志里有 ✓ ✗ → 会 UnicodeEncodeError 直接退出。
#   自己设 UTF-8，别指望调用方记得加 PYTHONIOENCODING（P3 §3.1 缺陷 6 的教训）。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PAPERS = os.path.join(ROOT, 'papers')


def load(p):
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def backfill_one(pid, dry_run=False):
    """返回 (状态, 明细 dict)。状态 ∈ ok / skip / error。"""
    d = os.path.join(PAPERS, pid)
    sp, gp = os.path.join(d, 'sections.json'), os.path.join(d, 'graph.json')
    if not os.path.exists(sp) or not os.path.exists(gp):
        return 'skip', {'reason': 'sections.json 或 graph.json 不存在'}

    try:
        secs = load(sp)
        graph = load(gp)
    except Exception as e:
        return 'error', {'reason': '读取失败：%s' % e}

    # sections.json 的权威值：节 id → pdfPages
    pmap, no_pp = {}, []
    for s in secs.get('sections', []):
        v = s.get('pdfPages')
        if v:
            pmap[s['id']] = v
        else:
            no_pp.append(s['id'])

    st = {'filled': 0, 'same': 0, 'conflict': 0, 'no_source': 0,
          'unmatched': [], 'sections_without_pdfpages': no_pp,
          'conflicts': [], 'changed': False}
    for g in graph.get('sections', []):
        sid = g.get('id')
        src = pmap.get(sid)
        if src is None:
            # graph.json 里有、sections.json 里没有（或那边也没 pdfPages）
            if sid not in {s.get('id') for s in secs.get('sections', [])}:
                st['unmatched'].append(sid)
            else:
                st['no_source'] += 1
            continue
        cur = g.get('pdfPages')
        if cur == src:
            st['same'] += 1
        elif cur is None:
            g['pdfPages'] = src
            st['filled'] += 1
            st['changed'] = True
        else:
            # 已经有值但和 sections.json 不一致 —— **照 sections.json 覆盖**（它是权威），
            # 但记下来，万一是人工改过就要有人拍板。
            st['conflicts'].append({'id': sid, 'graph': cur, 'sections': src})
            g['pdfPages'] = src
            st['conflict'] += 1
            st['changed'] = True

    if st['changed']:
        if not dry_run:
            bak = gp + '.bak'
            # 只写一次：留着**真正的**原始文件，重复跑不会把备份冲成"已经补过的版本"
            if not os.path.exists(bak):
                shutil.copy2(gp, bak)
            tmp = gp + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(graph, f, ensure_ascii=False, indent=1)
            os.replace(tmp, gp)          # 原子替换，别写一半崩了留个残文件
    return 'ok', st


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    dry = '--dry-run' in sys.argv
    if not args:
        if not os.path.isdir(PAPERS):
            print('找不到 papers 目录：%s' % PAPERS)
            return 1
        args = sorted(n for n in os.listdir(PAPERS)
                      if os.path.isdir(os.path.join(PAPERS, n)))
    if dry:
        print('（--dry-run：不落盘）')

    tot_f, tot_c, tot_p, n_ok, n_skip = 0, 0, 0, 0, 0
    print('=' * 68)
    for pid in args:
        stt, st = backfill_one(pid, dry_run=dry)
        if stt == 'skip':
            n_skip += 1
            print('－ %-46s 跳过（%s）' % (pid, st['reason']))
            continue
        if stt == 'error':
            print('✗ %-46s 出错：%s' % (pid, st['reason']))
            continue
        n_ok += 1
        tot_f += st['filled']
        tot_c += st['conflict']
        tot_p += len(st['unmatched'])
        verb = '将回填' if dry else '回填'
        if st['filled'] or st['conflict']:
            tag = verb
        elif st['same']:
            tag = '已是最新，未改动'
        else:
            tag = '无可回填的节'
        print('✓ %-46s %s：%d 节（%s %d 节 / 已是 %d 节 / 冲突 %d 节）'
              % (pid, tag, st['same'] + st['filled'] + st['conflict'] + st['no_source'],
                 verb, st['filled'], st['same'], st['conflict']))
        if st['sections_without_pdfpages']:
            print('    ⚠ sections.json 里这些节**没有** pdfPages（未回填）：%s'
                  % ', '.join(st['sections_without_pdfpages'][:8]))
        if st['no_source']:
            print('    ⚠ 有 %d 节在 sections.json 里也没有 pdfPages' % st['no_source'])
        if st['unmatched']:
            print('    ⚠ 对不上的 section id（graph 有 / sections 无）：%s'
                  % ', '.join(st['unmatched'][:12]))
        for c in st['conflicts'][:5]:
            print('    ⚠ 冲突 %s：graph 里是 %s，sections.json 是 %s（按后者覆盖）'
                  % (c['id'], c['graph'], c['sections']))
    print('=' * 68)
    print('合计：处理 %d 篇 / 跳过 %d 篇；%s %d 节、冲突覆盖 %d 节、对不上 %d 节%s'
          % (n_ok, n_skip, '待回填' if dry else '已回填', tot_f, tot_c, tot_p,
             '（--dry-run，未落盘）' if dry else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
