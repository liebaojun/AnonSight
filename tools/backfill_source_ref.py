# -*- coding: utf-8 -*-
"""
回填 `source_ref` 到已有的 annotations.json（**纯字符串重算，绝不重跑 AI 分析**）。

背景
----
`core/annotate_ai.py` 的 `locate_items()` 生成 `source_ref` 时写的是**期刊页码**：

    jp = jmap.get(h['pdfPage'], sec['pageRange'][0])       # jmap = 期刊页码表
    'source_ref': '%s p.%d' % (sec['title'], jp)           # ← 错：这是期刊内部页号

前端 `P0/index.html` 的悬停释义卡**直接印这串**，于是鼠标停在 PDF 高亮块上弹出的是
「SUMMARY p.855」（wu2020）/「Abstract p.298」（shi2025）——855/298 是期刊内部页号，
不是真 PDF 页号；同一句话点开箭头后的详情面板证据卡却写「第 2 页」，同屏打架。

根因是 `pageRange` 两代数据口径不同：
  · 早期手工样本 wu2020 / shi2025 → 内部页 id（855、298…）
  · 自动产线 → 真 PDF 页号（2、3…）
所以 `sec['pageRange'][0]` 这个兜底本身就是错的。**`pdfPage` 永远是真 PDF 页号。**

生成端已改为 `'%s p.%d' % (sec['title'], h['pdfPage'])`（本脚本处理存量数据）。
`journalPage` 字段**保留不动**（另一个语义，它被污染是另一个待办问题）。

做法
----
对每篇 `papers/<pid>/annotations.json` 的每一条：
    source_ref = '<节标题> p.<pdfPage>'
节标题从 `papers/<pid>/sections.json` 按 `sectionId` 查；查不到就**保留原标题部分**
（把原 source_ref 尾部的 ` p.<数字>` 剥掉，剩下的就是标题）。
`pdfPage` 本来就在每条里，全程零模型调用、零成本。

用法
----
    python tools/backfill_source_ref.py            # 全部论文
    python tools/backfill_source_ref.py <pid> ...  # 只处理点名的几篇
    python tools/backfill_source_ref.py --dry-run  # 只看会改什么，不落盘

保证
----
· **幂等**：跑第二遍不会有任何改动（报"已是最新"，也不重写文件，mtime 不变）。
· **备份**：第一次要改某篇时，先把原 annotations.json 存成 `annotations.json.bak`。
  `.bak` **只写一次**（已存在就不覆盖）——否则第二遍跑就把"真·原始文件"冲掉了，
  备份也就失去意义。
· 对不上的（sectionId 在 sections.json 里没有 / 条目没 pdfPage）**逐条打出来**，
  不静默吞掉，且**不动**那些条目。
· 原子替换（写 .tmp 再 os.replace），不会写一半崩了留残文件。
"""
import json
import os
import re
import shutil
import sys

# ⚠ Windows 控制台默认 GBK，日志里有 ✓ ✗ 和中文 → 会 UnicodeEncodeError 直接退出。
#   自己设 UTF-8，别指望调用方记得加 PYTHONIOENCODING。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PAPERS = os.path.join(ROOT, 'papers')

# 原 source_ref 尾部的页号后缀："SUMMARY p.855" / "第 1–2 页 p.1" → 剥成 "SUMMARY" / "第 1–2 页"
# 要求必须有 "p." 这个点，免得把标题里自带的数字误剥（如 "第 1–2 页"）。
REF_SUFFIX = re.compile(r'\s*p\.\s*\d+\s*$')


def load(p):
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def title_of(source_ref):
    """从原 source_ref 里剥出标题部分（查不到节标题时的兜底）。"""
    return REF_SUFFIX.sub('', str(source_ref or '')).strip()


def backfill_one(pid, dry_run=False):
    """返回 (状态, 明细 dict)。状态 ∈ ok / skip / error。"""
    d = os.path.join(PAPERS, pid)
    ap, sp = os.path.join(d, 'annotations.json'), os.path.join(d, 'sections.json')
    if not os.path.exists(ap):
        return 'skip', {'reason': 'annotations.json 不存在'}

    try:
        ann = load(ap)
    except Exception as e:
        return 'error', {'reason': '读取 annotations.json 失败：%s' % e}

    # sections.json 是节标题的权威来源；没有它也能跑（退化成用原 source_ref 的标题部分）
    tmap, have_sections = {}, False
    if os.path.exists(sp):
        try:
            for s in load(sp).get('sections', []):
                if s.get('id'):
                    tmap[s['id']] = (s.get('title') or '').strip()
            have_sections = True
        except Exception as e:
            print('    ⚠ %s 读取失败（%s），改用原 source_ref 的标题部分' % (sp, e))

    st = {'total': 0, 'changed': 0, 'same': 0, 'no_pdfpage': 0, 'no_section': 0,
          'renamed': 0, 'unmatched': [], 'missing_pdfpage': [], 'renames': [],
          'changed_flag': False}

    for it in (ann.get('items') or []):
        st['total'] += 1
        old = str(it.get('source_ref') or '')
        sid = it.get('sectionId')
        pp = it.get('pdfPage')

        # 标题：优先 sections.json（权威），查不到退回原 source_ref 的标题部分
        title = tmap.get(sid) if have_sections else None
        if not title:
            title = title_of(old)
            if have_sections and sid not in tmap:
                st['no_section'] += 1
                st['unmatched'].append({'id': it.get('id'), 'sectionId': sid})

        # 页号：只认 pdfPage（永远是真 PDF 页号）；没有就不动这一条
        if not isinstance(pp, int) or isinstance(pp, bool):
            st['no_pdfpage'] += 1
            st['missing_pdfpage'].append({'id': it.get('id'), 'pdfPage': pp, 'old': old})
            continue

        new = '%s p.%d' % (title, pp)
        if new == old:
            st['same'] += 1
            continue

        if title_of(old) != title:
            st['renamed'] += 1
            if len(st['renames']) < 8:
                st['renames'].append({'id': it.get('id'), 'from': title_of(old), 'to': title})

        it['source_ref'] = new
        st['changed'] += 1
        st['changed_flag'] = True

    if st['changed_flag'] and not dry_run:
        bak = ap + '.bak'
        # 只写一次：留着**真正的**原始文件，重复跑不会把备份冲成"已经补过的版本"
        if not os.path.exists(bak):
            shutil.copy2(ap, bak)
        tmp = ap + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(ann, f, ensure_ascii=False, indent=1)
        os.replace(tmp, ap)              # 原子替换
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

    n_ok, n_skip, n_err = 0, 0, 0
    tot_items, tot_changed, tot_same = 0, 0, 0
    tot_nopage, tot_nosec = 0, 0
    print('=' * 70)
    for pid in args:
        stt, st = backfill_one(pid, dry_run=dry)
        if stt == 'skip':
            n_skip += 1
            print('－ %-44s 跳过（%s）' % (pid, st['reason']))
            continue
        if stt == 'error':
            n_err += 1
            print('✗ %-44s 出错：%s' % (pid, st['reason']))
            continue
        n_ok += 1
        tot_items += st['total']
        tot_changed += st['changed']
        tot_same += st['same']
        tot_nopage += st['no_pdfpage']
        tot_nosec += st['no_section']
        if st['changed']:
            tag = '将改' if dry else '已改'
        else:
            tag = '已是最新，未改动'
        bak = '有' if os.path.exists(os.path.join(PAPERS, pid, 'annotations.json.bak')) else '无'
        print('✓ %-44s %s %d 条（共 %d 条 / 已对 %d 条）  备份:%s'
              % (pid, tag, st['changed'], st['total'], st['same'], bak))
        if st['no_section']:
            print('    ⚠ %d 条在 sections.json 里找不到 sectionId（已用原标题部分兜底）：%s'
                  % (st['no_section'],
                     ', '.join(str(u['sectionId']) for u in st['unmatched'][:8])))
        if st['no_pdfpage']:
            print('    ⚠ %d 条没有 pdfPage（**未改动**）：%s'
                  % (st['no_pdfpage'],
                     ', '.join('%s(pdfPage=%r)' % (u['id'], u['pdfPage'])
                               for u in st['missing_pdfpage'][:8])))
        for r in st['renames'][:5]:
            print('    · 节标题按 sections.json 更新：%s → %s（条 %s）'
                  % (r['from'], r['to'], r['id']))
    print('=' * 70)
    print('合计：处理 %d 篇 / 跳过 %d 篇 / 出错 %d 篇；条目 %d 条 → %s %d 条、已对 %d 条；'
          '缺 pdfPage %d 条、找不到节 %d 条%s'
          % (n_ok, n_skip, n_err, tot_items, '待改' if dry else '已改', tot_changed,
             tot_same, tot_nopage, tot_nosec, '（--dry-run，未落盘）' if dry else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
