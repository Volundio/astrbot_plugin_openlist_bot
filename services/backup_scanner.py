import os
from typing import Dict, List, Optional

from astrbot.api import logger

from ..lib.onebot_files import (
    call_onebot_action,
    extract_flash_descriptors_from_segments,
    extract_history_messages_list,
    get_bot_caller,
    is_http_url,
    is_self_event,
    maybe_int_id,
    normalize_message_segments,
    resolve_flash_transfer_files,
    sanitize_remote_filename,
    to_int_or_none,
)
from .base import PluginService


class BackupScanner(PluginService):
    """Scan group files, private chat files, and QQ Flash Transfers for backup."""

    PRIVATE_HISTORY_COUNT = 200
    GROUP_FLASH_HISTORY_COUNT = 100

    @staticmethod
    def _strip_transient_fields(item: Dict) -> Dict:
        cleaned = dict(item)
        cleaned.pop("refresh_url", None)
        cleaned.pop("file_component", None)
        return cleaned

    async def get_group_file_system_count(self, bot, group_id) -> Optional[int]:
        if not get_bot_caller(bot):
            return None
        try:
            res = await call_onebot_action(bot, "get_group_file_system_info", group_id=maybe_int_id(group_id))
            if not isinstance(res, dict):
                return None
            count = to_int_or_none(res.get("file_count"))
            logger.info(f"🔍 [群备份] 群 {group_id} 文件系统信息: {res}")
            return count
        except Exception as e:
            logger.warning(f"获取群 {group_id} 文件系统信息失败: {e}")
            return None

    async def get_group_files_recursive(
        self,
        bot,
        group_id,
        folder_id: str = "/",
        current_path: str = "",
        cancel_event=None,
    ) -> List[Dict]:
        """Recursively fetch group files from root and nested folders."""
        all_files = []
        if not get_bot_caller(bot):
            return all_files
        try:
            if cancel_event and cancel_event.is_set():
                return all_files

            gid = maybe_int_id(group_id)
            if folder_id == "/":
                res = await call_onebot_action(
                    bot,
                    "get_group_root_files",
                    group_id=gid,
                    file_count=self.GROUP_FILE_LIST_COUNT,
                )
            else:
                res = await call_onebot_action(
                    bot,
                    "get_group_files_by_folder",
                    group_id=gid,
                    folder_id=folder_id,
                    file_count=self.GROUP_FILE_LIST_COUNT,
                )

            if not isinstance(res, dict):
                return []

            files = res.get("files", []) or []
            folders = res.get("folders", []) or []
            logger.info(
                f"🔍 [群备份] 群 {group_id} 目录 {current_path or '/'} "
                f"返回文件 {len(files)} 个，子目录 {len(folders)} 个，file_count={self.GROUP_FILE_LIST_COUNT}"
            )

            for file_item in files:
                if cancel_event and cancel_event.is_set():
                    return all_files
                item_copy = dict(file_item)
                fname = item_copy.get("file_name") or item_copy.get("name") or ""
                item_copy["file_name"] = fname
                item_copy["source_type"] = "group"
                item_copy["relative_path"] = f"{current_path}/{fname}".lstrip("/")
                all_files.append(item_copy)

            for folder in folders:
                if cancel_event and cancel_event.is_set():
                    return all_files
                sub_folder_id = folder.get("folder_id")
                sub_folder_name = folder.get("folder_name")
                if sub_folder_id:
                    sub_files = await self.get_group_files_recursive(
                        bot,
                        group_id,
                        sub_folder_id,
                        f"{current_path}/{sub_folder_name}",
                        cancel_event,
                    )
                    all_files.extend(sub_files)

            return all_files
        except Exception as e:
            logger.error(f"递归获取群 {group_id} 文件失败: {e}", exc_info=True)
            return all_files

    async def _scan_flash_from_group_history(self, bot, group_id, cancel_event=None) -> List[Dict]:
        """Best-effort scan of recent group messages for QQ Flash Transfers."""
        if not get_bot_caller(bot):
            return []
        try:
            res = await call_onebot_action(
                bot,
                "get_group_msg_history",
                group_id=maybe_int_id(group_id),
                count=self.GROUP_FLASH_HISTORY_COUNT,
            )
        except Exception as e:
            logger.debug(f"扫描群 {group_id} 最近消息闪传跳过（协议端可能不支持 get_group_msg_history）: {e}")
            return []

        messages = extract_history_messages_list(res)
        seen_flash = set()
        flash_items: List[Dict] = []
        for msg in messages:
            if cancel_event and cancel_event.is_set():
                break
            if is_self_event(None, msg):
                continue
            segs = normalize_message_segments(msg.get("message"))
            descriptors = extract_flash_descriptors_from_segments(segs, include_plain_text=True)
            for desc in descriptors:
                key = desc.get("fileset_id") or desc.get("share_code") or desc.get("url")
                if not key or key in seen_flash:
                    continue
                seen_flash.add(key)
                _, resolved = await resolve_flash_transfer_files(bot, desc)
                for f_item in resolved:
                    flash_items.append(self._strip_transient_fields(f_item))
        return flash_items

    def _filter_items(self, items: List[Dict], user_config: Dict) -> tuple:
        allowed_exts = self._get_extension_filter(user_config, "backup_allowed_extensions")
        max_size_mb = self._get_size_limit_mb(user_config, "backup_max_size", 0)
        max_size = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0

        filtered_items = []
        ext_skipped = 0
        size_skipped = 0
        for item in items:
            name = (item.get("file_name") or item.get("name") or "").lower()
            size = to_int_or_none(item.get("file_size", item.get("size")))

            if allowed_exts:
                ext = os.path.splitext(name)[1]
                if ext not in allowed_exts:
                    ext_skipped += 1
                    continue

            if max_size > 0 and size is not None and size > max_size:
                size_skipped += 1
                continue

            filtered_items.append(item)

        return filtered_items, {
            "ext_skipped": ext_skipped,
            "size_skipped": size_skipped,
            "backup_allowed_extensions": allowed_exts,
            "backup_max_size_mb": max_size_mb,
        }

    async def scan(self, bot, group_id, target_path: str, user_config: Dict, cancel_event=None) -> Dict:
        reported_count = await self.get_group_file_system_count(bot, group_id)
        all_items = await self.get_group_files_recursive(bot, group_id, cancel_event=cancel_event)
        flash_items = await self._scan_flash_from_group_history(bot, group_id, cancel_event=cancel_event)
        if flash_items:
            logger.info(f"⚡ [群备份] 从群 {group_id} 近期消息额外发现 {len(flash_items)} 个 QQ 闪传文件")
            all_items.extend(flash_items)

        if cancel_event and cancel_event.is_set():
            return {
                "cancelled": True,
                "found": bool(all_items),
                "items": [],
                "stats": {"raw_count": len(all_items), "target_path": target_path, "reported_count": reported_count},
            }
        if not all_items:
            return {
                "cancelled": False,
                "found": False,
                "items": [],
                "stats": {"raw_count": 0, "target_path": target_path, "reported_count": reported_count},
            }

        filtered_items, filter_stats = self._filter_items(all_items, user_config)
        scan_stats = {
            "raw_count": len(all_items),
            "filtered_count": len(filtered_items),
            "ext_skipped": filter_stats["ext_skipped"],
            "size_skipped": filter_stats["size_skipped"],
            "target_path": target_path,
            "reported_count": reported_count,
            "flash_count": len(flash_items),
        }
        logger.info(
            f"🔍 [群备份] 扫描统计: group={group_id}, raw={len(all_items)} (flash={len(flash_items)}), "
            f"reported={reported_count}, "
            f"ext_skipped={filter_stats['ext_skipped']}, size_skipped={filter_stats['size_skipped']}, "
            f"filtered={len(filtered_items)}, "
            f"backup_allowed_extensions={filter_stats['backup_allowed_extensions'] or '不限制'}, "
            f"backup_max_size={filter_stats['backup_max_size_mb']}MB"
        )

        original_count = len(filtered_items)
        filtered_items = self.target.deduplicate_backup_items(filtered_items, target_path)
        scan_stats["deduped_count"] = len(filtered_items)
        scan_stats["duplicate_skipped"] = original_count - len(filtered_items)
        if original_count != len(filtered_items):
            logger.info(f"⏭️ [群备份] 已去重 {original_count - len(filtered_items)} 个重复文件记录。")

        return {
            "cancelled": False,
            "found": True,
            "items": filtered_items,
            "stats": scan_stats,
        }

    async def scan_private(
        self,
        bot,
        user_id: str,
        target_path: str,
        user_config: Dict,
        cached_records: Optional[List[Dict]] = None,
        cancel_event=None,
    ) -> Dict:
        """Scan private chat history and runtime offline_file/flash cache for a user."""
        user_id_str = str(user_id)
        all_items: List[Dict] = []
        seen_file_keys = set()
        seen_flash_keys = set()
        flash_descriptors: List[Dict] = []

        def add_private_file(data: Dict):
            raw_name = (
                data.get("name")
                or data.get("file_name")
                or data.get("filename")
                or data.get("file")
                or ""
            )
            if is_http_url(raw_name):
                raw_name = ""
            file_id = data.get("file_id") or data.get("id") or ""
            url = data.get("url") or data.get("file_url") or ""
            if not url and is_http_url(data.get("file")):
                url = str(data.get("file")).strip()
            size = to_int_or_none(data.get("file_size", data.get("size")))
            file_name = sanitize_remote_filename(str(raw_name), fallback="")
            if not file_name and not file_id and not url:
                return
            if not file_name:
                file_name = f"private_file_{len(all_items) + 1}"
            dedup_token = str(file_id) if file_id else f"{file_name}:{size}:{url}"
            if dedup_token in seen_file_keys:
                return
            seen_file_keys.add(dedup_token)
            all_items.append({
                "source_type": "private",
                "user_id": user_id_str,
                "file_id": str(file_id) if file_id else None,
                "file_hash": data.get("file_hash") or data.get("hash"),
                "file_name": file_name,
                "relative_path": file_name,
                "file_size": size,
                "url": url if is_http_url(url) else "",
            })

        # 1. 拉取 OneBot 私聊消息历史（NapCat / Lagrange get_friend_msg_history）
        if get_bot_caller(bot):
            try:
                res = await call_onebot_action(
                    bot,
                    "get_friend_msg_history",
                    user_id=maybe_int_id(user_id_str),
                    count=self.PRIVATE_HISTORY_COUNT,
                )
                for msg in extract_history_messages_list(res):
                    if cancel_event and cancel_event.is_set():
                        break
                    if is_self_event(None, msg):
                        continue
                    raw_text = str(msg.get("raw_message") or "").strip().lstrip("/")
                    is_cmd_msg = raw_text.lower().startswith(("ol ", "网盘 "))
                    segs = normalize_message_segments(msg.get("message"))
                    for seg in segs:
                        if seg.get("type") == "file":
                            add_private_file(seg.get("data") or {})
                    for desc in extract_flash_descriptors_from_segments(segs, include_plain_text=not is_cmd_msg):
                        key = desc.get("fileset_id") or desc.get("share_code") or desc.get("url")
                        if key and key not in seen_flash_keys:
                            seen_flash_keys.add(key)
                            flash_descriptors.append(desc)
            except Exception as e:
                logger.debug(f"拉取用户 {user_id_str} 私聊历史记录失败（将使用缓存记录）: {e}")

        # 2. 合并运行时捕获的 offline_file / 私聊文件与闪传缓存
        for record in cached_records or []:
            for seg in record.get("media_segments") or []:
                if seg.get("type") == "file":
                    add_private_file(seg.get("data") or {})
            for desc in record.get("flash_descriptors") or []:
                key = desc.get("fileset_id") or desc.get("share_code") or desc.get("url")
                if key and key not in seen_flash_keys:
                    seen_flash_keys.add(key)
                    flash_descriptors.append(desc)

        # 3. 展开所有 QQ 闪传文件集
        flash_count = 0
        for desc in flash_descriptors:
            if cancel_event and cancel_event.is_set():
                break
            _, resolved = await resolve_flash_transfer_files(bot, desc)
            for f_item in resolved:
                cleaned = self._strip_transient_fields(f_item)
                cleaned["user_id"] = user_id_str
                all_items.append(cleaned)
                flash_count += 1

        if cancel_event and cancel_event.is_set():
            return {
                "cancelled": True,
                "found": bool(all_items),
                "items": [],
                "stats": {"raw_count": len(all_items), "target_path": target_path, "reported_count": None},
            }
        if not all_items:
            return {
                "cancelled": False,
                "found": False,
                "items": [],
                "stats": {"raw_count": 0, "target_path": target_path, "reported_count": None},
            }

        filtered_items, filter_stats = self._filter_items(all_items, user_config)
        original_count = len(filtered_items)
        filtered_items = self.target.deduplicate_backup_items(filtered_items, target_path)
        scan_stats = {
            "raw_count": len(all_items),
            "filtered_count": original_count,
            "deduped_count": len(filtered_items),
            "ext_skipped": filter_stats["ext_skipped"],
            "size_skipped": filter_stats["size_skipped"],
            "duplicate_skipped": original_count - len(filtered_items),
            "target_path": target_path,
            "reported_count": None,
            "flash_count": flash_count,
        }
        return {
            "cancelled": False,
            "found": True,
            "items": filtered_items,
            "stats": scan_stats,
        }
