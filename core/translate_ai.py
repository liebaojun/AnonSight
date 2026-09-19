# -*- coding: utf-8 -*-
"""
PaperIDE · P4-a 翻译（两个入口，同一套 API、同一个缓存）

接续文档 §2.2⑤ 特别强调：翻译**不是**只做"导入时批量翻一次"。

    入口 A  导入时批量（预热）  一键分析那一步，给自动标注层每条表达配中文释义 → 灌进缓存
    入口 B  阅读时实时          用户划词/划句的当下，整句译文 + 语境义 → 先查缓存，没有再调

**两者共用一份 `notebook.json`**：
  · 导入时翻过的，阅读时划到就是**瞬时命中**（不联网）
  · 没翻过的当场调 API 并写回缓存
  → 越读越快、离线也有底子——这是这个设计的价值所在

⚠ B 对延迟敏感（划完就要看到东西）：提示词要短、单次调用、**先查缓存**。
   超过约 2 秒前端要出"翻译中…"占位。
"""
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core import adapters, paths                   # noqa: E402

# 用 RLock：save_nb 会在已经持锁的地方被调用（翻译写入那段），普通 Lock 会自锁死。
# 会撞车是真的——「一键分析」的翻译预热在后台写，用户同时在划词翻译就也在写，
# 两个 writer 交错会把 notebook.json 写成半截 JSON。
_LOCK = threading.RLock()
_CACHE = {}                                        # pid → notebook dict（进程内热缓存）


def load_prompt(name):
    # prompt 是**只读资源**（跟着程序走）；notebook 是**用户数据**（跟着论文走）。
    # 打包后这两者不在同一个目录里 —— 见 core/paths.py。
    return open(paths.res('prompts', name + '.md'), encoding='utf-8').read()


def nb_path(pid):
    return os.path.join(paths.paper_dir(pid), 'notebook.json')


def load_nb(pid):
    with _LOCK:
        if pid in _CACHE:
            return _CACHE[pid]
    p = nb_path(pid)
    if os.path.exists(p):
        d = json.load(open(p, encoding='utf-8'))
    else:
        d = {'schemaVersion': 'p4a-1', 'paperId': pid,
             'note': '阅读笔记层：翻译缓存 + 摘录库。由 core/translate_ai.py 读写。',
             'glossary': [], 'sentences': [], 'excerpts': []}
    d.setdefault('glossary', [])
    d.setdefault('sentences', [])
    d.setdefault('excerpts', [])
    with _LOCK:
        _CACHE[pid] = d
    return d


def save_nb(pid, d):
    """落盘。**先写临时文件再 os.replace** —— 直接覆盖的话，写到一半被中断
    （进程被杀 / 磁盘满）会留下半截 JSON，下次 `load_nb` 直接抛异常、整份缓存没了。"""
    d['savedAt'] = time.strftime('%Y-%m-%d %H:%M:%S')
    p = nb_path(pid)
    tmp = p + '.tmp'
    with _LOCK:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)


def find_sentence(nb, sentence):
    s = ' '.join((sentence or '').split()).strip()
    if not s:
        return None
    for x in nb['sentences']:
        if ' '.join((x.get('en') or '').split()) == s:
            return x
    return None


def find_term(nb, text):
    t = (text or '').strip().lower()
    if not t:
        return None
    for g in nb['glossary']:
        if (g.get('term') or '').strip().lower() == t:
            return g
    return None


def _adapter():
    return adapters.DeepSeekAdapter(timeout=60, temperature=0.2)


# ⚠ 模型回"这不是一个完整的词/句子"时，**这一条不许进词表缓存**（2026-09-19 修）。
#   为什么：选区的字符定位会差一个字符，**行尾断词**被削掉首字母就成了不存在的词
#   （hypophosphorylated → yphophosphorylated），模型答"无法确定"——**它答得对**。
#   错的是把这份答复当成词条存下来：查词按**前 5 个字母的词根**匹配（`hypop`），
#   垃圾条目会去抢真正词条的匹配，越用越脏。
#   判据用关键词（模型是自然语言回话，没有结构化字段）；宁可少存一条，不可存错一条。
_REFUSE_WORDS = ('无法确定', '不完整', '截断', '片段', '不是一个完整的', '不是完整的')


def _is_refusal(term_zh, warn):
    s = '%s %s' % (term_zh or '', warn or '')
    return any(w in s for w in _REFUSE_WORDS)


def translate(pid, text, sentence=None, page=None, section_id=None):
    """入口 B：阅读时的即时翻译。返回 {sentenceZh, termZh, kind, warn, note, cached, refused}

    `refused=True` 表示模型判定"选中的东西不是个完整的词/句子"——
    前端据此**不把它写进本地词表**（服务端这边也不写），两边判据一致。"""
    nb = load_nb(pid)
    sentence = sentence or text
    hit_s = find_sentence(nb, sentence)
    hit_t = find_term(nb, text)
    if hit_s and hit_t:
        return {'sentenceZh': hit_s['zh'], 'termZh': hit_t.get('zh', ''),
                'kind': hit_t.get('kind', '单词'), 'warn': hit_t.get('warn', ''),
                'note': hit_t.get('note', ''), 'cached': True, 'refused': False}

    prompt = (load_prompt('translate') +
              '\n\n---\n\n## 用户选中的部分\n%s\n\n## 它所在的整句\n%s\n' % (text, sentence))
    r = _adapter().json(prompt)
    sent_zh = (r.get('sentenceZh') or '').strip()
    term_zh = (r.get('termZh') or '').strip()
    warn = (r.get('warn') or '').strip()
    refused = _is_refusal(term_zh, warn)
    if refused:                                  # 判为拒答 → 两边都不落缓存，只把答复回给前端
        return {'sentenceZh': sent_zh, 'termZh': term_zh, 'kind': (r.get('kind') or '单词').strip(),
                'warn': warn, 'note': (r.get('note') or '').strip(),
                'cached': False, 'refused': True}
    with _LOCK:
        if sent_zh and not hit_s:
            nb['sentences'].append({'page': page, 'sectionId': section_id,
                                    'en': sentence, 'zh': sent_zh})
        if term_zh and not hit_t:
            nb['glossary'].append({
                'id': 'g.' + ''.join(ch for ch in text.lower() if ch.isalnum())[:32],
                'term': text, 'kind': (r.get('kind') or '单词').strip(), 'zh': term_zh,
                'warn': warn, 'note': (r.get('note') or '').strip(),
                'exampleEn': sentence, 'exampleZh': sent_zh,
            })
        save_nb(pid, nb)
    return {'sentenceZh': sent_zh, 'termZh': term_zh, 'kind': (r.get('kind') or '单词').strip(),
            'warn': warn, 'note': (r.get('note') or '').strip(),
            'cached': False, 'refused': False}


def prewarm(pid, progress=None, limit=None):
    """入口 A：把标注层里每条表达配好中文释义，灌进缓存——用户第一次划到就是瞬时命中。

    标注层已经有 `zh`（中文意思）了，所以这里**只需要补整句译文**，一条一次调用太多；
    改成按节批量：一节一次调用，把该节所有引用句一起翻。"""
    progress = progress or (lambda m: None)
    pdir = paths.paper_dir(pid)
    ap = os.path.join(pdir, 'annotations.json')
    if not os.path.exists(ap):
        return {'ok': 0, 'reason': '还没有标注层'}
    items = json.load(open(ap, encoding='utf-8'))['items']
    nb = load_nb(pid)

    # 标注层的中文释义直接并入术语表（这部分不用再问模型）
    added_g = 0
    for it in items:
        if find_term(nb, it['text']):
            continue
        nb['glossary'].append({
            'id': 'g.' + ''.join(ch for ch in it['text'].lower() if ch.isalnum())[:32],
            'term': it['text'], 'kind': it.get('type', '术语'), 'zh': it.get('zh', ''),
            'warn': '', 'note': it.get('tip', ''),
            'exampleEn': it.get('quote', ''), 'exampleZh': '',
            'src': 'auto-annotate',
        })
        added_g += 1

    # 只补还没翻过的整句
    todo = []
    for it in items:
        q = ' '.join((it.get('quote') or '').split())
        if q and not find_sentence(nb, q) and q not in [x[0] for x in todo]:
            todo.append((q, it.get('sectionId'), it.get('journalPage')))
    if limit:
        todo = todo[:limit]
    progress('术语表 +%d 条；待翻整句 %d 条' % (added_g, len(todo)))

    ad = _adapter()
    ok = 0
    for k in range(0, len(todo), 8):                 # 8 句一批，省调用也省时间
        batch = todo[k:k + 8]
        prompt = ('把下面每条英文各翻成一句通顺的中文学术译文。**专业名词保留英文原文**'
                  '（如 Csk、ITAM、CAR-T、TCR）。只输出 JSON：{"zh": ["译文1", "译文2", ...]}'
                  '，条数与输入一一对应。\n\n' +
                  '\n'.join('%d. %s' % (i + 1, b[0]) for i, b in enumerate(batch)))
        try:
            r = ad.json(prompt)
            zhs = r.get('zh') or []
        except adapters.AIError as e:
            progress('整句翻译失败（跳过这一批）：%s' % str(e)[:100])
            continue
        for b, z in zip(batch, zhs):
            if z:
                nb['sentences'].append({'page': b[2], 'sectionId': b[1], 'en': b[0],
                                        'zh': str(z).strip()})
                ok += 1
        progress('整句翻译 %d/%d' % (min(k + 8, len(todo)), len(todo)))
    save_nb(pid, nb)
    return {'ok': ok, 'glossary': added_g, 'sentences': len(nb['sentences'])}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        print(__doc__)
        return
    if '--lookup' in sys.argv:
        i = sys.argv.index('--lookup')
        text = sys.argv[i + 1]
        sent = sys.argv[i + 2] if len(sys.argv) > i + 2 else text
        print(json.dumps(translate(args[0], text, sent), ensure_ascii=False, indent=1))
        return
    print(json.dumps(prewarm(args[0], progress=lambda m: print(m, flush=True)),
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
