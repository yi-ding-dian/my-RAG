"""入库服务（解析 -> 切块 -> 向量化 -> 入库 的异步状态机）

模块结构（原 backend/services/ingestion_service.py 单文件按职责拆分而来，
行为零变化）：
- params.py: 参数校验与常量（resolve_parser_engine / _validate_pages /
  resolve_parser_config / 切块与解析参数的 _MIN_* _MAX_* 范围、_VALID_* 白名单）
- trace.py: 入库轨迹计时 mixin（_TraceMixin：阶段切换 / 轨迹落文档 / 清理）
- images.py: 解析图片上传与 markdown 引用重写 mixin（_ImageMixin）
- service.py: 主体（IngestionService：并发信号量 + 状态机编排 + 各 _stage_*），
  单例入口 get_ingestion_service

调用方按具体子模块导入（如 `from backend.services.ingestion.service import
get_ingestion_service`、`from backend.services.ingestion.params import
resolve_parser_config`）；轨迹/图片方法随 IngestionService 继承生效，不单独调用。
"""
