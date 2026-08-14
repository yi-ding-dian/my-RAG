"""评估结果导出与查询 API"""
from __future__ import annotations
import json
import csv
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, FileResponse
from pathlib import Path
from datetime import datetime

from backend.config import settings
from backend.models.eval_models import EvalStatus
from backend.services.eval_service import get_eval_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/results", tags=["结果"])


@router.get("/{task_id}/export/json")
def export_json(task_id: str):
    """导出评估结果为 JSON 文件"""
    results = get_eval_service().get_results(task_id)
    if not results:
        raise HTTPException(status_code=404, detail="结果未找到")

    content = json.dumps(results.model_dump(mode="json"), ensure_ascii=False, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="eval_{task_id}.json"'},
    )


@router.get("/{task_id}/export/csv")
def export_csv(task_id: str):
    """导出评估结果为 CSV 文件"""
    results = get_eval_service().get_results(task_id)
    if not results:
        raise HTTPException(status_code=404, detail="结果未找到")

    import io
    output = io.StringIO()
    writer = csv.writer(output)

    # 表头
    all_metrics = list(results.aggregate.scores.keys()) if results.aggregate.scores else []
    header = ["问题", "答案", "参考答案"] + all_metrics
    writer.writerow(header)

    # 数据行
    for r in results.results:
        row = [r.question, r.answer, r.ground_truth or ""]
        row += [r.scores.get(m, "") for m in all_metrics]
        writer.writerow(row)

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="eval_{task_id}.csv"'},
    )


@router.get("/{task_id}/export/html")
def export_html(task_id: str):
    """导出评估报告为 HTML"""
    results = get_eval_service().get_results(task_id)
    if not results:
        raise HTTPException(status_code=404, detail="结果未找到")

    task = get_eval_service().get_task(task_id)
    task_name = task.name if task else task_id

    rows_html = ""
    for i, r in enumerate(results.results):
        scores_html = "".join(
            f'<span class="metric-badge">{k}: {v:.4f}</span> '
            for k, v in r.scores.items() if v is not None
        )
        rows_html += f"""
        <tr>
            <td>{i + 1}</td>
            <td>{_escape_html(r.question[:100])}</td>
            <td>{_escape_html(r.answer[:200])}</td>
            <td>{scores_html}</td>
        </tr>"""

    agg_html = "".join(
        f'<div class="agg-item">{k}: <strong>{v:.4f}</strong></div>'
        for k, v in results.aggregate.scores.items()
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>RAGAS 评估报告 - {_escape_html(task_name)}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
h1 {{ color: #333; }}
.agg-box {{ background: #fff; padding: 20px; border-radius: 8px; margin: 20px 0; display: flex; gap: 20px; flex-wrap: wrap; box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
.agg-item {{ font-size: 16px; padding: 8px 16px; background: #e8f4fd; border-radius: 4px; }}
table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 14px; }}
th {{ background: #fafafa; font-weight: 600; color: #555; }}
.metric-badge {{ display: inline-block; padding: 2px 8px; border-radius: 3px; background: #e8f4fd; margin: 2px; font-size: 12px; }}
.footer {{ text-align: center; color: #999; margin-top: 20px; font-size: 13px; }}
</style>
</head>
<body>
<h1>RAGAS 评估报告</h1>
<p>任务: {_escape_html(task_name)} | 样本数: {len(results.results)} | {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
<div class="agg-box">{agg_html}</div>
<table>
<thead><tr><th>#</th><th>问题</th><th>答案</th><th>评分</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
<div class="footer">由 RAGAS 评估系统生成</div>
</body>
</html>"""

    return Response(content=html, media_type="text/html", headers={
        "Content-Disposition": f'attachment; filename="report_{task_id}.html"'
    })


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
