# -*- coding: utf-8 -*-
"""
抽取产线 · 第二层：受控词表 + 校验 + 合并

用法：
    python pipeline.py vocab            # 打印受控词表
    python pipeline.py check [id ...]   # 校验单节（默认全部）
    python pipeline.py digest [id ...]  # 打印节点/边摘要（我写数据时自己看）
    python pipeline.py build            # 合并进 wu2020.json（主产物）

校验铁律（对应 01-接续文档 §四）：
  · nodes[].id 全局命名，同 id 跨节复用（不是论文内编号）
  · 每条边必须有 evidence（页码 + 原句），且原句必须能在**本节** sourceText 里原样命中
    —— 命中不了 = 左栏高亮跳不过去 = 溯源链断，直接报错
  · rel 必须落在受控词表里；新造词要标 new + why
  · 结构关系可补（structural），机制关系只认原文（sentence/inferred），不许传递闭包
  · lead（每节导读）可选：写了就必须 gist 长句 + focus 2-4 条
    —— 它是给「读之前」看的：先说这节值不值得细读、核心结论是什么，不是复述原文
  · views[].primary 可选：标记本节「最有代表性」的那张图（右栏默认显示它）
    —— 一节**至多一张**。这个字段是后加的、**原样透传**（views 里没见过的字段一直都是放行的），
       所以这条只加了一道上限，**没有放宽原有任何一条规则**
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRCDIR = os.path.join(HERE, 'source')
SECDIR = os.path.join(HERE, 'sections')
MASTER = os.path.join(os.path.dirname(HERE), 'wu2020.json')
LEGACY = os.path.join(os.path.dirname(HERE), 'wu2020-abstract.json')

# ---------------------------------------------------------------- 受控词表
# 规划 §4.3 草案 + P0 实测归纳。new=True = 草案里没有、实测必须新增的
VOCAB = [
    # 结构：谁包含谁、谁是谁的一部分、长在哪里（可由枚举句/定义句自动补全）
    ('包含', '结构', False, ''),
    ('构成', '结构', False, ''),
    ('定位', '结构', True, '膜近端/膜远端、胞质域这种"在哪"的关系，草案没有对应词'),
    ('修饰', '结构', False, '磷酸化/去磷酸化/突变等共价或序列改变'),
    # 因果-正
    ('促进', '因果-正', False, ''),
    ('激活', '因果-正', False, ''),
    ('增强', '因果-正', False, ''),
    ('诱导', '因果-正', False, ''),
    ('招募', '因果-正', False, ''),
    ('上调', '因果-正', False, ''),
    ('介导', '因果-正', True, 'TCR 经由 CD3ε 传递信号——不是"促进"（不是增强原有通路），也不等于"包含"'),
    ('经由', '因果-正', True, '"X 通过 Y 起作用"，与"介导"的分工：介导=Y 是承载者，经由=通道/途径'),
    # 因果-负
    ('抑制', '因果-负', False, ''),
    ('衰减', '因果-负', False, ''),
    ('下调', '因果-负', False, ''),
    ('减弱', '因果-负', False, ''),
    ('阻断', '因果-负', False, ''),
    # 证据/论断
    ('测量', '证据', False, ''),
    ('提示', '证据', False, ''),
    ('支持', '证据', False, ''),
    ('反驳', '证据', False, ''),
    ('总结', '证据', True, '把若干事实收束成一句判断，草案没有对应词'),
    # 时序
    ('依赖于', '时序', True, 'A 以 B 为前提；草案的"依赖于"在时序类，实测常用'),
    ('先于', '时序', False, ''),
    ('随后', '时序', False, ''),
    # 应用/操作：工程改造类，是本篇 CAR 部分的骨架
    ('设计策略', '应用', True, '"把 X 用于设计 Y"，应用类论断，草案没有'),
    ('插入', '应用', True, '把一段序列装进构建体——CAR 部分的动作，属于操作不是因果'),
    ('突变', '应用', True, '定点突变/敲除某个基序，同样是操作'),
    ('替换', '应用', True, '用等长无关序列顶替（linker 对照），操作性对照实验'),
]
REL = {v[0] for v in VOCAB}


def vocab_json():
    return {
        'note': 'P0/P2 实测归纳的受控词表。new=true = 规划 §4.3 草案没有、实测必须新增（附 why）。'
                '边只许用这里的词；要新造就先在 VOCAB 里登记并写 why。',
        'used': [{'rel': r, 'category': c, 'new': n, 'why': w} for r, c, n, w in VOCAB],
    }


# ---------------------------------------------------------------- 载入
def load_source(sid):
    p = os.path.join(SRCDIR, sid + '.txt')
    if not os.path.exists(p):
        return None
    return open(p, encoding='utf-8').read()


def load_sections(ids=None):
    out = []
    for fn in sorted(os.listdir(SECDIR)) if os.path.isdir(SECDIR) else []:
        if not fn.endswith('.json'):
            continue
        sid = fn[:-5]
        if ids and sid not in ids:
            continue
        sec = json.load(open(os.path.join(SECDIR, fn), encoding='utf-8'))
        sec['sourceText'] = load_source(sid) or sec.get('sourceText', '')
        out.append(sec)
    return out


ABBR = re.compile(r'(?:^|[\s(])(?:Fig|Figs|Table|Tables|Suppl|Supp|Ref|Refs|No|vs|approx|et al|e\.g|i\.e|Dr|Mr|Ms|St|Sec|Eq)\.$', re.I)


def sentences(text):
    """与 index.html splitSentences 同款切句（高亮是逐句 indexOf，两边必须一致）：
    点后面跟数字不算句末（CD19.28Z / 0.98），常见缩写和单字母缩写也不算。"""
    out, last = [], 0
    for m in re.finditer(r'[.!?]+(?=\s|$)', text):
        at = m.start()
        after = text[at + 1:at + 2]
        head = text[last:at]
        if after and after.isdigit():
            continue
        # ⚠ P3 修的一个真 bug：判缩写的片段必须**把句号本身算进来**。
        #   ABBR 的正则以 `\.$` 结尾，而句号在 m 里、不在 head 里——过去拿 head 去比，
        #   "Fig." 永远判不出来，于是 "…(Fig. 2b). Conversely…" 被劈成
        #   "…(Fig." 和 "2b). " 两个碎片句，跨句引文一条都对不上
        #   （Nature Reviews 那篇因此有 5 节反复过不了校验）。
        #   修完只会让句子更长、更好命中，**不会让任何已通过的数据变成不过**。
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


# ---------------------------------------------------------------- 校验
def check(sec):
    errs, warns = [], []
    sid = sec['id']
    sents = sentences(sec['sourceText'])
    seen_ids = {}

    if not sec.get('sourceText'):
        errs.append('sourceText 为空（左边没有原文可跳）')

    _check_lead(sec.get('lead'), errs, warns)

    for v in sec['views']:
        vid = v['id']
        if v.get('type') == 'domain':
            _check_domain(v, sid, sents, errs, warns)
            continue
        if v.get('type') == 'matrix':
            _check_matrix(v, sid, sents, errs, warns)
            continue
        nid = set()
        for n in v['nodes']:
            i = n['id']
            if i in nid:
                errs.append('%s: 节点 id 重复 %s' % (vid, i))
            nid.add(i)
            if i in seen_ids and seen_ids[i] != v['id']:
                pass                      # 同节多视图复用同一概念是允许的
            seen_ids[i] = v['id']
            for k in ('label', 'short', 'cluster'):
                if not n.get(k):
                    errs.append('%s: 节点 %s 缺 %s' % (vid, i, k))
            if n.get('cluster') not in ('mechanism', 'application', 'conclusion'):
                errs.append('%s: 节点 %s 的 cluster 非法：%s' % (vid, i, n.get('cluster')))
            if not n.get('evidence'):
                warns.append('%s: 节点 %s 没有 evidence' % (vid, i))
            for e in n.get('evidence', []):
                _ev(e, sid, sents, errs, warns, '%s 节点 %s' % (vid, i))

        for e in v['edges']:
            tag = '%s 边 %s->%s' % (vid, e.get('from'), e.get('to'))
            if e.get('from') not in nid:
                errs.append('%s: from 不存在于本视图' % tag)
            if e.get('to') not in nid:
                errs.append('%s: to 不存在于本视图' % tag)
            if e.get('rel') not in REL:
                errs.append('%s: rel 不在受控词表：「%s」' % (tag, e.get('rel')))
            if not e.get('label'):
                errs.append('%s: 缺 label（给人看的中文）' % tag)
            if e.get('polarity') not in ('promote', 'inhibit', 'neutral'):
                errs.append('%s: polarity 非法：%s' % (tag, e.get('polarity')))
            if e.get('origin') not in ('sentence', 'structural', 'inferred'):
                errs.append('%s: origin 非法：%s' % (tag, e.get('origin')))
            if not e.get('evidence'):
                errs.append('%s: 没有 evidence（做不到溯源的关系宁可不画）' % tag)
            for ev in e.get('evidence', []):
                _ev(ev, sid, sents, errs, warns, tag)
        _dup_edges(v, errs)
        if v.get('type') == 'flow':
            _check_dag(v, errs)
    _check_primary(sec['views'], errs, warns)
    return errs, warns


def _check_lead(lead, errs, warns):
    """每节导读 lead{gist, focus[]}——可选字段（允许分步补齐：没写的节不报错）。
    gist 是给「读之前」看的，要能独立说清"这节在讲什么、得到什么结论"，太短就退化成标题了；
    focus 是"读的时候盯住哪几句"，2-4 条，一条一句。"""
    if lead is None:
        return
    if not isinstance(lead, dict):
        errs.append('lead 必须是对象：{gist, focus[]}')
        return
    gist = lead.get('gist')
    if not isinstance(gist, str) or not gist.strip():
        errs.append('lead.gist 缺失或为空（导读要先替读者把结论说出来）')
    elif len(gist.strip()) < 30:
        errs.append('lead.gist 只有 %d 字，太短（至少 30 字）：「%s」' % (len(gist.strip()), gist.strip()))
    focus = lead.get('focus')
    if not isinstance(focus, list) or not 2 <= len(focus) <= 4:
        errs.append('lead.focus 必须是 2-4 条（现在 %s）'
                    % (len(focus) if isinstance(focus, list) else repr(focus)))
    else:
        for i, f in enumerate(focus, 1):
            if not isinstance(f, str) or not f.strip():
                errs.append('lead.focus 第 %d 条是空的' % i)
    for k in ('gist', 'focus'):
        body = lead.get(k)
        for t in ([body] if isinstance(body, str) else (body if isinstance(body, list) else [])):
            if isinstance(t, str) and '"' in t:
                warns.append('lead.%s 里有英文双引号——要引用请用中文直角引号「」' % k)


def _check_dag(v, errs):
    """流程图的排版是「最长路径分层」，成环或有回头边会把整张图压扁到前几层去。
    回边几乎总是画错了方向——报出来让写的人自己看一眼。"""
    adj = {}
    for e in v['edges']:
        adj.setdefault(e['from'], []).append(e['to'])
    state = {}

    def dfs(u, path):
        state[u] = 1
        for w in adj.get(u, []):
            if state.get(w) == 1:
                errs.append('%s: 流程图成环/回头边：%s（回头边会把后半张图压到前面几层）'
                            % (v['id'], ' -> '.join(path + [u, w])))
            elif state.get(w) is None:
                dfs(w, path + [u])
        state[u] = 2
    for n in v['nodes']:
        if state.get(n['id']) is None:
            dfs(n['id'], [])


def _check_primary(views, errs, warns):
    """views[].primary：本节「最有代表性」的那张图——用户不点 tab 时，右栏默认显示它。

    只做**更严**的那一侧：**至多一张**。
      · 0 张合法——老数据根本没有这个字段，降级版「只要一张图」时不写也一样对；
      · 1 张合法——这就是默认视图；
      · 2 张以上 = 模型没想清楚哪张最重要（两张都标等于没标：用户看到的还是排第一的那张），
        这是真错误，得拦下来让它重挑。
    字段本身**不做必填**：新增必填项会把老数据判死，那不是校验，是破坏。
    """
    marked = []
    for v in views:
        if 'primary' not in v:
            continue
        p = v['primary']
        if not isinstance(p, bool):
            # 不升级成硬错误：模型写成 "true"/1 这种，前端按 `=== true` 取不到，最坏也就是
            # 退回"看第一张"——为这个把整节打成不过、逼它重来，反而可能让本节**一张图都没有**。
            # 但要报出来，别让它悄悄失灵。
            warns.append('%s: primary 应该是 true/false，现在是 %r' % (v.get('id'), p))
        if p:            # 真值（含上面那种非布尔的）都算"标了"，免得两个 "true" 溜过去
            marked.append(v.get('id'))
    if len(marked) > 1:
        errs.append('本节有 %d 张图标了 primary: true（%s）——一节只能有一张「默认显示」的图，'
                    '挑最能说清这节内容的那张' % (len(marked), '、'.join(str(m) for m in marked)))


KINDS = ('plain', 'itam', 'brs', 'prs', 'linker', 'costim', 'kinase', 'mutant', 'sp', 'scfv',
         'insert', 'signal')
MARKS = ('same', 'up', 'down', 'none', 'na')      # 对照表格子的取值


def _check_matrix(v, sid, sents, errs, warns):
    """对照表：行 = 被比较的对象（突变体/构建体），列 = 看哪些指标"""
    m = v.get('matrix')
    if not m or not m.get('cols') or not m.get('rows'):
        errs.append('%s: matrix 视图缺 cols/rows' % v['id'])
        return
    colids = [c['id'] for c in m['cols']]
    for c in m['cols']:
        if not c.get('id') or not c.get('label'):
            errs.append('%s: 列缺 id/label' % v['id'])
    for r in m['rows']:
        tag = '%s 行 %s' % (v['id'], r.get('label'))
        if not r.get('id') or not r.get('label'):
            errs.append('%s: 行缺 id/label' % tag)
        if r.get('cluster') and r['cluster'] not in ('mechanism', 'application', 'conclusion'):
            errs.append('%s: cluster 非法：%s' % (tag, r['cluster']))
        cells = r.get('cells') or {}
        for cid in colids:
            cell = cells.get(cid)
            if cell is None:
                errs.append('%s: 缺「%s」这一格（缺就写 na，别留空）' % (tag, cid))
                continue
            if cell.get('mark') not in MARKS:
                errs.append('%s/%s: mark 非法：%s（允许 %s）' % (tag, cid, cell.get('mark'), '/'.join(MARKS)))
            if not cell.get('evidence') and cell.get('mark') != 'na':
                warns.append('%s/%s: 这一格没有 evidence（原文没给就标 na）' % (tag, cid))
            for e in cell.get('evidence', []):
                _ev(e, sid, sents, errs, warns, '%s/%s' % (tag, cid))
        for cid in cells:
            if cid not in colids:
                errs.append('%s: 出现了没定义的列 %s' % (tag, cid))


def _check_domain(v, sid, sents, errs, warns):
    """模式图：横条 = 一条链/一个构建体，横条上的方块 = 结构域"""
    d = v.get('domain')
    if not d or not d.get('tracks'):
        errs.append('%s: domain 视图缺 tracks' % v['id'])
        return
    for t in d['tracks']:
        tag = '%s 轨道 %s' % (v['id'], t.get('id') or t.get('label'))
        if not t.get('id') or not t.get('label'):
            errs.append('%s: 缺 id/label' % tag)
        if t.get('cluster') and t['cluster'] not in ('mechanism', 'application', 'conclusion'):
            errs.append('%s: cluster 非法：%s' % (tag, t['cluster']))
        for e in t.get('evidence', []):
            _ev(e, sid, sents, errs, warns, tag)
        if not t.get('segments'):
            errs.append('%s: 没有 segments' % tag)
        for s in t.get('segments', []):
            st = '%s/%s' % (tag, s.get('label'))
            if not s.get('label'):
                errs.append('%s: 方块缺 label' % st)
            if s.get('kind') not in KINDS:
                errs.append('%s: kind 非法：%s（允许 %s）' % (st, s.get('kind'), '/'.join(KINDS)))
            if not isinstance(s.get('len'), (int, float)) or s.get('len') <= 0:
                errs.append('%s: len 必须是正数（相对宽度）' % st)
            if not s.get('evidence') and not t.get('evidence'):
                warns.append('%s: 方块和轨道都没有 evidence（点开是空面板）' % st)
            for e in s.get('evidence', []):
                _ev(e, sid, sents, errs, warns, st)


def _ev(ev, sid, sents, errs, warns, tag):
    q = ev.get('quote', '')
    if not isinstance(ev.get('page'), int):
        errs.append('%s: evidence.page 不是整数' % tag)
    hit = [s for s in sents if q in s]
    if not hit:
        whole = ' '.join(sents)
        if q in whole:
            warns.append('%s: 原句跨句号，左栏高亮可能打不中：「%s」' % (tag, q[:40]))
        elif ev.get('external'):
            warns.append('%s: 引的是别节原文（external）——左栏跳不过去，只能到页码' % tag)
        else:
            near = _nearest(q, sents)
            errs.append('%s: 原句在本节 sourceText 里找不到：「%s」\n        最接近的原文：%s'
                        % (tag, q[:60], near[:120]))
    if q and ('  ' in q or q != q.strip()):
        warns.append('%s: 原句含多余空白/首尾空格' % tag)


def _nearest(q, sents):
    """找不到时给条线索：按词重叠挑最像的那句"""
    words = set(re.findall(r'[A-Za-z0-9\-]{4,}', q.lower()))
    best, bs = '', -1
    for s in sents:
        sw = set(re.findall(r'[A-Za-z0-9\-]{4,}', s.lower()))
        sc = len(words & sw)
        if sc > bs:
            best, bs = s, sc
    return best.strip()


def _dup_edges(v, errs):
    seen = set()
    for e in v['edges']:
        k = (e['from'], e['to'], e['rel'])
        if k in seen:
            errs.append('%s: 重复的边 %s' % (v['id'], k))
        seen.add(k)


# ---------------------------------------------------------------- 合并
def build():
    # 基底用**当前主产物**（没有才退回 P0 的老文件）：
    # 否则每次 build 都从旧文件重建，主产物上直接改的东西会被悄悄丢掉（双源漂移）
    base = MASTER if os.path.exists(MASTER) else LEGACY
    master = json.load(open(base, encoding='utf-8'))
    print('基底：%s' % os.path.basename(base))
    have = {s['id'] for s in master['sections']}
    added = []
    for sec in load_sections():
        if sec['id'] in have:
            master['sections'] = [sec if s['id'] == sec['id'] else s for s in master['sections']]
        else:
            added.append(sec)

    # 按页码排序，右栏 tab 顺序 = 论文顺序
    master['sections'] = sorted(master['sections'] + added,
                                key=lambda s: (s['pageRange'][0], s['id']))
    master['relVocabulary'] = vocab_json()

    # 实际用到的词 vs 登记的词，对不上的直接报出来
    used = set()
    types = {}
    for s in master['sections']:
        for v in s['views']:
            types[v.get('type', 'concept-map')] = types.get(v.get('type', 'concept-map'), 0) + 1
            for e in v.get('edges', []):
                used.add(e['rel'])
    master['relVocabulary']['actuallyUsed'] = sorted(used)
    master['relVocabulary']['unused'] = sorted(REL - used)
    master['generatedBy'] = 'Claude（抽取产线 pipeline.py build）'
    master['viewsSummary'] = types

    json.dump(master, open(MASTER, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('写出 %s' % MASTER)
    print('  节 %d 个 / 视图 %d 张 / %s' % (len(master['sections']), sum(types.values()), types))
    for s in master['sections']:
        print('   %-20s p.%-9s %s' % (s['id'], '%d-%d' % tuple(s['pageRange']),
              ' · '.join('%s(%s, %s)' % (v['title'], v.get('type', 'concept-map'), _size(v)) for v in s['views'])))
    if used - REL:
        print('  ⚠ 有边用了词表外的词：%s' % (used - REL))


def _size(v):
    if v.get('type') == 'domain':
        ts = v.get('domain', {}).get('tracks', [])
        return '%d 条轨道 / %d 个方块' % (len(ts), sum(len(t.get('segments', [])) for t in ts))
    return '%d 节点 / %d 边' % (len(v.get('nodes', [])), len(v.get('edges', [])))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'check'
    ids = sys.argv[2:] or None
    if cmd == 'vocab':
        for r, c, n, w in VOCAB:
            print('%-6s %-6s %s %s' % (r, c, '新增' if n else '草案', w))
        return
    if cmd == 'build':
        build()
        return
    secs = load_sections(ids)
    if not secs:
        print('没有找到节文件（%s）' % SECDIR)
        return
    if cmd == 'digest':
        for sec in secs:
            print('=' * 20, sec['id'], sec['title'], sec['pageRange'])
            for v in sec['views']:
                print(' -- %s [%s] %s' % (v['id'], v.get('type', 'concept-map'), v['title']))
                for n in v['nodes']:
                    print('    N %-22s %s' % (n['id'], n.get('short')))
                for e in v['edges']:
                    print('    E %-20s -[%s/%s/%s/%s]-> %s' % (e['from'], e['rel'], e.get('label'),
                                                               e.get('polarity'), e.get('origin'), e['to']))
        return
    total_e, total_w = 0, 0
    for sec in secs:
        errs, warns = check(sec)
        total_e += len(errs)
        total_w += len(warns)
        flag = '✓' if not errs else '✗'
        print('%s %-20s 错误 %d 警告 %d' % (flag, sec['id'], len(errs), len(warns)))
        for x in errs:
            print('   ✗ ' + x)
        for x in warns:
            print('   · ' + x)
    print('\n合计：错误 %d / 警告 %d' % (total_e, total_w))


if __name__ == '__main__':
    main()
