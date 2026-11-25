# -*- coding: utf-8 -*-
"""BabelDOC 双语数据 → 覆盖层块表（AnonSight 翻译集成 · P2）

    输入：`translate_tracking.json`（BabelDOC 加 `--working-dir` 就会落，见 HANDOVER §5.2）
          + 原稿 PDF
    输出：块表 JSON —— 每块给出【第几页 · 占哪个矩形 · 原文 · 中文】

**为什么要有这一步**：tracking 里每段只有文本、没有位置；而覆盖层要把中文"贴"回它
原来待的地方，就必须知道每段在页面上的矩形。位置靠**字符流索引反查**拿到 ——
把整页文字按阅读顺序拼成一条字符流、每个字符记着自己的 bbox，再把 tracking 的
`pdf_unicode` 归一化后丢进去搜（2026-09-26 实测匹配率 99.7%）。

**三条踩过的坑**：

 ① `multi_paragraph_id` 是**批次号**（同一批送给模型的段落），**不是"同一段被切开"** ——
    按它合并会把不相干的段落粘成一条（实测 id=20 里既有期刊名 `nature reviews immunology`
    又有 `Perspective`，那是页眉和标签，根本不是一段）。本脚本**逐段独立处理**：
    真被切开的段，两半各自反查、各自覆盖，结果本来就是对的。

 ② `output` 带 `<style id='N'>` 富文本标记和 `{vN}` 占位符，必须剥掉。
    剥壳后**保留空格**（归一化只用于比对，不能拿去显示，否则中文黏成一坨）。

 ③ 同一页里同样的文本会出现多次（页眉、重复的 "Abstract" 之类）——
    记一个"上次找到的位置"，下次从那儿往后找，避免全部叠在第一处。

用法：
    py -3 core/translate_doc.py <原稿.pdf> <tracking.json> [输出.json]
"""
import json
import os
import re
import sys
import unicodedata

import fitz

TAG = re.compile(r"</?style[^>]*>")
PH = re.compile(r"\{v\d+\}")


def norm(s):
    """比对用归一化：剥富文本标记 → NFKC → **去掉所有空白**。

    两侧差异都要在这一步抹平（PDF 里是 `nature reviews`、模型回的是 `nature  reviews`，
    只差空格不该算不匹配）。"""
    s = TAG.sub("", s or "")
    s = PH.sub("", s)
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", s)


def clean_out(s):
    """显示用清洗：剥标记，**保留空格**（归一化那份不能拿去显示）。"""
    s = TAG.sub("", s or "")
    s = PH.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def page_char_index(page):
    """整页字符流 + 与之一一对应的 bbox 列表（空白字符不进流）。

    ⚠ NFKC **可能把一个字符展开成多个**（`™`→`TM`、`¨`→空格+组合分音符）。
    展开出来的每个字符都要分到同一个 bbox，否则 `len(stream) != len(boxes)`，
    `stream.find()` 得到的位置拿去切 `boxes` 就越界成空列表 —— 2026-11-25 实测：
    一篇含 9 个 `™` 的论文在 p23 崩 `min() iterable argument is empty`，
    整个翻译任务在最后"整理双语数据"一步失败（BabelDOC 本身已经成功）。"""
    chars, boxes = [], []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type"):
            continue                      # 图片块
        for line in blk["lines"]:
            for sp in line["spans"]:
                for ch in sp["text"]:
                    if ch.isspace():
                        continue
                    # 展开后的字符也要过一遍 isspace（`¨` 展开出的第一个是空格）
                    for c2 in unicodedata.normalize("NFKC", ch):
                        if c2.isspace():
                            continue
                        chars.append(c2)
                        boxes.append(sp["bbox"])
    return "".join(chars), boxes


def build(pdf_path, tracking_path, verbose=True):
    """→ (块表 dict, 未命中清单)。纯函数，server.py 以后直接 import 调它。"""
    doc = fitz.open(pdf_path)
    pages_meta, idx = [], {}
    for i in range(doc.page_count):
        r = doc[i].rect
        pages_meta.append({"page": i + 1, "w": round(r.width, 2), "h": round(r.height, 2)})
        idx[i] = page_char_index(doc[i])

    t = json.load(open(tracking_path, encoding="utf-8"))
    blocks, misses, used = [], [], {}

    for pi, page in enumerate(t.get("page") or []):
        if pi not in idx:
            continue                      # tracking 的页数可能少于 PDF（跳过空白页）
        stream, boxes = idx[pi]
        W = pages_meta[pi]["w"] or 1.0
        H = pages_meta[pi]["h"] or 1.0
        for para in page.get("paragraph") or []:
            key = norm(para.get("pdf_unicode") or para.get("input") or "")
            zh = clean_out(para.get("output"))
            if len(key) < 3 or not zh:
                continue
            start = used.get((pi, key), 0)          # ③ 从上次的位置往后找
            pos = stream.find(key, start)
            if pos < 0:
                pos = stream.find(key)
            if pos < 0:
                misses.append({"page": pi + 1, "src": key[:70]})
                continue
            used[(pi, key)] = pos + len(key)
            seg = boxes[pos:pos + len(key)]
            if not seg:                     # 兜底：对齐修好后理论上不会发生，防一手
                misses.append({"page": pi + 1, "src": key[:70]})
                continue
            blocks.append({
                "page": pi + 1,
                "rect": [round(min(b[0] for b in seg) / W, 5),
                         round(min(b[1] for b in seg) / H, 5),
                         round(max(b[2] for b in seg) / W, 5),
                         round(max(b[3] for b in seg) / H, 5)],   # 相对坐标，缩放无关
                "src": clean_out(para.get("pdf_unicode") or para.get("input")),
                "zh": zh,
            })

    hit, miss = len(blocks), len(misses)
    out = {
        "schemaVersion": "tr-1",
        "source": {"pdf": os.path.basename(pdf_path), "tracking": os.path.basename(tracking_path)},
        "pages": pages_meta,
        "blocks": blocks,
        "stats": {"blocks": hit, "missed": miss,
                  "hitRate": round(100.0 * hit / max(1, hit + miss), 1)},
    }
    if verbose:
        print('块数 %d · 未命中 %d · 匹配率 %.1f%%' % (hit, miss, out["stats"]["hitRate"]))
        for m in misses[:8]:
            print('  未命中 p%-3d %r' % (m["page"], m["src"][:60]))
    return out, misses


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    if len(args) < 2:
        print(__doc__)
        return 1
    pdf, tr = args[0], args[1]
    out_path = args[2] if len(args) > 2 else os.path.splitext(pdf)[0] + '.blocks.json'
    data, _ = build(pdf, tr)
    json.dump(data, open(out_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('→', out_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
