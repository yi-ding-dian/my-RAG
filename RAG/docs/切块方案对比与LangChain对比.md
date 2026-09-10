# 切块方案对比(六种方式 + 与 LangChain 对照)

> 本文档归纳知识库切块引擎的 6 种方式,并逐项与 LangChain 对应组件对比:
> 哪些是思想同源、哪些是**纯自研 LangChain 没有的能力**,以及整体为何不引入 LangChain。
> 对应代码:`backend/chunking/`——一个算法一个文件(`recursive.py` naive /
> `markdown_splitter.py` title / `regex_chunker.py` regex /
> `parent_child.py` parent_child / `qa_chunker.py` qa)、
> `backend/services/agentic_chunker.py`(Agentic)、
> `backend/services/spreadsheet/reader.py`(Excel 行窗口分段,与切块器协同)。

---

## 一、六种方式总览

| 方式 | 实现类 | 核心算法 | 主要参数(界面可配) | 适用场景 | 关键取舍 |
|---|---|---|---|---|---|
| 通用 naive | `RecursiveChunker` | 递归字符切分,分隔符优先级 `\n\n→\n→。→；→空格→字符`;**句子感知默认开**(块边界落在句间,单句超长才句内切)+ 保护区间 | chunk_size(800)/overlap(0)/delimiter | 常规文档兜底 | 不拆半句、不切表格/图片 |
| 标题 title | `MarkdownSplitter` | 识别 6 类标题样式(ATX/setext/包裹式/前导符号式/【】/━━ 等)按级别切章节段,段内递归;超长智能回退放宽级 | split_level(1~3)/chunk_size/overlap | 结构文档(报告/说明书) | 块=章节语义完整;**防误判**(表格行/冒号结尾/纯符号行不作标题) |
| 正则 regex | `RegexChunker` | 用户正则按匹配位置切分:匹配片段独立成块 + 匹配间文本成块,匹配段超长递归 | regex_pattern(必填)/chunk_size | 固定格式文本 | 用户完全控制边界;匹配漏了也不丢内容 |
| 父子 parent_child | `ParentChildChunker` | 父块=按标题层级聚合**完整章节**(无大小上限,>5 万字才按 parent_chunk_size 二次切兜底);子块=章节段内递归,**不跨章节边界**;检索返回父块上下文 | parent_chunk_size(1024)/parent_overlap(100)/parent_split_level(2)+ 子块参数 | 长章节型文档(合同/规章/手册) | 命中子块、整章看上下文;retrieval_mode=parent/child 可选 |
| 问答对 qa | `QaChunker` | 识别 `问:/Q:` 问题块,问题段→下一问题段前整体归一对问答对(可跨段),超长不切 | 无(兼容参数不生效) | FAQ/题库/培训资料 | 问答对完整;入库前 **QA 占比 ≥50% 规范性检测** |
| Agentic | `agentic_chunk`(LLM) | LLM 读全文自主判断逻辑段落;每块 5 类标签(论述/事实/操作/数据/其他);思考模式可选 | LLM 驱动;>5 万字拒绝、1-5 万字需确认 | 高层级语义内容 | 语义天花板;**失败自动回退 title 不阻塞入库**;与上下文增强互斥(成本保护) |

---

## 二、与 LangChain 对应物逐项对比

| 我方方式 | LangChain 对应物 | 相似点 | **差异 / LC 缺失** |
|---|---|---|---|
| 通用 naive | `RecursiveCharacterTextSplitter` | 分隔符优先级递归、chunk_size/overlap 参数一致 | LC 只按分隔符切;**句子感知(句间聚合到 chunk_size)LC 没有**;保护区间 LC 没有 |
| 标题 title | `MarkdownHeaderTextSplitter` / `HTMLHeaderTextSplitter` | 都按标题层级切段 | LC 只认 `#`/HTML 标签列;我们识别 6 类标题样式; **智能回退**(超长单块自动放宽级重切)、**连续标题不切**、**防误判** 均 LC 无 |
| 正则 regex | **无直接对应**(LC 各 splitter 的 separator 为字符串字面量,不解析正则) | - | **纯自研**:正则"匹配位置切分 + 匹配片段独立成块 + 段超长递归" |
| 父子 parent_child | `ParentDocumentRetriever` | 小块检索、大块送上下文 | LC 父块=尺寸化(token/页切,无章节语义);我们=标题章节聚合且只设兜底上限;LC 需 vector+memory store 双存储、检索二次查询;我们父全文入 metadata **单库单查**;retrieval_mode=parent/child 切换 LC 无概念 |
| 问答对 qa | **无** | - | **纯自研**:问答对整块(答案跨段完整)+ 占比检测 |
| Agentic | 无真正对应(`SemanticChunker` 是 embedding 相似度切分,非 LLM 逻辑段) | 均属"语义"切分 | **纯自研**:LLM 逻辑段落 + 5 类标签 + 成本护栏 + 失败回退 |

---

## 三、纯自研 LangChain 没有的能力(清单)

1. **保护区间系统**:管道表格/HTML 表格/围栏代码块/**图片引用**整体成块、切分边界不落入区间;
   修复过真实缺陷——图片引用含 `!`,被句界集合当作英文句界切碎,块文本残缺为 `[](url)` 链接(前端不渲染)。
2. **句子感知切块**:句子单元贪心聚合到 chunk_size,块边界永远在句间;单句超长(无标点长串)才句内递归兜底。
3. **6 类标题识别 + 智能回退 + 连续标题过滤 + 防误判**(LC 的 MarkdownHeaderTextSplitter 只认 `#`)。
4. **语义化父子分块**:章节=父块(标题聚合)、子块不跨章节;父全文随子块 metadata 单库存储(parent_text,截断 8000);
   `retrieval_mode` 控制"命中后给出整章 / 只给命中点"。
5. **QA 问答对完整切块 + 入库占比规范性检测**(问答对占比 <50% → 阻止入库,带详情可强制继续)。
6. **Agentic LLM 语义切块 + 5 类标签 + 双挡成本护栏**(>5 万字拒绝、1-5 万字需确认)+ 失败回退 title。
7. **块级全局偏移 char_start/char_end**,且保证 `text == full[char_start:char_end]` 切片一致性(溯源/引用/图谱实体定位全靠它;LC 默认不感知全文坐标)。
8. **Excel 行窗口分段**(700 字符/段、每段重复表头 + 标题带"第 x-y 行",与公式引擎/类 Excel 预览协同)——LC 无此概念。
9. 全链路零第三方切块依赖(私有化部署可控)。

---

## 四、为何不采用 LangChain(取舍说明)

| LangChain 优点(承认) | 我们的取舍理由 |
|---|---|
| 快速起步、组件丰富 | 起步期功能可枚举(切块+检索+流式);自研 6 种切法已覆盖,且每个都能讲清为什么 |
| 生态成熟、社区方案多 | ① 私有化离线部署:整包依赖重、版本波动(LC 大版本不兼容是常态);② 调试黑盒:引用溯源/偏移/保护区间都在"需要可控"处 |
| 现成 Retriever/组合链路 | 检索链路需要**逐级开关 + 对比实验**(重排/混合/图谱/上下文增强),自研链路开关直观、可测、无魔法 |

**承认的代价**:LC 在快速集成新组件、丰富 Retriever wrapper、社区知识复用上更省事。本项目的选择是**用可控性换集成速度**,在企业私有化场景计算得过来。

---

## 五、一句话总结

> 6 种切块方式中,naive/title/parent 与 LangChain 思想同源;句子感知、保护区间、标题智能回退、QA 对、Agentic 语义切、块级偏移这 6 样是**纯自研、LC 没有**的——通用方案做到了 LC 的 80%,差异化的 20% 才是检索质量的关键。
