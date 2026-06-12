import asyncio
import os
import time
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File

from .base import PluginService
from .backup_scanner import BackupScanner
from .backup_target import BackupTargetManager
from .backup_uploader import BackupUploader


class BackupService(PluginService):
    """Backup service."""

    def __init__(self, plugin):
        super().__init__(plugin)
        self._target_locks = {}
        self._target_lock_refs = {}
        self._target_locks_guard = asyncio.Lock()
        self._autobackup_full_cancel_events = {}
        self._autobackup_full_meta = {}
        self.target = BackupTargetManager(self)
        self.scanner = BackupScanner(self)
        self.uploader = BackupUploader(self)

    def _to_int_or_none(self, value):
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _backup_item_size(self, item: Dict):
        return self._to_int_or_none(item.get("file_size", item.get("size")))

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
        return "文件不存在" in text or "code=-103" in text

    def _extract_autobackup_file_payloads(self, event: AstrMessageEvent) -> List[Dict]:
        message_obj = getattr(event, "message_obj", None)
        raw_event_data = self._read_value(message_obj, "raw_message", {})
        group_id = self._get_event_group_id(event)
        file_components = [msg for msg in event.get_messages() if isinstance(msg, File)]

        def component_payload(component: File) -> Dict:
            return {
                "group_id": group_id,
                "file_name": self._read_value(component, "name") or self._read_value(component, "file"),
                "file_id": self._read_value(component, "file_id"),
                "file_size": self._to_int_or_none(
                    self._read_value(component, "file_size") or self._read_value(component, "size")
                ),
                "file_url": self._read_value(component, "url"),
                "busid": self._read_value(component, "busid", 0),
                "file_component": component,
                "source": "component",
            }

        notice_type = self._read_value(raw_event_data, "notice_type")
        if notice_type == "group_upload":
            file_info = self._read_value(raw_event_data, "file", {}) or {}
            file_name = (
                self._read_value(file_info, "name")
                or self._read_value(file_info, "file_name")
                or self._read_value(file_info, "file")
            )
            file_id = (
                self._read_value(file_info, "id")
                or self._read_value(file_info, "file_id")
                or self._read_value(raw_event_data, "file_id")
            )
            return [{
                "group_id": self._read_value(raw_event_data, "group_id", group_id),
                "file_name": file_name,
                "file_id": file_id,
                "file_size": self._to_int_or_none(
                    self._read_value(file_info, "size", self._read_value(raw_event_data, "file_size"))
                ),
                "file_url": self._read_value(file_info, "url", self._read_value(raw_event_data, "url")),
                "busid": self._read_value(file_info, "busid", self._read_value(raw_event_data, "busid", 0)),
                "file_component": None,
                "source": "notice",
            }]

        message_list = self._read_value(raw_event_data, "message")
        payloads = []
        if not isinstance(message_list, list):
            return [component_payload(component) for component in file_components]

        file_component_index = 0
        for segment_dict in message_list:
            if not isinstance(segment_dict, dict) or segment_dict.get("type") != "file":
                continue
            data_dict = segment_dict.get("data", {})
            file_name = data_dict.get("file") or data_dict.get("name") or data_dict.get("file_name")
            file_component = None
            for component in file_components:
                if getattr(component, "name", None) == file_name:
                    file_component = component
                    break
            if file_component is None and file_component_index < len(file_components):
                file_component = file_components[file_component_index]
            file_component_index += 1

            payloads.append({
                "group_id": group_id,
                "file_name": file_name,
                "file_id": data_dict.get("file_id") or data_dict.get("id"),
                "file_size": self._to_int_or_none(data_dict.get("file_size") or data_dict.get("size")),
                "file_url": data_dict.get("url"),
                "busid": data_dict.get("busid", 0),
                "file_component": file_component,
                "source": "message",
            })
        if not payloads:
            payloads = [component_payload(component) for component in file_components]
        return payloads

    def _format_backup_scan_summary(self, stats: Dict) -> str:
        raw_count = stats.get("raw_count", 0)
        reported_count = stats.get("reported_count")
        deduped_count = stats.get("deduped_count", stats.get("filtered_count", raw_count))
        ext_skipped = stats.get("ext_skipped", 0)
        size_skipped = stats.get("size_skipped", 0)
        duplicate_skipped = stats.get("duplicate_skipped", 0)

        lines = [f"📦 扫描完成：接口返回 {raw_count} 个，需备份 {deduped_count} 个。"]
        if reported_count is not None and reported_count != raw_count:
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

    async def _run_group_file_autobackup(
        self,
        event: AstrMessageEvent,
        file_component: Optional[File],
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
        async with self.autobackup_semaphore:
            file_path = None
            try:
                max_size_mb = self._get_size_limit_mb(user_config, "backup_max_size", 0)
                if max_size_mb > 0 and file_size is not None and file_size > (max_size_mb * 1024 * 1024):
                    logger.info(f"⏭️ [自动备份] 文件 {file_name} 事件大小 {file_size} 超过限制 {max_size_mb}MB，跳过。")
                    return

                logger.info(f"🚀 [自动备份] 发现新文件: {file_name} -> {target_path}")
                target_key = self._normalize_openlist_path(f"{target_path.rstrip('/')}/{file_name}")
                target_lock = await self._acquire_target_lock(target_key)
                try:
                    async with self._create_openlist_client(user_config) as client:
                        if not await client.ensure_dir(target_path):
                            logger.error(f"❌ [自动备份] 创建目标目录失败: {target_path}")
                            return
                        skip_existing = self._get_bool_config(user_config, "backup_skip_existing", True)
                        item = {
                            "file_id": file_id,
                            "file_name": file_name,
                            "file_size": file_size,
                            "busid": busid,
                        }
                        target_file_name = file_name
                        if skip_existing:
                            existing_files = await self.target.openlist_files_by_name(client, target_path, refresh=True)
                            existing = existing_files.get(file_name)
                            if existing and self.target.existing_entry_matches(existing, file_size):
                                logger.info(f"⏭️ [自动备份] 跳过已存在文件: {target_path}/{file_name}")
                                return
                            target_file_name = self.target.resolve_target_name(
                                existing_files,
                                file_name,
                                item,
                            )
                            if target_file_name != file_name:
                                item["_backup_target_name"] = target_file_name
                                logger.info(f"📌 [自动备份] 同名冲突文件改名备份: {file_name} -> {target_file_name}")
                        if file_url or file_id:
                            retry_attempts = self._get_positive_int_config(user_config, "backup_retry_attempts", 3)
                            retry_delay = self._get_positive_int_config(user_config, "backup_retry_delay", 5, minimum=0)
                            if file_id:
                                success, _ = await self.uploader.upload_group_file_with_retry(
                                    event.bot,
                                    client,
                                    int(group_id),
                                    item,
                                    target_path,
                                    retry_attempts,
                                    retry_delay,
                                    initial_url=file_url,
                                )
                            else:
                                logger.info(f"🚀 [自动备份] 使用 URL 流式中转: {target_file_name}, size={file_size}, target={target_path}")
                                success = await client.upload_url_stream(file_url, target_path, target_file_name, file_size)
                        elif file_component:
                            get_file_started_at = time.monotonic()
                            file_path = await file_component.get_file()
                            logger.info(
                                f"📥 [自动备份] 本地获取完成: {file_name}, path={file_path}, "
                                f"elapsed={time.monotonic() - get_file_started_at:.2f}s"
                            )

                            if not file_path or not os.path.exists(file_path):
                                logger.error(f"❌ [自动备份] 无法获取文件路径: {file_name}")
                                return

                            actual_size = os.path.getsize(file_path)
                            if max_size_mb > 0 and actual_size > (max_size_mb * 1024 * 1024):
                                logger.info(f"⏭️ [自动备份] 文件 {file_name} 实际下载大小 {actual_size} 超过限制 {max_size_mb}MB，跳过。")
                                return

                            success = await client.upload_file(file_path, target_path, target_file_name)
                        else:
                            logger.warning(f"⚠️ [自动备份] 无可用下载来源，跳过文件: {file_name}")
                            return

                        if success:
                            logger.info(f"✅ [自动备份] 文件 {target_file_name} 上传成功。")
                            self.cache_manager.clear_cache()
                        else:
                            logger.error(f"❌ [自动备份] 文件 {target_file_name} 上传失败。")
                finally:
                    await self._release_target_lock(target_key, target_lock)
            except Exception as e:
                logger.error(f"❌ [自动备份] 处理文件 {file_name} 出错: {e}", exc_info=True)
            finally:
                self._remove_file_quietly(file_path, f"[自动备份] 临时文件 group={group_id}, file={file_name}")

    async def handle_group_file_upload(self, event: AstrMessageEvent):
        """处理群文件上传事件（自动备份）"""
        payloads = self._extract_autobackup_file_payloads(event)
        if not payloads:
            return

        for payload in payloads:
            file_name = payload.get("file_name")
            if not file_name:
                logger.debug(f"🧵 [自动备份] 跳过缺少文件名的上传事件: {payload}")
                continue
            file_id = payload.get("file_id")
            file_size = payload.get("file_size")
            file_url = payload.get("file_url")
            file_component = payload.get("file_component")
            group_id = str(payload.get("group_id") or "")
            if not group_id:
                logger.debug(f"🧵 [自动备份] 跳过缺少群号的上传事件: file={file_name}")
                continue

            if not (file_id or file_url or file_component):
                logger.debug(f"🧵 [自动备份] 跳过缺少下载来源的上传事件: group={group_id}, file={file_name}")
                continue

            global_cfg = self.get_global_config()
            target_path = self._get_autobackup_target_path(global_cfg, group_id)

            if not target_path:
                continue

            user_config = global_cfg
            if not self._validate_config(user_config):
                logger.warning(f"⚠️ [自动备份] 群 {group_id} 触发了自动备份，但未找到有效的 Openlist 配置。")
                continue

            # 预先检查大小限制 (从事件数据获取)
            if file_size is not None:
                max_size_mb = self._get_size_limit_mb(user_config, "backup_max_size", 0)
                if max_size_mb > 0 and file_size > (max_size_mb * 1024 * 1024):
                    logger.info(f"⏭️ [自动备份] 文件 {file_name} 超过限制 {max_size_mb}MB (事件报送大小: {file_size})，跳过。")
                    continue

            # 使用配置中的备份过滤条件
            allowed_exts = self._get_extension_filter(user_config, "backup_allowed_extensions")
            if allowed_exts:
                ext = os.path.splitext(file_name.lower())[1]
                if ext not in allowed_exts:
                    logger.info(f"⏭️ [自动备份] 文件 {file_name} 后缀 {ext} 不在允许范围内，跳过。")
                    continue

            task_user_config = dict(user_config)
            asyncio.create_task(
                self._run_group_file_autobackup(
                    event=event,
                    file_component=file_component,
                    file_name=file_name,
                    file_size=file_size,
                    file_url=file_url,
                    target_path=target_path,
                    user_config=task_user_config,
                    group_id=group_id,
                    file_id=file_id,
                    busid=payload.get("busid", 0),
                )
            )
            logger.info(
                f"🧵 [自动备份] 已转入后台任务: group={group_id}, file={file_name}, "
                f"source={payload.get('source')}, has_file_id={bool(file_id)}, has_url={bool(file_url)}"
            )

    async def _backup_group_files(self, event: AstrMessageEvent, group_id: int, target_path: str, user_config: Dict):
        """执行群文件备份"""
        bot = event.bot
        async for result in self._do_backup_logic(
            bot,
            event,
            group_id,
            target_path,
            user_config,
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

        group_id = retry_state["group_id"]
        target_path = retry_state["target_path"]
        failed_items = retry_state["items"]
        if await self._deny_if_no_target_group_permission(event, group_id, "备份重试"):
            yield event.plain_result("❌ 权限不足：只能重试当前群备份，或由目标群群主/管理员重试指定群备份。")
            return

        self._delete_backup_retry_state(retry_key)
        yield event.plain_result(
            f"🔁 开始重试上次备份失败的 {len(failed_items)} 个文件\n"
            f"📂 目标: {target_path}"
        )
        async for result in self._do_backup_logic(
            event.bot,
            event,
            group_id,
            target_path,
            user_config,
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
        group_id: int,
        target_path: str,
        user_config: Dict,
        is_auto: bool = False,
        items_override: Optional[List[Dict]] = None,
        retry_key: str = None,
        is_retry: bool = False,
        cancel_event: Optional[asyncio.Event] = None,
        cancel_title: str = "备份任务",
    ):
        """核心备份逻辑，支持手动和自动备份"""
        cancelled = False

        def should_cancel() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        if not is_auto and not is_retry:
            logger.info(f"🔍 [群备份] 正在扫描群 {group_id} 的所有文件")

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
            if original_count != len(filtered_items):
                logger.info(f"⏭️ [群备份] 已去重 {original_count - len(filtered_items)} 个重复群文件记录。")
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
                    logger.info(f"🛑 [自动备份] 任务已取消。群 {group_id}: 扫描阶段取消")
                return
            if not scan_result.get("found"):
                if not is_auto:
                    yield event.plain_result("❌ 未找到任何群文件或获取失败。")
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
            else:
                logger.info(f"🛑 [自动备份] 任务已取消。群 {group_id}: 未开始上传")
            return

        if is_retry:
            logger.info(f"🔁 [群备份] 开始重试 {total} 个失败文件，目标路径: {target_path}")
        elif not is_auto:
            logger.info(f"📦 [群备份] 扫描完成，准备目标目录: {target_path}")
        else:
            logger.info(f"🚀 [自动备份] 发现 {total} 个新文件，准备备份到群 {group_id} 的目标路径: {target_path}")

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
            semaphore = asyncio.Semaphore(3)
            prepared_dirs = set()
            list_failed_dirs = set()

            async def get_existing_files(target_dir: str) -> Dict[str, Dict]:
                target_dir = target_dir or "/"
                async with existing_cache_lock:
                    return existing_cache.get(target_dir, {})

            async def refresh_existing_files(target_dir: str) -> Dict[str, Dict]:
                target_dir = target_dir or "/"
                list_result = await client.list_files(target_dir, per_page=0, refresh=True)
                files = {}
                if list_result is None:
                    list_failed_dirs.add(target_dir)
                    logger.warning(
                        f"⚠️ [群备份] 获取目标目录文件列表失败，本轮不对该目录执行已存在跳过: {target_dir}"
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
                logger.info(f"📁 [群备份] 检查/创建目标目录: {target_dir}")
                if await client.ensure_dir(target_dir):
                    prepared_dirs.add(target_dir)
                else:
                    failed_dirs.add(target_dir)
                    logger.error(f"❌ [群备份] 创建目标目录失败: {target_dir}")

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
                        failed_item = dict(item)
                        failed_item["_backup_fail_reason"] = f"创建目标目录失败: {target_dir}"
                        failed_items.append(failed_item)
                        logger.error(f"❌ [群备份] 目标目录创建失败，跳过文件: {target_dir}/{file_name}")
                    else:
                        upload_items.append(item)
            else:
                upload_items = list(filtered_items)

            if not is_auto and not cancelled:
                if upload_items:
                    if is_retry:
                        logger.info(f"🚀 [群备份] 目录检查完成，开始重试备份: {target_path}")
                    else:
                        yield event.plain_result(
                            f"{self._format_backup_scan_summary(scan_stats)}\n"
                            f"🚀 开始备份"
                        )
                elif failed_dirs:
                    yield event.plain_result(f"❌ 目标目录创建失败，无法开始备份: {', '.join(sorted(failed_dirs))}")

            async def upload_task(item, idx):
                nonlocal success_count, fail_count, skipped_count
                async with semaphore:
                    if should_cancel():
                        return
                    target_dir, file_name, _ = self.target.item_target(target_path, item)
                    original_target_dir, original_file_name, _ = self.target.item_target(target_path, item, use_override=False)
                    if not file_name:
                        fail_count += 1
                        failed_item = dict(item)
                        failed_item["_backup_fail_reason"] = "文件名为空"
                        failed_items.append(failed_item)
                        return

                    lock_key = self.target.item_lock_key(target_path, item)
                    target_lock = await self._acquire_target_lock(lock_key)
                    try:
                        target_dir = target_dir or "/"
                        if target_dir not in prepared_dirs and not await client.ensure_dir(target_dir):
                            fail_count += 1
                            failed_item = dict(item)
                            failed_item["_backup_fail_reason"] = f"创建目标目录失败: {target_dir}"
                            failed_items.append(failed_item)
                            return

                        if should_cancel():
                            return

                        existing_files = await get_existing_files(target_dir)
                        can_check_existing = skip_existing and target_dir not in list_failed_dirs
                        if can_check_existing:
                            existing = existing_files.get(file_name)
                            if existing and self.target.existing_entry_matches(existing, item.get("file_size")):
                                skipped_count += 1
                                logger.info(f"⏭️ [群备份] 跳过已存在文件: {target_dir}/{file_name}")
                                return
                            if (
                                item.get("_backup_target_name")
                                and original_target_dir == target_dir
                                and original_file_name != file_name
                            ):
                                original_existing = existing_files.get(original_file_name)
                                if original_existing and self.target.existing_entry_matches(original_existing, item.get("file_size")):
                                    skipped_count += 1
                                    logger.info(f"⏭️ [群备份] 跳过已存在文件: {target_dir}/{original_file_name}")
                                    return

                        resolved_name = self.target.resolve_target_name(existing_files, file_name, item)
                        if resolved_name != file_name:
                            item = dict(item)
                            item["_backup_target_name"] = resolved_name
                            logger.info(f"📌 [群备份] 同名冲突文件改名备份: {file_name} -> {resolved_name}")
                            file_name = resolved_name

                        if should_cancel():
                            return

                        resolved_existing = existing_files.get(file_name)
                        if (
                            can_check_existing
                            and resolved_existing
                            and self.target.existing_entry_matches(resolved_existing, item.get("file_size"))
                        ):
                            skipped_count += 1
                            logger.info(f"⏭️ [群备份] 跳过已存在文件: {target_dir}/{file_name}")
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
                        else:
                            fail_count += 1
                            failed_item = dict(item)
                            failed_item["_backup_fail_reason"] = self._short_backup_fail_reason(reason)
                            failed_items.append(failed_item)
                    except Exception as e:
                        reason = self._short_backup_fail_reason(e)
                        logger.error(f"备份文件 {file_name} 失败: {reason}", exc_info=True)
                        fail_count += 1
                        failed_item = dict(item)
                        failed_item["_backup_fail_reason"] = reason
                        failed_items.append(failed_item)
                    finally:
                        await self._release_target_lock(lock_key, target_lock)

            async def run_batch(batch_coroutines):
                nonlocal cancelled
                if cancel_event is None:
                    await asyncio.gather(*batch_coroutines)
                    return

                tasks = [asyncio.create_task(coro) for coro in batch_coroutines]
                batch_future = asyncio.gather(*tasks, return_exceptions=True)
                cancel_waiter = asyncio.create_task(cancel_event.wait())
                try:
                    done, _ = await asyncio.wait(
                        {batch_future, cancel_waiter},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_waiter in done and cancel_event.is_set():
                        cancelled = True
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await batch_future
                    else:
                        await batch_future
                        if cancel_event.is_set():
                            cancelled = True
                finally:
                    if not cancel_waiter.done():
                        cancel_waiter.cancel()
                    await asyncio.gather(cancel_waiter, return_exceptions=True)

            batch_size = 5
            for i in range(0, len(upload_items), batch_size):
                if should_cancel():
                    cancelled = True
                    break
                batch_tasks = [upload_task(item, j) for j, item in enumerate(upload_items[i:i+batch_size], start=i)]
                await run_batch(batch_tasks)
                processed_count = min(total, success_count + skipped_count + fail_count)
                logger.info(
                    f"⏳ 备份进度: {processed_count}/{total} "
                    f"(成功: {success_count}, 跳过: {skipped_count}, 失败: {fail_count})"
                )
                if cancelled or should_cancel():
                    cancelled = True
                    break

        if cancelled or should_cancel():
            if success_count:
                self.cache_manager.clear_cache()
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
                    f"🛑 [自动备份] 任务已取消。群 {group_id}: "
                    f"成功 {success_count}, 跳过 {skipped_count}, 失败 {fail_count}, 未处理 {remaining_count}"
                )
            return

        if not is_auto:
            if success_count:
                self.cache_manager.clear_cache()
            if retry_key:
                if failed_items:
                    self._save_backup_retry_state(
                        retry_key,
                        {
                            "group_id": group_id,
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
                self.cache_manager.clear_cache()
            logger.info(f"✅ [自动备份] 任务结束。群 {group_id}: 成功 {success_count}, 跳过 {skipped_count}, 失败 {fail_count}")

    async def backup_command(self, event: AstrMessageEvent, path_or_group: str = "", group_or_path: str = ""):
        """群文件备份到 Openlist"""
        user_id = event.get_sender_id()
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
        target_group_id = 0

        # 1. 智能解析参数
        for arg in [path_or_group, group_or_path]:
            if not arg: continue
            if arg.startswith("/"):
                target_path_arg = arg
            elif arg.startswith("@"):
                try:
                    target_group_id = int(arg[1:])
                except ValueError:
                    yield event.plain_result(self._format_backup_usage_tip(f"无效的群号格式：{arg}"))
                    return
            else:
                yield event.plain_result(self._format_backup_usage_tip(f"无法识别参数：{arg}"))
                return

        # 2. 确定群号 (手动指定优先，否则用当前群)
        if not target_group_id:
            event_group_id = self._get_event_group_id(event)
            if event_group_id:
                target_group_id = int(event_group_id)
            else:
                yield event.plain_result(self._format_backup_usage_tip("私聊中备份必须指定群号"))
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
            target_group_id,
        )

        async for result in self._backup_group_files(event, target_group_id, target_path, user_config):
            yield result

    async def autobackup_command(self, event: AstrMessageEvent, action: str = "show", target_or_group: str = "", path_or_group: str = ""):
        """配置自动备份"""
        global_cfg = self.get_global_config()
        if not self._is_event_admin(event):
            logger.warning(
                f"自动备份配置权限不足: user={event.get_sender_id()}, "
                f"group={getattr(event.message_obj, 'group_id', '')}, "
                f"role={self._extract_sender_role(event)!r}"
            )
            yield event.plain_result(self._format_autobackup_usage_tip("权限不足：只有群主或管理员可以配置自动备份"))
            return

        action = (action or "show").lower()
        cancel_actions = ("cancel", "stop", "取消", "停止")
        if action in ("show", "status", "list", "状态", "列表"):
            effective_groups = global_cfg.get("autobackup_groups", [])
            lines = ["🔄 自动备份配置", ""]
            if effective_groups:
                lines.append("已启用群组:")
                for item in effective_groups:
                    if not isinstance(item, str):
                        continue
                    if ":" in item:
                        gid, path = item.split(":", 1)
                        path = self._render_backup_path(path, gid)
                    else:
                        gid = item
                        path = self._render_backup_path(
                            global_cfg.get("autobackup_default_path", "/backup/group_{group_id}"),
                            gid,
                        )
                    lines.append(f"• 群 {gid} -> {path}")
            else:
                lines.append("当前没有启用自动备份的群组。")
            if self._autobackup_full_meta:
                lines.extend(["", "正在执行首次全量备份:"])
                for gid, meta in self._autobackup_full_meta.items():
                    cancel_event = self._autobackup_full_cancel_events.get(gid)
                    status = "取消中" if cancel_event and cancel_event.is_set() else "运行中"
                    target_path = meta.get("target_path", "")
                    lines.append(f"• 群 {gid} -> {target_path} ({status})")
            lines.extend([
                "",
                "用法：",
                "ol autobackup enable [@群号] [/OpenList路径]",
                "ol autobackup disable [@群号]",
                "ol autobackup cancel [@群号]",
                "",
                "示例：",
                "  ol autobackup enable",
                "  ol autobackup enable @123456 /backup/group_123456",
                "  ol autobackup disable @123456",
                "  ol autobackup cancel @123456",
                "",
                "未指定群号时使用当前群；未指定路径时使用 autobackup_default_path。",
                "cancel 只取消 enable 触发的首次全量备份，不会关闭后续自动备份。",
            ])
            yield event.plain_result("\n".join(lines))
            return

        target_gid = None
        target_path = None

        # 1. 智能解析参数: 路径必须以 / 开头，群号必须以 @ 开头
        for arg in [target_or_group, path_or_group]:
            if not arg: continue
            if arg.startswith("/"):
                target_path = arg
            elif arg.startswith("@"):
                target_gid = arg[1:]
            else:
                yield event.plain_result(self._format_autobackup_usage_tip(f"无法识别参数：{arg}"))
                return

        # 2. 确定群号 (手动指定优先，否则用当前群)
        if not target_gid:
            event_group_id = self._get_event_group_id(event)
            if event_group_id:
                target_gid = str(event_group_id)
            else:
                yield event.plain_result(self._format_autobackup_usage_tip("私聊中配置自动备份必须指定群号"))
                return

        if await self._deny_if_no_target_group_permission(event, target_gid, "自动备份配置"):
            yield event.plain_result(self._format_usage_tip(
                "权限不足：无法配置目标群自动备份",
                "ol autobackup <enable|disable|cancel> [@群号] [/OpenList目标路径]",
                [
                    "ol autobackup enable",
                    "ol autobackup enable @123456 /backup/group_123456",
                    "ol autobackup disable @123456",
                    "ol autobackup cancel @123456",
                ],
                "只能配置当前群，或由目标群群主/管理员指定 @群号。",
            ))
            return

        if action in cancel_actions:
            if target_path:
                yield event.plain_result(self._format_autobackup_usage_tip("取消首次全量备份不需要提供路径"))
                return
            cancel_event = self._autobackup_full_cancel_events.get(target_gid)
            if not cancel_event:
                yield event.plain_result(f"💡 群 {target_gid} 当前没有正在执行的首次全量备份。")
                return
            if cancel_event.is_set():
                yield event.plain_result(f"💡 群 {target_gid} 的首次全量备份正在取消中，请稍候。")
                return
            cancel_event.set()
            meta = self._autobackup_full_meta.get(target_gid, {})
            target_line = f"\n📂 目标: {meta.get('target_path')}" if meta.get("target_path") else ""
            yield event.plain_result(
                f"🛑 已请求取消群 {target_gid} 的首次全量备份。"
                f"{target_line}\n"
                f"正在停止未完成的上传任务，请稍候。"
            )
            return

        local_cfg = self.global_config_manager.load_config()
        groups = local_cfg.get("autobackup_groups", [])

        if action == "enable":
            target_path = self._render_backup_path(
                target_path or global_cfg.get("autobackup_default_path", "/backup/group_{group_id}"),
                target_gid,
            )

            running_cancel_event = self._autobackup_full_cancel_events.get(target_gid)
            if running_cancel_event:
                if running_cancel_event.is_set():
                    yield event.plain_result(f"💡 群 {target_gid} 的首次全量备份正在取消中，请稍候再重新开启。")
                else:
                    yield event.plain_result(
                        f"💡 群 {target_gid} 的首次全量备份正在执行。\n"
                        f"如需停止，请发送 ol autobackup cancel @{target_gid}"
                    )
                return

            new_entry = f"{target_gid}:{target_path}"
            # 过滤掉旧的该群配置
            new_groups = [item for item in groups if (item.split(":", 1)[0] if ":" in item else item) != target_gid]
            new_groups.append(new_entry)
            local_cfg["autobackup_groups"] = new_groups
            self.global_config_manager.save_config(local_cfg)
            yield event.plain_result(
                f"✅ 群 {target_gid} 自动备份已开启 -> {target_path}\n"
                f"📦 正在执行首次全量备份..."
            )
            backup_config = self.get_global_config()
            cancel_event = asyncio.Event()
            self._autobackup_full_cancel_events[target_gid] = cancel_event
            self._autobackup_full_meta[target_gid] = {
                "target_path": target_path,
                "started_at": time.time(),
            }
            try:
                async for result in self._do_backup_logic(
                    event.bot,
                    event,
                    int(target_gid),
                    target_path,
                    backup_config,
                    is_auto=False,
                    retry_key=self._get_backup_retry_key(event),
                    cancel_event=cancel_event,
                    cancel_title="首次全量备份",
                ):
                    yield result
            finally:
                if self._autobackup_full_cancel_events.get(target_gid) is cancel_event:
                    self._autobackup_full_cancel_events.pop(target_gid, None)
                    self._autobackup_full_meta.pop(target_gid, None)

        elif action == "disable":
            # disable 只需要群号，忽略路径
            new_groups = [item for item in groups if (item.split(":", 1)[0] if ":" in item else item) != target_gid]
            running_tip = ""
            running_cancel_event = self._autobackup_full_cancel_events.get(target_gid)
            if running_cancel_event and not running_cancel_event.is_set():
                running_tip = f"\n如需取消正在执行的首次全量备份，请发送 ol autobackup cancel @{target_gid}"
            if len(new_groups) < len(groups):
                local_cfg["autobackup_groups"] = new_groups
                self.global_config_manager.save_config(local_cfg)
                yield event.plain_result(f"✅ 群 {target_gid} 自动备份已禁用。{running_tip}")
            else:
                yield event.plain_result(f"💡 群 {target_gid} 当前未开启自动备份。{running_tip}")
        else:
            yield event.plain_result(self._format_autobackup_usage_tip(f"未知的自动备份操作：{action}"))
