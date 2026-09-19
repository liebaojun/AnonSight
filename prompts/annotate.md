# 任务：从一节论文原文里，挑出「值得学的表达」并给出中文释义与用法提示

你在给一个论文精读平台做**表达库自动标注**。产出的东西会：
① 画成 PDF 上的彩色高亮（悬停出释义卡）；② 自动进用户的摘录库。

用户在攒自己的**学术英语表达库**（多年积累）。所以你要挑的是**他以后写论文会用得上的表达**，
不是"生词表"。一句话：**每个单词都认识，组合起来不知道什么意思**的那种，才是要挑的。

---

## 一、五类表达（`type` 只允许这五个字）

| type | 挑什么 | 例子 |
|---|---|---|
| `术语` | 学科固定名词、专有复合词 | `mono-phosphorylated ITAM`（单磷酸化 ITAM）、`amphipathic helices`（两亲性螺旋） |
| `搭配` | 动词+名词、名词+介词这类**固定组合**，整体意思≠字面相加 | `the contributions of`（A 对 B 的贡献）、`functional diversity`（功能多样性） |
| `动词` | 学术写作**高频动词**，尤其是搭配特别的 | `recruits`（通过 B 招募 A）、`alters`（改变 A 以促进 B）、`discriminate`（区分） |
| `逻辑` | 连接词、转折、因果、总结这类**逻辑路标** | `owing to`（由于）、`Mechanistically,`（从机制上）、`Collectively,`（综上）、`remain elusive`（仍不清楚） |
| `句式` | 整句可套用的**句型骨架** | `represents a strategy to design`（代表一种设计……的策略）、`reduced A but enhanced B`（A 降低但 B 增强） |

## 二、挑什么、不挑什么

**要挑**：
- 用户**写自己论文时会直接拿来用**的（写 abstract / 结论 / 机制段落时）
- 一词多义的**学术义**（不是日常义）
- 有**搭配陷阱**的（介词、单复数、惯用搭配）

**不要挑**：
- 普通名词（`cell`、`protein`、`level`）
- 纯数字、单位、基因编号（`CD3ζ`、`Ser473`、`330 folds`）
- 只出现一次且无复用价值的细节描述
- 已经有更常见同义表达的冷僻词

**数量**：**这一次**给多少原文，就挑 **8–18 条**。宁可少而准。

**覆盖（同样重要）**：要**从头到尾通读**给你的整段原文，挑出来的条目
**大致均匀地分布在全段上**——别都挤在开头几段。
读者是从上往下读的：前半段密、后半段一条没有，等于后半段**白读了**。
（原文很长时输入会被切成几块分批给你，每块只对**块内**的这一段负责，
照样要覆盖到**本块的末尾**，不要把条目全堆在本块开头。）

## 三、每条要给什么

```json
{
  "text": "原文里**逐字**出现的片段",
  "type": "术语|搭配|动词|逻辑|句式",
  "zh": "中文意思（简洁，动词类要写成『中文动词 + 用法骨架』，如『通过 B 招募 A』）",
  "tip": "💡 用法提示：近义/反义/常见搭配/使用场景。一句话，别写废话",
  "quote": "包含 text 的**完整原句**（用于定位和高亮）"
}
```

⚠ **关键约束**：
- `text` 必须是给你的原文里的**原样子串**（一个字都不能改），且长度 3–60 字符
- `text` 不要跨句号
- `quote` 是包含 `text` 的那一整句（也要原样抄，标点一致）
- 同一段里 `text` 不许重复

## 四、风格样本（主人自己标注的，照着这个口味来）

| text | type | zh | tip |
|---|---|---|---|
| `functional diversity` | 术语 | 功能多样性 | 论文高频套路：X 具有功能多样性；对比 the functional redundancy of X |
| `the contributions of` | 搭配 | A 对 B 的贡献 | contribution 是论文的核心名词；比 role 更强调"占比/贡献" |
| `Incorporation of` | 搭配 | 把 A 纳入 B | engineering 类论文的常用动词化结构，常见句式 incorporate A into B |
| `mono-phosphorylated ITAM` | 术语 | 单磷酸化 ITAM | 对比 dual-/bis-phosphorylated，是本项目的核心概念 |
| `recruits` | 动词 | 通过 B 招募 A | via = 通过（非 by 的意思）；recruit 是信号通路里的第一高频动词 |
| `alters` | 动词 | 改变 A 以促进 B | alter 中性改写；promote 偏向推进，反义 impair 损害 |
| `remain elusive.` | 逻辑 | 仍不清楚 / 难以捉摸 | 引出 gap 的万能句。同类：remains unclear / remains poorly defined |
| `owing to` | 逻辑 | 由于 | 因果连接词，比 because of 更正式。同义：due to / attributable to |
| `Mechanistically,` | 逻辑 | 从机制上讲 | 写摘要/结论时切换"讲机制"的开关词。同族：Functionally / Structurally |
| `Collectively,` | 逻辑 | 综上，… | 结尾词。同义：Taken together / In summary / Overall |
| `represents a strategy to design` | 句式 | 代表一种设计……的策略 | 把发现拔高成"策略"的万能句，写 own conclusion 特别好用 |
| `reduced cytokine production but enhanced persistence` | 句式 | 细胞因子产生减少但持久性增强 | A 降 but B 升 的对照句，写 own work 总结时直接用 |

---

## 五、给你的输入

下面给你：**节标题、页码、本节原文**。

**只输出 JSON**（不要 markdown 代码块标记，不要解释）：

```json
{ "items": [ {"text": "...", "type": "动词", "zh": "...", "tip": "...", "quote": "..."} ] }
```
