"""入库方案（画像 → 推荐 → 完整入库配置 + 成本预估）

模块结构（一类职责一个文件）：
- types.py: 共享常量（文件类型归类、切块方式标签表）与数据类
- profiling.py: 纯规则画像（文本 → 篇幅/标题结构/QA/指代密集度），零 IO
- extract.py: 轻量本地文本提取与 docx 结构探测（本包唯一 IO 层，不调 MinerU/DeepDoc）
- sheets.py: 表格文档画像（Sheet/行列/合并单元格）
- engine.py: 解析引擎建议（文件类型 + 解析器探测 + docx 规范性 → 建议 + 理由）
- plan.py: 决策矩阵（规则引擎只定"免费"项：切块方式/引擎/参数/免费开关）
- cost.py: 成本预估（原始单位：LLM 调用次数 + 输入输出字符数 + 图片张数）
- build.py: 编排入口（analyze 接口调用 build_analyze）

对外唯一入口 `from backend.services.smart_parse.build import build_analyze`；
其余子模块供单测直接导入（纯函数可直接调，无需起 app）。

参数权威在 backend/services/ingestion/params.py（范围常量与默认值）——本包
**引用**它而不复制，避免出现第二份常量（历史上 agentic 的字数上限就在推荐
理由里抄过一份字符串）。同理，LLM 环节上限读系统配置不写死。

执行侧在 backend/services/ingestion/：本包决定"怎么入库"，那边执行入库。
"""
