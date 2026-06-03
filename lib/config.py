import os
import json
from typing import Dict
from astrbot.api import logger
from astrbot.api.star import StarTools

CONFIG_ALIASES = {
    "debug_upload_logging": "debug_transfer_logging",
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
    "backup_default_path", "backup_allowed_extensions", "backup_max_size", "backup_skip_existing",
    "backup_retry_attempts", "backup_retry_delay",
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
}

EXTENSION_CONFIG_KEYS = {
    "allowed_extensions",
    "backup_allowed_extensions",
}

CLEAR_EXTENSION_VALUES = {"none", "null", "empty", "clear", "all", "*", "空", "不限", "不限制"}

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
    "require_user_auth": "require_user_auth",
    "autobackup_groups": "autobackup_groups",
    "backup_allowed_extensions": "backup_allowed_extensions",
    "backup_max_size": "backup_max_size",
    "backup_skip_existing": "backup_skip_existing",
    "backup_retry_attempts": "backup_retry_attempts",
    "backup_retry_delay": "backup_retry_delay",
}

GLOBAL_LEGACY_CONFIG_KEYS = {
    "default_openlist_url": "openlist_url",
    "default_username": "username",
    "default_password": "password",
    "default_token": "token",
}

class UserConfigManager:
    """用户配置管理器 - 每个用户独立配置"""

    def __init__(self, plugin_name: str, user_id: str):
        self.plugin_name = plugin_name
        self.user_id = user_id
        self.config_dir = os.path.join(
            StarTools.get_data_dir(plugin_name), "users"
        )
        os.makedirs(self.config_dir, exist_ok=True)
        self.config_file = os.path.join(self.config_dir, f"{user_id}.json")
        self.default_config = {
            "openlist_url": "",
            "username": "",
            "password": "",
            "token": "",
            "public_openlist_url": "",
            "fixed_base_directory": "",
            "max_display_files": 20,
            "allowed_extensions": "",
            "max_preview_size": 0,
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
            "backup_allowed_extensions": "",
            "backup_max_size": 0,
            "backup_skip_existing": True,
            "backup_retry_attempts": 3,
            "backup_retry_delay": 5,
            "setup_completed": False,
        }

    def load_config(self) -> Dict:
        """从本地文件加载用户配置，若文件不存在则返回默认配置"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                merged_config = self.default_config.copy()
                merged_config.update(config)
                return merged_config
            return self.default_config.copy()
        except Exception as e:
            logger.error(f"加载用户 {self.user_id} 配置失败: {e}")
            return self.default_config.copy()

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
            "default_openlist_url": "",
            "public_openlist_url": "",
            "default_username": "",
            "default_password": "",
            "default_token": "",
            "fixed_base_directory": "",
            "max_display_files": 20,
            "allowed_extensions": "",
            "max_preview_size": 0,
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
            "autobackup_groups": [], # 启用自动备份的群号列表
            "backup_allowed_extensions": "",
            "backup_max_size": 0,
            "backup_skip_existing": True,
            "backup_retry_attempts": 3,
            "backup_retry_delay": 5,
        }

    def load_config(self) -> Dict:
        """从本地文件加载全局配置，若文件不存在则返回默认配置"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                merged_config = self.default_config.copy()
                merged_config.update(config)
                return merged_config
            return self.default_config.copy()
        except Exception as e:
            logger.error(f"加载全局配置失败: {e}")
            return self.default_config.copy()

    def save_config(self, config: Dict):
        """将全局配置保存到本地文件"""
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存全局配置失败: {e}")
