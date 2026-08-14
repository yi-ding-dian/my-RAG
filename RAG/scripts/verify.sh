#!/usr/bin/env bash
# ============================================================================
# my-RAG 知识库系统 · 端到端验证脚本（针对运行中的 8091 后端）
#
# 流程: health → 建库 → 上传txt(中文名) → ingest轮询(60s) → retrieve →
#       SSE流式问答(meta/delta/done) → stats → stats/ragas → 级联清理
# 新增（企业功能）: 检索参数化(top_k) / 多库检索(kb_ids) / 会话重命名+导出 /
#       质量统计 / 审计日志 / 标签设置过滤 / URL导入(容错跳过) / 图片代理token /
#       回收站(软删→恢复→彻底删除)
# 用法: bash scripts/verify.sh
# 说明: 只操作脚本自己创建的数据（kb_id 唯一），结束全部删除并自检；
#       依赖项目 .venv 中的 python 解析 JSON（无需安装 jq）。
#       网络/外部依赖不可用的步骤标记 ⏭ SKIP（不计入失败）。
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
PY="$ROOT_DIR/.venv/bin/python"
BASE="${BACKEND_URL:-http://127.0.0.1:8091}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

PASS=0
FAIL=0
SKIP=0

step() { echo; echo "===== $1 ====="; }
ok()   { echo "  ✅ $1"; PASS=$((PASS + 1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }
skip() { echo "  ⏭ $1"; SKIP=$((SKIP + 1)); }

# 从 JSON 提取点分路径值（如 kb.id）；key 不存在或 JSON 非法时输出空串
jget() { # jget <json> <dotted.key>
    echo "$1" | "$PY" -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    for k in sys.argv[1].split("."):
        if isinstance(d, list) and k.isdigit():
            d = d[int(k)]
        else:
            d = d[k]
    if isinstance(d, bool):
        print(str(d).lower())
    elif isinstance(d, (dict, list)):
        print(json.dumps(d, ensure_ascii=False))
    else:
        print(d)
except Exception:
    sys.exit(1)
' "$2" || echo ""
}

# ---------------------------------------------------------------------------
step "0. 前置检查：后端 8091 健康"
# ---------------------------------------------------------------------------
HEALTH="$(curl -s -m 5 "$BASE/api/health" || true)"
if [ "$(jget "$HEALTH" status)" = "ok" ]; then
    ok "GET /api/health → status=ok（模型 $(jget "$HEALTH" llm_model)）"
else
    bad "GET /api/health 失败。请确认后端已启动: .venv/bin/uvicorn backend.main:app --port 8091"
    echo "  返回: $HEALTH"
    exit 1
fi

# ---------------------------------------------------------------------------
step "0.5 登录获取 token（admin/admin123，后续全部请求带 Authorization）"
# ---------------------------------------------------------------------------
LOGIN_JSON="$(curl -s -m 10 -X POST "$BASE/api/auth/login" \
    -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"admin123"}')"
TOKEN="$(jget "$LOGIN_JSON" access_token)"
if [ -n "$TOKEN" ]; then
    ok "POST /api/auth/login → 获取 token（角色 $(jget "$LOGIN_JSON" user.role)）"
    AUTH=(-H "Authorization: Bearer $TOKEN")
else
    bad "登录失败（后端可能仍是旧代码）: $LOGIN_JSON"
    exit 1
fi

# ---------------------------------------------------------------------------
step "0.6 super_admin 权限验证（GET /api/users，非超管 403）"
# ---------------------------------------------------------------------------
USERS_CODE="$(curl -s -o "$TMP_DIR/users.json" -w '%{http_code}' -m 5 \
    "${AUTH[@]}" "$BASE/api/users" || true)"
USERS_CNT="$(jget "$(cat "$TMP_DIR/users.json")" 0.username 2>/dev/null || true)"
if [ "$USERS_CODE" = "200" ]; then
    ok "GET /api/users → HTTP 200（用户列表 $(jget "$(cat "$TMP_DIR/users.json")" 0.username 2>/dev/null || echo "?")）"
else
    bad "GET /api/users → HTTP $USERS_CODE（应 200）"
fi

# ---------------------------------------------------------------------------
step "1. 创建知识库"
# ---------------------------------------------------------------------------
KB_JSON="$(curl -s -m 10 -X POST "$BASE/api/kbs" \
    "${AUTH[@]}" \
    -H 'Content-Type: application/json' \
    -d '{"name":"verify-临时知识库","description":"端到端验证脚本创建，用完即删"}')"
KB_ID="$(jget "$KB_JSON" id)"
if [ -n "$KB_ID" ]; then
    ok "POST /api/kbs → kb_id=$KB_ID"
else
    bad "创建知识库失败: $KB_JSON"
    exit 1
fi

# ---------------------------------------------------------------------------
step "2. 上传测试文档（中文文件名）"
# ---------------------------------------------------------------------------
cat > "$TMP_DIR/verify_测试文档.txt" <<'EOF'
# Python 简介

Python 是一种高级编程语言，由 Guido van Rossum 于 1991 年首次发布，强调代码可读性。

## 主要特性

Python 语法简洁优雅，支持面向对象、函数式等多种编程范式，拥有庞大的标准库与第三方生态。

## 适用场景

Python 被广泛应用于 Web 开发、数据分析、人工智能、自动化脚本等领域，是当下最流行的编程语言之一。
EOF
UPLOAD_JSON="$(curl -s -m 15 -X POST "$BASE/api/kbs/$KB_ID/documents/upload" \
    "${AUTH[@]}" \
    -F "file=@$TMP_DIR/verify_测试文档.txt")"
DOC_ID="$(jget "$UPLOAD_JSON" id)"
DOC_NAME="$(jget "$UPLOAD_JSON" name)"
if [ -n "$DOC_ID" ]; then
    ok "上传成功 doc_id=$DOC_ID（内部文件名 $DOC_NAME）"
else
    bad "上传失败: $UPLOAD_JSON"
    exit 1
fi

# ---------------------------------------------------------------------------
step "3. 触发入库并轮询状态（超时 60s）"
# ---------------------------------------------------------------------------
curl -s -m 10 -X POST "$BASE/api/kbs/$KB_ID/documents/$DOC_ID/ingest" \
    "${AUTH[@]}" > /dev/null || true
STATUS=""
ELAPSED=0
for i in $(seq 1 60); do
    DOC_JSON="$(curl -s -m 5 "${AUTH[@]}" "$BASE/api/kbs/$KB_ID/documents/$DOC_ID" || true)"
    STATUS="$(jget "$DOC_JSON" status)"
    [ "$STATUS" = "ingested" ] && break
    if [ "$STATUS" = "failed" ]; then
        bad "入库失败: $(jget "$DOC_JSON" error)"
        exit 1
    fi
    sleep 1
    ELAPSED=$i
done
if [ "$STATUS" = "ingested" ]; then
    ok "ingest 完成（${ELAPSED}s，chunk_count=$(jget "$DOC_JSON" chunk_count)，parse_method=$(jget "$DOC_JSON" parse_method)）"
else
    bad "入库超时（60s），最后状态: $STATUS"
    exit 1
fi

# ---------------------------------------------------------------------------
step "4. 检索验证（POST /api/chat/retrieve，断言 sources 非空）"
# ---------------------------------------------------------------------------
RETRIEVE_JSON="$(curl -s -m 15 -X POST "$BASE/api/chat/retrieve" \
    "${AUTH[@]}" \
    -H 'Content-Type: application/json' \
    -d "{\"kb_id\":\"$KB_ID\",\"query\":\"Python 是什么语言\"}")"
HIT_COUNT="$(echo "$RETRIEVE_JSON" | "$PY" -c 'import json,sys; print(len(json.load(sys.stdin).get("sources",[])))' 2>/dev/null || echo 0)"
if [ "${HIT_COUNT:-0}" -ge 1 ] 2>/dev/null; then
    ok "retrieve 命中 $HIT_COUNT 条 sources"
else
    bad "retrieve 未命中: $RETRIEVE_JSON"
fi

# ---------------------------------------------------------------------------
step "4.1 检索参数化（top_k 覆盖 + 实验参数传值不报错）"
# ---------------------------------------------------------------------------
T1_JSON="$(curl -s -m 15 -X POST "$BASE/api/chat/retrieve" \
    "${AUTH[@]}" \
    -H 'Content-Type: application/json' \
    -d "{\"kb_id\":\"$KB_ID\",\"query\":\"Python 语言特性\",\"top_k\":1}")"
T1_CNT="$(echo "$T1_JSON" | "$PY" -c 'import json,sys; print(len(json.load(sys.stdin).get("sources",[])))' 2>/dev/null || echo 0)"
if [ "${T1_CNT:-9}" -le 1 ] 2>/dev/null; then
    ok "top_k=1 → 返回 $T1_CNT 条（应 ≤1）"
else
    bad "top_k=1 返回 $T1_CNT 条（应 ≤1）: $T1_JSON"
fi
T3_JSON="$(curl -s -m 15 -X POST "$BASE/api/chat/retrieve" \
    "${AUTH[@]}" \
    -H 'Content-Type: application/json' \
    -d "{\"kb_id\":\"$KB_ID\",\"query\":\"Python 语言特性\",\"top_k\":3}")"
T3_CNT="$(echo "$T3_JSON" | "$PY" -c 'import json,sys; print(len(json.load(sys.stdin).get("sources",[])))' 2>/dev/null || echo 0)"
if [ "${T3_CNT:-0}" -ge 1 ] && [ "${T3_CNT:-9}" -le 3 ] 2>/dev/null; then
    ok "top_k=3 → 返回 $T3_CNT 条（1~3）"
else
    bad "top_k=3 返回 $T3_CNT 条（应 1~3）: $T3_JSON"
fi
EXP_CODE="$(curl -s -o "$TMP_DIR/exp.json" -w '%{http_code}' -m 15 -X POST \
    "$BASE/api/chat/retrieve" \
    "${AUTH[@]}" \
    -H 'Content-Type: application/json' \
    -d "{\"kb_id\":\"$KB_ID\",\"query\":\"Python\",\"enable_hybrid\":false,\"enable_rerank\":false,\"similarity_threshold\":0.0}")"
if [ "$EXP_CODE" = "200" ]; then
    ok "实验参数 enable_hybrid=false / enable_rerank=false / threshold=0 → HTTP 200"
else
    bad "实验参数检索 → HTTP $EXP_CODE: $(cat "$TMP_DIR/exp.json")"
fi

# ---------------------------------------------------------------------------
step "4.2 多库检索（kb_ids，第二个为空库）"
# ---------------------------------------------------------------------------
KB2_JSON="$(curl -s -m 10 -X POST "$BASE/api/kbs" \
    "${AUTH[@]}" \
    -H 'Content-Type: application/json' \
    -d '{"name":"verify-多库测试空库","description":"端到端验证多库检索用，用完即删"}')"
KB2_ID="$(jget "$KB2_JSON" id)"
if [ -z "$KB2_ID" ]; then
    bad "创建多库检索用空库失败: $KB2_JSON"
else
    MULTI_JSON="$(curl -s -m 15 -X POST "$BASE/api/chat/retrieve" \
        "${AUTH[@]}" \
        -H 'Content-Type: application/json' \
        -d "{\"kb_ids\":[\"$KB_ID\",\"$KB2_ID\"],\"query\":\"Python 是什么\",\"top_k\":3}")"
    MULTI_CNT="$(echo "$MULTI_JSON" | "$PY" -c 'import json,sys; print(len(json.load(sys.stdin).get("sources",[])))' 2>/dev/null || echo 0)"
    MULTI_KBNAME="$(jget "$MULTI_JSON" 0.kb_name)"
    if [ "${MULTI_CNT:-0}" -ge 1 ] 2>/dev/null; then
        ok "kb_ids 多库检索命中 $MULTI_CNT 条（首个来源 kb_name=$MULTI_KBNAME）"
    else
        bad "kb_ids 多库检索未命中: $MULTI_JSON"
    fi
    curl -s -m 5 -X DELETE "${AUTH[@]}" "$BASE/api/kbs/$KB2_ID" > /dev/null || true
fi

# ---------------------------------------------------------------------------
step "5. SSE 流式问答（POST /api/chat/stream，curl -N 实时）"
# ---------------------------------------------------------------------------
SSE_FILE="$TMP_DIR/sse.txt"
curl -s -N -m 120 -X POST "$BASE/api/chat/stream" \
    "${AUTH[@]}" \
    -H 'Content-Type: application/json' \
    -d "{\"kb_id\":\"$KB_ID\",\"query\":\"Python 有哪些主要特性\"}" > "$SSE_FILE" || true
HAS_META=no; HAS_DELTA=no; HAS_DONE=no
grep -q "event: meta" "$SSE_FILE" && HAS_META=yes || true
grep -q "event: delta" "$SSE_FILE" && HAS_DELTA=yes || true
grep -q "event: done" "$SSE_FILE" && HAS_DONE=yes || true
if [ "$HAS_META" = "yes" ] && [ "$HAS_DELTA" = "yes" ] && [ "$HAS_DONE" = "yes" ]; then
    ok "SSE 事件齐全（meta→delta→done）"
else
    bad "SSE 事件缺失 meta=$HAS_META delta=$HAS_DELTA done=$HAS_DONE（LLM 不可达时会走 error 事件）"
fi
# SSE 流式文件可能因数据块边界跨行，用 python 全文正则提取（12 位 hex）
SESSION_ID="$(cat "$SSE_FILE" | "$PY" -c '
import sys, re
m = re.search(r"session_id[^0-9a-f]*([0-9a-f]{12})", sys.stdin.read())
print(m.group(1) if m else "")
' 2>/dev/null || true)"

# ---------------------------------------------------------------------------
step "5.1 会话重命名 + 导出（依赖步骤 5 的 session_id）"
# ---------------------------------------------------------------------------
if [ -n "${SESSION_ID:-}" ]; then
    SESS_JSON="$(curl -s -m 5 "${AUTH[@]}" "$BASE/api/chat/history/$SESSION_ID" || true)"
    ORIG_TITLE="$(jget "$SESS_JSON" title)"
    RN_JSON="$(curl -s -m 5 -X POST "${AUTH[@]}" \
        "$BASE/api/chat/history/$SESSION_ID/rename" \
        -H 'Content-Type: application/json' \
        -d '{"title":"verify-重命名测试会话"}' || true)"
    RN_TITLE="$(jget "$RN_JSON" title)"
    if [ "$RN_TITLE" = "verify-重命名测试会话" ]; then
        ok "会话重命名生效 → title=$RN_TITLE"
        EXP_CODE="$(curl -s -o "$TMP_DIR/export.md" -D "$TMP_DIR/export.headers" \
            -w '%{http_code}' -m 10 \
            "${AUTH[@]}" "$BASE/api/chat/history/$SESSION_ID/export" || true)"
        EXP_HDR="$(grep -i 'content-disposition' "$TMP_DIR/export.headers" 2>/dev/null | head -1 || true)"
        MD_HEAD="$(head -1 "$TMP_DIR/export.md" 2>/dev/null || true)"
        if [ "$EXP_CODE" = "200" ] && echo "$EXP_HDR" | grep -qi 'attachment' \
            && echo "$MD_HEAD" | grep -q '^# '; then
            ok "导出 Markdown → HTTP 200 + attachment（首行: $MD_HEAD）"
        else
            bad "导出异常 code=$EXP_CODE header=${EXP_HDR:-无} 首行=${MD_HEAD:-空}"
        fi
        # 还原标题（保持步骤 8 按标题前缀清理会话的能力）
        curl -s -m 5 -X POST "${AUTH[@]}" \
            "$BASE/api/chat/history/$SESSION_ID/rename" \
            -H 'Content-Type: application/json' \
            -d "{\"title\":\"$ORIG_TITLE\"}" > /dev/null || true
    else
        bad "会话重命名失败: $RN_JSON"
    fi
else
    skip "无 session_id（SSE 流异常），跳过会话重命名/导出验证"
fi

# ---------------------------------------------------------------------------
step "6. 系统统计（GET /api/stats）"
# ---------------------------------------------------------------------------
STATS_JSON="$(curl -s -m 5 "${AUTH[@]}" "$BASE/api/stats" || true)"
STATS_CHUNK="$(jget "$STATS_JSON" chunk_count)"
if [ -n "$STATS_CHUNK" ] && [ "$STATS_CHUNK" -ge 1 ] 2>/dev/null; then
    ok "stats 正常 kb=$(jget "$STATS_JSON" kb_count) doc=$(jget "$STATS_JSON" doc_count) chunk=$STATS_CHUNK"
else
    bad "stats 异常: $STATS_JSON"
fi

# ---------------------------------------------------------------------------
step "7. RAGAS 探测（GET /api/stats/ragas，无论 available 与否都应 HTTP 200）"
# ---------------------------------------------------------------------------
RAGAS_CODE="$(curl -s -o "$TMP_DIR/ragas.json" -w '%{http_code}' -m 8 \
    "${AUTH[@]}" "$BASE/api/stats/ragas" || true)"
if [ "$RAGAS_CODE" = "200" ]; then
    RAGAS_AV="$(jget "$(cat "$TMP_DIR/ragas.json")" available)"
    ok "stats/ragas → HTTP 200（available=$RAGAS_AV）"
else
    bad "stats/ragas 返回 HTTP $RAGAS_CODE（应恒为 200）"
fi

# ---------------------------------------------------------------------------
step "7.1 检索质量统计（GET /api/stats/quality，近 30 天）"
# ---------------------------------------------------------------------------
QUAL_CODE="$(curl -s -o "$TMP_DIR/quality.json" -w '%{http_code}' -m 8 \
    "${AUTH[@]}" "$BASE/api/stats/quality?kb_id=$KB_ID" || true)"
QUAL_JSON="$(cat "$TMP_DIR/quality.json" 2>/dev/null || true)"
QUAL_WIN="$(jget "$QUAL_JSON" window_days)"
QUAL_TOTAL="$(jget "$QUAL_JSON" total_retrievals)"
if [ "$QUAL_CODE" = "200" ]; then
    ok "stats/quality → HTTP 200（window_days=$QUAL_WIN，total_retrievals=$QUAL_TOTAL）"
else
    bad "stats/quality → HTTP $QUAL_CODE（应 200）: $QUAL_JSON"
fi

# ---------------------------------------------------------------------------
step "7.2 审计日志（GET /api/audit/logs + /actions，仅 super_admin）"
# ---------------------------------------------------------------------------
AUDIT_CODE="$(curl -s -o "$TMP_DIR/audit.json" -w '%{http_code}' -m 8 \
    "${AUTH[@]}" "$BASE/api/audit/logs?page_size=5" || true)"
AUDIT_JSON="$(cat "$TMP_DIR/audit.json" 2>/dev/null || true)"
AUDIT_TOTAL="$(jget "$AUDIT_JSON" total)"
if [ "$AUDIT_CODE" = "200" ] && [ "${AUDIT_TOTAL:-0}" -ge 1 ] 2>/dev/null; then
    ok "audit/logs → HTTP 200（total=$AUDIT_TOTAL 条，本流程已产生审计）"
else
    bad "audit/logs → HTTP $AUDIT_CODE total=$AUDIT_TOTAL（应 200 且 ≥1）: $AUDIT_JSON"
fi
ACTIONS_JSON="$(curl -s -m 8 "${AUTH[@]}" "$BASE/api/audit/actions" || true)"
ACTIONS_CNT="$(echo "$ACTIONS_JSON" | "$PY" -c 'import json,sys; print(len(json.load(sys.stdin).get("actions",[])))' 2>/dev/null || echo 0)"
if [ "${ACTIONS_CNT:-0}" -ge 1 ] 2>/dev/null; then
    ok "audit/actions → $ACTIONS_CNT 个可选操作类型"
else
    bad "audit/actions 异常: $ACTIONS_JSON"
fi

# ---------------------------------------------------------------------------
step "7.3 标签设置 + 过滤（PUT /kbs/{id}/tags → 聚合 → ?tag= 过滤）"
# ---------------------------------------------------------------------------
TAG_SET_JSON="$(curl -s -m 8 -X PUT "${AUTH[@]}" "$BASE/api/kbs/$KB_ID/tags" \
    -H 'Content-Type: application/json' \
    -d '{"tags":["verify-标签","端到端"]}')"
if [ "$(jget "$TAG_SET_JSON" tags.0)" = "verify-标签" ]; then
    ok "PUT tags → 设置成功（$(jget "$TAG_SET_JSON" tags.0) / $(jget "$TAG_SET_JSON" tags.1)）"
    TAGS_JSON="$(curl -s -m 8 "${AUTH[@]}" "$BASE/api/kbs/tags" || true)"
    if echo "$TAGS_JSON" | grep -q 'verify-标签'; then
        ok "GET /kbs/tags 聚合包含 verify-标签"
    else
        bad "GET /kbs/tags 未见 verify-标签: $TAGS_JSON"
    fi
    FILTER_JSON="$(curl -s -m 8 -G "${AUTH[@]}" "$BASE/api/kbs" \
        --data-urlencode "tag=verify-标签" || true)"
    if echo "$FILTER_JSON" | grep -q "$KB_ID"; then
        ok "GET /kbs?tag=verify-标签 过滤命中本库"
    else
        bad "GET /kbs?tag= 过滤未命中本库: $FILTER_JSON"
    fi
    # 清理：清空标签，避免残留
    curl -s -m 8 -X PUT "${AUTH[@]}" "$BASE/api/kbs/$KB_ID/tags" \
        -H 'Content-Type: application/json' -d '{"tags":[]}' > /dev/null || true
else
    bad "PUT tags 失败: $TAG_SET_JSON"
fi

# ---------------------------------------------------------------------------
step "7.4 URL 网页导入（http://example.com，网络不可用自动跳过）"
# ---------------------------------------------------------------------------
URL_JSON="$(curl -s -m 30 -X POST "$BASE/api/kbs/$KB_ID/documents/from-url" \
    "${AUTH[@]}" \
    -H 'Content-Type: application/json' \
    -d '{"url":"http://example.com"}' || true)"
URL_DOC_ID="$(jget "$URL_JSON" id)"
if [ -n "$URL_DOC_ID" ]; then
    if [ "$(jget "$URL_JSON" file_type)" = "url" ]; then
        ok "URL 导入成功（file_type=url，文件名: $(jget "$URL_JSON" original_name)）"
    else
        bad "URL 导入 file_type=$(jget "$URL_JSON" file_type)（应 url）"
    fi
    # 清理：软删 → 彻底删除
    curl -s -m 8 -X DELETE "${AUTH[@]}" \
        "$BASE/api/kbs/$KB_ID/documents/$URL_DOC_ID" > /dev/null || true
    curl -s -m 8 -X POST "${AUTH[@]}" \
        "$BASE/api/kbs/$KB_ID/documents/$URL_DOC_ID/purge" > /dev/null || true
else
    skip "URL 导入不可用（无外网或 example.com 不可达），跳过: $(echo "$URL_JSON" | head -c 200)"
fi

# ---------------------------------------------------------------------------
step "7.5 图片代理 ?token= 鉴权（无图片文档则跳过）"
# ---------------------------------------------------------------------------
DOC_DETAIL="$(curl -s -m 8 "${AUTH[@]}" "$BASE/api/kbs/$KB_ID/documents/$DOC_ID" || true)"
IMG_REF="$(echo "$DOC_DETAIL" | grep -o '/api/files/images/[^)"[:space:]]*' | head -1 || true)"
if [ -n "$IMG_REF" ]; then
    IMG_CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 10 \
        "${BASE}${IMG_REF}?token=$TOKEN" || true)"
    if [ "$IMG_CODE" = "200" ]; then
        ok "图片代理 ?token= → HTTP 200（$IMG_REF）"
    else
        bad "图片代理 ?token= → HTTP $IMG_CODE（应 200）"
    fi
else
    skip "当前测试文档无解析图片（txt 无图），跳过图片代理验证"
fi

# ---------------------------------------------------------------------------
step "7.6 回收站（软删 → trash 列表 → restore → 列表恢复 → purge）"
# ---------------------------------------------------------------------------
DEL_JSON="$(curl -s -m 8 -X DELETE "${AUTH[@]}" \
    "$BASE/api/kbs/$KB_ID/documents/$DOC_ID" || true)"
if [ "$(jget "$DEL_JSON" message)" = "文档已移入回收站（可恢复）" ]; then
    ok "软删除 → 文档已移入回收站"
    TRASH_JSON="$(curl -s -m 8 "${AUTH[@]}" \
        "$BASE/api/kbs/$KB_ID/documents/trash" || true)"
    TRASH_DEL_AT="$(echo "$TRASH_JSON" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(next((x.get("deleted_at", "") for x in d if x.get("id") == sys.argv[1]), ""))
except Exception:
    pass
' "$DOC_ID" 2>/dev/null || true)"
    if echo "$TRASH_JSON" | grep -q "$DOC_ID"; then
        ok "trash 列表包含 doc_id=$DOC_ID（deleted_at=$TRASH_DEL_AT）"
    else
        bad "trash 列表未见 $DOC_ID: $TRASH_JSON"
    fi
    RESTORE_JSON="$(curl -s -m 8 -X POST "${AUTH[@]}" \
        "$BASE/api/kbs/$KB_ID/documents/$DOC_ID/restore" || true)"
    if [ "$(jget "$RESTORE_JSON" deleted)" = "false" ]; then
        ok "restore → 文档恢复（deleted=false）"
    else
        bad "restore 失败: $RESTORE_JSON"
    fi
    LIST_JSON="$(curl -s -m 8 "${AUTH[@]}" \
        "$BASE/api/kbs/$KB_ID/documents" || true)"
    if echo "$LIST_JSON" | grep -q "$DOC_ID"; then
        ok "恢复后文档列表已包含 $DOC_ID"
    else
        bad "恢复后列表未见 $DOC_ID"
    fi
    # 最终清理：软删 → purge 彻底删除
    curl -s -m 8 -X DELETE "${AUTH[@]}" \
        "$BASE/api/kbs/$KB_ID/documents/$DOC_ID" > /dev/null || true
    PURGE_JSON="$(curl -s -m 8 -X POST "${AUTH[@]}" \
        "$BASE/api/kbs/$KB_ID/documents/$DOC_ID/purge" || true)"
    if [ "$(jget "$PURGE_JSON" message)" = "文档已彻底删除" ]; then
        ok "purge → 文档已彻底删除"
    else
        bad "purge 失败: $PURGE_JSON"
    fi
else
    bad "软删除失败: $DEL_JSON"
fi

# ---------------------------------------------------------------------------
step "8. 清理（删除会话/文档/知识库，级联自检）"
# ---------------------------------------------------------------------------
# 兜底：删除本次对话产生的会话（按标题前缀匹配，即使 SSE 异常也删干净）
[ -n "${SESSION_ID:-}" ] && \
    curl -s -m 5 -X DELETE "${AUTH[@]}" "$BASE/api/chat/history/$SESSION_ID" > /dev/null || true
HISTORY_JSON="$(curl -s -m 5 "${AUTH[@]}" "$BASE/api/chat/history" || true)"
echo "$HISTORY_JSON" | "$PY" -c "
import json, sys
try:
    items = json.load(sys.stdin)
    for h in items:
        if h.get('title', '').startswith('Python 有哪些主要特性'):
            print(h['id'])
except Exception:
    pass
" > "$TMP_DIR/sessions.txt" || true
while IFS= read -r sid; do
    [ -n "$sid" ] && curl -s -m 5 -X DELETE "${AUTH[@]}" "$BASE/api/chat/history/$sid" > /dev/null || true
done < "$TMP_DIR/sessions.txt"

curl -s -m 5 -X DELETE "${AUTH[@]}" "$BASE/api/kbs/$KB_ID/documents/$DOC_ID" > /dev/null || true
curl -s -m 5 -X DELETE "${AUTH[@]}" "$BASE/api/kbs/$KB_ID" > /dev/null || true

KBS_AFTER="$(curl -s -m 5 "${AUTH[@]}" "$BASE/api/kbs" || true)"
KB_STILL="$(echo "$KBS_AFTER" | "$PY" -c "
import json, sys
kb_id = sys.argv[1]
try:
    ids = [k['id'] for k in json.load(sys.stdin)]
    print('yes' if kb_id in ids else 'no')
except Exception:
    print('unknown')
" "$KB_ID")"
if [ "$KB_STILL" = "no" ]; then
    ok "知识库已级联删除，临时数据清理完成"
else
    bad "知识库仍存在（$KB_STILL），请手动清理 kb_id=$KB_ID"
fi

# ---------------------------------------------------------------------------
step "9. 汇总"
# ---------------------------------------------------------------------------
echo
if [ "$FAIL" -eq 0 ]; then
    if [ "$SKIP" -gt 0 ]; then
        echo "🎉 端到端验证全部通过（$PASS 项通过，$SKIP 项跳过）"
    else
        echo "🎉 端到端验证全部通过（$PASS 项）"
    fi
    exit 0
else
    echo "❌ 端到端验证完成：$PASS 项通过，$FAIL 项失败（$SKIP 项跳过）"
    exit 1
fi
