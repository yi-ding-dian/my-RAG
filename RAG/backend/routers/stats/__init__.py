"""系统统计 API：/api/stats（三个互不相干的子领域）

- overview.py  统计概览（按角色过滤）+ 检索质量
- ragas.py     RAGAS 评估全套（10 个端点）
- feedback.py  用户回答反馈查询

**为什么成目录**：三块只共享 URL 前缀，业务上各不相干——改 RAGAS 不该滚过
用户反馈的代码。按「谁跟谁一起变」拆开。
"""
from fastapi import APIRouter

from backend.routers.stats import feedback, overview, ragas

router = APIRouter(tags=["统计"])
router.include_router(overview.router)
router.include_router(ragas.router)
router.include_router(feedback.router)
