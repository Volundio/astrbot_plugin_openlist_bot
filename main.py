import asyncio
import json
import os
import posixpath
import time
import uuid
from typing import List, Dict, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.message_components import File
from astrbot.api import logger
from .lib.client import OpenlistClient
from .lib.config import (
    EXTENSION_CONFIG_KEYS,
    GLOBAL_LEGACY_CONFIG_KEYS,
    WEBUI_CONFIG_MAPPING,
    UserConfigManager,
    GlobalConfigManager,
)
from .lib.cache import CacheManager
from .services import BackupService, BrowseService, ConfigCommandService, DownloadService, HelpService, PreviewService, RestoreService, UploadService


class OpenlistPlugin(Star):
    LEGACY_ALLOWED_EXTENSIONS = {
        ".txt", ".pdf", ".doc", ".docx", ".zip", ".rar", ".jpg", ".png", ".gif", ".mp4", ".mp3"
    }

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.user_config_managers = {}
        self.config = config
        self.global_config_manager = GlobalConfigManager("openlist")
        self.cache_manager = CacheManager("openlist")
        self.user_navigation_state = {}
        self.upload_service = UploadService(self)
        self.download_service = DownloadService(self)
        self.backup_service = BackupService(self)
        self.browse_service = BrowseService(self)
        self.config_command_service = ConfigCommandService(self)
        self.restore_service = RestoreService(self)
        self.preview_service = PreviewService(self)
        self.help_service = HelpService(self)
        self.autobackup_semaphore = asyncio.Semaphore(2)

    def get_webui_config(self, key: str, default=None):
        """获取WebUI配置项"""
        if self.config:
            return self.config.get("global_settings", {}).get(key, default)
        return default

    def get_global_config(self) -> Dict:
        """获取整合后的全局配置（WebUI + global_config.json）"""
        # 直接加载本地配置
        config = self.global_config_manager.load_config()

        defaults = self.global_config_manager.default_config
        for webui_key, local_key in WEBUI_CONFIG_MAPPING.items():
            webui_val = self.get_webui_config(webui_key)
            if webui_val is not None:
                # 如果是列表（autobackup_groups），合并
                if isinstance(webui_val, list) and local_key == "autobackup_groups":
                    local_val = config.get(local_key, [])
                    if not isinstance(local_val, list):
                        local_val = []
                    # 简单的去重合并
                    combined = [str(item).strip() for item in local_val if str(item).strip()]
                    existing_gids = {item.split(":", 1)[0] for item in combined}
                    for item in webui_val:
                        item = str(item).strip()
                        if not item:
                            continue
                        gid = item.split(":", 1)[0]
                        if gid not in existing_gids:
                            combined.append(item)
                            existing_gids.add(gid)
                    config[local_key] = combined
                # 其他项，只有当本地配置为空或仍为默认值时才使用 WebUI
                else:
                    current_val = config.get(local_key)
                    default_val = defaults.get(local_key, defaults.get(webui_key))
                    if current_val in (None, "") or current_val == default_val:
                        config[local_key] = webui_val

        # 兼容旧版 global_config.json 中的 default_* 字段
        for legacy_key, local_key in GLOBAL_LEGACY_CONFIG_KEYS.items():
            if not config.get(local_key) and config.get(legacy_key):
                config[local_key] = config[legacy_key]

        # 统一将扩展名字符串转为列表
        for key in EXTENSION_CONFIG_KEYS:
            if isinstance(config.get(key), str):
                config[key] = [ext.strip().lower() for ext in config[key].split(",") if ext.strip()]
                config[key] = [ext if ext.startswith(".") else f".{ext}" for ext in config[key]]

        return config

    def _get_size_limit_mb(self, user_config: Dict, key: str, default: int) -> int:
        """读取大小限制配置；0 表示不限制。"""
        try:
            value = int(user_config.get(key, default))
        except (TypeError, ValueError):
            logger.warning(f"配置 {key} 的值无效: {user_config.get(key)!r}，已使用默认值 {default}MB")
            return default
        if value < 0:
            logger.warning(f"配置 {key} 的值不能为负数: {value}，已使用默认值 {default}MB")
            return default
        return value

    def _get_cache_duration_seconds(self, user_config: Dict) -> int:
        """读取缓存有效期，单位秒。"""
        try:
            duration = int(user_config.get("cache_duration", 300))
        except (TypeError, ValueError):
            logger.warning(f"配置 cache_duration 的值无效: {user_config.get('cache_duration')!r}，已使用默认值 300 秒")
            return 300
        if duration < 1:
            logger.warning(f"配置 cache_duration 的值过小: {duration}，已使用默认值 300 秒")
            return 300
        return duration

    def _get_positive_int_config(self, user_config: Dict, key: str, default: int, minimum: int = 1) -> int:
        """读取正整数配置。"""
        try:
            value = int(user_config.get(key, default))
        except (TypeError, ValueError):
            logger.warning(f"配置 {key} 的值无效: {user_config.get(key)!r}，已使用默认值 {default}")
            return default
        if value < minimum:
            logger.warning(f"配置 {key} 的值过小: {value}，已使用默认值 {default}")
            return default
        return value

    def _get_bool_config(self, user_config: Dict, key: str, default: bool = False) -> bool:
        """读取布尔配置。"""
        value = user_config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)

    def _get_transfer_config(self, user_config: Dict) -> Dict:
        """读取上传/中转传输调优配置。"""
        mb = 1024 * 1024
        return {
            "upload_chunk_size": self._get_positive_int_config(user_config, "upload_chunk_size_mb", 4) * mb,
            "upload_progress_step": self._get_positive_int_config(user_config, "upload_progress_step_mb", 64) * mb,
            "upstream_connect_timeout": self._get_positive_int_config(user_config, "upstream_connect_timeout", 60),
            "upstream_read_timeout": self._get_positive_int_config(user_config, "upstream_read_timeout", 180),
            "openlist_connect_timeout": self._get_positive_int_config(user_config, "openlist_connect_timeout", 30),
            "openlist_upload_response_timeout": self._get_positive_int_config(user_config, "openlist_upload_response_timeout", 3000),
            "debug_transfer_logging": self._get_bool_config(user_config, "debug_transfer_logging", False),
        }

    def _create_openlist_client(self, user_config: Dict) -> OpenlistClient:
        """基于用户配置创建 OpenList 客户端。"""
        return OpenlistClient(
            user_config["openlist_url"],
            user_config.get("public_openlist_url", ""),
            user_config.get("username", ""),
            user_config.get("password", ""),
            user_config.get("token", ""),
            user_config.get("fixed_base_directory", ""),
            transfer_config=self._get_transfer_config(user_config),
        )

    def _get_retry_config(self, user_config: Dict, prefix: str) -> tuple:
        """读取重试配置；attempts 包含首次尝试。"""
        return (
            self._get_positive_int_config(user_config, f"{prefix}_retry_attempts", 3),
            self._get_positive_int_config(user_config, f"{prefix}_retry_delay", 5, minimum=0),
        )

    async def _upload_file_with_retry(
        self,
        client: OpenlistClient,
        file_path: str,
        target_path: str,
        file_name: str,
        user_config: Dict,
    ) -> bool:
        """本地文件上传自动重试。"""
        return await self.upload_service._upload_file_with_retry(client, file_path, target_path, file_name, user_config)

    async def _upload_url_stream_with_retry(
        self,
        client: OpenlistClient,
        source_url: str,
        target_path: str,
        file_name: str,
        file_size: Optional[int],
        user_config: Dict,
        refresh_url=None,
    ) -> bool:
        """URL 中转上传自动重试；可在重试时刷新平台文件 URL。"""
        return await self.upload_service._upload_url_stream_with_retry(client, source_url, target_path, file_name, file_size, user_config, refresh_url)

    def _get_extension_filter(self, user_config: Dict, key: str = "allowed_extensions") -> List[str]:
        """读取扩展名过滤配置；空列表表示不限制。"""
        value = user_config.get(key, [])
        if isinstance(value, str):
            extensions = [ext.strip().lower() for ext in value.split(",") if ext.strip()]
        elif isinstance(value, list):
            extensions = [str(ext).strip().lower() for ext in value if str(ext).strip()]
        else:
            return []
        extensions = [ext if ext.startswith(".") else f".{ext}" for ext in extensions]
        if key == "allowed_extensions" and set(extensions) == self.LEGACY_ALLOWED_EXTENSIONS:
            return []
        return extensions

    def _is_extension_allowed(self, filename: str, user_config: Dict, key: str = "allowed_extensions") -> bool:
        """判断文件扩展名是否通过配置过滤。"""
        allowed_exts = self._get_extension_filter(user_config, key)
        if not allowed_exts:
            return True
        return os.path.splitext((filename or "").lower())[1] in allowed_exts

    def _format_extension_filter(self, user_config: Dict, key: str = "allowed_extensions") -> str:
        allowed_exts = self._get_extension_filter(user_config, key)
        return ", ".join(allowed_exts) if allowed_exts else "不限制"

    def _is_admin_role(self, role) -> bool:
        """兼容 AstrBot/适配器可能返回的数字或字符串群角色。"""
        if role is None:
            return False
        for attr in ("name", "value"):
            attr_value = getattr(role, attr, None)
            if attr_value is not None and attr_value is not role:
                if self._is_admin_role(attr_value):
                    return True
        if isinstance(role, str):
            role_text = role.strip().lower()
            if "." in role_text:
                role_text = role_text.rsplit(".", 1)[-1]
            if role_text in ("owner", "admin", "administrator", "superuser", "super_admin", "root", "群主", "管理员"):
                return True
            if role_text in ("member", "normal", "user", "guest", "成员", "群员", "普通用户"):
                return False
            try:
                return int(role_text) >= 2
            except ValueError:
                return False
        try:
            return int(role) >= 2
        except (TypeError, ValueError):
            return False

    def _read_value(self, obj, key: str, default=None):
        """从对象或映射中读取字段，兼容适配器原始事件对象。"""
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        value = getattr(obj, key, default)
        if value is not default:
            return value
        try:
            return obj[key]
        except Exception:
            return default

    def _get_event_group_id(self, event: AstrMessageEvent):
        """从 AstrBot 事件或平台原始事件中读取群号。"""
        message_obj = getattr(event, "message_obj", None)
        group_id = getattr(message_obj, "group_id", None)
        if group_id not in (None, ""):
            return group_id

        raw_message = self._read_value(message_obj, "raw_message")
        return self._read_value(raw_message, "group_id")

    def _get_navigation_state_key(self, event: AstrMessageEvent) -> str:
        """按会话隔离导航状态，避免同一用户在不同群/私聊串列表序号。"""
        user_id = event.get_sender_id()
        group_id = self._get_event_group_id(event)
        if group_id not in (None, ""):
            return f"group:{group_id}:user:{user_id}"
        return f"private:user:{user_id}"

    async def _get_group_member_role(self, event: AstrMessageEvent, group_id, user_id=None):
        """通过 OneBot 查询指定用户在目标群的角色。"""
        user_id = user_id or event.get_sender_id()
        try:
            member_info = await event.bot.api.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
        except Exception as e:
            logger.warning(f"查询目标群成员权限失败: group={group_id}, user={user_id}, err={e}")
            return None

        if not isinstance(member_info, dict):
            return None
        return member_info.get("role") or member_info.get("permission")

    async def _has_target_group_permission(self, event: AstrMessageEvent, group_id) -> bool:
        """允许当前群直接操作；跨群/私聊指定群时要求目标群群主或管理员。"""
        current_group_id = self._get_event_group_id(event)
        if current_group_id not in (None, "") and str(current_group_id) == str(group_id):
            return True

        role = await self._get_group_member_role(event, group_id)
        return self._is_admin_role(role)

    async def _deny_if_no_target_group_permission(self, event: AstrMessageEvent, group_id, action_name: str) -> bool:
        """返回 True 表示权限不足并已记录日志。"""
        if await self._has_target_group_permission(event, group_id):
            return False

        logger.warning(
            f"{action_name}目标群权限不足: user={event.get_sender_id()}, "
            f"current_group={self._get_event_group_id(event)}, target_group={group_id}"
        )
        return True

    def _extract_sender_role(self, event: AstrMessageEvent):
        """尽量从 AstrBot 事件和平台原始事件中提取发送者群角色。"""
        candidates = []

        role = getattr(event, "role", None)
        if role is not None:
            candidates.append(role)

        message_obj = getattr(event, "message_obj", None)
        sender = self._read_value(message_obj, "sender")
        for key in ("role", "permission"):
            value = self._read_value(sender, key)
            if value is not None:
                candidates.append(value)

        raw_message = self._read_value(message_obj, "raw_message")
        raw_sender = self._read_value(raw_message, "sender")
        for key in ("role", "permission"):
            value = self._read_value(raw_sender, key)
            if value is not None:
                candidates.append(value)
        raw_role = self._read_value(raw_message, "role")
        if raw_role is not None:
            candidates.append(raw_role)

        for candidate in candidates:
            if candidate not in (None, ""):
                return candidate
        return None

    def _is_event_admin(self, event: AstrMessageEvent) -> bool:
        """判断事件发送者是否为管理员，优先使用 AstrBot 能力，再回退到平台原始角色。"""
        is_admin = getattr(event, "is_admin", None)
        if callable(is_admin):
            try:
                if is_admin():
                    return True
            except Exception as e:
                logger.debug(f"调用 event.is_admin() 失败，继续使用角色字段判断: {e}")

        return self._is_admin_role(self._extract_sender_role(event))

    async def initialize(self):
        """插件初始化"""
        logger.info("Openlist文件管理插件已加载")
        global_cfg = self.get_global_config()
        default_url = global_cfg.get("openlist_url", "")
        require_auth = global_cfg.get("require_user_auth", True)
        if not default_url and not require_auth:
            logger.warning("Openlist URL未配置，请使用 /ol config 命令配置或在WebUI中配置")

    def get_user_config_manager(self, user_id: str) -> UserConfigManager:
        """获取用户配置管理器"""
        if user_id not in self.user_config_managers:
            self.user_config_managers[user_id] = UserConfigManager("openlist", user_id)
        return self.user_config_managers[user_id]

    def get_user_config(self, user_id: str) -> Dict:
        """获取用户配置"""
        global_cfg = self.get_global_config()
        if not global_cfg.get("require_user_auth", True):
            return global_cfg

        user_config = self.get_user_config_manager(user_id).load_config()

        # 简单的合并：用户配置优先，如果用户配置为空则使用全局配置
        final_cfg = global_cfg.copy()
        for k, v in user_config.items():
            default_val = self.get_user_config_manager(user_id).default_config.get(k)
            is_default_value = v == default_val
            if k == "allowed_extensions":
                if isinstance(v, str):
                    normalized_exts = [ext.strip().lower() for ext in v.split(",") if ext.strip()]
                elif isinstance(v, list):
                    normalized_exts = [str(ext).strip().lower() for ext in v if str(ext).strip()]
                else:
                    normalized_exts = []
                normalized_exts = [ext if ext.startswith(".") else f".{ext}" for ext in normalized_exts]
                if set(normalized_exts) == self.LEGACY_ALLOWED_EXTENSIONS:
                    is_default_value = True
            # 只要用户设置了非默认值，就覆盖全局；允许 0/False/[] 这类有效配置值。
            if not is_default_value:
                final_cfg[k] = v

        return final_cfg

    def _validate_config(self, user_config: Dict) -> bool:
        """验证配置是否有效"""
        return bool(user_config.get("openlist_url"))

    def _get_user_navigation_state(self, user_id: str) -> Dict:
        """获取用户导航状态"""
        if user_id not in self.user_navigation_state:
            self.user_navigation_state[user_id] = {
                "current_path": "/",
                "items": [],
                "parent_paths": [],
                "current_page": 1,
            }
        return self.user_navigation_state[user_id]

    def _update_user_navigation_state(self, user_id: str, path: str, items: List[Dict]):
        """更新用户导航状态"""
        nav_state = self._get_user_navigation_state(user_id)
        if path != nav_state["current_path"]:
            if self._is_forward_navigation(nav_state["current_path"], path):
                nav_state["parent_paths"].append(nav_state["current_path"])
            nav_state["current_path"] = path
            nav_state["current_page"] = 1
        nav_state["items"] = items

    def _is_forward_navigation(self, current_path: str, new_path: str) -> bool:
        """判断是否是前进导航"""
        current = current_path.rstrip("/")
        new = new_path.rstrip("/")
        return new.startswith(current + "/") if current != "/" else new.startswith("/")

    def _get_item_by_number(self, user_id: str, number: int) -> Optional[Dict]:
        """根据序号获取文件或目录项"""
        nav_state = self._get_user_navigation_state(user_id)
        items = nav_state.get("items")
        if items and 1 <= number <= len(items):
            return items[number - 1]
        return None

    def _get_backup_retry_key(self, event: AstrMessageEvent) -> str:
        """按会话和用户定位最近一次手动备份失败项。"""
        user_id = event.get_sender_id()
        message_obj = getattr(event, "message_obj", None)
        group_id = getattr(message_obj, "group_id", None)
        if group_id:
            return f"group:{group_id}:user:{user_id}"
        return f"private:user:{user_id}"

    def _get_backup_retry_file(self, retry_key: str) -> str:
        """返回备份失败清单临时文件路径。"""
        safe_key = "".join(c if c.isalnum() or c in "._-" else "_" for c in retry_key)
        retry_dir = os.path.join(StarTools.get_data_dir("openlist"), "backup_retry")
        os.makedirs(retry_dir, exist_ok=True)
        return os.path.join(retry_dir, f"{safe_key}.json")

    def _load_backup_retry_state(self, retry_key: str) -> Optional[Dict]:
        """读取最近一次备份失败清单。"""
        retry_file = self._get_backup_retry_file(retry_key)
        try:
            if not os.path.exists(retry_file):
                return None
            with open(retry_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            return state if isinstance(state, dict) else None
        except Exception as e:
            logger.warning(f"读取备份失败清单失败: {retry_file}, err={e}")
            return None

    def _save_backup_retry_state(self, retry_key: str, state: Dict):
        """写入备份失败清单临时文件。"""
        retry_file = self._get_backup_retry_file(retry_key)
        try:
            with open(retry_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存备份失败清单失败: {retry_file}, err={e}")

    def _delete_backup_retry_state(self, retry_key: str):
        """删除备份失败清单临时文件。"""
        retry_file = self._get_backup_retry_file(retry_key)
        try:
            if os.path.exists(retry_file):
                os.remove(retry_file)
        except OSError as e:
            logger.warning(f"删除备份失败清单失败: {retry_file}, err={e}")

    def _normalize_openlist_path(self, path: str) -> str:
        """标准化 OpenList 路径，统一为以 / 开头的绝对路径。"""
        normalized = (path or "").strip().replace("\\", "/")
        if not normalized:
            return "/"
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        normalized = posixpath.normpath(normalized)
        if normalized in ("", "."):
            return "/"
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        return normalized

    def _resolve_target_path(self, user_id: str, path: str, default_to_current: bool = True) -> str:
        """将目标路径解析为 OpenList 绝对路径，支持当前目录相对路径。"""
        raw_path = (path or "").strip()
        current_path = self._get_user_navigation_state(user_id)["current_path"]
        if not isinstance(current_path, str) or not current_path.startswith("/"):
            current_path = "/"

        if not raw_path:
            if default_to_current:
                return self._normalize_openlist_path(current_path)
            return "/"

        if raw_path.startswith("/"):
            return self._normalize_openlist_path(raw_path)

        current_path = self._normalize_openlist_path(current_path)
        return self._normalize_openlist_path(f"{current_path.rstrip('/')}/{raw_path}")

    def _resolve_path_candidates(self, user_id: str, path: str, default_to_current: bool = True) -> List[str]:
        """生成候选路径: 先当前目录相对路径，再尝试根目录路径（用于兼容旧用法）。"""
        raw_path = (path or "").strip()
        primary_path = self._resolve_target_path(user_id, raw_path, default_to_current=default_to_current)
        candidates = [primary_path]
        if raw_path and not raw_path.startswith("/"):
            root_path = self._normalize_openlist_path(raw_path)
            if root_path not in candidates:
                candidates.append(root_path)
        return candidates

    def _strip_fixed_base_directory(self, path: str, user_config: Dict) -> str:
        """从 OpenList 返回路径中剥离下载链接前缀，得到用户视角路径。"""
        path = self._normalize_openlist_path(path)
        fixed_base_dir = self._normalize_openlist_path(user_config.get("fixed_base_directory", ""))
        if fixed_base_dir != "/" and (path == fixed_base_dir or path.startswith(fixed_base_dir + "/")):
            path = path[len(fixed_base_dir):]
            if not path:
                return "/"
            if not path.startswith("/"):
                path = "/" + path
        return self._normalize_openlist_path(path)

    def _get_item_full_path(self, user_id: str, item: Dict, user_config: Dict) -> str:
        """根据列表项生成 OpenList 绝对路径，兼容普通列表和搜索结果。"""
        item_name = item.get("name", "")
        parent_path = item.get("parent")
        if parent_path:
            parent_path = self._strip_fixed_base_directory(parent_path, user_config)
            return self._normalize_openlist_path(f"{parent_path.rstrip('/')}/{item_name}")

        current_path = self._get_user_navigation_state(user_id).get("current_path", "/")
        if not isinstance(current_path, str) or not current_path.startswith("/"):
            current_path = "/"
        return self._normalize_openlist_path(f"{current_path.rstrip('/')}/{item_name}")

    def _format_file_size(self, size: int) -> str:
        """格式化文件大小"""
        if size < 1024: return f"{size}B"
        elif size < 1024 * 1024: return f"{size / 1024:.1f}KB"
        elif size < 1024 * 1024 * 1024: return f"{size / (1024 * 1024):.1f}MB"
        else: return f"{size / (1024 * 1024 * 1024):.1f}GB"

    def _sanitize_filename(self, filename: str, fallback: str = "file") -> str:
        """生成可用于临时附件名的文件名片段。"""
        safe_name = "".join(c for c in (filename or "") if c.isalnum() or c in "._- ").strip(" .")
        return (safe_name[:100] or fallback)

    def _unique_suffix(self) -> str:
        """生成临时文件名后缀，避免同一秒内并发请求撞名。"""
        return f"{time.time_ns()}_{uuid.uuid4().hex[:12]}"

    def _render_backup_path(self, path_template: str, group_id) -> str:
        """渲染备份目录模板，支持 {group_id}、{gid}、{group} 占位符。"""
        group_id = str(group_id)
        template = (path_template or "").strip() or f"/backup/group_{group_id}"
        rendered = (
            template
            .replace("{group_id}", group_id)
            .replace("{gid}", group_id)
            .replace("{group}", group_id)
        )
        return self._normalize_openlist_path(rendered)

    def _get_autobackup_target_path(self, global_cfg: Dict, group_id: str) -> Optional[str]:
        """从自动备份群配置中解析目标路径。"""
        group_id = str(group_id)
        default_path = global_cfg.get("autobackup_default_path", "/backup/group_{group_id}")
        for item in global_cfg.get("autobackup_groups", []):
            if not isinstance(item, str):
                continue
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                gid, path = item.split(":", 1)
                gid = gid.strip()
                path = path.strip()
            else:
                gid = item
                path = ""
            if gid == group_id:
                return self._render_backup_path(path or default_path, group_id)
        return None

    async def _cleanup_temp_file(self, file_path: str, delay: int = 10):
        """延迟清理已发送的临时文件。"""
        await asyncio.sleep(delay)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError as e:
            logger.debug(f"清理临时文件失败: {file_path}, err={e}")

    def _normalize_download_headers(self, headers: Dict) -> Dict[str, str]:
        """将 OpenList link.header 转为 aiohttp 可用的单值请求头。"""
        normalized = {}
        if not isinstance(headers, dict):
            return normalized
        for key, value in headers.items():
            if value is None:
                continue
            if isinstance(value, list):
                values = [str(v) for v in value if v is not None]
                if not values:
                    continue
                normalized[key] = "; ".join(values) if key.lower() == "cookie" else ",".join(values)
            else:
                normalized[key] = str(value)
        return normalized

    async def _send_download_link_txt(
        self,
        event: AstrMessageEvent,
        file_name: str,
        file_size: int,
        file_path: str,
        download_url: str,
    ):
        """将下载链接写入 txt 附件发送，避免长文本被平台转为图片。"""
        links_dir = os.path.join(StarTools.get_data_dir("openlist"), "links")
        os.makedirs(links_dir, exist_ok=True)
        safe_base = self._sanitize_filename(file_name, "download")
        attachment_name = f"{safe_base}_download_link.txt"
        temp_file_path = os.path.join(
            links_dir,
            f"{event.get_sender_id()}_{self._unique_suffix()}_{attachment_name}",
        )
        content = (
            "OpenList 下载链接\n\n"
            f"文件: {file_name}\n"
            f"路径: {file_path}\n"
            f"大小: {self._format_file_size(file_size)}\n"
            f"链接: {download_url}\n"
        )
        with open(temp_file_path, "w", encoding="utf-8") as f:
            f.write(content)

        yield event.plain_result(f"✅ 已获取下载链接，正在作为 txt 文件发送: {file_name}")
        yield event.chain_result([File(name=attachment_name, file=temp_file_path)])
        asyncio.create_task(self._cleanup_temp_file(temp_file_path))

    def _format_file_list(self, files: List[Dict], current_path: str, user_config: Dict, user_id: str = None) -> str:
        """格式化文件列表或搜索结果"""
        is_search_result = current_path.startswith("🔍 搜索")
        title = f"📁 {current_path}" if not is_search_result else current_path

        if not files: return f"{title}\n\n❌ 列表为空"

        nav_state = self._get_user_navigation_state(user_id)
        current_page = nav_state.get("current_page", 1)
        max_files_per_page = user_config.get("max_display_files", 20)
        total_items = len(files)
        total_pages = (total_items + max_files_per_page - 1) // max_files_per_page
        start_index = (current_page - 1) * max_files_per_page
        end_index = start_index + max_files_per_page
        items_to_display = files[start_index:end_index]

        result = f"{title}\n\n"

        dirs_count = 0
        files_only_count = 0
        if not is_search_result:
            dirs_count = len([f for f in files if f.get("is_dir", False)])
            files_only_count = total_items - dirs_count

        for i, item in enumerate(items_to_display, start=start_index + 1):
            name = item.get("name", "")
            size = item.get("size", 0)
            modified = item.get("modified", "")
            is_dir = item.get("is_dir", False)

            if is_dir: icon = "📂"
            else:
                ext = os.path.splitext(name)[1].lower()
                if ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp"]: icon = "🖼️"
                elif ext in [".mp4", ".avi", ".mkv", ".mov"]: icon = "🎬"
                elif ext in [".mp3", ".wav", ".flac", ".aac"]: icon = "🎵"
                elif ext in [".pdf"]: icon = "📄"
                elif ext in [".doc", ".docx"]: icon = "📝"
                elif ext in [".zip", ".rar", ".7z"]: icon = "📦"
                else: icon = "📄"

            result += f"{i:2d}. {icon} {name}{'/' if is_dir else ''}\n"

            extra_info = []
            if is_search_result:
                parent = item.get("parent", "")
                if parent:
                    parent = self._strip_fixed_base_directory(parent, user_config)
                    extra_info.append(f"📍 {parent}")
                if not is_dir or size > 0:
                    extra_info.append(f"💾 {self._format_file_size(size)}")
            else:
                if not is_dir or size > 0:
                    extra_info.append(f"💾 {self._format_file_size(size)}")

                modified_date_part = modified.split('T')[0] if modified else ''
                if modified_date_part:
                    extra_info.append(f"📅 {modified_date_part}")

            if extra_info:
                result += f"      {' | '.join(extra_info)}\n"

        result += f"\n📄 第 {current_page} / {total_pages} 页"
        if is_search_result:
            result += f" | 📊 总计: {total_items} 个结果"
        else:
            dirs_count = len([f for f in files if f.get("is_dir", False)])
            files_only_count = total_items - dirs_count
            result += f" | 📊 总计: {dirs_count} 个文件夹, {files_only_count} 个文件"

        result += f"\n\n💡 快速导航:"
        result += f"\n\n   • /ol ls 序号 - 进入目录/获取链接"
        result += f"\n\n   • /ol download 序号 - 下载并发送文件"
        if not is_search_result:
             result += f"\n\n   • /ol quit - 返回上级目录"
        if total_pages > 1:
            result += f"\n   • /ol prev - ⬅️ 上一页"
            result += f"\n   • /ol next - ➡️ 下一页"
        return result

    async def _download_file(self, event: AstrMessageEvent, file_item: Dict, user_config: Dict, full_path_override: str = None):
        """下载文件并作为附件发送给用户"""
        async for result in self.download_service._download_file(event, file_item, user_config, full_path_override):
            yield result

    async def _get_and_send_download_link(self, event: AstrMessageEvent, item: Dict, user_config: Dict, full_path: str = None):
        """获取指定项目的文件链接并发送"""
        async for result in self.download_service._get_and_send_download_link(event, item, user_config, full_path):
            yield result

    async def _run_group_file_autobackup(
        self,
        event: AstrMessageEvent,
        file_component: File,
        file_name: str,
        file_size: Optional[int],
        file_url: str,
        target_path: str,
        user_config: Dict,
        group_id: str,
        file_id: str = None,
        busid: int = 0,
    ) -> None:
        """后台执行群文件自动备份，避免阻塞同一条消息上的其他处理器。"""
        return await self.backup_service._run_group_file_autobackup(event, file_component, file_name, file_size, file_url, target_path, user_config, group_id, file_id, busid)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=2)
    async def handle_group_file_upload(self, event: AstrMessageEvent):
        """处理群文件上传事件（自动备份）"""
        return await self.backup_service.handle_group_file_upload(event)

    async def _get_group_files_recursive(self, bot, group_id: int, folder_id: str = "/", current_path: str = "") -> List[Dict]:
        """递归获取群文件列表"""
        return await self.backup_service._get_group_files_recursive(bot, group_id, folder_id, current_path)

    async def _backup_group_files(self, event: AstrMessageEvent, group_id: int, target_path: str, user_config: Dict):
        """执行群文件备份"""
        async for result in self.backup_service._backup_group_files(event, group_id, target_path, user_config):
            yield result

    async def _retry_last_backup(self, event: AstrMessageEvent, user_config: Dict):
        """重试最近一次手动备份失败项。"""
        async for result in self.backup_service._retry_last_backup(event, user_config):
            yield result

    async def _upload_group_file_with_retry(
        self,
        bot,
        client,
        group_id: int,
        item: Dict,
        target_dir: str,
        retry_attempts: int,
        retry_delay: int,
        initial_url: str = None,
    ) -> tuple:
        """获取群文件 URL 并上传；失败时重新获取 URL 后重试。"""
        return await self.backup_service._upload_group_file_with_retry(bot, client, group_id, item, target_dir, retry_attempts, retry_delay, initial_url)

    async def _do_backup_logic(
        self,
        bot,
        event: AstrMessageEvent,
        group_id: int,
        target_path: str,
        user_config: Dict,
        is_auto: bool = False,
        items_override: Optional[List[Dict]] = None,
        retry_key: str = None,
        is_retry: bool = False,
    ):
        """核心备份逻辑，支持手动和自动备份"""
        async for result in self.backup_service._do_backup_logic(bot, event, group_id, target_path, user_config, is_auto, items_override, retry_key, is_retry):
            yield result
    @filter.command_group("ol", alias=["网盘"])
    def openlist_group(self):
        """Openlist文件管理命令组"""
        pass

    @openlist_group.command("config", alias=["配置"])
    async def config_command(self, event: AstrMessageEvent, action: str = "show", key: str = "", value: str = ""):
        """配置 Openlist 连接与插件参数"""
        async for result in self.config_command_service.config_command(event, action, key, value):
            yield result

    @openlist_group.command("ls", alias=["列表", "直链"])
    async def list_files(self, event: AstrMessageEvent, path: str = ""):
        """列出文件和目录，或获取文件链接"""
        async for result in self.browse_service.list_files(event, path):
            yield result

    @openlist_group.command("next", alias=["下一页"])
    async def next_page(self, event: AstrMessageEvent):
        """下一页"""
        async for result in self.browse_service.next_page(event):
            yield result

    @openlist_group.command("prev", alias=["上一页"])
    async def prev_page(self, event: AstrMessageEvent):
        """上一页"""
        async for result in self.browse_service.prev_page(event):
            yield result

    @openlist_group.command("search", alias=["搜索"])
    async def search_files(self, event: AstrMessageEvent, keyword: str, path: str = "/"):
        """搜索文件"""
        async for result in self.browse_service.search_files(event, keyword, path):
            yield result

    @openlist_group.command("info", alias=["信息"])
    async def file_info(self, event: AstrMessageEvent, path: str):
        """获取文件详细信息"""
        async for result in self.browse_service.file_info(event, path):
            yield result

    @openlist_group.command("download", alias=["下载"])
    async def get_download_link(self, event: AstrMessageEvent, path: str):
        """直接下载指定的文件"""
        async for result in self.browse_service.get_download_link(event, path):
            yield result

    @openlist_group.command("quit", alias=["上一级", "返回"])
    async def quit_navigation(self, event: AstrMessageEvent):
        """返回上级目录"""
        async for result in self.browse_service.quit_navigation(event):
            yield result

    @openlist_group.command("upload", alias=["上传"])
    async def upload_command(self, event: AstrMessageEvent, target: str = ""):
        """上传引用消息中的文件、图片或视频"""
        async for result in self.upload_service.upload_command(event, target):
            yield result

    @openlist_group.command("backup", alias=["备份"])
    async def backup_command(self, event: AstrMessageEvent, arg1: str = None, arg2: str = None):
        """群文件备份到 Openlist"""
        async for result in self.backup_service.backup_command(event, arg1, arg2):
            yield result

    @openlist_group.command("autobackup", alias="自动备份")
    async def autobackup_command(self, event: AstrMessageEvent, action: str = "show", arg1: str = None, arg2: str = None):
        """配置自动备份"""
        async for result in self.backup_service.autobackup_command(event, action, arg1, arg2):
            yield result

    @openlist_group.command("restore", alias=["恢复"])
    async def restore_command(self, event: AstrMessageEvent, path: str, target: str = None):
        """将 Openlist 路径中的文件恢复到群组或私聊"""
        async for result in self.restore_service.restore_command(event, path, target):
            yield result

    @openlist_group.command("preview", alias=["预览"])
    async def preview_command(self, event: AstrMessageEvent, path: str):
        """预览文件内容"""
        async for result in self.preview_service.preview_command(event, path):
            yield result

    @openlist_group.command("rm", alias=["删除"])
    async def remove_command(self, event: AstrMessageEvent, path: str):
        """删除文件或文件夹"""
        async for result in self.browse_service.remove_command(event, path):
            yield result

    @openlist_group.command("mkdir", alias=["新建"])
    async def mkdir_command(self, event: AstrMessageEvent, name: str):
        """创建文件夹"""
        async for result in self.browse_service.mkdir_command(event, name):
            yield result

    @openlist_group.command("help", alias=["帮助"])
    async def help_command(self, event: AstrMessageEvent):
        """显示帮助信息"""
        async for result in self.help_service.help_command(event):
            yield result

    async def terminate(self):
        """插件卸载时执行的清理操作"""
        logger.info("OpenList助手已卸载")
