# -*- coding: utf-8 -*-
"""
Ano 格式 · 固化（把工作区载荷写回 `.ano`）

**这是"会话级固化"的落地**（方案 §5.3 流程三）：日常编辑只写工作区 JSON（毫秒级），
用户在会话边界（关论文 / 切论文 / 退出）才触发一次固化。
一次固化约 0.14–0.76 秒（5 篇实测，与 PDF 体积**不成正比** —— 瓶颈是图片要不要重压缩），
所以**调用方应做成异步**，别阻塞界面。

固化 = 就地在原 `.ano` 上"清附件 → 重加 → 重建注释 → 原子替换"，**不重新生成 PDF 页面**：
  · PDF 底座（页面内容）一个字节都不动；
  · **外部阅读器打的注释原样保留**（判据：`/T` 不以 `AnonSight` 开头）；
  · 我们的注释先删后建（**必须删** —— 忘了删会静默累积，实测 5 轮从 217 涨到 1303 条）。

用法：
    from core import ano_solidify
    rep = ano_solidify.solidify('papers/wu2020/paper.ano', 'papers/wu2020')
    if not rep['ok']:
        ...  # 原文件没被动过，可以重试
"""
import fitz
import os
import json
import traceback

try:                              # 放进 core/ 时是包内相对导入；单独测试时按同目录模块导入
    from . import ano_pack as P
except ImportError:
    import ano_pack as P

MAX_ROUNDS = 1                     # 保留：将来若要做"重试 N 次"从这里改


def _baseline(doc):
    """打开时的基线。

    ⚠ 这里的 `resolve_names()` **恰好是可以信的** —— 它读的是"加载那一刻的快照"，
    而加载那一刻正是**文件当前的真实状态**。不能信的是"改完立刻读"（那时快照是旧的）。
    所以基线这么取是对的，别把这条经验用反了。见方案 §5.5。
    """
    return {'pages': doc.page_count,
            'dests': len(doc.resolve_names()),
            'external': sum(1 for pg in doc for a in pg.annots()
                            if not (a.info.get('title') or '').startswith(P.AUTHOR))}


def solidify(ano_path, payload_dir, *, marks=None, keep_backup=False):
    """把 `payload_dir` 的 5 份 JSON（+ `marks`）固化进 `ano_path`

    返回报告 dict（**不抛异常**）：`{'ok', 'errors', 'pages', 'dests_before/after', ...}`。
    **失败时原文件保证没被动过**（写临时文件 → 自检 → 原子替换）。
    """
    if not os.path.exists(ano_path):
        return P._fail('找不到 .ano：%s' % ano_path)
    try:
        payload = P._read_payload(payload_dir)
    except Exception as e:
        return P._fail('读载荷失败：%s' % e)
    if marks is None:
        marks = P._marks_from_payload(payload)        # 用户高亮就在 notebook.json 里

    doc = None
    tmp = ano_path + '.tmp'
    try:
        doc = fitz.open(ano_path)
        base = _baseline(doc)

        # ① 清附件名字树（不能用 embfile_del；不能清整个 Names —— 见 ano_pack.clear_embedded_files）
        how = P.clear_embedded_files(doc)

        # ② 重建注释：先删我们的，再写当前的
        removed = P.strip_our_highlights(doc)

        ai_items = []
        if 'Ano.40-annotations.json' in payload:
            try:
                ai_items = json.loads(payload['Ano.40-annotations.json'].decode('utf-8')).get('items', [])
            except Exception:
                ai_items = []
        n_ai, skip_ai = P.write_highlights(doc, ai_items, None)
        n_mk, skip_mk = P.write_highlights(doc, None, marks)

        # ③ 更新 manifest（重算载荷哈希；底座没动，pages/sha 沿用旧值）
        old_man = {}
        try:
            if 'Ano.00-manifest.json' in doc.embfile_names():
                old_man = json.loads(doc.embfile_get('Ano.00-manifest.json').decode('utf-8'))
        except Exception:
            old_man = {}
        others = {k: v for k, v in payload.items() if k != 'Ano.00-manifest.json'}
        new_man = P._build_manifest(
            old_man.get('pid') or _pid_of(ano_path), others, None, doc.page_count,
            highlights=n_ai + n_mk, pdf_only=base['external'],
            createdAt=old_man.get('createdAt'),
            pdf_sha=(old_man.get('pdf') or {}).get('sha256'))
        payload['Ano.00-manifest.json'] = json.dumps(new_man, ensure_ascii=False,
                                                     indent=1).encode('utf-8')

        # ④ 写附件
        P._add_payload(doc, payload)
        doc.xref_set_key(doc.pdf_catalog(), 'Ano',
                         '<</Version %d/App(%s)/Pid(%s)>>'
                         % (P.VERSION, P.APP, _pid_of(ano_path)))

        # ⑤ 存临时文件 → 自检 → 原子替换
        doc.save(tmp, deflate=True, garbage=4, clean=True)
        doc.close()
        doc = None

        rep = P.verify(tmp, expect={'dests': base['dests'], 'pages': base['pages']})
        if not rep['ok']:
            os.remove(tmp)
            return P._fail(*rep['errors'], stage='自检未过，原文件未改动')

        if keep_backup and not os.path.exists(ano_path + '.bak'):
            os.replace(ano_path, ano_path + '.bak')     # 仅首次留一份
            os.replace(tmp, ano_path)
        else:
            os.replace(tmp, ano_path)

        rep.update(path=ano_path, clear_mode=how, removed=removed,
                   wrote_ai=n_ai, wrote_marks=n_mk,
                   skipped_ai=skip_ai, skipped_marks=skip_mk,
                   dests_before=base['dests'], external_kept=rep.get('external', 0))
        return rep
    except Exception as e:
        traceback.print_exc()
        return P._fail('固化失败：%s: %s' % (type(e).__name__, e))
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def _pid_of(ano_path):
    try:
        d = fitz.open(ano_path)
        names = d.embfile_names()
        if 'Ano.00-manifest.json' in names:
            pid = json.loads(d.embfile_get('Ano.00-manifest.json').decode('utf-8')).get('pid')
            d.close()
            return pid
        d.close()
    except Exception:
        pass
    return os.path.basename(os.path.abspath(ano_path)).split('.')[0]


def needs_solidify(ano_path, payload_dir):
    """脏判定（方案 §5.4）：**比哈希，不比时间戳**

    工作区的 JSON 与 `.ano` 里 manifest 记的哈希一比，就知道"这边是不是比容器新"。
    时间戳会被复制/解压/同步搞乱，哈希是内容本身，不骗人。
    """
    try:
        d = fitz.open(ano_path)
        names = d.embfile_names()
        if 'Ano.00-manifest.json' not in names:
            d.close()
            return True, '容器里没有 manifest'
        man = json.loads(d.embfile_get('Ano.00-manifest.json').decode('utf-8'))
        for name, fname in P.PAYLOAD_FILES:
            if fname is None:
                continue
            meta = (man.get('payload') or {}).get(name)
            p = os.path.join(payload_dir, fname)
            if not os.path.exists(p):
                continue
            with open(p, 'rb') as f:
                cur = P._sha(f.read())
            if not meta or meta.get('sha256') != cur:
                d.close()
                return True, '%s 与容器不一致（工作区更新）' % fname
        d.close()
        return False, '一致'
    except Exception as e:
        return True, '判不了（%s），保守起见按需要固化处理' % e
