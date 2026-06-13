from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..lib.config import (
    BOOLEAN_CONFIG_KEYS,
    CLEAR_EXTENSION_VALUES,
    CONFIG_ALIASES,
    EXTENSION_CONFIG_KEYS,
    INTEGER_CONFIG_RULES,
    USER_SETTABLE_CONFIG_KEYS,
)
from .base import PluginService


class ConfigCommandService(PluginService):
    """ConfigCommand service."""

    async def config_command(self, event: AstrMessageEvent, action: str = "show", key: str = "", value: str = ""):
        """配置 Openlist 连接与插件参数"""
        user_id = event.get_sender_id()
        if action == "show":
            user_config = self.get_user_config(user_id)
            config_text = f"📋 用户 {event.get_sender_name()} 的配置:\n\n"
            safe_config = user_config.copy()
            if safe_config.get("password"): safe_config["password"] = "***"
            if safe_config.get("token"): safe_config["token"] = "***"
            for k, v in safe_config.items():
                if k != "setup_completed" and not k.startswith("_"):
                    config_text += f"🔹 {k}: {v}\n"
            global_cfg = self.get_global_config()
            require_auth = global_cfg.get("require_user_auth", True)
            default_url = global_cfg.get("openlist_url", "")
            if require_auth:
                config_text += f"\n💡 提示: 当前启用了用户独立配置模式"
                if default_url: config_text += f"\n🌐 默认服务器: {default_url}"
            else:
                config_text += f"\n💡 提示: 当前使用全局配置模式"
            yield event.plain_result(config_text)
        elif action == "setup":
            setup_text = """🛠️ Openlist配置向导

请按以下步骤配置：

1️⃣ 设置Openlist服务器地址:
   ol config set openlist_url http://your-server:5244

2️⃣ 设置用户名(可选):
   ol config set username your_username

3️⃣ 设置密码(可选):
   ol config set password your_password

4️⃣ 测试连接:
   ol config test

5️⃣ 开始使用:
   ol ls /

💡 如果服务器不需要登录，只需要设置openlist_url即可"""
            yield event.plain_result(setup_text)
        elif action == "set":
            if not key:
                yield event.plain_result(self._format_usage_tip(
                    "缺少配置项名称",
                    "ol config set <配置项> <值>",
                    ["ol config set openlist_url http://your-server:5244", "ol config set max_upload_size 100"],
                    "发送 ol config show 可以查看当前配置。",
                ))
                return
            if not value:
                yield event.plain_result(self._format_usage_tip(
                    "缺少配置项值",
                    f"ol config set {key} <值>",
                    [f"ol config set {key} 示例值"],
                    "如需清空扩展名限制，可使用 none、clear、all 或 不限制。",
                ))
                return
            user_manager = self.get_user_config_manager(user_id)
            user_config = user_manager.load_config()
            if key not in USER_SETTABLE_CONFIG_KEYS:
                yield event.plain_result(self._format_usage_tip(
                    f"未知的配置项：{key}",
                    "ol config set <配置项> <值>",
                    [
                        "ol config set openlist_url http://127.0.0.1:5244",
                        "ol config set max_upload_size 100",
                        "ol config set allowed_extensions .jpg,.png,.mp4",
                        "ol config set enable_cache true",
                    ],
                    f"可用配置项：{', '.join(USER_SETTABLE_CONFIG_KEYS)}",
                ))
                return
            key = CONFIG_ALIASES.get(key, key)

            if key in INTEGER_CONFIG_RULES:
                try:
                    value = int(value)
                    min_value, max_value, error_message = INTEGER_CONFIG_RULES[key]
                    if value < min_value or (max_value is not None and value > max_value):
                        yield event.plain_result(self._format_usage_tip(
                            error_message,
                            f"ol config set {key} <数字>",
                            [f"ol config set {key} {min_value}"],
                            "0 通常表示不限制；具体含义以该配置项说明为准。",
                        ))
                        return
                except ValueError:
                    yield event.plain_result(self._format_usage_tip(
                        f"{key} 必须是数字",
                        f"ol config set {key} <数字>",
                        [f"ol config set {key} 100"],
                    ))
                    return
            elif key in BOOLEAN_CONFIG_KEYS:
                value = value.lower() in ["true", "1", "yes", "on"]
            elif key in EXTENSION_CONFIG_KEYS:
                # 允许输入逗号分隔的字符串，存为列表
                if isinstance(value, str):
                    if value.strip().lower() in CLEAR_EXTENSION_VALUES:
                        value = []
                    else:
                        value = [ext.strip().lower() for ext in value.split(",") if ext.strip()]
                    # 确保后缀带点
                    value = [ext if ext.startswith(".") else f".{ext}" for ext in value]

            user_config[key] = value
            if key == "openlist_url" and value:
                user_config["setup_completed"] = True
            user_manager.save_config(user_config)

            display_value = "***" if key in ["password", "token"] else str(value)
            yield event.plain_result(f"✅ 已为用户 {event.get_sender_name()} 设置 {key} = {display_value}")
        elif action == "test":
            user_config = self.get_user_config(user_id)
            if not self._validate_config(user_config):
                yield event.plain_result(self._format_usage_tip(
                    "尚未配置 OpenList 服务器地址",
                    "ol config set openlist_url <OpenList地址>",
                    [
                        "ol config set openlist_url http://127.0.0.1:5244",
                        "ol config setup",
                    ],
                    "配置完成后发送 ol config test 测试连接。",
                ))
                return
            try:
                async with self._create_openlist_client(user_config) as client:
                    files = await client.list_files("/")
                    if files is not None:
                        yield event.plain_result("✅ Openlist连接测试成功!")
                    else:
                        yield event.plain_result("❌ Openlist连接失败，请检查配置")
            except Exception as e:
                logger.error(f"用户 {user_id} 连接测试失败: {e}, 服务器: {user_config.get('openlist_url')}", exc_info=True)
                yield event.plain_result(f"❌ 连接测试失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
        elif action == "clear_cache":
            self.cache_manager.clear_cache(user_id)
            yield event.plain_result("✅ 已清理您的文件列表缓存")
        else:
            yield event.plain_result(self._format_config_actions_tip(f"未知的配置操作：{action}"))
