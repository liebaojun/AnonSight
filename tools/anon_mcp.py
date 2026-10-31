# -*- coding: utf-8 -*-
"""AnonSight MCP 服务器（stdio）

主人 2026-10-25 要的：「你用那个 anonsight 能不能搞个 MCP 什么的，就不用在你用的时候也
打开 UI 界面了。你直接后台操作就行，UI 的图形化是给我用的」。

这个 MCP 把 AnonSight 的本地 HTTP 接口包成工具：
  · 找不到服务时，自动启动 **headless**（`packaging/app.py --headless`，不开窗）；
  · 找到已有服务（UI 或 headless）就直接复用，不会起第二个抢同一个 papers/；
  · 数据目录默认 `E:\\AnonSight`（可用环境变量 PAPERIDE_DATA 覆盖）。

工具：
  anonsight_status / ensure_server / list_papers / import_pdf / import_folder
  analyze_paper / resection_paper / get_sections / get_graph / get_job / stop_headless
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.abspath(os.environ.get('PAPERIDE_DATA') or r'E:\AnonSight')
PY = sys.executable
APP = os.path.join(REPO, 'packaging', 'app.py')
#: 优先用安装版（自带 headless，2026-10-25 起）；没有就退回源码版 app.py
EXE = r'E:\AnonSight\AnonSight.exe'
LOG = os.path.join(REPO, 'packaging', '_headless.log')
PORTS = [8777] + list(range(8800, 8820))

mcp = FastMCP('anonsight')

_PROC = None


# ==================================================================== 发现 / 启动
def _probe(port, timeout=0.6):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/api/version' % int(port),
                                    timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception:
        return None


def _local_ids():
    try:
        d = os.path.join(DATA_DIR, 'papers')
        return {x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x))}
    except Exception:
        return set()


def _matches(base, v):
    """这个服务读的到底是不是 DATA_DIR 那个库？

    新版本 /api/version 会回 dataDir，直接比。老版本没有这个字段 → 拿论文库 ID
    交叉验证（服务器报的 ID 至少 80% 在本地 papers/ 里有目录）。这样 MCP 不会
    因为 8777 上恰好跑着**另一个数据目录**的 AnonSight 就把论文导错地方。
    """
    dd = (v or {}).get('dataDir')
    if dd:
        return os.path.abspath(dd) == DATA_DIR
    try:
        m = _request(base + '/api/papers', timeout=3)
        ids = {p.get('id') for p in (m.get('papers') or [])}
        local = _local_ids()
        if not ids or not local:
            return False
        return len(ids & local) >= max(1, int(len(ids) * 0.8))
    except Exception:
        return False


def _base_from_info(info):
    if not info:
        return None
    try:
        port = int(info.get('port') or 0)
        if port <= 0:
            return None
        base = 'http://127.0.0.1:%d' % port
        v = _probe(port)
        if not v or not _matches(base, v):
            return None
        return base
    except Exception:
        return None


def _discover():
    # ① 端口文件（headless / UI 共用）
    try:
        info = json.load(open(os.path.join(DATA_DIR, 'paperide.server.json'),
                              encoding='utf-8'))
        base = _base_from_info(info)
        if base:
            return base
    except Exception:
        pass
    # ② 端口探测（兼容没有端口文件的旧版本）
    for p in PORTS:
        v = _probe(p, timeout=0.15)
        if not v:
            continue
        base = 'http://127.0.0.1:%d' % p
        if _matches(base, v):
            return base
    return None


def _start_headless():
    global _PROC
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    env = os.environ.copy()
    env['PAPERIDE_DATA'] = DATA_DIR
    env['PYTHONIOENCODING'] = 'utf-8'
    log_start = os.path.getsize(LOG) if os.path.exists(LOG) else 0
    logf = open(LOG, 'a', encoding='utf-8')
    logf.write('\n=== MCP headless start %s ===\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
    logf.flush()
    if os.path.exists(EXE):
        cmd = [EXE, '--headless', '--port', '8777']
        cwd = os.path.dirname(EXE)
    else:
        cmd = [PY, APP, '--headless', '--port', '8777']
        cwd = REPO
    _PROC = subprocess.Popen(
        cmd,
        cwd=cwd, env=env, stdout=logf, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    info_path = os.path.join(DATA_DIR, 'paperide.server.json')
    t0 = time.time()
    while time.time() - t0 < 45:
        if _PROC.poll() is not None:
            raise RuntimeError('headless 启动失败（退出码 %s），日志：%s'
                               % (_PROC.returncode, LOG))
        # ① 只认**本进程**写的端口文件（别被 8777 上另一个实例骗了）
        try:
            info = json.load(open(info_path, encoding='utf-8'))
            if int(info.get('pid') or -1) == _PROC.pid:
                base = _base_from_info(info)
                if base:
                    return base
        except Exception:
            pass
        # ② 兜底：从本次启动的日志里读 [headless] URL
        try:
            with open(LOG, encoding='utf-8') as f:
                f.seek(log_start)
                txt = f.read()
            m = re.search(r'\[headless\] (http://127\.0\.0\.1:\d+)/', txt)
            if m:
                base = m.group(1)
                v = _probe(int(base.rsplit(':', 1)[1]))
                if v and _matches(base, v):
                    return base
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError('headless 等了 45 秒没起来，日志：%s' % LOG)


def _ensure():
    base = _discover()
    if base:
        return base
    return _start_headless()


# ==================================================================== HTTP
def _request(url, method='GET', obj=None, timeout=120):
    data = None
    headers = {}
    if obj is not None:
        data = json.dumps(obj).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'replace')
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode('utf-8', 'replace')
        except Exception:
            body = ''
        raise RuntimeError('HTTP %s %s：%s' % (e.code, url, body[:400]))
    except Exception as e:
        raise RuntimeError('连不上 AnonSight：%s（%s）' % (e, url))


def _wait_job(base, jid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = _request(base + '/api/jobs/' + jid, timeout=30)
        if j.get('state') in ('done', 'failed'):
            return j
        time.sleep(1.5)
    return _request(base + '/api/jobs/' + jid, timeout=30)


def _sections_summary(base, pid):
    try:
        d = _request(base + '/api/papers/%s/sections' % pid, timeout=60)
        secs = d.get('sections') or []
        return {
            'sections': len(secs),
            'structureSource': d.get('structureSource'),
            'warnings': d.get('warnings') or [],
            'titles': [{'title': s.get('title'), 'level': s.get('level'),
                        'parent': s.get('parent'), 'pdfPages': s.get('pdfPages'),
                        'chars': s.get('chars')} for s in secs[:20]],
        }
    except Exception as e:
        return {'error': '读 sections 失败：%s' % e}


# ==================================================================== 工具
@mcp.tool()
def anonsight_status() -> dict:
    """查看 AnonSight 后台服务是否在跑（不会自动启动）。返回 base / 版本 / 数据目录。"""
    base = _discover()
    if not base:
        return {'running': False, 'dataDir': DATA_DIR,
                'hint': '调用 ensure_server 可启动 headless 服务'}
    v = _probe(int(base.rsplit(':', 1)[1])) or {}
    return {'running': True, 'base': base, 'dataDir': v.get('dataDir') or DATA_DIR,
            'version': v}


@mcp.tool()
def ensure_server() -> dict:
    """确保有一个 AnonSight 服务在跑：优先复用现有 UI/headless，否则后台启动 headless（不开窗）。"""
    try:
        base = _ensure()
        return {'ok': True, 'base': base, 'dataDir': DATA_DIR}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@mcp.tool()
def list_papers(analyzed_only: bool = False) -> dict:
    """列出论文库（id / 标题 / 作者 / 节数 / 是否已分析）。analyzed_only=True 只列已出图的。"""
    try:
        base = _ensure()
        d = _request(base + '/api/library', timeout=60)
        items = d.get('papers') or []
        if analyzed_only:
            items = [x for x in items if x.get('analyzed')]
        return {'ok': True, 'count': len(items), 'papers': items}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@mcp.tool()
def import_pdf(path: str, wait: bool = True, analyze: bool = False) -> dict:
    """导入本机一个 PDF/.ano（走 AnonSight 的入库 + AI 分节）。

    wait=True 等入库完成并返回分节摘要；wait=False 只返回 job id。
    analyze=True 时入库成功后紧接着启动一键分析（不等待，除非 wait=True 且能接受长耗时）。
    """
    try:
        if not os.path.isfile(path):
            return {'ok': False, 'error': '文件不存在：%s' % path}
        base = _ensure()
        r = _request(base + '/api/import-local', 'POST', {'path': os.path.abspath(path)},
                     timeout=120)
        out = {'ok': True, 'base': base, 'import': r, 'id': r.get('id'),
               'opened': bool(r.get('opened'))}
        jid = r.get('job')
        if jid and wait:
            j = _wait_job(base, jid, timeout=900)
            out['job'] = {'id': jid, 'state': j.get('state'), 'error': j.get('error'),
                          'steps': [s.get('detail') for s in (j.get('steps') or [])]}
            out['sections'] = _sections_summary(base, r.get('id'))
        if analyze and r.get('id'):
            a = _request(base + '/api/papers/%s/analyze' % r.get('id'), 'POST', {},
                         timeout=120)
            out['analyzeJob'] = a.get('job')
            if wait and a.get('job'):
                j = _wait_job(base, a['job'], timeout=5400)
                out['analyze'] = {'state': j.get('state'), 'error': j.get('error'),
                                  'summary': j.get('summary')}
        return out
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@mcp.tool()
def import_folder(folder: str, analyze: bool = False) -> dict:
    """把文件夹里的 PDF 全部导入（串行、幂等）。返回每个文件的结果；analyze=True 逐篇启动分析（不等待）。"""
    try:
        if not os.path.isdir(folder):
            return {'ok': False, 'error': '目录不存在：%s' % folder}
        paths = []
        for root, _dirs, files in os.walk(folder):
            for f in files:
                if f.lower().endswith('.pdf'):
                    paths.append(os.path.join(root, f))
        paths.sort()
        base = _ensure()
        results = []
        for p in paths:
            try:
                r = _request(base + '/api/import-local', 'POST', {'path': p}, timeout=60)
                rec = {'pdf': p, 'id': r.get('id'), 'opened': bool(r.get('opened')),
                       'job': r.get('job')}
                if r.get('job'):
                    j = _wait_job(base, r['job'], timeout=900)
                    rec['state'] = j.get('state')
                    rec['error'] = j.get('error')
                    rec.update(_sections_summary(base, r.get('id')))
                if analyze and r.get('id') and not r.get('opened'):
                    a = _request(base + '/api/papers/%s/analyze' % r.get('id'),
                                 'POST', {}, timeout=60)
                    rec['analyzeJob'] = a.get('job')
                results.append(rec)
            except Exception as e:
                results.append({'pdf': p, 'error': str(e)})
        return {'ok': True, 'count': len(results), 'results': results}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@mcp.tool()
def analyze_paper(paper_id: str, wait: bool = False, timeout_seconds: int = 5400) -> dict:
    """启动一篇的一键分析（逐节思想图 + 标注 + 翻译预热 + 打包 .ano）。

    默认 wait=False 立刻返回 job id；需要等结果就传 wait=True（可能几十分钟）。
    """
    try:
        base = _ensure()
        r = _request(base + '/api/papers/%s/analyze' % paper_id, 'POST', {}, timeout=120)
        out = {'ok': True, 'paperId': paper_id, 'job': r.get('job'),
               'reused': bool(r.get('reused'))}
        if wait and r.get('job'):
            j = _wait_job(base, r['job'], timeout=timeout_seconds)
            out.update({'state': j.get('state'), 'error': j.get('error'),
                        'summary': j.get('summary')})
        return out
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@mcp.tool()
def resection_paper(paper_id: str, wait: bool = True) -> dict:
    """用当前分节器（LLM 语义分节）重新切一篇论文。"""
    try:
        base = _ensure()
        r = _request(base + '/api/papers/%s/resection' % paper_id, 'POST', {}, timeout=60)
        out = {'ok': True, 'paperId': paper_id, 'job': r.get('job')}
        if wait and r.get('job'):
            j = _wait_job(base, r['job'], timeout=600)
            out.update({'state': j.get('state'), 'error': j.get('error')})
            out['sections'] = _sections_summary(base, paper_id)
        return out
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@mcp.tool()
def get_sections(paper_id: str, full: bool = False) -> dict:
    """读一篇的 sections.json。默认给摘要（标题/层级/页码/字数）；full=True 返回含 sourceText 的全文。"""
    try:
        base = _ensure()
        d = _request(base + '/api/papers/%s/sections' % paper_id, timeout=60)
        if full:
            return {'ok': True, 'data': d}
        secs = d.get('sections') or []
        return {'ok': True, 'paperId': paper_id,
                'structureSource': d.get('structureSource'),
                'warnings': d.get('warnings') or [],
                'count': len(secs),
                'sections': [{'id': s.get('id'), 'title': s.get('title'),
                              'level': s.get('level'), 'parent': s.get('parent'),
                              'pdfPages': s.get('pdfPages'), 'chars': s.get('chars')}
                             for s in secs]}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@mcp.tool()
def get_graph(paper_id: str, full: bool = False) -> dict:
    """读一篇的 graph.json（右栏思想图数据）。默认返回节/图/视图计数与标题；full=True 返回原文件。"""
    try:
        base = _ensure()
        d = _request(base + '/api/papers/%s/graph' % paper_id, timeout=120)
        if full:
            return {'ok': True, 'data': d}
        secs = d.get('sections') or []
        return {'ok': True, 'paperId': paper_id, 'title': (d.get('paper') or {}).get('title'),
                'sections': len(secs),
                'views': sum(len(s.get('views') or []) for s in secs),
                'items': [{'id': s.get('id'), 'title': s.get('title'),
                           'views': len(s.get('views') or []),
                           'analysisFailed': s.get('analysisFailed')}
                          for s in secs]}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@mcp.tool()
def get_job(job_id: str) -> dict:
    """查一个后台 job 的状态（导入 / 分析 / 重分节）。"""
    try:
        base = _ensure()
        return {'ok': True, 'job': _request(base + '/api/jobs/' + job_id, timeout=30)}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@mcp.tool()
def stop_headless() -> dict:
    """停止由 MCP 启动的 headless 服务（不会动你正在用的 UI 窗口）。"""
    try:
        info = json.load(open(os.path.join(DATA_DIR, 'paperide.server.json'),
                              encoding='utf-8'))
        if not info.get('headless'):
            return {'ok': False, 'error': '当前服务不是 headless（可能是 UI），不关'}
        pid = int(info.get('pid') or 0)
        subprocess.run(['taskkill', '/PID', str(pid), '/F'],
                       capture_output=True, text=True)
        return {'ok': True, 'stopped': pid}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


if __name__ == '__main__':
    # stdio：Claude Code 通过标准输入/输出说话；日志一律走 stderr
    sys.stderr.write('[anonsight-mcp] dataDir=%s repo=%s\n' % (DATA_DIR, REPO))
    mcp.run(transport='stdio')
