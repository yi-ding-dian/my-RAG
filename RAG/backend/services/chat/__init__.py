"""问答（chat）服务

模块结构：
- query_rewriter.py: 检索前查询改写（口语→正式 + 指代消解）与复合问题拆分
- agentic_service.py: Agentic 检索决策层（retrieve → grade → rewrite 循环）
- chat_service.py: 会话编排 + 流式问答（ChatService）—— 待搬入
- session_store.py / refs.py: 待从 chat_service 拆出

**为什么成目录**：这几个模块只服务「问答」这一个流程——query_rewriter 与
agentic_service 经 grep 验证**只被 chat_service 引用**，按「谁跟谁一起变」
收在一起；chat_service 拆出的会话持久化/引用组装同理。
"""
