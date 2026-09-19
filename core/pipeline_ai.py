# -*- coding: utf-8 -*-
"""
PaperIDE · P3-b 一键分析（AI 抽取管线）

    sections.json ──逐节──→ prompt ──→ 模型 ──→ JSON ──→ 校验器 ──✗──→ 带错误重试
                                                            │
                                                            ✓
                                                            ↓
                                                   papers/<id>/ai/<sec>.json
                                                            ↓
                                                      graph.json（右栏渲染用）

**质量闭环**（接续文档 §5.1）：模型产出 → 过 `P0/_extract/pipeline.py check` → 不过就把
错误信息回灌给模型重试 → 过了才落盘。校验器**一个字都不用改**（它就是 P0 那个，
被 11 节手工数据验证过的闸门）。

增量：每节结果单独落盘在 `ai/` 下，重跑时已通过的节直接复用（`only=` 可指定单节重跑）。
"""
import json
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core import adapters, pdftext, quotefix, paths  # noqa: E402

# 校验器：直接 import P0 那个，**不改一行**（接续文档 §1.3 说它是全项目的质量闸门）
#
# ⚠ 这里踩过一个坑（2026-09-19 打包实测）：`_extract/` 一度不在打包白名单里，
#   于是 exe 里 `import pipeline` 直接 ModuleNotFoundError —— 界面照常打开、
#   论文照常读，**只有「一键分析」整个是死的**，而且死在 `run_analyze` 的
#   第一行（在 try 外面），后台线程静默死掉、进度条卡在"运行中"5 分钟。
#   PyInstaller 其实报了 `missing module named pipeline`，但它只是条警告，
#   构建照样"成功"。**白名单里必须有 `P0/_extract/pipeline.py`。**
#   它只用到 `check()` / `vocab_json()`（都吃传进来的对象），
#   不读 `_extract/sections/` `_extract/source/` 那些样例数据。
sys.path.insert(0, paths.res('P0', '_extract'))
try:
    import pipeline as P0CHECK                   # noqa: E402
except ImportError as _e:                        # 说清楚"是谁缺了、该去补哪儿"
    raise ImportError(
        '质量闸门 P0/_extract/pipeline.py 不在程序里（%s）。'
        '源码模式请确认文件还在；打包版请把它加进 packaging/app.spec 的 datas。' % _e)

PROMPT_DIR = paths.res('prompts')
MAX_ATTEMPT = 3                                  # 首次 + 2 次带错重试
CLUSTERS = {
    'mechanism': {'label': '机制发现', 'slot': 1},
    'application': {'label': '应用与工程改造', 'slot': 2},
    'conclusion': {'label': '结论与展望', 'slot': 3},
}


# ==================================================================== 工具
def _slug(s, n=48):
    return (re.sub(r'[^a-z0-9]+', '-', (s or '').lower()).strip('-') or 'v')[:n].strip('-')


def load_prompt(name):
    return open(os.path.join(PROMPT_DIR, name + '.md'), encoding='utf-8').read()


def paginate(text, page_range):
    """给正文插 `[第 N 页]` 标记——让模型知道每句话在哪一页，好写 evidence.page。
    分页是**按长度估的**（节内平均分布），够用：页码只用于"跳到附近"，±1 页可接受。"""
    a, b = page_range[0], page_range[1]
    n = b - a + 1
    if n <= 1:
        return '[第 %d 页]\n%s' % (a, text)
    # 尽量切在句末，别把句子劈开
    chunks, size, i = [], max(1, len(text) // n), 0
    while i < len(text):
        j = min(len(text), i + size)
        if j < len(text):
            k = text.rfind('. ', i + size // 2, min(len(text), j + size // 2))
            if k > 0:
                j = k + 1
        chunks.append(text[i:j].strip())
        i = j
    return '\n\n'.join('[第 %d 页]\n%s' % (a + k, c) for k, c in enumerate(chunks) if c)


# ==================================================================== 规范化
def normalize(view):
    """只补**外观性**默认值（短名、id 这类），**不碰**证据/词表/枚举——
    那些是校验器该拦的，补了就等于把错误藏起来。"""
    v = dict(view)
    v.setdefault('type', 'concept-map')
    v.setdefault('title', {'concept-map': '概念网络', 'flow': '流程图',
                           'domain': '模式图', 'matrix': '对照表'}.get(v['type'], '视图'))
    v['id'] = _slug(v.get('id') or v.get('title'))
    if v['type'] == 'domain':
        # 方块也要 id：前端的"概念气泡 → 高亮相关句"是按 id 找的（没 id 就点不开）
        for t in (v.get('domain') or {}).get('tracks', []):
            for s in t.get('segments', []):
                s.setdefault('id', _slug(s.get('label'), 32))
        return v
    if v['type'] == 'matrix':
        for r in (v.get('matrix') or {}).get('rows', []):
            r.setdefault('id', _slug(r.get('label'), 32))
        return v
    nodes = []
    for n in v.get('nodes', []):
        n = dict(n)
        n.setdefault('short', n.get('label'))
        n.setdefault('zh', n.get('label'))
        n['id'] = (n.get('id') or _slug(n.get('label'))).strip()
        nodes.append(n)
    v['nodes'] = nodes
    v['edges'] = [dict(e) for e in v.get('edges', [])]
    return v


def collect_ids(view, into):
    """累积全局概念 id 表——后面几节抽的时候能看到前面用过的 id，才能复用而不是新造。
    （规划 §4.2：id 必须全局，"第二篇论文进来时同名概念才不会变陌生人"）"""
    for n in view.get('nodes', []):
        if n.get('id'):
            into.setdefault(n['id'], n.get('short') or n.get('label') or n['id'])


def concepts_block(idmap):
    if not idmap:
        return '（这是第一节，还没有已用概念）'
    items = sorted(idmap.items())
    return '\n'.join('- `%s` = %s' % (k, v) for k, v in list(items)[:120])


# ==================================================================== 一节
def _dump_raw(rawdir, sid, raws, note):
    """把被校验器打回的**原稿**存盘。

    为什么要有它：一节没过校验时，日志只打 `errs[0][:110]`，而错误里的引文又只截前 60 字——
    2026-09-18 排查 eLife 那篇 Results 连挂 3 次时，**看不到模型到底写了什么**，
    只能靠猜（真凶在截断处之后）。留着原稿，同类问题下次一分钟定位。
    """
    if not rawdir or not raws:
        return
    try:
        os.makedirs(rawdir, exist_ok=True)
        json.dump({'note': note, 'attempts': raws},
                  open(os.path.join(rawdir, sid + '.json'), 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
    except Exception:
        pass


def analyze_section(sec, meta, idmap, adapter, progress, lock, rawdir=None):
    sid = sec['id']
    base = load_prompt('extract')
    # ⚠ **按本节规模给节点数封顶**——这条是血的教训：
    #   一个 1514 字、只有 6 句的 "Limitations" 小节，模型硬凑 6~14 个节点，
    #   凑不出来就开始**编原文里没有的句子**，一版下来 37 条校验错误，试 4 次全灭。
    #   把"你有几句话"直接写给它，比反复叮嘱"不要编造"有用得多。
    nsent = len(pdftext.sentences(sec['sourceText']))
    cap = max(3, min(14, nsent))
    # 出几张图：短节 1 张；中等节按句数 2–3 张（**原样保留，现有论文行为不变**）；
    # **长节放宽到 6 张**——长节里往往有好几个"意思块"（几段讲同一件事、再几段换下一件），
    # 硬卡 3 张会把内容挤掉。实测 eLife 那篇的 Results 14622 字只出了 3 张，
    # 而且第一张被一张流程图占了，**全节没有一张覆盖全局的概览概念网**。
    # ⚠ 6 是**安全上限、不是目标**：提示词里明写了"别为了凑满上限硬分"。
    _chars = len(sec['sourceText'])
    if _chars < 1600 or nsent < 10:
        nview = 1
    elif _chars < 6000:
        nview = 3 if nsent >= 22 else 2
    else:
        nview = 6
    def build_prompt(nv):
        """按"最多几张图"生成提示词。

        ⚠ 必须能**重建**：长节要 6 张图时，模型回话的 JSON 会顶到 max_tokens 被截断
        （见 adapters 里 finish_reason 那段），这时**重发一模一样的请求只会再被截断一次**。
        得把上限压下来重试（6 → 3 → 1），先保证这节能出图。
        """
        scale = ('## 本节规模（**按这个来定图的规模，不许超**）\n'
                 '原文 %d 字 / 切出 %d 句 → 最多 **%d 张图**，**每张图不超过 %d 个节点**。\n'
                 '**张数上限只是兜底、不是任务**：先把这一节分成几个"意思块"，一块一张图；'
                 '只讲一件事的节就出一张，**别为了凑满上限硬分**。\n'
                 '道理很简单：**每个节点都得有一句原文撑着**。句子不够就是不够——'
                 '与其硬凑节点去编一句原文里没有的话，不如少画几个。\n'
                 % (len(sec['sourceText']), nsent, nv, cap))
        payload = ('\n\n---\n\n## 本节\n标题：%s\n所属大节：%s\n页码：%d–%d\n节 id：`%s`\n\n'
                   '%s\n## 本节原文\n%s\n\n## 已有全局概念表\n%s\n' % (
                       sec['title'], sec.get('parent') or '（本节就是一级标题，无上级）',
                       sec['pageRange'][0], sec['pageRange'][1], sid,
                       scale, paginate(sec['sourceText'], sec['pageRange']),
                       concepts_block(dict(idmap))))
        return base + payload

    prompt = build_prompt(nview)

    def too_big(err):
        """这次失败是"输出太长被砍"吗？（max_tokens 截断 → 只剩半截 JSON）"""
        s = str(err)
        return ('截断' in s) or ('views 是空的' in s)

    def shrink(nv):
        """把"最多几张图"砍半，并重建提示词（重发原请求只会再被截断一次）"""
        nv2 = max(1, nv // 2)
        with lock:
            progress('%s 输出太长被截断 → 图的张数上限 %d → %d，换小一档重试' % (sid, nv, nv2))
        return nv2, build_prompt(nv2)

    last_errs = []
    raws = []                                  # 每次模型原稿都留着（出问题要能复盘，见 _dump_raw）
    for attempt in range(1, MAX_ATTEMPT + 1):
        p = prompt
        if last_errs:
            p += ('\n\n---\n\n## ⚠ 上一版被校验器打回了，逐条改掉再来\n'
                  '（每条都是硬错误，尤其注意：`quote` 必须是**上面原文里的原样片段**）\n'
                  + '\n'.join('- %s' % e for e in last_errs[:25]) +
                  '\n\n重新输出**完整**的 JSON（不要只给改动部分，不要解释）。\n')
        t0 = time.time()
        try:
            raw = adapter.json(p)
        except adapters.AIError as e:
            last_errs = ['模型调用失败：%s' % e]
            with lock:
                progress('%s 第 %d 次调用失败：%s' % (sid, attempt, str(e)[:120]))
            if too_big(e) and nview > 1:
                nview, prompt = shrink(nview)
            continue
        raws.append({'attempt': attempt, 'errorsBeforeThisAttempt': list(last_errs), 'raw': raw})
        title_zh = (raw.get('titleZh') or '').strip()
        # 导读 focus 校验器要 2–4 条，模型爱给 5 条。**多给的裁掉**（不是内容错，是形状超了），
        # 不值得为这个整份重来一次；少于 2 条才是真问题，留给重试去治。
        lead = raw.get('lead')
        if isinstance(lead, dict) and isinstance(lead.get('focus'), list) and len(lead['focus']) > 4:
            lead = dict(lead)
            lead['focus'] = lead['focus'][:4]
        sec_obj = {
            'id': sid, 'title': title_zh or sec['title'], 'titleEn': sec['title'],
            # ⚠ `pageRange` 的口径**不一致**（wu2020/shi2025 是期刊页码，降级篇是 PDF 页码），
            #   前端直接拿它当"第几页"显示会出「第 855 页」这种鬼页号。
            #   所以**照抄** sections.json 里的 `pdfPages`（真实 PDF 页码，9 篇全一致），
            #   由前端优先用它；`pageRange` 保留不动（别的地方还在读）。
            'pageRange': sec['pageRange'], 'pdfPages': sec.get('pdfPages'),
            'sourceText': sec['sourceText'],
            'lead': lead, 'clusters': CLUSTERS,
            'views': [normalize(v) for v in (raw.get('views') or [])],
        }
        if not sec_obj['views']:
            last_errs = ['views 是空的——这一节至少要出一张图']
            if nview > 1:                     # 多半是被截断了（只吐完 lead），换小一档
                nview, prompt = shrink(nview)
            continue
        # **导读是硬要求**：校验器把 lead 当可选字段（P0 时代允许分步补齐），
        # 模型偶尔就真的不写——第一轮验收抓到的"9 节里 3 节没导读"里，有一部分是这个。
        # 所以在这一层把关：缺了/太短/条数不对，都算错误，让它重来。
        prob = None
        if not isinstance(lead, dict) or not str(lead.get('gist') or '').strip():
            prob = '缺 lead（每节都必须有导读：一句话说清这节讲什么、结论是什么）'
        elif len(str(lead['gist']).strip()) < 30:
            prob = 'lead.gist 只有 %d 字（至少 30 字）' % len(str(lead['gist']).strip())
        elif not isinstance(lead.get('focus'), list) or not 2 <= len(lead['focus']) <= 4:
            prob = 'lead.focus 必须是 2–4 条（现在 %s）' % (
                len(lead['focus']) if isinstance(lead.get('focus'), list) else '缺')
        if prob:
            last_errs = [prob]
            with lock:
                progress('%s %s，重试' % (sid, prob))
            continue
        # **先把引文吸附回原文**（模型十次有八次是"记忆式复述"，不是原样复制）：
        # 不改校验器（那会让溯源失真），而是把引文修成原文里的真实片段。
        # 这一下能省掉绝大多数重试——实测每节平均少重试一次。
        fixed = quotefix.repair_section(sec_obj)
        if fixed:
            with lock:
                progress('%s 吸附了 %d 条引文回原文' % (sid, fixed))
        errs, warns = P0CHECK.check(sec_obj)
        if not errs:
            if attempt > 1:                       # 一次就过 = 正常；重试才过 = 有质量损失，留个底
                _dump_raw(rawdir, sid, raws, '第 %d 次才过（前 %d 次被校验器打回）' % (attempt, attempt - 1))
            with lock:
                for v in sec_obj['views']:
                    collect_ids(v, idmap)
            return sec_obj, warns, attempt, time.time() - t0
        last_errs = errs
        with lock:
            progress('%s 第 %d 次没过校验（%d 条错），带错误重试：%s'
                     % (sid, attempt, len(errs), errs[0][:110]))

    # ── 兜底：常规重试都失败后，**降级**再试一次 ──
    # 用户看到的应该是"每节都有图"。宁可图简单一点，也不要留一节空的。
    # 所以把要求压到最小：只要一张概念网、6–8 个节点、每条边一句原文引用。
    _dump_raw(rawdir, sid, raws, '常规重试都失败，改用最简版兜底')     # 掉到兜底 = 这一节的质量损失，留证据
    with lock:
        progress('%s 常规重试都失败，改用**最简版**再试一次' % sid)
    # ⚠ 简化版**只能简化"要几张图"，不能把约束一起简化掉**：
    #   第一版直接切掉了后半段，结果受控词表和枚举约束全丢了，
    #   模型立刻开始写 `cluster: phenotype`、`rel: interacts_with` —— 照样过不了。
    #   做法：按 `## ` 小标题切块，**只替换"输出格式"那一块**，其余原样保留。
    blocks, cur = [], []
    for line in prompt.split('\n'):
        if line.startswith('## ') and cur:
            blocks.append('\n'.join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append('\n'.join(cur))
    base = '\n'.join(b for b in blocks if not b.startswith('## 二、输出格式'))
    simple = (base +
              '\n\n## 二、输出格式（**简化版**）\n\n'
              '这一次**只要一张图**：`type` 用 `concept-map`，**6–8 个节点**，节点之间 5–8 条边。\n'
              '不要流程图、不要模式图、不要对照表。\n\n'
              '```json\n{"titleZh": "本节标题的中文",'
              '"lead": {"gist": "≥30字", "focus": ["…", "…"]},'
              '"views": [{"id": "v1", "type": "concept-map", "title": "概念网络",'
              '"nodes": [{"id": "全局id", "label": "…", "short": "…", "zh": "…",'
              '"cluster": "mechanism", "evidence": [{"page": 页码, "quote": "原文原句"}]}],'
              '"edges": [{"from": "…", "to": "…", "rel": "受控词表里的词", "label": "中文",'
              '"polarity": "promote|inhibit|neutral", "origin": "sentence|structural|inferred",'
              '"evidence": [{"page": 页码, "quote": "原文原句"}]}]}]}\n```\n\n'
              '⚠ 每一条 `quote` 都必须是**上面那段原文里的原样片段**，一个字都不能改。\n')
    # ⚠ 这里原来写的是 `+ payload` —— 而 `payload` 是 `build_prompt()` 里的**局部变量**，
    #   在这儿根本不存在 → `NameError` → **兜底路径一次都没成功过**。
    #   （2026-09-19 用 pyflakes 扫出来的 `undefined name 'payload'`。独立验收反馈
    #     "有节不过校验时那一节就没图"，根因之一就是这里 —— 安全网自己破了个洞。）
    #
    #   删掉是对的，不是"补一个变量进来"：上面按 `## ` 切块时，`## 本节` / `## 本节原文` /
    #   `## 已有全局概念表` 都是**独立的块**、不属于"输出格式"那一块，所以它们**原样留在 `base` 里**了。
    #   再 `+ payload` 等于把本节原文和概念表**重复贴一遍**，纯属多余。
    if last_errs:
        simple += ('\n\n## ⚠ 前面几版被校验器打回的错误（这次务必避开）\n'
                   + '\n'.join('- %s' % e for e in last_errs[:12]) + '\n')
    try:
        raw = adapter.json(simple)
        raws.append({'attempt': 'fallback', 'errorsBeforeThisAttempt': list(last_errs), 'raw': raw})
        obj = {
            'id': sid, 'title': (raw.get('titleZh') or '').strip() or sec['title'],
            'titleEn': sec['title'], 'pageRange': sec['pageRange'],
            'pdfPages': sec.get('pdfPages'),          # 见 analyze_section 里的说明
            'sourceText': sec['sourceText'],
            'lead': lead, 'clusters': CLUSTERS,
            'views': [normalize(v) for v in (raw.get('views') or [])],
        }
        if obj['views']:
            errs, warns = P0CHECK.check(obj)
            if not errs:
                with lock:
                    for v in obj['views']:
                        collect_ids(v, idmap)
                return obj, warns, MAX_ATTEMPT + 1, 0.0
            last_errs = errs
    except adapters.AIError as e:
        last_errs = ['简化版也调用失败：%s' % str(e)[:120]]
    _dump_raw(rawdir, sid, raws, '连最简版兜底都没过（这一节会没有图）')
    return None, last_errs, MAX_ATTEMPT + 1, 0.0


# ==================================================================== 全篇
def analyze(pid, only=None, workers=3, progress=None, force=False):
    progress = progress or (lambda m: None)
    pdir = paths.paper_dir(pid)
    sections = json.load(open(os.path.join(pdir, 'sections.json'), encoding='utf-8'))['sections']
    meta = {}
    mp = os.path.join(pdir, 'meta.json')
    if os.path.exists(mp):
        meta = json.load(open(mp, encoding='utf-8'))
    aidir = os.path.join(pdir, 'ai')
    os.makedirs(aidir, exist_ok=True)

    # 缓存要**全量读**：只补跑一节时，别的节的成果也得算进来，
    # 否则 build_graph 会拿着"只有一节"的结果去重建整篇图——其他节的图全没了。
    # ⚠ --force 的语义是"**这几节**重抽"，不是"全篇推倒"：
    #   过去一句 `or force` 把整份缓存清空，只补跑一节就会让其他节的图全没了
    #   （build_graph 拿着只剩一节的结果去重建整篇）。只对本次点名要跑的节生效。
    force_ids = set(only) if only else None
    done_cache, stale = {}, []
    for s in sections:
        f = os.path.join(aidir, s['id'] + '.json')
        if not os.path.exists(f) or (force and (force_ids is None or s['id'] in force_ids)):
            continue
        try:
            obj = json.load(open(f, encoding='utf-8'))
        except Exception:
            continue
        # 缓存也要**按当前正文复检**：重切分节（或改了清洗规则）之后，旧成果里的引文
        # 可能已经对不上新正文了——不复检就会把过期的图当成好数据发出去。
        obj['sourceText'] = s['sourceText']
        errs, _w = P0CHECK.check(obj)
        if errs:
            stale.append((s['id'], errs[0]))
        else:
            done_cache[s['id']] = obj
    for sid, e in stale:
        progress('⚠ %s 的旧成果对不上新正文，作废重抽：%s' % (sid, e[:90]))
    todo = [s for s in sections if not only or s['id'] in only]
    todo = [s for s in todo if s['id'] not in done_cache]
    progress('共 %d 节，已有成果 %d 节，本次要抽 %d 节' % (len(sections), len(done_cache), len(todo)))
    rawdir = os.path.join(aidir, '_raw')       # 被打回的原稿存这儿（见 _dump_raw）

    idmap, lock = {}, threading.Lock()
    results, failed = dict(done_cache), {}
    adapter = adapters.get_adapter()

    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(analyze_section, s, meta, idmap, adapter, progress, lock, rawdir): s for s in todo}
        for k, f in enumerate(cf.as_completed(futs), 1):
            s = futs[f]
            sec_obj, errs, attempt, dt = f.result()
            if sec_obj:
                results[s['id']] = sec_obj
                json.dump(sec_obj, open(os.path.join(aidir, s['id'] + '.json'), 'w', encoding='utf-8'),
                          ensure_ascii=False, indent=1)
                progress('[%d/%d] ✓ %s — %d 张图，%d 次通过，%.0fs'
                         % (k, len(todo), s['id'], len(sec_obj['views']), attempt, dt))
            else:
                failed[s['id']] = errs
                progress('[%d/%d] ✗ %s — %d 次都没过校验' % (k, len(todo), s['id'], attempt))

    if failed:
        json.dump(failed, open(os.path.join(pdir, 'ai', '_failed.json'), 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)

    gp = build_graph(pid, sections, results, meta)
    try:
        from core import layout
        n = layout.run(gp)
        progress('语义布局：%d 张概念网算了坐标' % n)
    except Exception as e:                       # 布局是锦上添花，算不出来不该让整轮失败
        progress('语义布局跳过（%s）' % str(e)[:100])
    return {'ok': len(results), 'failed': failed, 'total': len(sections)}


def build_graph(pid, sections, results, meta):
    """合并成右栏渲染要的 graph.json（格式与 P0 的 wu2020.json 一致，前端不用改）。"""
    # ⚠ `pdir` 必须在**本函数里**取一次。2026-09-19 的路径重构里，
    #   下面几处 `os.path.join(ROOT, 'papers', pid, ...)` 被改成了 `pdir` ——
    #   但 `pdir` 是 `analyze()` 的**局部变量**，`build_graph()` 根本看不到它。
    #   结果：一键分析把 9 节全抽完、标注翻译都成功，最后一步
    #   `NameError: name 'pdir' is not defined` → **análisis 完成但没有 graph.json**
    #   → 右栏永远「这一篇还没有图」，而 job 照样报 done。
    #   （独立验收抓到的，见 验收/验收报告-11。**重构之后一定要跑一遍真流程**，
    #    光跑读数据的老测试是看不出来的 —— 它们读的是已经落盘的旧成果。）
    pdir = paths.paper_dir(pid)
    out, types = [], {}
    for s in sections:
        r = results.get(s['id'])
        # ⚠ 失败的节**也要留在 tab 里**（views 空着）——不然用户看到的是"论文少了一节"，
        #   根本不知道是抽取没通过。留着才能明确告诉他"这一节没出图，可以单独重跑"。
        views = r['views'] if r else []
        # ⚠ 视图 id 必须在一节内**唯一**，不靠模型自觉：
        #   模型看到 prompt 里的"节 id"就照着抄，三张图的 id 全等于节 id ——
        #   前端 `views.find(v => v.id === vid)` 于是永远命中第一张，
        #   **点哪个视图 tab 都只显示第一张图**（实测踩过，多图等于白做）。
        for k, v in enumerate(views, 1):
            v['id'] = '%s-v%d' % (s['id'], k)
            types[v.get('type', 'concept-map')] = types.get(v.get('type', 'concept-map'), 0) + 1
        out.append({'id': s['id'],
                    # 顶栏 tab 上显示中文名（跟手工版一个规格）；英文原名留着备查
                    'title': (r or {}).get('title') or s['title'],
                    'titleEn': s['title'],
                    # ⚠ 两套页码都带上，**口径写死在这里**：
                    #   `pdfPages` = 真实 PDF 页号（sections.json 里的权威值，前端优先用它）
                    #   `pageRange` = 老口径，wu2020/shi2025 是期刊页码、降级篇是 PDF 页码，
                    #                 **不一致**，只为向后兼容保留（别的代码可能还在读）
                    #   优先取抽取结果里的（老缓存没有就不取），退回 sections.json——
                    #   两处都缺时给 None，绝不抛异常。
                    'pageRange': s['pageRange'],
                    'pdfPages': (r or {}).get('pdfPages') or s.get('pdfPages'),
                    'lead': (r or {}).get('lead'), 'sourceText': s['sourceText'],
                    'clusters': CLUSTERS, 'views': views,
                    'analysisFailed': (not r)})
    g = {
        'schemaVersion': 'p3b-1',
        'generatedBy': 'PaperIDE 一键分析（core/pipeline_ai.py + %s 适配器）' % adapters.get_adapter().name,
        'generatedAt': time.strftime('%Y-%m-%d %H:%M:%S'),
        'paper': {'id': pid, 'title': meta.get('title', pid),
                  'citation': meta.get('authors', ''), 'pdf': 'paper.pdf'},
        'relVocabulary': P0CHECK.vocab_json(),
        # PDF 页 → 期刊页码：前端显示"论文第 X 页"要用（不能写死 +853 那种偏移，
        # 每篇论文的起始页码都不一样）
        'pageMap': (json.load(open(os.path.join(pdir, 'sections.json'),
                                   encoding='utf-8')).get('doc', {}).get('pageMap', {})),
        'sections': out,
        'viewsSummary': types,
    }
    p = os.path.join(pdir, 'graph.json')
    if os.path.exists(p):
        # 覆盖前留一份 —— Wu2020 的 graph.json 是**手工做的金标准**，
        # 被自动版盖掉就找不回来了（P0/wu2020.json 还在，但别再赌一次）
        try:
            os.replace(p, os.path.join(pdir, 'graph.prev.json'))
        except OSError:
            pass
    json.dump(g, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return p


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        print(__doc__)
        return
    pid = args[0]
    only = args[1:] or None
    r = analyze(pid, only=only, force='--force' in sys.argv,
                progress=lambda m: print(m, flush=True))
    print('\n完成：%d 节成功 / %d 节失败（共 %d 节）' % (r['ok'], len(r['failed']), r['total']))
    for k, v in r['failed'].items():
        print('  ✗ %s：%s' % (k, '; '.join(v[:3])))


if __name__ == '__main__':
    main()
