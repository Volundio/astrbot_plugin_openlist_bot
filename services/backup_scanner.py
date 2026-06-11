import os
from typing import Dict, List, Optional

from astrbot.api import logger

from .base import PluginService


class BackupScanner(PluginService):
    """Scan group files and apply backup filters before upload planning."""

    async def get_group_file_system_count(self, bot, group_id: int) -> Optional[int]:
        try:
            res = await bot.api.call_action("get_group_file_system_info", group_id=group_id)
            if not isinstance(res, dict):
                return None
            count = self._to_int_or_none(res.get("file_count"))
            logger.info(f"🔍 [群备份] 群 {group_id} 文件系统信息: {res}")
            return count
        except Exception as e:
            logger.warning(f"获取群 {group_id} 文件系统信息失败: {e}")
            return None

    async def get_group_files_recursive(
        self,
        bot,
        group_id: int,
        folder_id: str = "/",
        current_path: str = "",
        cancel_event=None,
    ) -> List[Dict]:
        """Recursively fetch group files from root and nested folders."""
        all_files = []
        try:
            if cancel_event and cancel_event.is_set():
                return all_files

            if folder_id == "/":
                res = await bot.api.call_action(
                    "get_group_root_files",
                    group_id=group_id,
                    file_count=self.GROUP_FILE_LIST_COUNT,
                )
            else:
                res = await bot.api.call_action(
                    "get_group_files_by_folder",
                    group_id=group_id,
                    folder_id=folder_id,
                    file_count=self.GROUP_FILE_LIST_COUNT,
                )

            if not res:
                return []

            files = res.get("files", [])
            folders = res.get("folders", [])
            logger.info(
                f"🔍 [群备份] 群 {group_id} 目录 {current_path or '/'} "
                f"返回文件 {len(files)} 个，子目录 {len(folders)} 个，file_count={self.GROUP_FILE_LIST_COUNT}"
            )

            for file_item in files:
                if cancel_event and cancel_event.is_set():
                    return all_files
                file_item["relative_path"] = f"{current_path}/{file_item['file_name']}".lstrip("/")
                all_files.append(file_item)

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

    def _filter_items(self, items: List[Dict], user_config: Dict) -> tuple:
        allowed_exts = self._get_extension_filter(user_config, "backup_allowed_extensions")
        max_size_mb = self._get_size_limit_mb(user_config, "backup_max_size", 0)
        max_size = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0

        filtered_items = []
        ext_skipped = 0
        size_skipped = 0
        for item in items:
            name = item.get("file_name", "").lower()
            size = self._to_int_or_none(item.get("file_size"))

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

    async def scan(self, bot, group_id: int, target_path: str, user_config: Dict, cancel_event=None) -> Dict:
        reported_count = await self.get_group_file_system_count(bot, group_id)
        all_items = await self.get_group_files_recursive(bot, group_id, cancel_event=cancel_event)
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
        }
        logger.info(
            f"🔍 [群备份] 扫描统计: group={group_id}, raw={len(all_items)}, "
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
            logger.info(f"⏭️ [群备份] 已去重 {original_count - len(filtered_items)} 个重复群文件记录。")

        return {
            "cancelled": False,
            "found": True,
            "items": filtered_items,
            "stats": scan_stats,
        }
