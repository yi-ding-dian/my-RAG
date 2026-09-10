"""文档域路由（原 routers 下三个平铺模块归组）

模块结构：
- crud.py: 文档明细 / 上传下载 / 原始内容预览 / 解析入库（prefix /api/kbs/{kb_id}/documents）
- admin.py: 超管跨库文档管理（prefix /api/admin/documents）
- smart_parse.py: 智能解析引导（prefix /api/kbs/{kb_id}/documents）

main.py 直接注册各子模块的 router 对象，URL 路径与拆分前完全一致。
"""
