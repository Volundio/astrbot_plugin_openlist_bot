import hashlib
import json
import os
import posixpath
import time
from typing import Dict, Optional
from astrbot.api import logger
from astrbot.api.star import StarTools


class CacheManager:
    """文件缓存管理器"""

    def __init__(self, plugin_name: str):
        self.plugin_name = plugin_name
        self.cache_dir = os.path.join(
            StarTools.get_data_dir(plugin_name), "cache"
        )
        os.makedirs(self.cache_dir, exist_ok=True)

    @staticmethod
    def build_auth_scope(user_config: Dict, fallback_user_id: str = "") -> str:
        """基于服务端认证凭据生成缓存作用域，使全局共享模式下多用户共享且同步失效缓存。"""
        if not isinstance(user_config, dict):
            return str(fallback_user_id or "anon")
        token = str(user_config.get("token") or "").strip()
        username = str(user_config.get("username") or "").strip()
        base_dir = str(user_config.get("fixed_base_directory") or "").strip()
        if token:
            token_sig = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
            return f"token:{token_sig}:{base_dir}"
        if username:
            return f"user:{username}:{base_dir}"
        return f"anon:{base_dir}"

    def _normalize_path(self, path: str) -> str:
        norm = posixpath.normpath((path or "/").strip().replace("\\", "/") or "/")
        if norm in ("", "."):
            return "/"
        return norm if norm.startswith("/") else f"/{norm}"

    def _get_cache_key(self, url: str, path: str, scope: str) -> str:
        """根据URL、归一化路径和认证作用域生成唯一缓存键"""
        norm_url = (url or "").rstrip("/")
        norm_path = self._normalize_path(path)
        content = f"{norm_url}:{norm_path}:{scope}"
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def _get_cache_file(self, cache_key: str) -> str:
        """根据缓存键生成缓存文件路径"""
        return os.path.join(self.cache_dir, f"{cache_key}.json")

    def _remove_cache_file(self, cache_file: str):
        """删除缓存文件，失败时只记录调试日志。"""
        try:
            os.remove(cache_file)
        except FileNotFoundError:
            return
        except OSError as e:
            logger.debug(f"删除缓存文件失败: {cache_file}, err={e}")

    def get_cache(
        self,
        url: str,
        path: str,
        user_id: str = "",
        max_age: int = 300,
        auth_scope: Optional[str] = None,
    ) -> Optional[Dict]:
        """从本地获取缓存数据，并检查是否过期"""
        try:
            scope = auth_scope if auth_scope is not None else str(user_id or "")
            cache_key = self._get_cache_key(url, path, scope)
            cache_file = self._get_cache_file(cache_key)

            if not os.path.exists(cache_file):
                return None

            if time.time() - os.path.getmtime(cache_file) > max_age:
                self._remove_cache_file(cache_file)
                return None

            with open(cache_file, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
                return cache_data.get("data")
        except Exception as e:
            logger.debug(f"读取缓存失败: {e}")
            return None

    def set_cache(
        self,
        url: str,
        path: str,
        user_id: str = "",
        data: Dict = None,
        auth_scope: Optional[str] = None,
    ):
        """将数据保存到本地缓存"""
        try:
            scope = auth_scope if auth_scope is not None else str(user_id or "")
            norm_url = (url or "").rstrip("/")
            norm_path = self._normalize_path(path)
            cache_key = self._get_cache_key(norm_url, norm_path, scope)
            cache_file = self._get_cache_file(cache_key)

            cache_data = {
                "timestamp": time.time(),
                "url": norm_url,
                "path": norm_path,
                "user_id": user_id,
                "auth_scope": scope,
                "data": data,
            }

            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"写入缓存失败: {e}")

    def invalidate_path(self, url: str, path: str = None):
        """精确失效指定 OpenList 服务器的目录（及其父目录）缓存，避免误删其他服务器缓存。"""
        try:
            norm_url = (url or "").rstrip("/")
            target_paths = None
            if path is not None:
                norm_path = self._normalize_path(path)
                parent_path = posixpath.dirname(norm_path.rstrip("/")) or "/"
                target_paths = {norm_path, parent_path}

            for filename in os.listdir(self.cache_dir):
                if not filename.endswith(".json"):
                    continue
                cache_file = os.path.join(self.cache_dir, filename)
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        cache_data = json.load(f)
                    cached_url = (cache_data.get("url") or "").rstrip("/")
                    if norm_url and cached_url != norm_url:
                        continue
                    if target_paths is not None and cache_data.get("path") not in target_paths:
                        continue
                    self._remove_cache_file(cache_file)
                except (OSError, json.JSONDecodeError):
                    self._remove_cache_file(cache_file)
        except Exception as e:
            logger.debug(f"按路径失效缓存失败: {e}")

    def clear_cache(self, user_id: str = None, auth_scope: str = None):
        """清理缓存"""
        try:
            if user_id or auth_scope:
                for filename in os.listdir(self.cache_dir):
                    if filename.endswith(".json"):
                        cache_file = os.path.join(self.cache_dir, filename)
                        try:
                            with open(cache_file, "r", encoding="utf-8") as f:
                                cache_data = json.load(f)
                            matched_user = bool(user_id and cache_data.get("user_id") in (user_id, None))
                            matched_scope = bool(auth_scope and cache_data.get("auth_scope") == auth_scope)
                            if matched_user or matched_scope:
                                self._remove_cache_file(cache_file)
                        except (OSError, json.JSONDecodeError) as e:
                            logger.debug(f"缓存文件不可读，直接清理: {cache_file}, err={e}")
                            self._remove_cache_file(cache_file)
            else:
                for filename in os.listdir(self.cache_dir):
                    if filename.endswith(".json"):
                        self._remove_cache_file(os.path.join(self.cache_dir, filename))
        except Exception as e:
            logger.debug(f"清理缓存失败: {e}")
