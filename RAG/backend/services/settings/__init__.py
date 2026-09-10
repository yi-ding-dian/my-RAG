"""服务配置档案（多 profile 持久化 + 活跃切换 + 连接测试）

模块结构（原单文件按职责拆分而来，行为零变化）：
- service.py: 编排层（SettingsService 单例、档案增删改查、活跃切换）
- schema.py: 声明式 Schema（SECTION_SCHEMA 单一来源 / 白名单 / 密钥与类型辅助）
- merge.py: 聊天/LLM 配置字段级合并纯函数（无单例依赖）
- validate.py: 配置字段范围校验（update_profile 专用）
- connect_test.py: 档案连接测试 mixin（SettingsTester，被 SettingsService 继承）

调用方按具体子模块导入（如 `from backend.services.settings.service import
get_settings_service`）。
"""
