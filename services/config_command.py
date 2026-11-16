from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..lib.client import OpenlistClient
from ..lib.config import (
    BOOLEAN_CONFIG_KEYS,
    CLEAR_EXTENSION_VALUES,
    CONFIG_ALIASES,
    EXTENSION_CONFIG_KEYS,
    GLOBAL_SETTABLE_CONFIG_KEYS,
    INTEGER_CONFIG_RULES,
    USER_SETTABLE_CONFIG_KEYS,
)
from .base import PluginService


class ConfigCommandService(PluginService):
    """ConfigCommand service."""

    HIDDEN_SHOW_KEYS = {
        "setup_completed",
        "default_openlist_url",
        "default_username",
        "default_password",
        "default_token",
        "autobackup_groups",
        "autobackup_private_users",
    }

    def _parse_and_validate_config_value(self, key: str, raw_value: str):
        """Returns (parsed_value, error_tip_or_none)."""
        if key in INTEGER_CONFIG_RULES:
            try:
                parsed = int(raw_value)
                min_value, max_value, error_message = INTEGER_CONFIG_RULES[key]
                if parsed < min_value or (max_value is not None and parsed > max_value):
                    return None, self._format_usage_tip(
                        error_message,
                        f"ol config set {key} <数字>",
                        [f"ol config set {key} {min_value}"],
                        "0 通常表示不限制；具体含义以该配置项说明为准。",
                    )
                return parsed, None
            except ValueError:
                return None, self._format_usage_tip(
                    f"{key} 必须是数字",
                    f"ol config set {key} <数字>",
                    [f"ol config set {key} 100"],
                )
        if key in BOOLEAN_CONFIG_KEYS:
            lowered = raw_value.strip().lower()
            if lowered in ("true", "1", "yes", "on", "开启", "启用"):
                return True, None
            if lowered in ("false", "0", "no", "off", "关闭", "禁用"):
                return False, None
            return None, self._format_usage_tip(
                f"{key} 必须是布尔值 (true/false)",
                f"ol config set {key} <true|false>",
                [f"ol config set {key} true", f"ol config set {key} false"],
            )
        if key in EXTENSION_CONFIG_KEYS:
            if raw_value.strip().lower() in CLEAR_EXTENSION_VALUES:
                return [], None
            exts = [ext.strip().lower() for ext in raw_value.split(",") if ext.strip()]
            exts = [ext if ext.startswith(".") else f".{ext}" for ext in exts]
            return exts, None
        return raw_value, None

    async def config_command(self, event: AstrMessageEvent, action: str = "show", key: str = "", value: str = ""):
        """配置 Openlist 连接与插件参数"""
        user_id = event.get_sender_id()
        action = (action or "show").strip().lower()
        global_cfg = self.get_global_config()
        require_auth = bool(global_cfg.get("require_user_auth", False))

        if action == "show":
            effective_config = self.get_user_config(user_id)
            if require_auth:
                config_text = f"📋 用户 {event.get_sender_name()} 的生效配置:\n\n"
            else:
                config_text = "📋 当前全局共享配置:\n\n"
            safe_config = effective_config.copy()
            if safe_config.get("password"):
                safe_config["password"] = "***"
            if safe_config.get("token"):
                safe_config["token"] = "***"
            for k, v in safe_config.items():
                if k not in self.HIDDEN_SHOW_KEYS and not k.startswith("_"):
                    config_text += f"🔹 {k}: {v}\n"
            default_url = global_cfg.get("openlist_url", "")
            if require_auth:
                config_text += "\n💡 提示: 当前为用户独立配置模式（修改仅对本人生效）"
                if default_url:
                    config_text += f"\n🌐 全局默认服务器: {default_url}"
            else:
                config_text += "\n💡 提示: 当前为全局共享模式（管理员可通过 ol config set 直接修改全局配置）"
            yield event.plain_result(config_text)

        elif action == "setup":
            mode_note = (
                "当前为全局共享模式（需管理员执行，设置将对所有用户生效）："
                if not require_auth
                else "当前为用户独立配置模式（设置仅对您个人生效）："
            )
            setup_text = f"""🛠️ Openlist配置向导
{mode_note}

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

💡 如果服务器不需要登录，只需要设置 openlist_url 即可"""
            yield event.plain_result(setup_text)

        elif action == "set":
            if not require_auth and not self._is_event_admin(event):
                yield event.plain_result(
                    "❌ 权限不足：当前为全局共享模式，只有管理员可以通过指令修改全局配置。"
                )
                return
            if not key:
                yield event.plain_result(self._format_usage_tip(
                    "缺少配置项名称",
                    "ol config set <配置项> <值>",
                    ["ol config set openlist_url http://your-server:5244", "ol config set max_upload_size 100"],
                    "发送 ol config show 可以查看当前配置。",
                ))
                return
            if value == "":
                yield event.plain_result(self._format_usage_tip(
                    "缺少配置项值",
                    f"ol config set {key} <值>",
                    [f"ol config set {key} 示例值"],
                    "如需清空扩展名限制，可使用 none、clear、all 或 不限制；个人模式下可用 ol config unset <配置项> 恢复继承。",
                ))
                return

            allowed_keys = GLOBAL_SETTABLE_CONFIG_KEYS if (not require_auth or self._is_event_admin(event)) else USER_SETTABLE_CONFIG_KEYS
            if key not in allowed_keys:
                yield event.plain_result(self._format_usage_tip(
                    f"未知或无权修改的配置项：{key}",
                    "ol config set <配置项> <值>",
                    [
                        "ol config set openlist_url http://127.0.0.1:5244",
                        "ol config set max_upload_size 100",
                        "ol config set allowed_extensions .jpg,.png,.mp4",
                        "ol config set enable_cache true",
                    ],
                    f"可用配置项：{', '.join(allowed_keys)}",
                ))
                return

            canonical_key = CONFIG_ALIASES.get(key, key)
            parsed_value, err_tip = self._parse_and_validate_config_value(canonical_key, value)
            if err_tip:
                yield event.plain_result(err_tip)
                return

            display_value = "***" if canonical_key in ("password", "token") else str(parsed_value)
            # 若为全局共享模式，或管理员显式修改全局专属配置项（如 require_user_auth），直接修改全局配置
            is_global_only_key = canonical_key in ("require_user_auth", "autobackup_default_path", "private_autobackup_default_path")
            if not require_auth or is_global_only_key:
                if not self._is_event_admin(event):
                    yield event.plain_result("❌ 权限不足：只有管理员可以修改全局配置项。")
                    return
                self.set_global_config_value(canonical_key, parsed_value)
                yield event.plain_result(f"✅ 已更新全局配置 {canonical_key} = {display_value}")
            else:
                user_manager = self.get_user_config_manager(user_id)
                user_manager.set_value(canonical_key, parsed_value)
                if canonical_key in ("openlist_url", "username", "password", "token"):
                    OpenlistClient.clear_token_cache()
                yield event.plain_result(f"✅ 已为用户 {event.get_sender_name()} 设置 {canonical_key} = {display_value}")

        elif action in ("unset", "reset"):
            if not key:
                yield event.plain_result(self._format_usage_tip(
                    "缺少要重置的配置项名称",
                    "ol config unset <配置项>",
                    ["ol config unset max_preview_size", "ol config unset allowed_extensions"],
                    "unset 用于移除个人覆盖配置，恢复继承全局配置。",
                ))
                return
            canonical_key = CONFIG_ALIASES.get(key, key)
            if not require_auth:
                if not self._is_event_admin(event):
                    yield event.plain_result("❌ 权限不足：当前为全局共享模式，只有管理员可以重置全局配置。")
                    return
                default_val = self.global_config_manager.default_config.get(canonical_key)
                if canonical_key not in self.global_config_manager.default_config:
                    yield event.plain_result(f"❌ 未知的配置项：{key}")
                    return
                self.set_global_config_value(canonical_key, default_val)
                yield event.plain_result(f"✅ 已将全局配置 {canonical_key} 重置为默认值: {default_val}")
            else:
                user_manager = self.get_user_config_manager(user_id)
                user_manager.unset_value(canonical_key)
                yield event.plain_result(f"✅ 已移除个人覆盖项 {canonical_key}，现已恢复继承全局配置。")

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
                        yield event.plain_result("❌ Openlist连接失败，请检查服务器地址、账号密码或 Token 配置")
            except Exception as e:
                logger.error(f"用户 {user_id} 连接测试失败: {e}, 服务器: {user_config.get('openlist_url')}", exc_info=True)
                yield event.plain_result(f"❌ 连接测试失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

        elif action == "clear_cache":
            user_config = self.get_user_config(user_id)
            auth_scope = self.cache_manager.build_auth_scope(user_config, fallback_user_id=user_id)
            self.cache_manager.clear_cache(user_id=user_id, auth_scope=auth_scope)
            yield event.plain_result("✅ 已清理当前 OpenList 文件列表缓存")

        else:
            yield event.plain_result(self._format_config_actions_tip(f"未知的配置操作：{action}"))
