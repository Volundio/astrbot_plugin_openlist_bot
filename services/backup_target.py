import os
from typing import Dict, List

from astrbot.api import logger

from .base import PluginService


class BackupTargetManager(PluginService):
    """Resolve backup target paths, names, duplicate records, and existing files."""

    def item_target(self, target_path: str, item: Dict, use_override: bool = True) -> tuple:
        source_name = self._backup_item_source_name(item)
        file_name = item.get("_backup_target_name") if use_override else None
        file_name = file_name or source_name
        rel_path = item.get("relative_path") or source_name
        file_dir = os.path.dirname(rel_path)
        target_root = self._normalize_openlist_path(target_path or "/")
        target_dir = target_root
        if file_dir:
            target_dir = self._normalize_openlist_path(f"{target_root.rstrip('/')}/{file_dir}")
        target_key = self._normalize_openlist_path(f"{target_dir.rstrip('/')}/{file_name}")
        return target_dir, file_name, target_key

    def item_lock_key(self, target_path: str, item: Dict) -> str:
        return self.item_target(target_path, item, use_override=False)[2]

    def item_duplicate_key(self, target_path: str, item: Dict) -> tuple:
        target_dir, file_name, _ = self.item_target(target_path, item, use_override=False)
        return target_dir, file_name

    def item_identity(self, _target_path: str, item: Dict) -> tuple:
        size = self._backup_item_size(item)
        if size is not None:
            return ("size", size)
        file_id = item.get("file_id")
        if file_id:
            return ("file_id", str(file_id))
        return ("unknown_size",)

    def _safe_suffix_part(self, value: str) -> str:
        suffix = "".join(c for c in str(value or "") if c.isalnum() or c in "-_").strip("-_")
        return suffix[:16] or "duplicate"

    def duplicate_suffix(self, item: Dict) -> str:
        file_id = item.get("file_id")
        if file_id:
            return self._safe_suffix_part(str(file_id).strip("/").split("/")[-1])
        size = self._backup_item_size(item)
        if size is not None:
            return f"size-{size}"
        return "duplicate"

    def filename_with_suffix(self, filename: str, suffix: str) -> str:
        stem, ext = os.path.splitext(filename)
        return f"{stem} [{suffix}]{ext}" if stem else f"{filename} [{suffix}]"

    def deduplicate_backup_items(self, items: List[Dict], target_path: str) -> List[Dict]:
        candidates = []
        seen_items = set()
        target_counts = {}
        for item in items:
            target_dir, file_name = self.item_duplicate_key(target_path, item)
            if not file_name:
                continue
            duplicate_key = (target_dir, file_name)
            identity = (duplicate_key, self.item_identity(target_path, item))
            if identity in seen_items:
                logger.info(f"⏭️ [群备份] 跳过同目录重复群文件记录: {target_dir}/{file_name}")
                continue
            seen_items.add(identity)
            target_counts[duplicate_key] = target_counts.get(duplicate_key, 0) + 1
            candidates.append((duplicate_key, item))

        deduped = []
        used_names = {}
        for duplicate_key, item in candidates:
            if target_counts.get(duplicate_key, 0) <= 1:
                deduped.append(item)
                continue

            _, file_name, _ = self.item_target(target_path, item, use_override=False)
            used_for_target = used_names.setdefault(duplicate_key, set())
            if not used_for_target:
                used_for_target.add(file_name)
                deduped.append(item)
                continue

            renamed_item = dict(item)
            suffix = self.duplicate_suffix(item)
            target_name = self.filename_with_suffix(file_name, suffix)
            index = 2
            while target_name in used_for_target:
                target_name = self.filename_with_suffix(file_name, f"{suffix}-{index}")
                index += 1
            used_for_target.add(target_name)
            renamed_item["_backup_target_name"] = target_name
            logger.info(f"📌 [群备份] 同名群文件改名备份: {file_name} -> {target_name}")
            deduped.append(renamed_item)
        return deduped

    def existing_entry_matches(self, existing: Dict, file_size) -> bool:
        try:
            expected_size = int(file_size) if file_size is not None else None
            existing_size = int(existing.get("size", 0))
        except (TypeError, ValueError):
            return True
        return expected_size is None or existing_size == expected_size

    async def openlist_files_by_name(self, client, target_dir: str, refresh: bool = False) -> Dict[str, Dict]:
        try:
            list_result = await client.list_files(target_dir or "/", per_page=0, refresh=refresh)
        except Exception as e:
            logger.warning(f"检查目标目录文件列表失败: {target_dir}, err={e}")
            return {}
        if list_result is None:
            return {}
        files = {}
        for existing in list_result.get("content") or []:
            if not existing.get("is_dir", False):
                files[existing.get("name", "")] = existing
        return files

    def resolve_target_name(
        self,
        existing_files: Dict[str, Dict],
        target_name: str,
        item: Dict,
        force_unique: bool = False,
    ) -> str:
        existing = existing_files.get(target_name)
        file_size = self._backup_item_size(item)
        if not existing or (not force_unique and self.existing_entry_matches(existing, file_size)):
            return target_name

        suffix = self.duplicate_suffix(item)
        candidate = self.filename_with_suffix(target_name, suffix)
        existing = existing_files.get(candidate)
        if not existing or self.existing_entry_matches(existing, file_size):
            return candidate

        for index in range(2, 1000):
            candidate = self.filename_with_suffix(target_name, f"{suffix}-{index}")
            existing = existing_files.get(candidate)
            if not existing or self.existing_entry_matches(existing, file_size):
                return candidate

        logger.warning(f"无法为同名文件生成未占用名称，将使用原名: {target_name}")
        return target_name
