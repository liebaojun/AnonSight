# -*- coding: utf-8 -*-
"""打完包自查：**这个 exe 能不能给别人**？

主人 2026-09-19 的硬要求：打包版必须清空一切属于他的东西
（API Key、已分析的论文、涉及他电脑的绝对路径）。

这个脚本从两个角度查：
  ① **清单**：产物里到底有哪些文件（直接走 `dist/AnonSight/` 目录树）
  ② **内容**：在**整个产物**的字节里搜敏感串（key 前缀、用户名、仓库路径）

用法：
    python packaging/check-export.py                     # 查 packaging/dist/AnonSight/
    python packaging/check-export.py 别的目录

⚠ 2026-09-19 更新：改成认**文件夹模式**（onedir）。
   以前这份脚本读的是单文件 exe 的归档目录（`CArchiveReader`），
   改成 onedir 之后它第一步就 `找不到 AnonSight.exe` 直接退出 ——
   "打完必做的第一步"就这么静默失效了。**工具本身也会过期。**
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'dist', 'AnonSight')

#: 绝不该出现在产物里的文件（名字 → 为什么）
FORBIDDEN = {
    '.keys.json': 'API Key 文件',
    'paper.ano': '已分析的论文（.ano 成品）',
    'paper.pdf': '论文原文',
    'graph.json': '论文的思想图',
    'sections.json': '论文的分节结果',
    'annotations.json': '论文的标注层',
    'notebook.json': '论文的翻译缓存',
}

#: 必须在的（缺了就跑不起来 —— 都是踩过的）
REQUIRED = [
    ('AnonSight.exe', '程序本体'),
    (os.path.join('_internal', 'server.py'), None),          # 打包后代码在 PYZ 里，这里只兜底
    (os.path.join('_internal', 'P0', 'index.html'), '阅读器页面'),
    (os.path.join('_internal', 'P0', 'library.html'), '论文库页面'),
    (os.path.join('_internal', 'P0', 'ano-file.ico'), '.ano 文件图标'),
    # ★ 质量闸门：没有它，「一键分析」在打包版里直接崩（见 app.spec 里那段注释）
    (os.path.join('_internal', 'P0', '_extract', 'pipeline.py'), '一键分析的校验器'),
]

#: 论文数据的目录名 —— 产物根下出现 `papers/` 且非空就是事故
PAPERS_DIR = 'papers'


def fail(msg):
    print('  ✗ ' + msg)
    return 1


def ok(msg):
    print('  ✓ ' + msg)
    return 0


def walk(root):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def main():
    if not os.path.isdir(TARGET):
        print('✗ 找不到产物目录 %s —— 先跑 build.cmd（或 build 里的 PyInstaller 那步）' % TARGET)
        return 1

    files = list(walk(TARGET))
    total = sum(os.path.getsize(p) for p in files)
    print('检查：%s\n      %d 个文件，%.1f MB\n' % (TARGET, len(files), total / 1048576))
    bad = 0

    # ---------- ⓪ 体量哨兵 ----------
    # ★ 2026-09-20 加的。**为什么需要**：`torch` 359.6 MB + `onnxruntime` 28.1 MB +
    #   `torchvision` 11.2 MB 在包里躺了很久没人发现 —— 因为整条链上**没有任何一环
    #   问过"这个包为什么这么大"**。本脚本原本只看"有没有个人数据"，
    #   体积是它唯一会打印、却从不判定的数字（打印了 664 MB，没人拿它当回事）。
    #   教训跟"PyInstaller 少数据文件只报 warning"是同一条：
    #   **没有阈值的数字等于没测**。所以给一条线，超了就出声。
    #   正常约 220 MB（scipy 48 + pymupdf 36 + numpy 20 + sklearn 12 + …）。
    TOTAL_LIMIT_MB = 320
    if total / 1048576 > TOTAL_LIMIT_MB:
        bad += fail('产物 %.0f MB，超过 %d MB 哨兵线 —— 多半是 PyInstaller 收了'
                    '用不上的大包。先去 app.spec 的 EXCLUDES 里对一遍'
                    '（torch 系列曾在这里躲了很久）' % (total / 1048576, TOTAL_LIMIT_MB))

    # ---------- ① 清单 ----------
    print('【一】产物里有哪些东西')
    rel = [os.path.relpath(p, TARGET) for p in files]
    for key, why in FORBIDDEN.items():
        hits = [r for r in rel if os.path.basename(r) == key]
        if hits:
            bad += fail('发现了 %s（%s）：%s' % (key, why, hits[:3]))
    pd = os.path.join(TARGET, PAPERS_DIR)
    if os.path.isdir(pd) and os.listdir(pd):
        bad += fail('产物里有非空的 %s/（%d 项）—— 那是开发者的论文库' % (PAPERS_DIR, len(os.listdir(pd))))
    for relp, why in REQUIRED:
        full = os.path.join(TARGET, relp)
        if os.path.exists(full) or (why is None):
            continue
        bad += fail('缺了 %s（%s）—— 打出来也跑不起来' % (relp, why))
    if not bad:
        ok('清单干净：没有 key、没有论文、必需的都在')

    # ---------- ② 内容 ----------
    print('\n【二】在产物字节里搜敏感串')

    targets = []
    try:                                   # 开发者的 key（从本机取，只比对前缀，不打印全文）
        sys.path.insert(0, os.path.dirname(HERE))
        from core import adapters
        # ★ 2026-09-20：key **不再只有 deepseek 一个槽**了 —— 通用适配器之后每家服务
        #   一个槽（deepseek / kimi / zhipu / …）。只盯 deepseek 的话，开发机换过别家
        #   key 再打包，漏出去的就是没被盯的那一个。所以**把 .keys.json 里所有 key
        #   全当靶子**（依然只比对前 12 字节，不打印全文）。
        try:
            _all = json.load(open(adapters.KEYS, encoding='utf-8')) or {}
        except Exception:
            _all = {}
        for _name, _v in _all.items():
            _v = (_v or '').strip()
            if len(_v) > 12:
                targets.append((_v[:12].encode(), '开发者的 %s API Key' % _name))
    except Exception:
        pass
    targets += [
        # 开发者自己的仓库路径与用户目录 —— **本机现算**，不写死
        (os.path.dirname(HERE).encode(), '开发者的仓库路径'),
        (os.path.expanduser('~').encode(), '开发者的用户目录'),
    ]
    targets = [(n, w) for n, w in targets if n]

    # 单趟扫全部：一次读完分块、对每条 needle 都数一遍。
    # （分多趟扫 664 MB 会白读几遍 I/O）
    counts = [0] * len(targets)
    CHUNK = 8 << 20
    OVERLAP = 64                            # 让跨块边界的匹配也能命中
    for p in files:
        try:
            with open(p, 'rb') as f:
                tail = b''
                while True:
                    buf = f.read(CHUNK)
                    if not buf:
                        break
                    hay = tail + buf
                    for i, (needle, _w) in enumerate(targets):
                        if needle in hay:
                            counts[i] += hay.count(needle)
                    tail = buf[-OVERLAP:]
        except OSError:
            continue

    hit_any = False
    for (needle, why), n in zip(targets, counts):
        if n:
            hit_any = True
            bad += fail('发现 %d 处「%s」：%s' % (n, why, needle.decode('utf-8', 'replace')[:40]))
    if not hit_any:
        ok('没搜到 key / 仓库路径 / 个人目录')

    print()
    if bad:
        print('✗ 共 %d 个问题 —— **这个包不能给别人**' % bad)
        return 1
    print('✓ 通过。可以给别人用。')
    print('  （最后一步建议：手动双击跑一次，确认能起来、papers 是空的、设置里显示"尚未配置"）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
