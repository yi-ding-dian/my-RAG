"""文档解析（引擎调用 / 图片链路 / 可用性探测 / DeepDoc 客户端）

模块结构（一类职责一个文件）：
- client.py: 解析客户端（MinerU / 纯文本 / 结构解析 / 自动降级）
- images.py: Markdown 图片引用处理纯函数（可单测）
- probe.py: 解析器可用性探测（解析弹窗状态徽标 + ingestion 自动降级）
- probes.py: 统一探测服务（LLM / Embedding / MinerU / DeepDoc / MySQL / MinIO）
- deepdoc.py: DeepDoc 解析客户端（RAGFlow API）

probe.py 是上层包装（探测结果 → 解析器契约），probes.py 是底层统一探测实现，
两者职责不同不可合并。调用方按具体子模块导入。
"""
