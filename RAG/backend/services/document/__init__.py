"""文档服务（document）

模块结构：
- service.py: 文档元数据持久化 + 状态机（DocumentService / get_document_service）
- lifecycle.py: 跨存储的生命周期编排（可见性切换 / 彻底删除 / 配额校验）

**为什么成目录**：service.py 是**纯元数据层**——只管 data/documents/*.json
与状态机，不碰向量、对象存储、BM25。而"改一个文档的可见性"要同时动向量
metadata / 元数据 / BM25 三处、且顺序有约束，属于**跨存储一致性**，与
"元数据怎么存"是两种变化原因。后者原本散在路由层，随端点复制而漂移
（四个端点各写一遍同一个公式，其中一处还带没名字的特例，见 lifecycle 模块
文档）。拆开后两者各自独立演进。
"""
