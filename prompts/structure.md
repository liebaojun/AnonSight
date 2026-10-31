# 论文结构分析（语义分节）

你要为一篇学术论文建立**语义分节表**。程序已经给出「候选标题」和带行号的全文；
候选只是线索，**最终结构由你按语义判断**——版式（字体/字号/编号）变了、标题改了名字、
少了某个部分，都不该影响你。

## 判定规则

1. **只分正文**：从摘要/引言开始，到讨论/结论结束。方法（Methods / Materials and Methods /
   Experimental Procedures / STAR Methods）、致谢、参考文献、图注、补充材料都算「后置内容」，
   放进 `excluded`，**不要**当正文节。
2. `level`：1 = 大板块（Abstract / Summary / Introduction / Results / Discussion / Conclusion）；
   2 = 大板块下的子标题（Results 里的每一个小标题都要有）；3 = 更细一级。
3. 标题按**原文**抄写，保留编号、大小写和标点；**不要翻译、不要改写、不要补全**。
4. 每个节必须给出：
   - `title`：原文标题
   - `level`：1 / 2 / 3
   - `parentTitle`：上一级大板块的原文标题（level 1 留空）
   - `page`：标题所在的 PDF 页（全文里的 `[PDF 第 N 页]`）
   - `anchorLine`：标题所在行的 **L 编号**（全文每行左侧的 `L数字|`）
   - `anchor`：该行原文（逐字复制，5–20 个词）
   - `kind`：有印刷标题写 `"heading"`；没有印刷标题（例如摘要直接开始）写 `"implicit"`，
     此时 `anchorLine` / `anchor` 指向该节的**第一行正文**
5. **不要漏节**：
   - 摘要 / Summary 通常是大板块，除非确实没有；
   - 摘要常见两种写法：独立标题（`ABSTRACT` / `Summary`），或**和正文同一行的 `ABSTRACT: …`**；
     有些期刊摘要整段字号比正文小 —— 这些都要算摘要，**必须单列一节**；
   - **有些期刊摘要没有标题、也没有 `ABSTRACT:` 字样**（首页标题/作者后直接是摘要正文）；
     有些排版还会把它在 PDF 里提取到 Introduction **之后**。按语义识别出来，仍然列成
     Abstract（anchorLine 指向摘要第一行；平台会把它排到最前）；
   - **结构化摘要**里可能出现 `Introduction:` / `Methods:` / `Results:` 这类小标签，
     它们是摘要内部的栏目，**不是正文大板块**；整段摘要只列一条 Abstract；
   - Introduction 即使没有小标题也要列一条；
   - Results 下**每个**小标题都要列；
   - Discussion / Conclusion 按原文列。
6. **不要编造**：`anchorLine` 必须真实存在，`anchor` 必须是该行原文。
   候选标题里的页眉、期刊行、图注、正文碎片要剔除；候选 level 是程序猜的，**不要信**。
7. 候选标题可能**漏掉**真标题（尤其大板块与小标题同字号、标题不在候选里）——
   通读全文时发现了就补上。
8. 节与节之间必须**连续覆盖正文**，不要把同一段话分给两个节；也不要留下没人管的正文。

## 输出（只输出 JSON，不要解释，不要 Markdown 代码围栏）

```json
{"sections":[
  {"title":"Abstract","level":1,"parentTitle":"","page":1,"anchorLine":42,
   "anchor":"The tumor microenvironment ...","kind":"implicit"},
  {"title":"Introduction","level":1,"parentTitle":"","page":2,"anchorLine":88,
   "anchor":"Introduction","kind":"heading"},
  {"title":"Results","level":1,"parentTitle":"","page":3,"anchorLine":210,
   "anchor":"Results","kind":"heading"},
  {"title":"CCL5 is stored in cytoplasmic vesicles","level":2,"parentTitle":"Results",
   "page":3,"anchorLine":218,"anchor":"CCL5 is stored in cytoplasmic vesicles","kind":"heading"}
 ],
 "excluded":[
  {"title":"Materials and Methods","page":3,"level":1,"anchorLine":150},
  {"title":"References","page":11,"level":1,"anchorLine":990}
 ],
 "notes":"可选：你对结构拿不准的地方"}
```
