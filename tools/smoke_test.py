# -*- coding: utf-8 -*-
"""
PaperIDE · 冒烟测试（服务层，秒级）

**这不是验收**——验收要真人真鼠标走界面（见 验收/00-验收手册.md）。
这个是给开发自己用的：接口层有没有明显的破绽，几秒钟就能知道。
验收之前先跑这个，别让验收的人在明显坏掉的东西上浪费一小时。

用法：
    python tools/smoke_test.py [paper_id]
    python tools/smoke_test.py --analyze   # 连"一键分析"也真跑一遍（慢）
"""
import json
import sys
import time
import urllib.request

BASE = 'http://127.0.0.1:8777'


def get(path, timeout=30):
    req = urllib.request.Request(BASE + path)
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def post(path, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else b''
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


class Check(object):
    def __init__(self):
        self.ok = self.bad = 0
        self.fails = []

    def __call__(self, cond, label, detail=''):
        if cond:
            self.ok += 1
            print('  ✓ %s' % label)
        else:
            self.bad += 1
            self.fails.append(label)
            print('  ✗ %s   %s' % (label, detail))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    c = Check()
    print('== 0. 服务在跑吗 ==')
    try:
        lib = get('/api/papers')
    except Exception as e:
        print('  ✗ 连不上 %s —— 先双击 打开平台.cmd\n    %s' % (BASE, e))
        return 2
    c(bool(lib.get('papers')), '论文库能列出', str(lib)[:80])

    pid = args[0] if args else lib['papers'][0]['id']
    print('\n== 1. 论文 %s 的接口 ==' % pid)
    meta = get('/api/papers/%s/meta' % pid)
    c(bool(meta.get('title')), 'meta.json 有标题', meta.get('title', '')[:60])
    secs = get('/api/papers/%s/sections' % pid)
    c(len(secs['sections']) >= 3, 'sections.json 有 ≥3 节', '%d 节' % len(secs['sections']))
    c(all(s.get('sourceText') for s in secs['sections']), '每节都有正文')
    c(all(s.get('pageRange') for s in secs['sections']), '每节都有页码')

    print('\n== 2. 静态资源 ==')
    for p, name in [('/', '首页'), ('/d3.min.js', 'd3'), ('/pdf.min.js', 'pdf.js'),
                    ('/api/papers/%s/pdf' % pid, '论文 PDF')]:
        try:
            r = urllib.request.urlopen(BASE + p, timeout=60)
            c(r.status == 200 and len(r.read(64)) > 0, name + ' 能取到')
        except Exception as e:
            c(False, name + ' 能取到', str(e)[:60])

    print('\n== 3. 图 / 标注 / 翻译 ==')
    try:
        g = get('/api/papers/%s/graph' % pid)
    except Exception as e:
        print('  ! graph.json 还没有（没分析过）— %s' % e)
        g = None
    if g:
        ss = g.get('sections', [])
        withv = [s for s in ss if s.get('views')]
        c(len(withv) == len(ss), '每一节都有图', '%d/%d' % (len(withv), len(ss)))
        c(all(s.get('lead') for s in ss), '每一节都有导读',
          '%d/%d' % (len([s for s in ss if s.get('lead')]), len(ss)))
        bad = []
        for s in ss:
            for v in s.get('views', []):
                if v.get('type') in ('domain', 'matrix'):
                    continue
                if not v.get('nodes') or not v.get('edges'):
                    bad.append(s['id'])
        c(not bad, '概念网/流程图都有节点和边', '空的：%s' % bad[:4])
        c(bool(g.get('paper', {}).get('title')), 'paper.title 有值')
    try:
        a = get('/api/papers/%s/annotations' % pid)
        c(a.get('count', 0) >= 20, '标注层 ≥20 条', '%d 条' % a.get('count', 0))
        c(all(i.get('rects') for i in a.get('items', [])), '每条标注都有坐标')
        c(all(i.get('zh') for i in a.get('items', [])), '每条标注都有中文释义')
        c(all(i.get('type') in ('术语', '搭配', '动词', '逻辑', '句式') for i in a.get('items', [])),
          '标注类型都在五类里')
    except Exception as e:
        print('  ! annotations.json 还没有 — %s' % e)
    try:
        n = get('/api/papers/%s/notebook' % pid)
        c(len(n.get('glossary', [])) > 0, '翻译缓存·词表非空', '%d 条' % len(n.get('glossary', [])))
    except Exception as e:
        print('  ! notebook.json 还没有 — %s' % e)

    print('\n== 4. 划词翻译（真调一次）==')
    try:
        r = post('/api/papers/%s/translate' % pid,
                 {'text': 'signalling', 'sentence': 'Charge-based immunoreceptor signalling in health and disease',
                  'page': 1})
        c(bool(r.get('sentenceZh')), '整句译文拿到了', (r.get('sentenceZh') or '')[:50])
        c(bool(r.get('termZh')), '词义拿到了', (r.get('termZh') or '')[:30])
        t0 = time.time()
        r2 = post('/api/papers/%s/translate' % pid,
                  {'text': 'signalling', 'sentence': 'Charge-based immunoreceptor signalling in health and disease',
                   'page': 1})
        c(r2.get('cached') is True and (time.time() - t0) < 1.0, '第二次命中缓存且瞬时',
          '%.2fs cached=%s' % (time.time() - t0, r2.get('cached')))
    except Exception as e:
        c(False, '划词翻译', str(e)[:120])

    if '--analyze' in sys.argv:
        print('\n== 5. 一键分析（真跑，会很慢）==')
        j = post('/api/papers/%s/analyze' % pid)
        while True:
            time.sleep(10)
            st = get('/api/jobs/%s' % j['job'])
            print('   … %s' % (st['log'][-1] if st.get('log') else st['state']))
            if st['state'] in ('done', 'failed'):
                c(st['state'] == 'done', '一键分析跑完', str(st.get('error'))[:120])
                break

    print('\n' + '=' * 56)
    print('通过 %d / 失败 %d' % (c.ok, c.bad))
    if c.fails:
        print('失败项：' + '、'.join(c.fails))
    return 0 if c.bad == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
