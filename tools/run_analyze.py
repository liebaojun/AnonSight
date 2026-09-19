# -*- coding: utf-8 -*-
"""
触发「一键分析」并等它跑完，最后打印一份**核对用的底账**。

给验收的人用：这样它不用自己写轮询、也不用来回问接口。
用法：
    python tools/run_analyze.py <paper_id>          # 触发并等待
    python tools/run_analyze.py <paper_id> --digest # 只看底账（不触发）
    python tools/run_analyze.py <paper_id> --job <id>   # 等一个已经在跑的 job

输出最后那份底账（"服务端认为的真相"）是**拿去跟界面上看到的对**的：
节数对不对、每节几张图、标注多少条。对不上就是有问题。
"""
import json
import sys
import time
import urllib.request

# ⚠ 验收实测这个脚本在 GBK 控制台会崩：日志里有 ✓ ✗ 这些字符，
#   Windows 默认控制台编码是 GBK，一 print 就 UnicodeEncodeError 直接退出。
#   自己把输出流设成 UTF-8，别指望调用方记得加 PYTHONIOENCODING。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE = 'http://127.0.0.1:8777'


def get(path, timeout=60):
    return json.load(urllib.request.urlopen(BASE + path, timeout=timeout))


def post(path):
    req = urllib.request.Request(BASE + path, data=b'', headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=60))


def digest(pid):
    print('=' * 62)
    print('底账（服务端认为的真相）· 论文 %s' % pid)
    print('=' * 62)
    meta = get('/api/papers/%s/meta' % pid)
    print('标题：%s  ·  %s 页' % (meta.get('title'), meta.get('pdfPages')))
    g = get('/api/papers/%s/graph' % pid)
    ss = g.get('sections', [])
    withv = [s for s in ss if s.get('views')]
    withl = [s for s in ss if s.get('lead')]
    print('节数：%d ｜ 有图：%d ｜ 有导读：%d' % (len(ss), len(withv), len(withl)))
    print('视图总数：%s' % g.get('viewsSummary'))
    print('-' * 62)
    for s in ss:
        v = ' · '.join('%s[%s]' % (x['title'][:18], x['type']) for x in s.get('views', []))
        flag = ' ' if s.get('views') else '✗'
        print('%s %-28s %-9s %s' % (flag, s['title'][:28], '%d-%d' % tuple(s['pageRange']), v[:52]))
    try:
        a = get('/api/papers/%s/annotations' % pid)
        print('-' * 62)
        print('标注层：%d 条' % a['count'])
        for it in a['items'][:3]:
            print('   [%s] %-30s → %s' % (it['type'], it['text'][:30], it['zh'][:24]))
    except Exception as e:
        print('标注层：读不到（%s）' % e)
    try:
        n = get('/api/papers/%s/notebook' % pid)
        print('翻译缓存：词表 %d 条 · 整句 %d 条' % (len(n.get('glossary', [])), len(n.get('sentences', []))))
    except Exception as e:
        print('翻译缓存：读不到（%s）' % e)
    print('=' * 62)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        print(__doc__)
        return
    pid = args[0]
    if '--digest' in sys.argv:
        digest(pid)
        return
    if '--job' in sys.argv:
        jid = sys.argv[sys.argv.index('--job') + 1]
    else:
        print('触发一键分析…')
        r = post('/api/papers/%s/analyze' % pid)
        jid = r['job']
    print('job = %s（在跑就给这个窗口留着，别关）' % jid)
    last = ''
    t0 = time.time()
    while True:
        time.sleep(20)
        try:
            j = get('/api/jobs/%s' % jid)
        except Exception:
            continue
        line = (j.get('log') or [''])[-1]
        if line != last:
            last = line
            print('  [%5.0fs] %s' % (time.time() - t0, line[:130]), flush=True)
        if j['state'] != 'running':
            print('\n结束：%s  %s' % (j['state'], j.get('error') or ''))
            for s in j.get('steps', []):
                print('   %s %s — %s' % ({'done': '✓', 'failed': '✗'}.get(s['state'], '·'),
                                         s['name'], s.get('detail', '')))
            break
    print()
    digest(pid)


if __name__ == '__main__':
    main()
