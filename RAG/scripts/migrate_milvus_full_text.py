#!/usr/bin/env python
"""存量 Milvus 集合迁移：补全文检索字段（BM25）

背景：旧集合用"快速建表 + 动态字段"承载 chunk 文本，而全文检索要求 text 是
**显式字段**——Milvus 的分词器（analyzer）是字段级属性，动态字段配不了；真配错了
也不会报错，只是 BM25 退化成整句一个 token（向量检索照常工作，很难发现）。
本脚本把旧集合无损迁到新 schema：text 提升为显式字段（挂中文分词器）+
BM25 Function 自动生成稀疏向量，其余 metadata 继续留在动态字段（写入侧代码不用改）。

行为：
- 默认**预演**：只体检不动数据，列出待迁移集合、条数、向量维度
- --execute 才真正迁移；每个库迁移前先把全量数据（含向量）备份到
  data/migration_backup/kb_{kb_id}.pkl，失败可据此恢复
- 单个库的迁移步骤：读全量 → 备份 → 删旧集合 → 按新 schema 重建 → 写回 →
  校验（条数一致 + 抽样本能 BM25 命中）
- --kb 可只迁指定知识库

前置条件：**先停后端服务**（迁移会删集合，与在线写入冲突）。

用法：
    .venv/bin/python scripts/migrate_milvus_full_text.py                  # 预演
    .venv/bin/python scripts/migrate_milvus_full_text.py --execute        # 迁移全部
    .venv/bin/python scripts/migrate_milvus_full_text.py --execute --kb 72d526c7efd6
"""
from __future__ import annotations

import argparse
import pickle
import re
import sys
from pathlib import Path

# 脚本可独立运行（不在项目根时也能找到 backend 包）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pymilvus import MilvusClient  # noqa: E402

from backend.config import DATA_DIR, get_active_config  # noqa: E402
from backend.services.vector_store import (  # noqa: E402
    SPARSE_FIELD, MilvusVectorBackend, normalize_milvus_uri)

# 单次 query/upsert 的批量大小（Milvus query 上限 16384，取小一点更稳）
QUERY_PAGE = 1000
WRITE_BATCH = 200

BACKUP_DIR = DATA_DIR / "migration_backup"


def connect() -> tuple[MilvusClient, str]:
    """连接激活配置里的 Milvus（与线上传同一地址，避免迁错实例）"""
    # SettingsService 构造时会加载 data/settings.json 并把活跃档案应用到全局配置；
    # 不构造它的话 get_active_config() 只有环境变量默认值（chroma），会连错库。
    from backend.services.settings.service import get_settings_service
    get_settings_service()
    vs = get_active_config().vector_store
    if getattr(vs, "backend", "") != "milvus":
        sys.exit(f"当前激活配置的向量后端是 {getattr(vs, 'backend', '?')!r}，"
                 "只有 milvus 后端需要本迁移")
    uri = normalize_milvus_uri(vs.milvus_uri)
    print(f"Milvus: {uri}")
    return MilvusClient(uri=uri), uri


def list_pending(client: MilvusClient, only_kb: str = "") -> list[dict]:
    """待迁移集合：缺少稀疏向量字段（旧版 schema）的知识库集合"""
    pending = []
    for name in sorted(client.list_collections()):
        if not name.startswith("kb_"):
            continue
        kb_id = name[len("kb_"):]
        if only_kb and kb_id != only_kb:
            continue
        fields = {f["name"] for f in
                  client.describe_collection(name).get("fields", [])}
        if SPARSE_FIELD in fields:
            continue  # 已是新 schema
        rows = client.query(collection_name=name, filter="",
                            output_fields=["count(*)"])
        count = rows[0]["count(*)"] if rows else 0
        pending.append({"name": name, "kb_id": kb_id, "count": count})
    return pending


def read_all_rows(client: MilvusClient, name: str) -> list[dict]:
    """翻页读全量（含向量与动态字段——写回时要连向量一起搬，避免重新 embedding）"""
    rows: list[dict] = []
    offset = 0
    while True:
        batch = client.query(collection_name=name, filter="",
                             output_fields=["id", "vector", "*"],
                             limit=QUERY_PAGE, offset=offset)
        rows.extend(batch)
        if len(batch) < QUERY_PAGE:
            break
        offset += QUERY_PAGE
    return rows


def save_backup(kb_id: str, rows: list[dict]) -> Path:
    """迁移前备份（pickle 二进制，含向量；失败可据此恢复）"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKUP_DIR / f"kb_{kb_id}.pkl"
    with path.open("wb") as f:
        pickle.dump(rows, f)
    print(f"  备份 {len(rows)} 条 → {path} "
          f"（{path.stat().st_size / 1e6:.1f} MB）")
    return path


def write_back(client: MilvusClient, name: str, rows: list[dict]) -> None:
    """分批写回（text 落到显式字段、其余进动态字段，Milvus 自动分流）"""
    for i in range(0, len(rows), WRITE_BATCH):
        client.upsert(collection_name=name, data=rows[i:i + WRITE_BATCH])
    client.flush(name)


def _sample_query(text: str) -> str:
    """从 chunk 文本里取一段可检索样本

    **优先取中文片段**：不少 chunk 以图片引用开头（`![](/api/files/images/…)`），
    去掉符号后剩下的拉丁字母串（如 "apifilesim"）没有检索意义，会让校验误判
    "BM25 无命中"。
    """
    chinese = re.findall(r"[一-鿿]{2,}", text or "")
    if chinese:
        return chinese[0][:10]
    return re.sub(r"[^\w]", "", text or "")[:10]


def verify_migration(client: MilvusClient, name: str,
                     expect_count: int, rows: list[dict]) -> tuple[bool, str]:
    """校验：条数一致 + 抽样本条能被 BM25 检索命中"""
    got = client.query(collection_name=name, filter="",
                       output_fields=["count(*)"])
    count = got[0]["count(*)"] if got else 0
    if count != expect_count:
        return False, f"条数不一致：迁移后 {count} != 迁移前 {expect_count}"

    sample = next((_sample_query(r.get("text", "")) for r in rows
                   if _sample_query(r.get("text", ""))), "")
    if not sample:
        return True, f"条数 {count} 一致（无可检索样本，跳过检索校验）"
    res = client.search(collection_name=name, data=[sample],
                        anns_field=SPARSE_FIELD, limit=3)
    hits = res[0] if res else []
    if not hits:
        return False, f"BM25 检索无命中（样本 {sample!r}），分词器可能未生效"
    return True, f"条数 {count} 一致，BM25 样本检索命中 {len(hits)} 条"


def migrate_one(client: MilvusClient, uri: str, target: dict,
                execute: bool) -> bool:
    """迁移单个知识库集合，返回是否成功"""
    name, kb_id = target["name"], target["kb_id"]
    print(f"\n=== {name}（{target['count']} 条）===")
    rows = read_all_rows(client, name)
    if not rows:
        print("  空集合：直接按新 schema 重建")
        dim = None
    else:
        dim = len(rows[0].get("vector") or [])
        print(f"  读取 {len(rows)} 条，向量维度 {dim}")
    if not execute:
        print("  （预演，未改动数据）")
        return True

    save_backup(kb_id, rows)

    # 建新集合：复用后端的建表逻辑，保证 schema 与线上一致
    # （backend 自建连接指向同一实例，ensure_collection 只在集合不存在时建表，
    #   所以先 drop 掉旧集合）
    backend = MilvusVectorBackend(uri=uri)
    backend.drop_collection(kb_id)
    if dim:
        backend.ensure_collection(kb_id, dim)
        write_back(client, name, rows)
    print("  写回完成，开始校验…")

    ok, message = verify_migration(client, name, len(rows), rows)
    print(("  ✓ " if ok else "  ✗ ") + message)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移 Milvus 集合以启用 BM25 全文检索")
    parser.add_argument("--execute", action="store_true",
                        help="真正执行迁移（缺省只预演，不动数据）")
    parser.add_argument("--kb", default="", help="只迁移指定知识库 id")
    args = parser.parse_args()

    client, uri = connect()
    pending = list_pending(client, args.kb)
    if not pending:
        print("没有需要迁移的集合（都已是新 schema）")
        return 0

    print(f"\n待迁移 {len(pending)} 个集合：")
    for t in pending:
        print(f"  - {t['name']}：{t['count']} 条")
    if not args.execute:
        print("\n这是预演（未改动数据）。确认无误后加 --execute 执行迁移；"
              "\n**执行前请先停后端服务**（迁移会删集合，与在线写入冲突）。")
        return 0

    failed = [t["name"] for t in pending
              if not migrate_one(client, uri, t, execute=True)]
    print("\n" + ("全部迁移完成" if not failed
                  else f"以下集合迁移失败：{failed}（备份在 {BACKUP_DIR}）"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
