import asyncio
import os
import posixpath
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File

from ..lib.onebot_files import (
    extract_flash_descriptors_from_segments,
    extract_flash_descriptors_from_text,
    is_self_event,
    maybe_int_id,
    normalize_message_segments,
    read_field,
    resolve_flash_transfer_files,
    sanitize_relative_subpath,
    to_int_or_none,
)
from .backup_scanner import BackupScanner
from .backup_target import BackupTargetManager
from .backup_uploader import BackupUploader
from .base import PluginService


class BackupService(PluginService):
    """Backup service supporting group files, private chat files, and QQ Flash Transfers."""

    AUTOBACKUP_DEDUP_TTL_SECONDS = 90
    MAX_AUTOBACKUP_DEDUP_KEYS = 1000
    PRIVATE_CACHE_TTL_SECONDS = 24 * 3600
    MAX_PRIVATE_CACHE_USERS = 200
    MAX_PRIVATE_CACHE_RECORDS_PER_USER = 200

    def __init__(self, plugin):
        super().__init__(plugin)
        self._target_locks = {}
        self._target_lock_refs = {}
        self._target_locks_guard = asyncio.Lock()
        self._autobackup_full_cancel_events = {}
        self._autobackup_full_meta = {}
        self._recent_autobackup_fingerprints: Dict[str, float] = {}
        self._private_transfer_cache: Dict[str, List[Dict]] = {}
        self.target = BackupTargetManager(self)
        self.scanner = BackupScanner(self)
        self.uploader = BackupUploader(self)

    def _to_int_or_none(self, value):
        return to_int_or_none(value)

    def _backup_item_size(self, item: Dict):
        return to_int_or_none(item.get("file_size", item.get("size")))

    def _backup_item_source_name(self, item: Dict) -> str:
        return item.get("file_name") or item.get("name") or ""

    def _short_backup_fail_reason(self, reason, limit: int = 140) -> str:
        text = str(reason or "未知原因").replace("\n", " ").strip()
        if "wording='" in text:
            text = text.split("wording='", 1)[1].split("'", 1)[0]
        elif "message='" in text:
            text = text.split("message='", 1)[1].split("'", 1)[0]
        if len(text) > limit:
            text = text[: limit - 3] + "..."
        return text or "未知原因"

    @staticmethod
    def _sanitize_retry_item(item: Dict) -> Dict:
        cleaned = {
            k: v for k, v in item.items()
            if k not in ("refresh_url", "file_component", "_component") and not callable(v)
        }
        return cleaned

    def _format_failed_backup_items(self, failed_items: List[Dict], limit: int = 10) -> str:
        if not failed_items:
            return ""
        lines = ["", "❌ 失败文件:"]
        for index, item in enumerate(failed_items[:limit], start=1):
            file_name = item.get("_backup_target_name") or self._backup_item_source_name(item) or "未知文件"
            reason = self._short_backup_fail_reason(item.get("_backup_fail_reason"))
            lines.append(f"{index}. {file_name}")
            lines.append(f"   原因: {reason}")
        remaining = len(failed_items) - limit
        if remaining > 0:
            lines.append(f"... 其余 {remaining} 个失败文件已保存，可使用 ol backup retry 重试。")
        return "\n".join(lines)

    def _is_permanent_group_file_error(self, reason: str) -> bool:
        text = str(reason or "")
        return "文件不存在" in text or "code=-103" in text or "已过期" in text

    def record_private_transfer_cache(
        self,
        user_id: str,
        media_segments: Optional[List[Dict]] = None,
        flash_descriptors: Optional[List[Dict]] = None,
    ):
        """缓存私聊中收到的离线文件或闪传元数据，补充 get_friend_msg_history 覆盖不到的通知事件。"""
        uid = str(user_id or "").strip()
        if not uid:
            return
        file_segs = [
            {
                "type": "file",
                "data": dict(seg.get("data") or {}),
            }
            for seg in (media_segments or [])
            if isinstance(seg, dict) and seg.get("type") == "file"
        ]
        flashes = [dict(d) for d in (flash_descriptors or []) if isinstance(d, dict)]
        if not file_segs and not flashes:
            return

        now = time.time()
        if len(self._private_transfer_cache) >= self.MAX_PRIVATE_CACHE_USERS and uid not in self._private_transfer_cache:
            oldest_uid = next(iter(self._private_transfer_cache))
            self._private_transfer_cache.pop(oldest_uid, None)

        bucket = self._private_transfer_cache.setdefault(uid, [])
        bucket = [r for r in bucket if now - r.get("timestamp", 0) <= self.PRIVATE_CACHE_TTL_SECONDS]
        bucket.append({
            "timestamp": now,
            "media_segments": file_segs,
            "flash_descriptors": flashes,
        })
        if len(bucket) > self.MAX_PRIVATE_CACHE_RECORDS_PER_USER:
            bucket = bucket[-self.MAX_PRIVATE_CACHE_RECORDS_PER_USER:]
        self._private_transfer_cache[uid] = bucket

    def get_private_transfer_cache(self, user_id: str) -> List[Dict]:
        uid = str(user_id or "").strip()
        now = time.time()
        records = self._private_transfer_cache.get(uid, [])
        valid = [r for r in records if now - r.get("timestamp", 0) <= self.PRIVATE_CACHE_TTL_SECONDS]
        if valid:
            self._private_transfer_cache[uid] = valid
        else:
            self._private_transfer_cache.pop(uid, None)
        return valid

    def _is_duplicate_autobackup_event(self, fingerprint: str) -> bool:
        """拦截同一文件在短时间内因通知事件+消息段同时上报引发的重复自动备份。"""
        now = time.monotonic()
        expired = [
            k for k, ts in self._recent_autobackup_fingerprints.items()
            if now - ts > self.AUTOBACKUP_DEDUP_TTL_SECONDS
        ]
        for k in expired:
            self._recent_autobackup_fingerprints.pop(k, None)
        if fingerprint in self._recent_autobackup_fingerprints:
            return True
        if len(self._recent_autobackup_fingerprints) >= self.MAX_AUTOBACKUP_DEDUP_KEYS:
            oldest = min(self._recent_autobackup_fingerprints, key=self._recent_autobackup_fingerprints.get)
            self._recent_autobackup_fingerprints.pop(oldest, None)
        self._recent_autobackup_fingerprints[fingerprint] = now
        return False

    def _extract_autobackup_payloads(self, event: AstrMessageEvent) -> Tuple[List[Dict], List[Dict]]:
        """
        提取自动备份候选事件，返回 (file_payloads, flash_payloads)。
        同时兼容群文件、私聊离线文件以及 QQ 闪传消息。
        """
        message_obj = getattr(event, "message_obj", None)
        raw_event_data = read_field(message_obj, "raw_message", {})
        if is_self_event(event, raw_event_data):
            return [], []

        group_id = self._get_event_group_id(event)
        user_id = str(event.get_sender_id() or read_field(raw_event_data, "user_id") or "")
        is_cmd = self._is_openlist_command_message(event)
        get_messages = getattr(event, "get_messages", None)
        raw_messages = (get_messages() or []) if callable(get_messages) else []
        file_components = [msg for msg in raw_messages if isinstance(msg, File)]

        def component_payload(component: File) -> Dict:
            return {
                "group_id": group_id,
                "user_id": user_id,
                "file_name": read_field(component, "name") or read_field(component, "file"),
                "file_id": read_field(component, "file_id"),
                "file_hash": read_field(component, "file_hash"),
                "file_size": to_int_or_none(
                    read_field(component, "file_size") or read_field(component, "size")
                ),
                "file_url": read_field(component, "url"),
                "busid": read_field(component, "busid", 0),
                "file_component": component,
                "source": "component",
            }

        notice_type = read_field(raw_event_data, "notice_type")
        if notice_type == "group_upload":
            file_info = read_field(raw_event_data, "file", {}) or {}
            file_name = (
                read_field(file_info, "name")
                or read_field(file_info, "file_name")
                or read_field(file_info, "file")
            )
            file_id = (
                read_field(file_info, "id")
                or read_field(file_info, "file_id")
                or read_field(raw_event_data, "file_id")
            )
            return ([{
                "group_id": read_field(raw_event_data, "group_id", group_id),
                "user_id": str(read_field(raw_event_data, "user_id") or user_id),
                "file_name": file_name,
                "file_id": file_id,
                "file_hash": None,
                "file_size": to_int_or_none(
                    read_field(file_info, "size", read_field(raw_event_data, "file_size"))
                ),
                "file_url": read_field(file_info, "url", read_field(raw_event_data, "url")),
                "busid": read_field(file_info, "busid", read_field(raw_event_data, "busid", 0)),
                "file_component": None,
                "source": "notice_group_upload",
            }], [])

        if notice_type == "offline_file":
            file_info = read_field(raw_event_data, "file", {}) or {}
            file_name = (
                read_field(file_info, "name")
                or read_field(file_info, "file_name")
                or read_field(file_info, "file")
            )
            file_id = (
                read_field(file_info, "id")
                or read_field(file_info, "file_id")
                or read_field(raw_event_data, "file_id")
            )
            return ([{
                "group_id": None,
                "user_id": str(read_field(raw_event_data, "user_id") or user_id),
                "file_name": file_name,
                "file_id": file_id,
                "file_hash": read_field(file_info, "file_hash") or read_field(file_info, "hash"),
                "file_size": to_int_or_none(
                    read_field(file_info, "size", read_field(raw_event_data, "file_size"))
                ),
                "file_url": read_field(file_info, "url", read_field(raw_event_data, "url")),
                "busid": 0,
                "file_component": None,
                "source": "notice_offline_file",
            }], [])

        raw_msg_content = read_field(raw_event_data, "message")
        segments = normalize_message_segments(raw_msg_content)
        file_payloads: List[Dict] = []
        flash_payloads: List[Dict] = []

        if segments:
            file_component_index = 0
            for segment_dict in segments:
                if not isinstance(segment_dict, dict) or segment_dict.get("type") != "file":
                    continue
                data_dict = segment_dict.get("data", {}) or {}
                file_name = data_dict.get("name") or data_dict.get("file_name") or data_dict.get("file")
                file_component = None
                for component in file_components:
                    if getattr(component, "name", None) == file_name:
                        file_component = component
                        break
                if file_component is None and file_component_index < len(file_components):
                    file_component = file_components[file_component_index]
                file_component_index += 1

                file_payloads.append({
                    "group_id": group_id,
                    "user_id": user_id,
                    "file_name": file_name,
                    "file_id": data_dict.get("file_id") or data_dict.get("id"),
                    "file_hash": data_dict.get("file_hash") or data_dict.get("hash"),
                    "file_size": to_int_or_none(data_dict.get("file_size") or data_dict.get("size")),
                    "file_url": data_dict.get("url"),
                    "busid": data_dict.get("busid", 0),
                    "file_component": file_component,
                    "source": "message",
                })

            for desc in extract_flash_descriptors_from_segments(segments, include_plain_text=not is_cmd):
                flash_payloads.append({
                    "group_id": group_id,
                    "user_id": user_id,
                    "descriptor": desc,
                    "source": "flash_segment",
                })
        else:
            file_payloads = [component_payload(component) for component in file_components]
            if not is_cmd:
                for desc in extract_flash_descriptors_from_text(getattr(event, "message_str", "") or ""):
                    flash_payloads.append({
                        "group_id": group_id,
                        "user_id": user_id,
                        "descriptor": desc,
                        "source": "flash_text",
                    })

        return file_payloads, flash_payloads

    def _format_backup_scan_summary(self, stats: Dict) -> str:
        raw_count = stats.get("raw_count", 0)
        reported_count = stats.get("reported_count")
        flash_count = stats.get("flash_count", 0)
        deduped_count = stats.get("deduped_count", stats.get("filtered_count", raw_count))
        ext_skipped = stats.get("ext_skipped", 0)
        size_skipped = stats.get("size_skipped", 0)
        duplicate_skipped = stats.get("duplicate_skipped", 0)

        flash_note = f"（含闪传 {flash_count} 个）" if flash_count else ""
        lines = [f"📦 扫描完成：共发现 {raw_count} 个{flash_note}，需备份 {deduped_count} 个。"]
        if reported_count is not None and reported_count != (raw_count - flash_count):
            lines.append(f"📊 群文件系统统计: {reported_count} 个。")
        skipped_parts = []
        if ext_skipped:
            skipped_parts.append(f"后缀 {ext_skipped}")
        if size_skipped:
            skipped_parts.append(f"大小 {size_skipped}")
        if duplicate_skipped:
            skipped_parts.append(f"重复 {duplicate_skipped}")
        if skipped_parts:
            lines.append(f"⏭️ 跳过: {'，'.join(skipped_parts)}。")
        lines.append(f"📂 目标: {stats.get('target_path', '')}")
        return "\n".join(lines)

    async def _acquire_target_lock(self, target_key: str):
        async with self._target_locks_guard:
            lock = self._target_locks.get(target_key)
            if lock is None:
                lock = asyncio.Lock()
                self._target_locks[target_key] = lock
                self._target_lock_refs[target_key] = 0
            self._target_lock_refs[target_key] += 1
        try:
            await lock.acquire()
        except BaseException:
            async with self._target_locks_guard:
                refs = self._target_lock_refs.get(target_key, 0) - 1
                if refs <= 0:
                    self._target_lock_refs.pop(target_key, None)
                    if self._target_locks.get(target_key) is lock:
                        self._target_locks.pop(target_key, None)
                else:
                    self._target_lock_refs[target_key] = refs
            raise
        return lock

    async def _release_target_lock(self, target_key: str, lock):
        lock.release()
        async with self._target_locks_guard:
            refs = self._target_lock_refs.get(target_key, 0) - 1
            if refs <= 0:
                self._target_lock_refs.pop(target_key, None)
                if self._target_locks.get(target_key) is lock:
                    self._target_locks.pop(target_key, None)
            else:
                self._target_lock_refs[target_key] = refs

    async def _run_single_file_autobackup(
        self,
        event: AstrMessageEvent,
        file_component: Optional[File],
        file_name: str,
        file_size: Optional[int],
        file_url: str,
        target_path: str,
        user_config: Dict,
        group_id: str = "",
        user_id: str = "",
        file_id: str = None,
        file_hash: str = None,
        busid: int = 0,
    ) -> None:
        """后台执行单个群聊或私聊文件自动备份，避免阻塞同一条消息上的其他处理器。"""
        async with self.autobackup_semaphore:
            file_path = None
            try:
                safe_name = self._sanitize_remote_filename(file_name, fallback="file")
                max_size_mb = self._get_size_limit_mb(user_config, "backup_max_size", 0)
                if max_size_mb > 0 and file_size is not None and file_size > (max_size_mb * 1024 * 1024):
                    logger.info(f"⏭️ [自动备份] 文件 {safe_name} 事件大小 {file_size} 超过限制 {max_size_mb}MB，跳过。")
                    return

                logger.info(f"🚀 [自动备份] 发现新文件: {safe_name} -> {target_path}")
                target_key = self._normalize_openlist_path(f"{target_path.rstrip('/')}/{safe_name}")
                target_lock = await self._acquire_target_lock(target_key)
                try:
                    async with self._create_openlist_client(user_config) as client:
                        if not await client.ensure_dir(target_path):
                            logger.error(f"❌ [自动备份] 创建目标目录失败: {target_path}")
                            return
                        skip_existing = self._get_bool_config(user_config, "backup_skip_existing", True)
                        source_type = "group" if group_id else "private"
                        item = {
                            "source_type": source_type,
                            "user_id": user_id,
                            "file_id": file_id,
                            "file_hash": file_hash,
                            "file_name": safe_name,
                            "file_size": file_size,
                            "busid": busid,
                            "url": file_url,
                        }
                        target_file_name = safe_name
                        if skip_existing:
                            existing_files = await self.target.openlist_files_by_name(client, target_path, refresh=True)
                            existing = existing_files.get(safe_name)
                            if existing and self.target.existing_entry_matches(existing, file_size):
                                logger.info(f"⏭️ [自动备份] 跳过已存在文件: {target_path}/{safe_name}")
                                return
                            target_file_name = self.target.resolve_target_name(
                                existing_files,
                                safe_name,
                                item,
                            )
                            if target_file_name != safe_name:
                                item["_backup_target_name"] = target_file_name
                                logger.info(f"📌 [自动备份] 同名冲突文件改名备份: {safe_name} -> {target_file_name}")

                        retry_attempts = self._get_positive_int_config(user_config, "backup_retry_attempts", 3)
                        retry_delay = self._get_positive_int_config(user_config, "backup_retry_delay", 5, minimum=0)
                        if file_url or file_id:
                            success, _ = await self.uploader.upload_group_file_with_retry(
                                getattr(event, "bot", None),
                                client,
                                maybe_int_id(group_id) if group_id else None,
                                item,
                                target_path,
                                retry_attempts,
                                retry_delay,
                                initial_url=file_url,
                            )
                        elif file_component:
                            get_file_started_at = time.monotonic()
                            file_path = await file_component.get_file()
                            logger.info(
                                f"📥 [自动备份] 本地获取完成: {safe_name}, path={file_path}, "
                                f"elapsed={time.monotonic() - get_file_started_at:.2f}s"
                            )
                            if not file_path or not os.path.exists(file_path):
                                logger.error(f"❌ [自动备份] 无法获取文件路径: {safe_name}")
                                return
                            actual_size = os.path.getsize(file_path)
                            if max_size_mb > 0 and actual_size > (max_size_mb * 1024 * 1024):
                                logger.info(f"⏭️ [自动备份] 文件 {safe_name} 实际下载大小 {actual_size} 超过限制 {max_size_mb}MB，跳过。")
                                return
                            success = await client.upload_file(file_path, target_path, target_file_name)
                        else:
                            logger.warning(f"⚠️ [自动备份] 无可用下载来源，跳过文件: {safe_name}")
                            return

                        if success:
                            logger.info(f"✅ [自动备份] 文件 {target_file_name} 上传成功。")
                            self._invalidate_openlist_cache(user_config, target_path, user_id or None)
                        else:
                            logger.error(f"❌ [自动备份] 文件 {target_file_name} 上传失败。")
                finally:
                    await self._release_target_lock(target_key, target_lock)
            except Exception as e:
                logger.error(f"❌ [自动备份] 处理文件 {file_name} 出错: {e}", exc_info=True)
            finally:
                self._remove_file_quietly(file_path, f"[自动备份] 临时文件 file={file_name}")

    async def _run_flash_autobackup(
        self,
        event: AstrMessageEvent,
        descriptor: Dict,
        target_root_path: str,
        user_config: Dict,
        group_id: str = "",
        user_id: str = "",
    ) -> None:
        """后台解析并自动备份 QQ 闪传文件集中的所有符合条件的文件（保留子目录结构）。"""
        async with self.autobackup_semaphore:
            try:
                fileset_id, flash_files = await resolve_flash_transfer_files(event, descriptor)
                if not flash_files:
                    logger.warning(f"⚠️ [闪传自动备份] 未解析到闪传文件列表: {descriptor}")
                    return
                logger.info(
                    f"⚡ [闪传自动备份] 解析到 {len(flash_files)} 个闪传文件 (fileset={fileset_id}) -> {target_root_path}"
                )
                allowed_exts = self._get_extension_filter(user_config, "backup_allowed_extensions")
                max_size_mb = self._get_size_limit_mb(user_config, "backup_max_size", 0)
                max_size = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0
                retry_attempts = self._get_positive_int_config(user_config, "backup_retry_attempts", 3)
                retry_delay = self._get_positive_int_config(user_config, "backup_retry_delay", 5, minimum=0)
                skip_existing = self._get_bool_config(user_config, "backup_skip_existing", True)

                async with self._create_openlist_client(user_config) as client:
                    ensured_dirs = set()
                    uploaded_any = False
                    for raw_item in flash_files:
                        item = self.scanner._strip_transient_fields(raw_item)
                        file_name = item.get("file_name") or "flash_file"
                        file_size = to_int_or_none(item.get("file_size"))
                        if allowed_exts and os.path.splitext(file_name.lower())[1] not in allowed_exts:
                            continue
                        if max_size > 0 and file_size is not None and file_size > max_size:
                            continue

                        rel_path = sanitize_relative_subpath(
                            item.get("relative_path") or file_name,
                            fallback_name=file_name,
                        )
                        rel_dir = posixpath.dirname(rel_path)
                        target_dir = (
                            self._normalize_openlist_path(f"{target_root_path.rstrip('/')}/{rel_dir}")
                            if rel_dir
                            else self._normalize_openlist_path(target_root_path)
                        )
                        lock_key = self._normalize_openlist_path(f"{target_dir.rstrip('/')}/{file_name}")
                        target_lock = await self._acquire_target_lock(lock_key)
                        try:
                            if target_dir not in ensured_dirs:
                                if not await client.ensure_dir(target_dir):
                                    logger.error(f"❌ [闪传自动备份] 创建目录失败: {target_dir}")
                                    continue
                                ensured_dirs.add(target_dir)

                            if skip_existing:
                                existing_files = await self.target.openlist_files_by_name(client, target_dir, refresh=True)
                                existing = existing_files.get(file_name)
                                if existing and self.target.existing_entry_matches(existing, file_size):
                                    logger.info(f"⏭️ [闪传自动备份] 跳过已存在文件: {target_dir}/{file_name}")
                                    continue
                                resolved_name = self.target.resolve_target_name(existing_files, file_name, item)
                                if resolved_name != file_name:
                                    item["_backup_target_name"] = resolved_name

                            ok, _ = await self.uploader.upload_group_file_with_retry(
                                getattr(event, "bot", None),
                                client,
                                maybe_int_id(group_id) if group_id else None,
                                item,
                                target_dir,
                                retry_attempts,
                                retry_delay,
                                initial_url=item.get("url"),
                            )
                            if ok:
                                uploaded_any = True
                        finally:
                            await self._release_target_lock(lock_key, target_lock)

                    if uploaded_any:
                        self._invalidate_openlist_cache(user_config, target_root_path, user_id or None)
            except Exception as e:
                logger.error(f"❌ [闪传自动备份] 处理闪传出错: {e}", exc_info=True)

    def _resolve_autobackup_context(self, group_id: str, user_id: str) -> Tuple[Optional[str], Optional[Dict], str]:
        """返回 (target_path, effective_config, scope_key)。"""
        global_cfg = self.get_global_config()
        if group_id:
            target_path = self._get_autobackup_target_path(global_cfg, group_id)
            return target_path, global_cfg, f"group:{group_id}"
        if user_id:
            target_path = self._get_private_autobackup_target_path(global_cfg, user_id)
            effective_cfg = self.get_user_config(user_id)
            return target_path, effective_cfg, f"private:{user_id}"
        return None, None, ""

    async def handle_group_file_upload(self, event: AstrMessageEvent):
        """处理群聊文件、私聊离线文件与 QQ 闪传事件（自动备份）"""
        file_payloads, flash_payloads = self._extract_autobackup_payloads(event)
        if not file_payloads and not flash_payloads:
            return

        for payload in file_payloads:
            file_name = payload.get("file_name")
            if not file_name:
                continue
            file_id = payload.get("file_id")
            file_hash = payload.get("file_hash")
            file_size = payload.get("file_size")
            file_url = payload.get("file_url")
            file_component = payload.get("file_component")
            group_id = str(payload.get("group_id") or "")
            user_id = str(payload.get("user_id") or "")
            if not group_id and not user_id:
                continue
            if not (file_id or file_url or file_component):
                continue

            target_path, user_config, scope_key = self._resolve_autobackup_context(group_id, user_id)
            if not target_path or not user_config:
                continue
            if not self._validate_config(user_config):
                logger.warning(f"⚠️ [自动备份] {scope_key} 触发了自动备份，但未找到有效的 Openlist 配置。")
                continue

            if file_size is not None:
                max_size_mb = self._get_size_limit_mb(user_config, "backup_max_size", 0)
                if max_size_mb > 0 and file_size > (max_size_mb * 1024 * 1024):
                    logger.info(f"⏭️ [自动备份] 文件 {file_name} 超过限制 {max_size_mb}MB，跳过。")
                    continue

            allowed_exts = self._get_extension_filter(user_config, "backup_allowed_extensions")
            if allowed_exts:
                ext = os.path.splitext(file_name.lower())[1]
                if ext not in allowed_exts:
                    logger.info(f"⏭️ [自动备份] 文件 {file_name} 后缀 {ext} 不在允许范围内，跳过。")
                    continue

            dedup_id = str(file_id) if file_id else f"{file_name}:{file_size}"
            fingerprint = f"{scope_key}:file:{dedup_id}"
            if self._is_duplicate_autobackup_event(fingerprint):
                logger.debug(f"⏭️ [自动备份] 忽略短时间内重复上报的文件事件: {fingerprint}")
                continue

            task_user_config = dict(user_config)
            self._spawn_background_task(
                self._run_single_file_autobackup(
                    event=event,
                    file_component=file_component,
                    file_name=file_name,
                    file_size=file_size,
                    file_url=file_url or "",
                    target_path=target_path,
                    user_config=task_user_config,
                    group_id=group_id,
                    user_id=user_id,
                    file_id=file_id,
                    file_hash=file_hash,
                    busid=payload.get("busid", 0),
                )
            )
            logger.info(
                f"🧵 [自动备份] 已转入后台任务: scope={scope_key}, file={file_name}, "
                f"source={payload.get('source')}, has_file_id={bool(file_id)}, has_url={bool(file_url)}"
            )

        for f_payload in flash_payloads:
            group_id = str(f_payload.get("group_id") or "")
            user_id = str(f_payload.get("user_id") or "")
            descriptor = f_payload.get("descriptor") or {}
            if not group_id and not user_id:
                continue
            target_path, user_config, scope_key = self._resolve_autobackup_context(group_id, user_id)
            if not target_path or not user_config or not self._validate_config(user_config):
                continue

            flash_id = descriptor.get("fileset_id") or descriptor.get("share_code") or descriptor.get("url")
            if not flash_id:
                continue
            fingerprint = f"{scope_key}:flash:{flash_id}"
            if self._is_duplicate_autobackup_event(fingerprint):
                continue

            self._spawn_background_task(
                self._run_flash_autobackup(
                    event=event,
                    descriptor=dict(descriptor),
                    target_root_path=target_path,
                    user_config=dict(user_config),
                    group_id=group_id,
                    user_id=user_id,
                )
            )
            logger.info(f"🧵 [闪传自动备份] 已转入后台任务: scope={scope_key}, flash={flash_id}")

    async def _backup_group_files(self, event: AstrMessageEvent, group_id, target_path: str, user_config: Dict):
        """执行群文件备份"""
        async for result in self._do_backup_logic(
            getattr(event, "bot", None),
            event,
            group_id,
            target_path,
            user_config,
            retry_key=self._get_backup_retry_key(event),
        ):
            yield result

    async def _backup_private_files(self, event: AstrMessageEvent, private_user_id: str, target_path: str, user_config: Dict):
        """执行私聊文件与闪传手动备份"""
        async for result in self._do_backup_logic(
            getattr(event, "bot", None),
            event,
            None,
            target_path,
            user_config,
            private_user_id=str(private_user_id),
            retry_key=self._get_backup_retry_key(event),
        ):
            yield result

    async def _retry_last_backup(self, event: AstrMessageEvent, user_config: Dict):
        """重试最近一次手动备份失败项。"""
        retry_key = self._get_backup_retry_key(event)
        retry_state = self._load_backup_retry_state(retry_key)
        if not retry_state or not retry_state.get("items"):
            yield event.plain_result("💡 当前会话没有可重试的备份失败项。")
            return

        scope_type = retry_state.get("scope_type", "group")
        group_id = retry_state.get("group_id")
        private_user_id = retry_state.get("private_user_id")
        target_path = retry_state["target_path"]
        failed_items = retry_state["items"]

        if scope_type == "private" or (not group_id and private_user_id):
            sender_id = str(event.get_sender_id())
            if str(private_user_id) != sender_id and not self._is_bot_global_admin(event):
                yield event.plain_result("❌ 权限不足：只能重试本人私聊备份，或由机器人管理员操作。")
                return
        elif group_id:
            if await self._deny_if_no_target_group_permission(event, group_id, "备份重试"):
                yield event.plain_result("❌ 权限不足：只能重试当前群备份，或由目标群群主/管理员重试指定群备份。")
                return

        self._delete_backup_retry_state(retry_key)
        yield event.plain_result(
            f"🔁 开始重试上次备份失败的 {len(failed_items)} 个文件\n"
            f"📂 目标: {target_path}"
        )
        async for result in self._do_backup_logic(
            getattr(event, "bot", None),
            event,
            group_id,
            target_path,
            user_config,
            private_user_id=private_user_id,
            is_auto=False,
            items_override=failed_items,
            retry_key=retry_key,
            is_retry=True,
        ):
            yield result

    async def _do_backup_logic(
        self,
        bot,
        event: AstrMessageEvent,
        group_id,
        target_path: str,
        user_config: Dict,
        private_user_id: Optional[str] = None,
        is_auto: bool = False,
        items_override: Optional[List[Dict]] = None,
        retry_key: str = None,
        is_retry: bool = False,
        cancel_event: Optional[asyncio.Event] = None,
        cancel_title: str = "备份任务",
    ):
        """核心备份逻辑，支持群文件、私聊文件与闪传的手动和自动备份"""
        cancelled = False
        is_private_scope = bool(private_user_id and not group_id)
        scope_label = f"私聊 {private_user_id}" if is_private_scope else f"群 {group_id}"

        def should_cancel() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        if not is_auto and not is_retry:
            logger.info(f"🔍 [备份] 正在扫描 {scope_label} 的文件与闪传")

        if items_override is not None:
            filtered_items = list(items_override)
            scan_stats = {
                "raw_count": len(filtered_items),
                "filtered_count": len(filtered_items),
                "target_path": target_path,
            }
            original_count = len(filtered_items)
            filtered_items = self.target.deduplicate_backup_items(filtered_items, target_path)
            scan_stats["deduped_count"] = len(filtered_items)
            scan_stats["duplicate_skipped"] = original_count - len(filtered_items)
        elif is_private_scope:
            scan_result = await self.scanner.scan_private(
                bot,
                str(private_user_id),
                target_path,
                user_config,
                cached_records=self.get_private_transfer_cache(str(private_user_id)),
                cancel_event=cancel_event,
            )
            if scan_result.get("cancelled") or should_cancel():
                if not is_auto:
                    yield event.plain_result(
                        f"🛑 {cancel_title}已取消\n"
                        f"📊 已处理: 总计 0, 成功 0, 跳过 0, 失败 0, 未处理 0\n"
                        f"📂 目标: {target_path}"
                    )
                return
            if not scan_result.get("found"):
                if not is_auto:
                    yield event.plain_result("💡 未在当前私聊历史或缓存中找到可备份的文件或 QQ 闪传。")
                return
            filtered_items = scan_result["items"]
            scan_stats = scan_result["stats"]
        else:
            scan_result = await self.scanner.scan(
                bot,
                group_id,
                target_path,
                user_config,
                cancel_event=cancel_event,
            )
            if scan_result.get("cancelled") or should_cancel():
                if not is_auto:
                    yield event.plain_result(
                        f"🛑 {cancel_title}已取消\n"
                        f"📊 已处理: 总计 0, 成功 0, 跳过 0, 失败 0, 未处理 0\n"
                        f"📂 目标: {target_path}"
                    )
                else:
                    logger.info(f"🛑 [自动备份] 任务已取消。{scope_label}: 扫描阶段取消")
                return
            if not scan_result.get("found"):
                if not is_auto:
                    yield event.plain_result("❌ 未找到任何群文件、闪传或获取失败。")
                return
            filtered_items = scan_result["items"]
            scan_stats = scan_result["stats"]

        if not filtered_items:
            if not is_auto:
                message = "⚠️ 没有可重试的失败项。" if is_retry else "⚠️ 扫描完成，但没有符合过滤条件的文件需要备份。"
                yield event.plain_result(message)
            return

        total = len(filtered_items)
        if should_cancel():
            if not is_auto:
                yield event.plain_result(
                    f"🛑 {cancel_title}已取消\n"
                    f"📊 已处理: 总计 {total}, 成功 0, 跳过 0, 失败 0, 未处理 {total}\n"
                    f"📂 目标: {target_path}"
                )
            return

        success_count = 0
        fail_count = 0
        skipped_count = 0
        failed_items = []
        retry_attempts = self._get_positive_int_config(user_config, "backup_retry_attempts", 3)
        retry_delay = self._get_positive_int_config(user_config, "backup_retry_delay", 5, minimum=0)
        skip_existing = self._get_bool_config(user_config, "backup_skip_existing", True)
        existing_cache = {}
        existing_cache_lock = asyncio.Lock()

        async with self._create_openlist_client(user_config) as client:
            prepared_dirs = set()
            list_failed_dirs = set()

            async def get_existing_files(target_dir: str) -> Dict[str, Dict]:
                target_dir = target_dir or "/"
                async with existing_cache_lock:
                    return dict(existing_cache.get(target_dir, {}))

            async def record_uploaded_file(target_dir: str, fname: str, fsize):
                target_dir = target_dir or "/"
                async with existing_cache_lock:
                    dir_map = existing_cache.setdefault(target_dir, {})
                    dir_map[fname] = {"name": fname, "size": fsize or 0}

            async def refresh_existing_files(target_dir: str) -> Dict[str, Dict]:
                target_dir = target_dir or "/"
                list_result = await client.list_files(target_dir, per_page=0, refresh=True)
                files = {}
                if list_result is None:
                    list_failed_dirs.add(target_dir)
                    logger.warning(
                        f"⚠️ [备份] 获取目标目录文件列表失败，本轮不对该目录执行已存在跳过: {target_dir}"
                    )
                else:
                    for existing in list_result.get("content") or []:
                        if not existing.get("is_dir", False):
                            files[existing.get("name", "")] = existing
                async with existing_cache_lock:
                    existing_cache[target_dir] = files
                return files

            target_dirs = sorted(
                {
                    (self.target.item_target(target_path, item)[0] or "/")
                    for item in filtered_items
                }
            )
            failed_dirs = set()
            for target_dir in target_dirs:
                if should_cancel():
                    cancelled = True
                    break
                logger.info(f"📁 [备份] 检查/创建目标目录: {target_dir}")
                if await client.ensure_dir(target_dir):
                    prepared_dirs.add(target_dir)
                else:
                    failed_dirs.add(target_dir)
                    logger.error(f"❌ [备份] 创建目标目录失败: {target_dir}")

            if not cancelled and skip_existing:
                for target_dir in sorted(prepared_dirs):
                    if should_cancel():
                        cancelled = True
                        break
                    await refresh_existing_files(target_dir)

            upload_items = []
            if failed_dirs:
                for item in filtered_items:
                    target_dir, file_name, _ = self.target.item_target(target_path, item)
                    target_dir = target_dir or "/"
                    if target_dir in failed_dirs:
                        fail_count += 1
                        failed_item = self._sanitize_retry_item(item)
                        failed_item["_backup_fail_reason"] = f"创建目标目录失败: {target_dir}"
                        failed_items.append(failed_item)
                        logger.error(f"❌ [备份] 目标目录创建失败，跳过文件: {target_dir}/{file_name}")
                    else:
                        upload_items.append(item)
            else:
                upload_items = list(filtered_items)

            if not is_auto and not cancelled:
                if upload_items:
                    if is_retry:
                        logger.info(f"🚀 [备份] 目录检查完成，开始重试备份: {target_path}")
                    else:
                        yield event.plain_result(
                            f"{self._format_backup_scan_summary(scan_stats)}\n"
                            f"🚀 开始备份"
                        )
                elif failed_dirs:
                    yield event.plain_result(f"❌ 目标目录创建失败，无法开始备份: {', '.join(sorted(failed_dirs))}")

            async def process_one_item(item):
                nonlocal success_count, fail_count, skipped_count
                if should_cancel():
                    return
                target_dir, file_name, _ = self.target.item_target(target_path, item)
                original_target_dir, original_file_name, _ = self.target.item_target(target_path, item, use_override=False)
                if not file_name:
                    fail_count += 1
                    failed_item = self._sanitize_retry_item(item)
                    failed_item["_backup_fail_reason"] = "文件名为空"
                    failed_items.append(failed_item)
                    return

                lock_key = self.target.item_lock_key(target_path, item)
                target_lock = await self._acquire_target_lock(lock_key)
                try:
                    target_dir = target_dir or "/"
                    if target_dir not in prepared_dirs and not await client.ensure_dir(target_dir):
                        fail_count += 1
                        failed_item = self._sanitize_retry_item(item)
                        failed_item["_backup_fail_reason"] = f"创建目标目录失败: {target_dir}"
                        failed_items.append(failed_item)
                        return

                    if should_cancel():
                        return

                    existing_files = await get_existing_files(target_dir)
                    can_check_existing = skip_existing and target_dir not in list_failed_dirs
                    item_size = self._backup_item_size(item)
                    if can_check_existing:
                        existing = existing_files.get(file_name)
                        if existing and self.target.existing_entry_matches(existing, item_size):
                            skipped_count += 1
                            logger.info(f"⏭️ [备份] 跳过已存在文件: {target_dir}/{file_name}")
                            return
                        if (
                            item.get("_backup_target_name")
                            and original_target_dir == target_dir
                            and original_file_name != file_name
                        ):
                            original_existing = existing_files.get(original_file_name)
                            if original_existing and self.target.existing_entry_matches(original_existing, item_size):
                                skipped_count += 1
                                logger.info(f"⏭️ [备份] 跳过已存在文件: {target_dir}/{original_file_name}")
                                return

                    resolved_name = self.target.resolve_target_name(existing_files, file_name, item)
                    if resolved_name != file_name:
                        item = dict(item)
                        item["_backup_target_name"] = resolved_name
                        logger.info(f"📌 [备份] 同名冲突文件改名备份: {file_name} -> {resolved_name}")
                        file_name = resolved_name

                    if should_cancel():
                        return

                    resolved_existing = existing_files.get(file_name)
                    if (
                        can_check_existing
                        and resolved_existing
                        and self.target.existing_entry_matches(resolved_existing, item_size)
                    ):
                        skipped_count += 1
                        logger.info(f"⏭️ [备份] 跳过已存在文件: {target_dir}/{file_name}")
                        return

                    up_res, reason = await self.uploader.upload_group_file_with_retry(
                        bot,
                        client,
                        group_id,
                        item,
                        target_dir,
                        retry_attempts,
                        retry_delay,
                    )
                    if up_res:
                        success_count += 1
                        await record_uploaded_file(target_dir, file_name, item_size)
                    else:
                        fail_count += 1
                        failed_item = self._sanitize_retry_item(item)
                        failed_item["_backup_fail_reason"] = self._short_backup_fail_reason(reason)
                        failed_items.append(failed_item)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    reason = self._short_backup_fail_reason(e)
                    logger.error(f"备份文件 {file_name} 失败: {reason}", exc_info=True)
                    fail_count += 1
                    failed_item = self._sanitize_retry_item(item)
                    failed_item["_backup_fail_reason"] = reason
                    failed_items.append(failed_item)
                finally:
                    await self._release_target_lock(lock_key, target_lock)

            if not cancelled and upload_items:
                queue: asyncio.Queue = asyncio.Queue()
                for entry in upload_items:
                    queue.put_nowait(entry)

                progress_counter = 0

                async def worker():
                    nonlocal progress_counter
                    while not should_cancel():
                        try:
                            current_item = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        try:
                            await process_one_item(current_item)
                        finally:
                            queue.task_done()
                            progress_counter += 1
                            if progress_counter % 5 == 0 or progress_counter == len(upload_items):
                                processed_count = min(total, success_count + skipped_count + fail_count)
                                logger.info(
                                    f"⏳ 备份进度: {processed_count}/{total} "
                                    f"(成功: {success_count}, 跳过: {skipped_count}, 失败: {fail_count})"
                                )

                worker_count = min(3, len(upload_items))
                workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
                workers_future = asyncio.gather(*workers, return_exceptions=True)

                if cancel_event is None:
                    await workers_future
                else:
                    cancel_waiter = asyncio.create_task(cancel_event.wait())
                    try:
                        done, _ = await asyncio.wait(
                            {workers_future, cancel_waiter},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if cancel_waiter in done and cancel_event.is_set():
                            cancelled = True
                            for w in workers:
                                if not w.done():
                                    w.cancel()
                            await workers_future
                        else:
                            await workers_future
                            if cancel_event.is_set():
                                cancelled = True
                    finally:
                        if not cancel_waiter.done():
                            cancel_waiter.cancel()
                        await asyncio.gather(cancel_waiter, return_exceptions=True)

        if cancelled or should_cancel():
            if success_count:
                self._invalidate_openlist_cache(user_config, target_path)
            processed_count = success_count + skipped_count + fail_count
            remaining_count = max(0, total - processed_count)
            if not is_auto:
                failed_list = self._format_failed_backup_items(failed_items)
                yield event.plain_result(
                    f"🛑 {cancel_title}已取消\n"
                    f"📊 已处理: 总计 {total}, 成功 {success_count}, 跳过 {skipped_count}, "
                    f"失败 {fail_count}, 未处理 {remaining_count}\n"
                    f"📂 目标: {target_path}"
                    f"{failed_list}"
                )
            else:
                logger.info(
                    f"🛑 [自动备份] 任务已取消。{scope_label}: "
                    f"成功 {success_count}, 跳过 {skipped_count}, 失败 {fail_count}, 未处理 {remaining_count}"
                )
            return

        if not is_auto:
            if success_count:
                self._invalidate_openlist_cache(user_config, target_path)
            if retry_key:
                if failed_items:
                    self._save_backup_retry_state(
                        retry_key,
                        {
                            "scope_type": "private" if is_private_scope else "group",
                            "group_id": group_id,
                            "private_user_id": private_user_id,
                            "target_path": target_path,
                            "items": failed_items,
                            "timestamp": time.time(),
                        },
                    )
                else:
                    self._delete_backup_retry_state(retry_key)
            retry_hint = "\n💡 发送 ol backup retry 可只重试失败项。" if failed_items else ""
            failed_list = self._format_failed_backup_items(failed_items)
            yield event.plain_result(
                f"✅ 备份任务结束!\n"
                f"📊 统计: 总计 {total}, 成功 {success_count}, 跳过 {skipped_count}, 失败 {fail_count}\n"
                f"📂 目标: {target_path}"
                f"{failed_list}"
                f"{retry_hint}"
            )
        else:
            if success_count:
                self._invalidate_openlist_cache(user_config, target_path)
            logger.info(f"✅ [自动备份] 任务结束。{scope_label}: 成功 {success_count}, 跳过 {skipped_count}, 失败 {fail_count}")

    @staticmethod
    def _parse_scope_target_token(token: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        解析备份/自动备份中的目标标识参数：
        - '@123456' -> ('group', '123456', None)
        - 'private:@987654' / 'user:@987654' / '@u987654' -> ('private', '987654', None)
        - 'private:all' / 'private:*' -> ('private', '*', None)
        返回 (scope_type, target_id, error_msg)
        """
        lower = token.lower()
        if lower.startswith(("private:", "user:")):
            val = token.split(":", 1)[1].strip().lstrip("@")
            if not val:
                return None, None, f"无效的私聊目标格式：{token}"
            if val.lower() in ("all", "*"):
                return "private", "*", None
            return "private", val, None
        if lower.startswith("@u") and len(token) > 2 and token[2:].isdigit():
            return "private", token[2:], None
        if token.startswith("@"):
            gid_part = token[1:].strip()
            if not gid_part:
                return None, None, f"无效的群号格式：{token}"
            return "group", gid_part, None
        return None, None, f"无法识别参数：{token}"

    async def backup_command(self, event: AstrMessageEvent, path_or_group: str = "", group_or_path: str = ""):
        """群聊文件、私聊文件或闪传手动备份到 Openlist"""
        user_id = str(event.get_sender_id())
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result(self._format_usage_tip(
                "请先配置 OpenList 连接信息",
                "ol config setup",
                [
                    "ol config setup",
                    "ol config set openlist_url http://127.0.0.1:5244",
                    "ol config test",
                ],
            ))
            return

        path_or_group = (path_or_group or "").strip()
        group_or_path = (group_or_path or "").strip()
        if path_or_group.lower() in ("retry", "重试") or group_or_path.lower() in ("retry", "重试"):
            async for result in self._retry_last_backup(event, user_config):
                yield result
            return

        target_path_arg = None
        explicit_scope = None
        explicit_target_id = None

        for arg in [path_or_group, group_or_path]:
            if not arg:
                continue
            if arg.startswith("/"):
                target_path_arg = arg
            else:
                scope, tid, err = self._parse_scope_target_token(arg)
                if err:
                    yield event.plain_result(self._format_backup_usage_tip(err))
                    return
                if scope == "private" and tid == "*":
                    yield event.plain_result(self._format_backup_usage_tip("手动备份不支持通配符私聊目标"))
                    return
                explicit_scope = scope
                explicit_target_id = tid

        if explicit_scope == "group":
            if explicit_target_id.isdigit():
                target_group_id = int(explicit_target_id)
            else:
                yield event.plain_result(self._format_backup_usage_tip(f"无效的群号格式：@{explicit_target_id}"))
                return
            if await self._deny_if_no_target_group_permission(event, target_group_id, "手动备份"):
                yield event.plain_result(self._format_usage_tip(
                    "权限不足：无法备份目标群",
                    "ol backup [@群号] [/OpenList目标路径]",
                    [
                        "ol backup",
                        "ol backup @123456 /backup/group_123456",
                    ],
                    "只能备份当前群，或由目标群群主/管理员指定 @群号。",
                ))
                return
            target_path = self._render_backup_path(
                target_path_arg or user_config.get("backup_default_path", "/backup/group_{group_id}"),
                group_id=target_group_id,
                user_id=user_id,
            )
            async for result in self._backup_group_files(event, target_group_id, target_path, user_config):
                yield result
            return

        if explicit_scope == "private":
            if str(explicit_target_id) != user_id and not self._is_bot_global_admin(event):
                yield event.plain_result("❌ 权限不足：只能备份本人私聊文件，或由机器人全局管理员操作。")
                return
            target_path = self._render_backup_path(
                target_path_arg or user_config.get("private_backup_default_path", "/backup/private_{user_id}"),
                user_id=explicit_target_id,
            )
            async for result in self._backup_private_files(event, explicit_target_id, target_path, user_config):
                yield result
            return

        # 未显式指定目标：群聊默认备份当前群，私聊默认备份当前私聊会话文件与闪传
        event_group_id = self._get_event_group_id(event)
        if event_group_id not in (None, ""):
            target_group_id = maybe_int_id(event_group_id)
            target_path = self._render_backup_path(
                target_path_arg or user_config.get("backup_default_path", "/backup/group_{group_id}"),
                group_id=target_group_id,
                user_id=user_id,
            )
            async for result in self._backup_group_files(event, target_group_id, target_path, user_config):
                yield result
        else:
            target_path = self._render_backup_path(
                target_path_arg or user_config.get("private_backup_default_path", "/backup/private_{user_id}"),
                user_id=user_id,
            )
            async for result in self._backup_private_files(event, user_id, target_path, user_config):
                yield result

    async def autobackup_command(self, event: AstrMessageEvent, action: str = "show", target_or_group: str = "", path_or_group: str = ""):
        """配置群聊或私聊自动备份"""
        global_cfg = self.get_global_config()
        sender_id = str(event.get_sender_id())
        action = (action or "show").lower()
        cancel_actions = ("cancel", "stop", "取消", "停止")

        if action in ("show", "status", "list", "状态", "列表"):
            if not self._is_event_admin(event) and not bool(global_cfg.get("require_user_auth", False)):
                yield event.plain_result(self._format_autobackup_usage_tip("权限不足：只有群主或管理员可以查看/配置自动备份"))
                return

            effective_groups = global_cfg.get("autobackup_groups", [])
            effective_privates = global_cfg.get("autobackup_private_users", [])
            lines = ["🔄 自动备份配置", ""]
            if effective_groups:
                lines.append("已启用群组:")
                for item in effective_groups:
                    if not isinstance(item, str):
                        continue
                    if ":" in item:
                        gid, path = item.split(":", 1)
                        path = self._render_backup_path(path, group_id=gid)
                    else:
                        gid = item
                        path = self._render_backup_path(
                            global_cfg.get("autobackup_default_path", "/backup/group_{group_id}"),
                            group_id=gid,
                        )
                    lines.append(f"• 群 {gid} -> {path}")
            else:
                lines.append("当前没有启用自动备份的群组。")

            if effective_privates:
                lines.extend(["", "已启用私聊:"])
                for item in effective_privates:
                    if not isinstance(item, str):
                        continue
                    if ":" in item:
                        uid, path = item.split(":", 1)
                        label = "全部私聊" if uid in ("*", "all") else f"用户 {uid}"
                        path_str = path.strip() or global_cfg.get("private_autobackup_default_path", "/backup/private_{user_id}")
                        if uid not in ("*", "all"):
                            path_str = self._render_backup_path(path_str, user_id=uid)
                    else:
                        uid = item
                        label = "全部私聊" if uid in ("*", "all") else f"用户 {uid}"
                        path_str = global_cfg.get("private_autobackup_default_path", "/backup/private_{user_id}")
                        if uid not in ("*", "all"):
                            path_str = self._render_backup_path(path_str, user_id=uid)
                    lines.append(f"• {label} -> {path_str}")

            if self._autobackup_full_meta:
                lines.extend(["", "正在执行首次全量备份:"])
                for cancel_key, meta in self._autobackup_full_meta.items():
                    cancel_event = self._autobackup_full_cancel_events.get(cancel_key)
                    status = "取消中" if cancel_event and cancel_event.is_set() else "运行中"
                    target_path = meta.get("target_path", "")
                    scope_desc = f"私聊 {cancel_key.split(':', 1)[1]}" if str(cancel_key).startswith("private:") else f"群 {cancel_key}"
                    lines.append(f"• {scope_desc} -> {target_path} ({status})")

            lines.extend([
                "",
                "用法：",
                "ol autobackup enable [@群号|private:@QQ号] [/OpenList路径]",
                "ol autobackup disable [@群号|private:@QQ号]",
                "ol autobackup cancel [@群号|private:@QQ号]",
                "",
                "示例：",
                "  ol autobackup enable",
                "  ol autobackup enable @123456 /backup/group_123456",
                "  ol autobackup enable private:@987654 /backup/private_987654",
                "  ol autobackup disable @123456",
            ])
            yield event.plain_result("\n".join(lines))
            return

        scope_type = None
        target_id = None
        target_path = None

        for arg in [target_or_group, path_or_group]:
            if not arg:
                continue
            if arg.startswith("/"):
                target_path = arg
            else:
                parsed_scope, parsed_id, err = self._parse_scope_target_token(arg)
                if err:
                    yield event.plain_result(self._format_autobackup_usage_tip(err))
                    return
                scope_type = parsed_scope
                target_id = parsed_id

        # 未指定目标时：群聊操作当前群，私聊操作当前私聊用户
        if not scope_type:
            event_group_id = self._get_event_group_id(event)
            if event_group_id not in (None, ""):
                scope_type = "group"
                target_id = str(event_group_id)
            else:
                scope_type = "private"
                target_id = sender_id

        # 权限检查
        if scope_type == "group":
            if not self._is_event_admin(event):
                yield event.plain_result(self._format_autobackup_usage_tip("权限不足：只有群主或管理员可以配置群自动备份"))
                return
            if await self._deny_if_no_target_group_permission(event, target_id, "自动备份配置"):
                yield event.plain_result(self._format_usage_tip(
                    "权限不足：无法配置目标群自动备份",
                    "ol autobackup <enable|disable|cancel> [@群号] [/OpenList目标路径]",
                    [
                        "ol autobackup enable",
                        "ol autobackup enable @123456 /backup/group_123456",
                    ],
                    "只能配置当前群，或由目标群群主/管理员指定 @群号。",
                ))
                return
        else:
            # 私聊自动备份权限：机器人全局管理员，或用户独立认证模式下配置本人私聊
            is_self_private_user_auth = (
                bool(global_cfg.get("require_user_auth", False))
                and target_id == sender_id
                and self._validate_config(self.get_user_config(sender_id))
            )
            if not self._is_bot_global_admin(event) and not is_self_private_user_auth:
                yield event.plain_result(
                    self._format_autobackup_usage_tip("权限不足：全局共享模式下只有机器人管理员可以配置私聊自动备份")
                )
                return

        cancel_key = str(target_id) if scope_type == "group" else f"private:{target_id}"
        target_label = f"群 {target_id}" if scope_type == "group" else (
            "全部私聊" if target_id == "*" else f"私聊用户 {target_id}"
        )

        if action in cancel_actions:
            if target_path:
                yield event.plain_result(self._format_autobackup_usage_tip("取消首次全量备份不需要提供路径"))
                return
            cancel_event = self._autobackup_full_cancel_events.get(cancel_key)
            if not cancel_event:
                yield event.plain_result(f"💡 {target_label} 当前没有正在执行的首次全量备份。")
                return
            if cancel_event.is_set():
                yield event.plain_result(f"💡 {target_label} 的首次全量备份正在取消中，请稍候。")
                return
            cancel_event.set()
            meta = self._autobackup_full_meta.get(cancel_key, {})
            target_line = f"\n📂 目标: {meta.get('target_path')}" if meta.get("target_path") else ""
            yield event.plain_result(
                f"🛑 已请求取消 {target_label} 的首次全量备份。"
                f"{target_line}\n"
                f"正在停止未完成的上传任务，请稍候。"
            )
            return

        raw_local = self.global_config_manager.load_raw_file()
        list_field = "autobackup_groups" if scope_type == "group" else "autobackup_private_users"
        entries = raw_local.get(list_field, [])
        if not isinstance(entries, list):
            entries = []
        target_path_was_provided = target_path is not None

        if action == "enable":
            if scope_type == "group":
                requested_target_path = self._render_backup_path(
                    target_path or global_cfg.get("autobackup_default_path", "/backup/group_{group_id}"),
                    group_id=target_id,
                )
                current_target_path = self._get_autobackup_target_path(global_cfg, target_id)
            else:
                default_tpl = global_cfg.get("private_autobackup_default_path", "/backup/private_{user_id}")
                if target_id == "*":
                    requested_target_path = (target_path or default_tpl).strip()
                else:
                    requested_target_path = self._render_backup_path(
                        target_path or default_tpl,
                        user_id=target_id,
                    )
                current_target_path = None
                for entry in entries:
                    if self._autobackup_entry_group_id(entry) == target_id:
                        raw_p = entry.split(":", 1)[1].strip() if ":" in entry else default_tpl
                        current_target_path = (
                            raw_p if target_id == "*" else self._render_backup_path(raw_p, user_id=target_id)
                        )
                        break

            running_cancel_event = self._autobackup_full_cancel_events.get(cancel_key)
            if running_cancel_event:
                if running_cancel_event.is_set():
                    yield event.plain_result(f"💡 {target_label} 的首次全量备份正在取消中，请稍候再重新开启。")
                else:
                    cancel_arg = f"@{target_id}" if scope_type == "group" else f"private:@{target_id}"
                    yield event.plain_result(
                        f"💡 {target_label} 的首次全量备份正在执行。\n"
                        f"如需停止，请发送 ol autobackup cancel {cancel_arg}"
                    )
                return

            if current_target_path:
                if current_target_path == requested_target_path:
                    yield event.plain_result(
                        f"💡 {target_label} 自动备份已开启。\n"
                        f"📂 目标: {current_target_path}\n"
                        "不会重复执行首次全量备份。"
                    )
                    return
                if not target_path_was_provided:
                    yield event.plain_result(
                        f"💡 {target_label} 自动备份已开启。\n"
                        f"📂 当前目标: {current_target_path}\n"
                        "未提供新的 OpenList 路径，已保持现有配置，不会重复执行首次全量备份。"
                    )
                    return

            target_path = requested_target_path
            new_entry = f"{target_id}:{target_path}"
            new_entries = [item for item in entries if self._autobackup_entry_group_id(item) != target_id]
            new_entries.append(new_entry)
            self.global_config_manager.save_runtime_fields({list_field: new_entries})

            if target_id == "*":
                yield event.plain_result(
                    f"✅ 已开启全部私聊自动备份 -> 模板 {target_path}\n"
                    "后续收到的私聊文件与 QQ 闪传将自动备份。"
                )
                return

            if current_target_path:
                yield event.plain_result(
                    f"✅ {target_label} 自动备份目录已更新\n"
                    f"📂 原目标: {current_target_path}\n"
                    f"📂 新目标: {target_path}\n"
                    "📦 正在对新目标执行首次全量备份..."
                )
            else:
                yield event.plain_result(
                    f"✅ {target_label} 自动备份已开启 -> {target_path}\n"
                    "📦 正在执行首次全量备份..."
                )

            backup_config = self.get_global_config() if scope_type == "group" else self.get_user_config(target_id)
            cancel_event = asyncio.Event()
            self._autobackup_full_cancel_events[cancel_key] = cancel_event
            self._autobackup_full_meta[cancel_key] = {
                "target_path": target_path,
                "started_at": time.time(),
            }
            try:
                if scope_type == "group":
                    async for result in self._do_backup_logic(
                        getattr(event, "bot", None),
                        event,
                        maybe_int_id(target_id),
                        target_path,
                        backup_config,
                        is_auto=False,
                        retry_key=self._get_backup_retry_key(event),
                        cancel_event=cancel_event,
                        cancel_title="首次全量备份",
                    ):
                        yield result
                else:
                    async for result in self._do_backup_logic(
                        getattr(event, "bot", None),
                        event,
                        None,
                        target_path,
                        backup_config,
                        private_user_id=target_id,
                        is_auto=False,
                        retry_key=self._get_backup_retry_key(event),
                        cancel_event=cancel_event,
                        cancel_title="首次全量备份",
                    ):
                        yield result
            finally:
                if self._autobackup_full_cancel_events.get(cancel_key) is cancel_event:
                    self._autobackup_full_cancel_events.pop(cancel_key, None)
                    self._autobackup_full_meta.pop(cancel_key, None)

        elif action == "disable":
            if target_path:
                yield event.plain_result(self._format_autobackup_usage_tip("禁用自动备份不需要提供路径"))
                return

            current_target_path = (
                self._get_autobackup_target_path(global_cfg, target_id)
                if scope_type == "group"
                else self._get_private_autobackup_target_path(global_cfg, target_id)
            )
            new_entries = [item for item in entries if self._autobackup_entry_group_id(item) != target_id]
            removed_local = len(new_entries) < len(entries)
            running_tip = ""
            running_cancel_event = self._autobackup_full_cancel_events.get(cancel_key)
            if running_cancel_event and not running_cancel_event.is_set():
                cancel_arg = f"@{target_id}" if scope_type == "group" else f"private:@{target_id}"
                running_tip = f"\n如需取消正在执行的首次全量备份，请发送 ol autobackup cancel {cancel_arg}"
            if removed_local:
                self.global_config_manager.save_runtime_fields({list_field: new_entries})
            if removed_local or current_target_path:
                yield event.plain_result(f"✅ {target_label} 自动备份已禁用。{running_tip}")
            else:
                yield event.plain_result(f"💡 {target_label} 当前未开启自动备份。{running_tip}")
        else:
            yield event.plain_result(self._format_autobackup_usage_tip(f"未知的自动备份操作：{action}"))
