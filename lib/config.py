import json
import os
from typing import Dict, Set
from astrbot.api import logger
from astrbot.api.star import StarTools

PLUGIN_DATA_DIR_NAME = "openlist"

CONFIG_ALIASES = {
    "debug_upload_logging": "debug_transfer_logging",
    "default_openlist_url": "openlist_url",
    "default_username": "username",
    "default_password": "password",
    "default_token": "token",
}

USER_SETTABLE_CONFIG_KEYS = [
    "openlist_url", "username", "password", "token",
    "max_display_files", "public_openlist_url",
    "fixed_base_directory", "allowed_extensions", "max_preview_size", "text_preview_length",
    "enable_cache", "cache_duration", "max_download_size", "max_upload_size",
    "upload_retry_attempts", "upload_retry_delay",
    "upload_chunk_size_mb", "upload_progress_step_mb", "upstream_connect_timeout",
    "upstream_read_timeout", "openlist_connect_timeout", "openlist_upload_response_timeout",
    "debug_transfer_logging", "debug_upload_logging",
    "backup_default_path", "private_backup_default_path",
    "backup_allowed_extensions", "backup_max_size", "backup_skip_existing",
    "backup_retry_attempts", "backup_retry_delay",
]

GLOBAL_ONLY_SETTABLE_CONFIG_KEYS = [
    "require_user_auth",
    "autobackup_default_path",
    "private_autobackup_default_path",
    "default_openlist_url",
    "default_username",
    "default_password",
    "default_token",
]

GLOBAL_SETTABLE_CONFIG_KEYS = USER_SETTABLE_CONFIG_KEYS + [
    k for k in GLOBAL_ONLY_SETTABLE_CONFIG_KEYS if k not in USER_SETTABLE_CONFIG_KEYS
]

INTEGER_CONFIG_RULES = {
    "max_display_files": (1, 100, "max_display_files 必须在1-100之间"),
    "cache_duration": (1, None, "cache_duration 必须大于0"),
    "backup_max_size": (0, None, "backup_max_size 必须大于等于0"),
    "backup_retry_attempts": (1, None, "backup_retry_attempts 必须大于0"),
    "backup_retry_delay": (0, None, "backup_retry_delay 必须大于等于0"),
    "max_download_size": (0, None, "max_download_size 必须大于等于0"),
    "max_upload_size": (0, None, "max_upload_size 必须大于等于0"),
    "upload_retry_attempts": (1, None, "upload_retry_attempts 必须大于0"),
    "upload_retry_delay": (0, None, "upload_retry_delay 必须大于等于0"),
    "upload_chunk_size_mb": (1, None, "upload_chunk_size_mb 必须大于0"),
    "upload_progress_step_mb": (1, None, "upload_progress_step_mb 必须大于0"),
    "upstream_connect_timeout": (1, None, "upstream_connect_timeout 必须大于0"),
    "upstream_read_timeout": (1, None, "upstream_read_timeout 必须大于0"),
    "openlist_connect_timeout": (1, None, "openlist_connect_timeout 必须大于0"),
    "openlist_upload_response_timeout": (1, None, "openlist_upload_response_timeout 必须大于0"),
    "max_preview_size": (-1, None, "max_preview_size 必须大于等于 -1 (-1表示禁用, 0表示不限制)"),
    "text_preview_length": (1, None, "text_preview_length 必须大于0"),
}

BOOLEAN_CONFIG_KEYS = {
    "enable_cache",
    "debug_transfer_logging",
    "backup_skip_existing",
    "require_user_auth",
}

EXTENSION_CONFIG_KEYS = {
    "allowed_extensions",
    "backup_allowed_extensions",
}

CLEAR_EXTENSION_VALUES = {"none", "null", "empty", "clear", "all", "*", "空", "不限", "不限制"}
AUTOBACKUP_GROUPS_WEBUI_MIGRATION_KEY = "_autobackup_groups_webui_migrated"
EXPLICIT_KEYS_FIELD = "_explicit_keys"
GLOBAL_OVERRIDES_FIELD = "_global_overrides"

WEBUI_CONFIG_MAPPING = {
    "default_openlist_url": "openlist_url",
    "public_openlist_url": "public_openlist_url",
    "default_username": "username",
    "default_password": "password",
    "default_token": "token",
    "fixed_base_directory": "fixed_base_directory",
    "max_display_files": "max_display_files",
    "allowed_extensions": "allowed_extensions",
    "max_preview_size": "max_preview_size",
    "text_preview_length": "text_preview_length",
    "enable_cache": "enable_cache",
    "cache_duration": "cache_duration",
    "max_download_size": "max_download_size",
    "max_upload_size": "max_upload_size",
    "upload_retry_attempts": "upload_retry_attempts",
    "upload_retry_delay": "upload_retry_delay",
    "upload_chunk_size_mb": "upload_chunk_size_mb",
    "upload_progress_step_mb": "upload_progress_step_mb",
    "upstream_connect_timeout": "upstream_connect_timeout",
    "upstream_read_timeout": "upstream_read_timeout",
    "openlist_connect_timeout": "openlist_connect_timeout",
    "openlist_upload_response_timeout": "openlist_upload_response_timeout",
    "debug_transfer_logging": "debug_transfer_logging",
    "backup_default_path": "backup_default_path",
    "autobackup_default_path": "autobackup_default_path",
    "private_backup_default_path": "private_backup_default_path",
    "private_autobackup_default_path": "private_autobackup_default_path",
    "require_user_auth": "require_user_auth",
    "backup_allowed_extensions": "backup_allowed_extensions",
    "backup_max_size": "backup_max_size",
    "backup_skip_existing": "backup_skip_existing",
    "backup_retry_attempts": "backup_retry_attempts",
    "backup_retry_delay": "backup_retry_delay",
}

LOCAL_TO_WEBUI_CONFIG_MAPPING = {
    local_key: webui_key for webui_key, local_key in WEBUI_CONFIG_MAPPING.items()
}

GLOBAL_LEGACY_CONFIG_KEYS = {
    "default_openlist_url": "openlist_url",
    "default_username": "username",
    "default_password": "password",
    "default_token": "token",
}


def _safe_user_id_filename(user_id: str) -> str:
    raw = str(user_id or "").strip()
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in raw).strip("._")
    return safe or "unknown_user"


class UserConfigManager:
    """用户配置管理器 - 每个用户独立配置"""

    # 兼容 v1.1.2 旧版默认 max_preview_size=0 的遗留对比
    LEGACY_DEFAULTS = {
        "max_preview_size": 0,
    }

    def __init__(self, plugin_name: str, user_id: str):
        self.plugin_name = plugin_name
        self.user_id = str(user_id)
        self.config_dir = os.path.join(
            StarTools.get_data_dir(plugin_name), "users"
        )
        os.makedirs(self.config_dir, exist_ok=True)
        safe_name = _safe_user_id_filename(self.user_id)
        safe_file = os.path.join(self.config_dir, f"{safe_name}.json")
        legacy_file = (
            os.path.join(self.config_dir, f"{self.user_id}.json")
            if "/" not in self.user_id and "\\" not in self.user_id and ".." not in self.user_id
            else safe_file
        )
        self.config_file = legacy_file if os.path.exists(legacy_file) else safe_file
        self.default_config = {
            "openlist_url": "",
            "username": "",
            "password": "",
            "token": "",
            "public_openlist_url": "",
            "fixed_base_directory": "",
            "max_display_files": 20,
            "allowed_extensions": "",
            "max_preview_size": 10,
            "text_preview_length": 1000,
            "enable_cache": True,
            "cache_duration": 300,
            "max_download_size": 50,
            "max_upload_size": 100,
            "upload_retry_attempts": 3,
            "upload_retry_delay": 5,
            "upload_chunk_size_mb": 4,
            "upload_progress_step_mb": 64,
            "upstream_connect_timeout": 60,
            "upstream_read_timeout": 180,
            "openlist_connect_timeout": 30,
            "openlist_upload_response_timeout": 3000,
            "debug_transfer_logging": False,
            "backup_default_path": "/backup/group_{group_id}",
            "private_backup_default_path": "/backup/private_{user_id}",
            "backup_allowed_extensions": "",
            "backup_max_size": 0,
            "backup_skip_existing": True,
            "backup_retry_attempts": 3,
            "backup_retry_delay": 5,
            "setup_completed": False,
        }

    def load_raw_file(self) -> Dict:
        """读取原始用户配置 JSON，不混入默认值。"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.error(f"读取用户 {self.user_id} 原始配置失败: {e}")
        return {}

    def get_explicit_keys(self, raw_data: Dict = None) -> Set[str]:
        data = raw_data if raw_data is not None else self.load_raw_file()
        explicit = data.get(EXPLICIT_KEYS_FIELD)
        if isinstance(explicit, list):
            return {str(k) for k in explicit if str(k)}
        return set()

    def load_config(self) -> Dict:
        """从本地文件加载用户配置，若文件不存在则返回默认配置"""
        raw = self.load_raw_file()
        merged_config = self.default_config.copy()
        if raw:
            merged_config.update(raw)
        return merged_config

    def set_value(self, key: str, value):
        """显式设置单个用户配置项，并记录到 _explicit_keys 以确保可覆盖全局配置。"""
        raw = self.load_raw_file()
        explicit = self.get_explicit_keys(raw)
        explicit.add(key)
        raw[key] = value
        raw[EXPLICIT_KEYS_FIELD] = sorted(explicit)
        if key == "openlist_url" and value:
            raw["setup_completed"] = True
        self.save_config(raw)

    def unset_value(self, key: str) -> bool:
        """移除单个用户配置覆盖项，恢复继承全局配置。"""
        raw = self.load_raw_file()
        if not raw:
            return False
        explicit = self.get_explicit_keys(raw)
        existed = key in raw or key in explicit
        raw.pop(key, None)
        explicit.discard(key)
        raw[EXPLICIT_KEYS_FIELD] = sorted(explicit)
        self.save_config(raw)
        return existed

    def save_config(self, config: Dict):
        """将用户配置保存到本地文件"""
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存用户 {self.user_id} 配置失败: {e}")

    def is_configured(self) -> bool:
        """检查用户是否完成基础配置"""
        config = self.load_config()
        return config.get("setup_completed", False) and bool(config.get("openlist_url"))


class GlobalConfigManager:
    """全局配置管理器"""

    def __init__(self, plugin_name: str):
        self.config_dir = StarTools.get_data_dir(plugin_name)
        os.makedirs(self.config_dir, exist_ok=True)
        self.config_file = os.path.join(self.config_dir, "global_config.json")
        self.default_config = {
            "require_user_auth": False,
            "openlist_url": "",
            "default_openlist_url": "",
            "public_openlist_url": "",
            "username": "",
            "default_username": "",
            "password": "",
            "default_password": "",
            "token": "",
            "default_token": "",
            "fixed_base_directory": "",
            "max_display_files": 20,
            "allowed_extensions": "",
            "max_preview_size": 10,
            "text_preview_length": 1000,
            "enable_cache": True,
            "cache_duration": 300,
            "max_download_size": 50,
            "max_upload_size": 100,
            "upload_retry_attempts": 3,
            "upload_retry_delay": 5,
            "upload_chunk_size_mb": 4,
            "upload_progress_step_mb": 64,
            "upstream_connect_timeout": 60,
            "upstream_read_timeout": 180,
            "openlist_connect_timeout": 30,
            "openlist_upload_response_timeout": 3000,
            "debug_transfer_logging": False,
            "backup_default_path": "/backup/group_{group_id}",
            "autobackup_default_path": "/backup/group_{group_id}",
            "private_backup_default_path": "/backup/private_{user_id}",
            "private_autobackup_default_path": "/backup/private_{user_id}",
            "autobackup_groups": [],  # 由 ol autobackup 指令维护的自动备份群号列表
            "autobackup_private_users": [],  # 由 ol autobackup 指令维护的自动备份私聊用户列表
            "backup_allowed_extensions": "",
            "backup_max_size": 0,
            "backup_skip_existing": True,
            "backup_retry_attempts": 3,
            "backup_retry_delay": 5,
        }

    def load_raw_file(self) -> Dict:
        """读取原始全局配置 JSON，不混入默认值。"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.error(f"读取全局原始配置失败: {e}")
        return {}

    def load_config(self) -> Dict:
        """从本地文件加载全局配置，若文件不存在则返回默认配置"""
        raw = self.load_raw_file()
        merged_config = self.default_config.copy()
        if raw:
            merged_config.update(raw)
        return merged_config

    def save_runtime_fields(self, updates: Dict):
        """仅更新运行时状态字段，避免将全部静态默认值刷入磁盘遮蔽 WebUI。"""
        raw = self.load_raw_file()
        raw.update(updates)
        self.save_config(raw)

    def set_global_override(self, key: str, value):
        """保存通过聊天命令直接设置的全局配置覆盖项。"""
        raw = self.load_raw_file()
        overrides = raw.get(GLOBAL_OVERRIDES_FIELD)
        if not isinstance(overrides, dict):
            overrides = {}
        overrides[key] = value
        raw[GLOBAL_OVERRIDES_FIELD] = overrides
        raw[key] = value
        self.save_config(raw)

    def clear_global_override(self, key: str):
        """当已成功同步写入 WebUI AstrBotConfig 时，清理本地重复覆盖项以免遮蔽后续 WebUI 修改。"""
        raw = self.load_raw_file()
        overrides = raw.get(GLOBAL_OVERRIDES_FIELD)
        changed = False
        if isinstance(overrides, dict) and key in overrides:
            overrides.pop(key, None)
            raw[GLOBAL_OVERRIDES_FIELD] = overrides
            changed = True
        if key in raw and key not in ("autobackup_groups", "autobackup_private_users"):
            raw.pop(key, None)
            changed = True
        if changed:
            self.save_config(raw)

    def get_global_overrides(self, raw_data: Dict = None) -> Dict:
        data = raw_data if raw_data is not None else self.load_raw_file()
        overrides = data.get(GLOBAL_OVERRIDES_FIELD)
        return dict(overrides) if isinstance(overrides, dict) else {}

    def save_config(self, config: Dict):
        """将全局配置保存到本地文件"""
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存全局配置失败: {e}")
