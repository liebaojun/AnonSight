# -*- coding: utf-8 -*-
"""
Ano 格式 · 打包 / 解包 / 识别（`.ano` 容器 = 合法 PDF：高亮走标准注释，其余数据走 PDF 附件）

`.ano` = 一个标准 PDF + 6 份 JSON 载荷（藏在 `/EmbeddedFiles`）+ 一批标准 `/Highlight` 注释。
**本模块只做"PDF 路径 ↔ 载荷目录"的双向搬运**，不碰平台、不碰前端。

三条铁律（都是踩出来的，改动前先读 `调研/Ano格式-设计方案.md` §5.5）：
  ① **附件层只用 `embfile_add` 和"清名字树"** —— `embfile_del` 会串位、`embfile_upd` 会崩；
  ② **清名字树有两种形态**（xref / 内联 dict），只处理一种会在真实 PDF 上翻车；
  ③ **校验必须"存盘重开后"做** —— 内存态读的是加载时的快照，会给出假象。

所有对外函数**不抛异常**，失败写进返回的 `{'ok': False, 'errors': [...]}` ——
方案 P6 要求"失败必须明确"，调用方要能拿到原因给用户看。
"""
import fitz
import os
import re
import json
import hashlib
import tempfile

# ---------------------------------------------------------------- 常量

#: 载荷文件名（顺序即写入顺序，数字前缀让"插入序 == 字典序"，绕开规范 §7.9.6 的排序要求）
PAYLOAD_FILES = [
    ('Ano.00-manifest.json', None),            # None = 由 pack 现生成
    ('Ano.10-meta.json', 'meta.json'),
    ('Ano.20-sections.json', 'sections.json'),
    ('Ano.30-graph.json', 'graph.json'),
    ('Ano.40-annotations.json', 'annotations.json'),
    ('Ano.50-notebook.json', 'notebook.json'),
]
REQUIRED = [n for n, _ in PAYLOAD_FILES]       # 全部必需
OPTIONAL_PREFIX = 'Ano.9'                      # 90+ 留给自由扩展，加载路径不认

FORMAT = 'Ano'
VERSION = 1
APP = 'AnonSight'

#: 类型 → 高亮颜色（与前端 `PRE_COLOR` 同义，见方案 §4.4）
COLORS = {
    '术语': (1.0, 0.588, 0.235),
    '搭配': (1.0, 0.804, 0.275),
    '动词': (0.275, 0.745, 0.490),
    '逻辑': (0.290, 0.498, 0.878),
    '句式': (0.627, 0.431, 0.863),
}
DEFAULT_COLOR = (1.0, 0.804, 0.275)

#: 我们写的注释用这个作者名打标（判据 = startswith，将来细分 `:user` 不用改判据）
AUTHOR = APP
AUTHOR_USER = APP + ':user'

# ---- 安全上限（方案 §7；解压炸弹只能靠"边解边数"，不能信 /Params /Size）----
MAX_EMB_PER_FILE = 64 * 1024 * 1024        # 单个载荷解压后 64 MB
MAX_EMB_TOTAL = 256 * 1024 * 1024          # 全部载荷合计 256 MB
MAX_EMB_COUNT = 64                         # 附件条目数上限
MAX_UPLOAD = 200 * 1024 * 1024             # 上传上限（与 server.py 现有口径一致）


# ---------------------------------------------------------------- 小工具

def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _ok(**kw):
    return dict(ok=True, errors=[], **kw)


def _fail(*errs, **kw):
    return dict(ok=False, errors=[e for e in errs if e], **kw)


def _read_payload(dirpath):
    """读载荷目录 → {附件名: bytes}，顺带做大小上限检查"""
    out, total = {}, 0
    for name, fname in PAYLOAD_FILES:
        if fname is None:
            continue
        p = os.path.join(dirpath, fname)
        if not os.path.exists(p):
            continue
        with open(p, 'rb') as f:
            data = f.read()
        if len(data) > MAX_EMB_PER_FILE:
            raise ValueError('%s 超过单文件上限 %d MB' % (fname, MAX_EMB_PER_FILE >> 20))
        total += len(data)
        if total > MAX_EMB_TOTAL:
            raise ValueError('载荷合计超过上限 %d MB' % (MAX_EMB_TOTAL >> 20))
        out[name] = data
    return out


def _marks_from_payload(payload):
    """从 `notebook.json` 载荷里取出用户高亮（`marks`）。

    为什么要这一步：用户高亮就存在 `50-notebook.json` 的 `marks` 字段里，
    打包/固化时若不显式传 `marks=`，它们**不会进兼容层** —— 别的阅读器里就看不到
    用户自己标的高亮。让它自己读，调用方就不必记得传。
    （只带得上**有 `rects` 的**那些；存量老标记没有坐标，见方案 §3.3 的 E11 结论，
      宁可跳过也不做 20% 命中率的文本反查。）
    """
    try:
        nb = json.loads(payload['Ano.50-notebook.json'].decode('utf-8'))
        m = [x for x in (nb.get('marks') or []) if x.get('rects')]
        return m or None
    except Exception:
        return None


def _build_manifest(pid, payload, pdf_bytes, pages, highlights=0, pdf_only=0, **kw):
    """构造 manifest。`pdf_bytes=None` 时不写 sha（固化场景：底座没动，沿用旧值）"""
    now = _now()
    m = {
        'format': FORMAT, 'version': VERSION, 'app': APP, 'appVersion': '0.1.0',
        'pid': pid, 'createdAt': kw.get('createdAt') or now, 'modifiedAt': now,
        'payload': {n: {'bytes': len(d), 'sha256': _sha(d)} for n, d in payload.items()},
        'pdf': {'pages': pages, 'sha256': _sha(pdf_bytes) if pdf_bytes is not None
                else kw.get('pdf_sha')},
        'compat': {'highlights': highlights, 'pdfOnly': pdf_only},
    }
    return m


def _now():
    import datetime
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- ① 清名字树（两种形态）

def _strip_dict_key(s, key):
    """从 PDF 对象字符串里删掉 `/key` 及其值（括号/尖括号配平扫描）

    只在内联 `/Names` 形态下用到（见 `clear_embedded_files`）。
    """
    tag = '/' + key
    i = s.find(tag)
    while i >= 0:
        j = i + len(tag)
        if j >= len(s) or not (s[j].isalnum() or s[j] in '-_'):
            break                              # 是完整键名，不是 `/EmbeddedFilesX` 这种前缀
        i = s.find(tag, i + 1)
    if i < 0:
        return s
    j = i + len(tag)
    while j < len(s) and s[j].isspace():
        j += 1
    if j >= len(s):
        return s[:i]
    if s[j] in '<[':
        oc, cc = s[j], ('>' if s[j] == '<' else ']')
        depth, k = 0, j
        while k < len(s):
            if s[k] == oc:
                depth += 1
            elif s[k] == cc:
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
        return s[:i] + s[k:]
    k = j
    while k < len(s) and s[k] not in '/>]' and not s[k].isspace():
        k += 1
    return s[:i] + s[k:]


def clear_embedded_files(doc):
    """清空 `/EmbeddedFiles` 名字树，**保住 `/Dests` 等其它子键**

    ⚠ 三件事必须同时做到，缺一出事：

    1. **不能用 `embfile_del`** —— 实测两种失败形态（真删后内容串位 / 假删后 add 被拒），
       且 `embfile_names()` 与 `embfile_get()` 会互相矛盾，写不出正确的"删一条再改一条"。
    2. **`/Names` 有两种物理形态，都要处理**：
       · `xref` 形态（Elsevier / Nature / Cell 系）→ 直接把它下面的 `EmbeddedFiles` 设 null；
       · **内联 dict 形态**（eLife 等）→ 没有独立 xref，"取对象再设子键"根本拿不到手柄，
         会**静默跳过**，然后 `embfile_add` 撞上已存在名字抛 `ValueError`。
    3. **绝不能偷懒清整个 `/Names`** —— 那会把 `/Dests`（PDF 全部命名目的地）一起清掉，
       **平台「点 [12] 引用跳转」的功能就废了**。而它极难发现：清空成功、重加也成功，
       只测"附件在不在"完全正常；连 `resolve_names()` 都要**存盘重开后**才读得到真实值
       （清完立刻读是加载时的快照）。见方案 §5.5。

    返回做了什么（便于调用方记日志）。
    """
    cat = doc.pdf_catalog()
    nk = doc.xref_get_key(cat, 'Names')
    if nk[0] == 'xref':
        nx = int(nk[1].split()[0])
        if doc.xref_get_key(nx, 'EmbeddedFiles')[0] == 'null':
            return 'already-empty'
        doc.xref_set_key(nx, 'EmbeddedFiles', 'null')
        return 'cleared/xref'
    if nk[0] == 'dict':
        if '/EmbeddedFiles' not in nk[1]:
            return 'already-empty'
        cleaned = _strip_dict_key(nk[1], 'EmbeddedFiles')
        doc.xref_set_key(cat, 'Names', cleaned if cleaned.strip('<> ') else 'null')
        return 'cleared/inline'
    return 'no-names-tree'


# ---------------------------------------------------------------- ② 写附件 + 注释

def _add_payload(doc, payload):
    """把 6 份载荷加进名字树，并补 `/Subtype`（PyMuPDF 不写）"""
    streams = []
    for name, data in payload.items():
        doc.embfile_add(name, data, filename=name)
        streams.append(name)
    # 补 MIME：PyMuPDF 写的流字典里没有 /Subtype，PDF 2.0 对 associated file 是 required
    for name in streams:
        try:
            xr = _find_embfile_stream(doc, name)
            if xr:
                doc.xref_set_key(xr, 'Subtype', '/application#2Fjson')   # `/` 转义成 #2F
        except Exception:
            pass                                   # 补不上不影响读取，不致命


def _find_embfile_stream(doc, name):
    """顺着 catalog → Names → EmbeddedFiles → Filespec → /EF → /F 找到文件流的 xref"""
    cat = doc.pdf_catalog()
    nk = doc.xref_get_key(cat, 'Names')
    if nk[0] == 'xref':
        ef = doc.xref_get_key(int(nk[1].split()[0]), 'EmbeddedFiles')
    else:
        return None
    if ef[0] != 'xref':
        return None
    efx = int(ef[1].split()[0])
    names = doc.xref_get_key(efx, 'Names')
    if names[0] != 'array':
        return None
    arr = _parse_array(names[1])
    for i in range(0, len(arr) - 1, 2):
        if arr[i] == name and arr[i + 1].endswith('R'):
            fs = int(arr[i + 1].split()[0])
            e = doc.xref_get_key(fs, 'EF')
            if e[0] == 'xref':
                f = doc.xref_get_key(int(e[1].split()[0]), 'F')
                if f[0] == 'xref':
                    return int(f[1].split()[0])
    return None


def _parse_array(s):
    """把 PDF 数组字符串拆成 token 列表（够用即可：不处理嵌套字符串）"""
    return [t for t in re.findall(r'\((?:\\.|[^\\()])*\)|<[^>]*>|\[[^\]]*\]|[^\s\[\]]+', s)]


def _annots_content(a):
    """`/Contents` 的三段式（方案 §4.2）

    第一行放**英文**是刻意的：Chrome/Edge 用 CP1252 画弹窗，CJK 会被**逐字跳过**，
    英文那行能让 Chrome 用户至少看出"这是哪个词的标注"。Acrobat 只显示两行，
    所以前两行必须自成完整信息，tip 放第三行。
    """
    txt = (a.get('text') or '').strip()
    zh = (a.get('zh') or '').strip()
    tip = (a.get('type') or '').strip()
    if a.get('tip'):
        tip = (tip + ' · ' + a['tip']) if tip else a['tip']
    return '\n'.join(x for x in (txt[:120], zh[:400], tip[:300]) if x)[:900]


def write_highlights(doc, items, marks=None, *, page_of=None):
    """把标注写成标准 `/Highlight` 注释。返回 (写入条数, 跳过条数)

    items：AI 标注（`annotations.json` 的 items，自带归一化 rects）
    marks：用户高亮（`notebook.json` 的 marks；**需带 rects** —— 见方案 §3.3 的 E11 结论，
           没有坐标的存量 marks **不能**靠文本反查补，命中率只有 20%，宁可跳过）
    page_of：`marks[].page`(期刊页) → PDF 页 的可选换算函数
    """
    wrote = skipped = 0
    # ⚠ **两批数据的页号字段名不一样，必须分别取**：
    #   AI 标注（annotations.json）用 `pdfPage`；
    #   用户高亮（notebook.json 的 marks）用 `page` —— 而且它**就是 PDF 页号**
    #   （`applyMarks` 拿它和 `.pdfPage[data-page]` 直接比，不是期刊页码）。
    #   第一版统一读 pdfPage，结果**用户高亮一条都写不进兼容层**，还静默跳过不报错。
    for it, author, pagekey in ([(x, AUTHOR, 'pdfPage') for x in (items or [])] +
                                [(m, AUTHOR_USER, 'page') for m in (marks or [])]):
        rects = it.get('rects') or []
        pgno = it.get(pagekey)
        if not pgno and page_of:
            pgno = page_of(it.get('page'))
        if not pgno or not rects:
            skipped += 1
            continue
        try:
            page = doc[pgno - 1]
        except Exception:
            skipped += 1
            continue
        W, H = page.rect.width, page.rect.height          # 显示尺寸（方案 §4.3 的口径）
        quads = [fitz.Rect(r[0] * W, r[1] * H, r[2] * W, r[3] * H) for r in rects if len(r) == 4]
        quads = [q for q in quads if q.width > 0 and q.height > 0]
        if not quads:
            skipped += 1
            continue
        try:
            an = page.add_highlight_annot(quads=quads if len(quads) > 1 else quads[0])
            an.set_info(content=_annots_content(it), title=author,
                        subject=(it.get('type') or it.get('kind') or '')[:60])
            an.set_colors(stroke=COLORS.get(it.get('type'), DEFAULT_COLOR))
            an.set_opacity(0.6 if author == AUTHOR_USER else 0.4)
            # ⚠ `/NM` 不能用 set_info(id=…) 设（不接受该关键字），必须写底层对象
            if it.get('id'):
                doc.xref_set_key(an.xref, 'NM', '(%s)' % re.sub(r'[()\\]', '', str(it['id'])))
            wrote += 1
        except Exception:
            skipped += 1
    return wrote, skipped


def strip_our_highlights(doc):
    """删掉上一轮"我们写的"注释（判据 = `/T` 以 `AnonSight` 开头）

    ⚠ **必须先删后建**：忘记删会静默累积（实测 5 轮后注释从 217 涨到 1303 条）。
    外部阅读器打的注释（`/T` 是别的值或空）**不在此列，原样保留**。
    """
    n = 0
    for pg in doc:
        for a in list(pg.annots()):
            if (a.info.get('title') or '').startswith(AUTHOR):
                pg.delete_annot(a)
                n += 1
    return n


# ---------------------------------------------------------------- ③ 识别三查

def identify(path):
    """三查识别（方案 §2.5）：魔数 → 字面量 → 结构。**不押在扩展名上**

    返回 `{'is_ano', 'version', 'pid', 'pages', 'embfiles', 'reason'}`；失败时 `is_ano=False`
    并给 `reason`。粗筛不过就不解析 PDF（省时间，也避免被畸形文件拖住）。
    """
    r = {'is_ano': False, 'version': None, 'pid': None, 'pages': 0,
         'embfiles': 0, 'reason': ''}
    try:
        with open(path, 'rb') as f:
            head = f.read(1024)
    except Exception as e:
        r['reason'] = '读不了文件：%s' % e
        return r
    if b'%PDF-' not in head:
        r['reason'] = '不是 PDF（文件头没有 %PDF-）'
        return r
    try:
        with open(path, 'rb') as f:
            blob = f.read()
    except Exception as e:
        r['reason'] = '读不了文件：%s' % e
        return r
    if b'Ano.00-manifest.json' not in blob:      # 二查：快速粗筛
        r['reason'] = '没有 Ano 载荷标记（是个普通 PDF）'
        return r
    try:                                          # 三查：结构
        doc = fitz.open(path)
        cat = doc.pdf_catalog()
        has_key = doc.xref_get_key(cat, 'Ano')[0] != 'null'
        names = []
        try:
            names = doc.embfile_names()
        except Exception:
            pass
        has_tree = any(n.startswith('Ano.') for n in names)
        r['pages'] = doc.page_count
        r['embfiles'] = len(names)
        if not (has_key or has_tree):
            r['reason'] = '结构里没有 Ano 标记'
            doc.close()
            return r
        if doc.page_count <= 0:                   # ⚠ 截断文件不报错、静默给 0 页（方案 §7.3）
            r['reason'] = 'PDF 结构损坏（页数为 0）'
            doc.close()
            return r
        if 'Ano.00-manifest.json' in names:
            m = json.loads(doc.embfile_get('Ano.00-manifest.json').decode('utf-8'))
            r['version'] = m.get('version')
            r['pid'] = m.get('pid')
            if m.get('format') != FORMAT:
                r['reason'] = 'manifest.format 不是 %s' % FORMAT
                doc.close()
                return r
        else:
            r['reason'] = '缺少 manifest 载荷'
            doc.close()
            return r
        doc.close()
    except Exception as e:
        r['reason'] = '解析失败：%s: %s' % (type(e).__name__, e)
        return r
    r['is_ano'] = True
    return r


# ---------------------------------------------------------------- ④ 自检（必须存盘重开后做）

def verify(ano_path, expect=None):
    """七项自检（方案 §5.3 流程一）。`expect` 可传原 manifest 用于比对

    ⚠ **一定要在"保存之后、重新打开"的文件上跑** —— `resolve_names()` 之类读的是
    加载时的快照，在内存态上验证等于没验（方案 §5.5 / 实验 E17）。
    """
    errs = []
    try:
        doc = fitz.open(ano_path)
    except Exception as e:
        return _fail('打不开：%s: %s' % (type(e).__name__, e))

    info = {'pages': doc.page_count, 'embfiles': doc.embfile_count(),
            'embnames': [], 'ours': 0, 'external': 0, 'dests': None}
    # ① 页数
    if doc.page_count <= 0:
        errs.append('页数为 0（文件可能被截断）')
    # ② 附件齐不齐 + ③ 逐份哈希
    names = []
    try:
        names = doc.embfile_names()
    except Exception as e:
        errs.append('读附件列表失败：%s' % e)
    info['embnames'] = names
    if len(names) > MAX_EMB_COUNT:
        errs.append('附件条目过多（%d > %d）' % (len(names), MAX_EMB_COUNT))
    total = 0
    hashes = {}
    for n in REQUIRED:
        if n not in names:
            errs.append('缺载荷：%s' % n)
            continue
        try:
            data = doc.embfile_get(n)             # 边取边计数（不信 /Params /Size）
            total += len(data)
            if len(data) > MAX_EMB_PER_FILE or total > MAX_EMB_TOTAL:
                errs.append('载荷超过安全上限')
                break
            hashes[n] = _sha(data)
        except Exception as e:
            errs.append('读不出 %s：%s' % (n, e))
    info['payloadSha'] = hashes
    # ④ manifest 自洽
    man = None
    if 'Ano.00-manifest.json' in names:
        try:
            man = json.loads(doc.embfile_get('Ano.00-manifest.json').decode('utf-8'))
            if man.get('format') != FORMAT:
                errs.append('manifest.format 不对：%r' % man.get('format'))
            if not isinstance(man.get('version'), int):
                errs.append('manifest.version 不是整数')
            if man.get('pdf', {}).get('pages') not in (None, doc.page_count):
                errs.append('manifest 记的页数(%s) 与实际(%d) 不符'
                            % (man['pdf']['pages'], doc.page_count))
            for n, meta in (man.get('payload') or {}).items():
                if n in hashes and meta.get('sha256') and meta['sha256'] != hashes[n]:
                    errs.append('载荷哈希不符：%s' % n)
        except Exception as e:
            errs.append('manifest 解析失败：%s' % e)
    info['manifest'] = man
    # ⑤ 注释统计（**只报数，不判定** —— 注释条数会因用户增删而变）
    for pg in doc:
        for a in pg.annots():
            if (a.info.get('title') or '').startswith(AUTHOR):
                info['ours'] += 1
            else:
                info['external'] += 1
    # ⑥ /Dests 条数（内部链接目标）—— 这条必须留，见 clear_embedded_files 的第 3 条
    try:
        info['dests'] = len(doc.resolve_names())
    except Exception:
        info['dests'] = None
    # ⑦ 与期望值比对（有就给）
    if expect:
        if expect.get('dests') is not None and info['dests'] is not None \
                and info['dests'] != expect['dests']:
            errs.append('/Dests 条数变了：%s → %s（内部链接会废）'
                        % (expect['dests'], info['dests']))
        if expect.get('pages') and info['pages'] != expect['pages']:
            errs.append('页数变了：%s → %s' % (expect['pages'], info['pages']))
    doc.close()
    info['bytes'] = os.path.getsize(ano_path)
    return (_fail(*errs, **info) if errs else _ok(**info))


# ---------------------------------------------------------------- ⑤ 打包 / 解包

def pack(pdf_path, payload_dir, out_path, *, pid=None, marks=None, extra=None):
    """把「PDF + 载荷目录」打成 `.ano`

    - `marks`：用户高亮（带 `rects` 的那些才写得进去，见 `write_highlights`）
    - `extra`：要一并附上的自由扩展（`{'Ano.90-x.json': b'...'}`）
    - **写临时文件 → 自检 → 原子替换**（方案 §5.6：写坏了也不能毁掉原文件）
    """
    if not os.path.exists(pdf_path):
        return _fail('PDF 不存在：%s' % pdf_path)
    try:
        payload = _read_payload(payload_dir)
    except Exception as e:
        return _fail('读载荷失败：%s' % e)
    if marks is None:
        marks = _marks_from_payload(payload)          # 用户高亮就在 notebook.json 里
    pid = pid or os.path.basename(os.path.abspath(pdf_path)).split('.')[0]
    try:
        with open(pdf_path, 'rb') as f:
            pdf_bytes = f.read()
    except Exception as e:
        return _fail('读 PDF 失败：%s' % e)

    doc = None
    try:
        doc = fitz.open(pdf_path)
        dests_before = len(doc.resolve_names())
        clear_embedded_files(doc)                 # 幂等：已经是空的就直接返回
        n_ext = sum(1 for pg in doc for a in pg.annots()
                    if not (a.info.get('title') or '').startswith(AUTHOR))
        n_ai, _s1 = write_highlights(doc, (json.loads(payload['Ano.40-annotations.json'].decode('utf-8')).get('items')
                                           if 'Ano.40-annotations.json' in payload else []), None)
        n_mk, _s2 = write_highlights(doc, None, marks)
        man = _build_manifest(pid, payload, pdf_bytes, doc.page_count,
                              highlights=n_ai + n_mk, pdf_only=n_ext)
        payload['Ano.00-manifest.json'] = json.dumps(man, ensure_ascii=False,
                                                     indent=1).encode('utf-8')
        if extra:
            payload.update(extra)
        _add_payload(doc, payload)
        doc.xref_set_key(doc.pdf_catalog(), 'Ano',
                         '<</Version %d/App(%s)/Pid(%s)>>' % (VERSION, APP, pid))
        tmp = out_path + '.tmp'
        doc.save(tmp, deflate=True, garbage=4, clean=True)
        doc.close()
        doc = None
        rep = verify(tmp)                          # ⚠ 在临时文件（已存盘）上自检
        if not rep['ok']:
            os.remove(tmp)
            return _fail(*rep['errors'], stage='自检未过，已放弃')
        os.replace(tmp, out_path)                  # 原子替换
        rep.update(pid=pid, wrote_ai=n_ai, wrote_marks=n_mk,
                   dests_before=dests_before, path=out_path)
        return rep
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _fail('打包失败：%s: %s' % (type(e).__name__, e))
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


def unpack(ano_path, dest_dir, *, overwrite=False):
    """把 `.ano` 解包到目录：写 5 份 JSON（manifest 不落盘）+ 返回 manifest

    **不写 PDF**：`.ano` 自己就是 PDF，平台要 PDF 时直接传 `.ano` 的路径即可
    （见方案 §8.2 第 6 条"先找 `paper.ano` 再找 `paper.pdf`"）。
    """
    idr = identify(ano_path)
    if not idr['is_ano']:
        return _fail('不是 Ano 文件：%s' % idr.get('reason'), **idr)
    if idr.get('version') and idr['version'] > VERSION:
        return _fail('这个文件是 Ano v%d 写的，当前只支持到 v%d，请升级 %s'
                     % (idr['version'], VERSION, APP))
    os.makedirs(dest_dir, exist_ok=True)
    doc = None
    try:
        doc = fitz.open(ano_path)
        names = doc.embfile_names()
        try:
            man = json.loads(doc.embfile_get('Ano.00-manifest.json').decode('utf-8'))
        except Exception as e:
            return _fail('manifest 读不出：%s' % e)
        written, total = [], 0
        for name, fname in PAYLOAD_FILES:
            if fname is None or name not in names:
                continue
            data = doc.embfile_get(name)
            total += len(data)
            if len(data) > MAX_EMB_PER_FILE or total > MAX_EMB_TOTAL:
                return _fail('载荷超过安全上限，已中止')
            if not fname.endswith('.json'):
                continue
            try:                                   # L1：顶层能解析
                json.loads(data.decode('utf-8'))
            except Exception as e:
                return _fail('%s 不是合法 JSON：%s' % (fname, e))
            p = os.path.join(dest_dir, fname)
            if os.path.exists(p) and not overwrite:
                return _fail('%s 已存在（要覆盖请传 overwrite=True）' % fname)
            with open(p, 'wb') as f:
                f.write(data)
            written.append(fname)
        # 自检：payload 里记的哈希必须对得上
        for n, meta in (man.get('payload') or {}).items():
            if n in names and meta.get('sha256'):
                if _sha(doc.embfile_get(n)) != meta['sha256']:
                    return _fail('载荷哈希不符（文件可能损坏）：%s' % n)
        doc.close()
        return _ok(manifest=man, written=written, bytes=total, id=idr)
    except Exception as e:
        return _fail('解包失败：%s: %s' % (type(e).__name__, e))
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


# ---------------------------------------------------------------- ⑥ 平台接入点

def paper_file(paper_dir_path):
    """论文的"正文件"路径：**优先 `.ano`，没有就 `.pdf`**。

    为什么需要它（方案 §8.2 第 6 条）：`.ano` 自己就是合法 PDF（兼容层就在文件里），
    后端要 PDF 时**直接把 `.ano` 的路径交给它**即可 —— 磁盘上不必再留一份 `paper.pdf`。
    迁移期两种形态并存，所以判据是"谁在就用谁"，**现有全是 `.pdf` 的论文行为零变化**。

    ⚠ 后端真读 PDF 的地方**一共 5 处**，改的时候最容易漏（上游规划就漏了 `server.py:604`）：
        server.py:337 / :576   入库时（那时只有刚上传的 .pdf，**不用改**）
        server.py:604          重新分节
        server.py:820          `/api/papers/<id>/pdf` 出口（前端取 PDF 走这条）
        core/annotate_ai.py:261  自动标注
        core/sectioner.py:704    参数传入，**不用改**
    """
    for fn in ('paper.ano', 'paper.pdf'):
        p = os.path.join(paper_dir_path, fn)
        if os.path.exists(p):
            return p
    return os.path.join(paper_dir_path, 'paper.pdf')      # 都没有：返回 .pdf 路径，让调用方去报错
