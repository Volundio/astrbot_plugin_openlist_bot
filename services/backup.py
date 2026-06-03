import asyncio
import os
import time
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File
from astrbot.api.star import StarTools

from .base import PluginService


class BackupService(PluginService):
    """Backup service."""

    def __init__(self, plugin):
        super().__init__(plugin)
        self._target_locks = {}
        self._target_lock_refs = {}
        self._target_locks_guard = asyncio.Lock()
        self._autobackup_full_cancel_events = {}
        self._autobackup_full_meta = {}

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

    def _backup_item_target(self, target_path: str, item: Dict, use_override: bool = True) -> tuple:
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

    def _backup_item_lock_key(self, target_path: str, item: Dict) -> str:
        return self._backup_item_target(target_path, item, use_override=False)[2]

    def _backup_item_duplicate_key(self, target_path: str, item: Dict) -> tuple:
        target_dir, file_name, _ = self._backup_item_target(target_path, item, use_override=False)
        return target_dir, file_name

    def _backup_item_identity(self, target_path: str, item: Dict) -> tuple:
        duplicate_key = self._backup_item_duplicate_key(target_path, item)
        size = self._backup_item_size(item)
        if size is not None:
            return ("target_size", duplicate_key, size)
        file_id = item.get("file_id")
        if file_id:
            return ("file_id", duplicate_key, str(file_id))
        return ("target_unknown_size", duplicate_key)

    def _safe_suffix_part(self, value: str) -> str:
        suffix = "".join(c for c in str(value or "") if c.isalnum() or c in "-_").strip("-_")
        return suffix[:16] or "duplicate"

    def _backup_duplicate_suffix(self, item: Dict) -> str:
        file_id = item.get("file_id")
        if file_id:
            return self._safe_suffix_part(str(file_id).strip("/").split("/")[-1])
        size = self._backup_item_size(item)
        if size is not None:
            return f"size-{size}"
        return "duplicate"

    def _filename_with_suffix(self, filename: str, suffix: str) -> str:
        stem, ext = os.path.splitext(filename)
        return f"{stem} [{suffix}]{ext}" if stem else f"{filename} [{suffix}]"

    def _deduplicate_backup_items(self, items: List[Dict], target_path: str) -> List[Dict]:
        candidates = []
        seen_items = set()
        target_counts = {}
        for item in items:
            target_dir, file_name = self._backup_item_duplicate_key(target_path, item)
            if not file_name:
                continue
            duplicate_key = (target_dir, file_name)
            identity = (duplicate_key, self._backup_item_identity(target_path, item))
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

            _, file_name, _ = self._backup_item_target(target_path, item, use_override=False)
            used_for_target = used_names.setdefault(duplicate_key, set())
            if not used_for_target:
                used_for_target.add(file_name)
                deduped.append(item)
                continue

            renamed_item = dict(item)
            suffix = self._backup_duplicate_suffix(item)
            target_name = self._filename_with_suffix(file_name, suffix)
            index = 2
            while target_name in used_for_target:
                target_name = self._filename_with_suffix(file_name, f"{suffix}-{index}")
                index += 1
            used_for_target.add(target_name)
            renamed_item["_backup_target_name"] = target_name
            logger.info(f"📌 [群备份] 同名群文件改名备份: {file_name} -> {target_name}")
            deduped.append(renamed_item)
        return deduped

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

    def _existing_entry_matches(self, existing: Dict, file_size) -> bool:
        try:
            expected_size = int(file_size) if file_size is not None else None
            existing_size = int(existing.get("size", 0))
        except (TypeError, ValueError):
            return True
        return expected_size is None or existing_size == expected_size

    async def _openlist_files_by_name(self, client, target_dir: str) -> Dict[str, Dict]:
        try:
            list_result = await client.list_files(target_dir or "/", per_page=0)
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

    async def _openlist_file_matches(self, client, target_dir: str, file_name: str, file_size) -> bool:
        existing_files = await self._openlist_files_by_name(client, target_dir)
        existing = existing_files.get(file_name)
        return bool(existing and self._existing_entry_matches(existing, file_size))

    def _resolve_backup_target_name(
        self,
        existing_files: Dict[str, Dict],
        target_name: str,
        item: Dict,
        force_unique: bool = False,
    ) -> str:
        existing = existing_files.get(target_name)
        file_size = self._backup_item_size(item)
        if not existing or (not force_unique and self._existing_entry_matches(existing, file_size)):
            return target_name

        suffix = self._backup_duplicate_suffix(item)
        candidate = self._filename_with_suffix(target_name, suffix)
        existing = existing_files.get(candidate)
        if not existing or self._existing_entry_matches(existing, file_size):
            return candidate

        for index in range(2, 1000):
            candidate = self._filename_with_suffix(target_name, f"{suffix}-{index}")
            existing = existing_files.get(candidate)
            if not existing or self._existing_entry_matches(existing, file_size):
                return candidate

        logger.warning(f"无法为同名文件生成未占用名称，将使用原名: {target_name}")
        return target_name

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
                            existing_files = await self._openlist_files_by_name(client, target_path)
                            existing = existing_files.get(file_name)
                            if existing and self._existing_entry_matches(existing, file_size):
                                logger.info(f"⏭️ [自动备份] 跳过已存在文件: {target_path}/{file_name}")
                                return
                            target_file_name = self._resolve_backup_target_name(
                                existing_files,
                                file_name,
                                item,
                            )
                            if target_file_name != file_name:
                                item["_backup_target_name"] = target_file_name
                                logger.info(f"📌 [自动备份] 同名冲突文件改名备份: {file_name} -> {target_file_name}")
                        if (file_url or file_id) and file_size is not None:
                            retry_attempts = self._get_positive_int_config(user_config, "backup_retry_attempts", 3)
                            retry_delay = self._get_positive_int_config(user_config, "backup_retry_delay", 5, minimum=0)
                            if file_id:
                                success, _ = await self._upload_group_file_with_retry(
                                    event.bot,
                                    client,
                                    int(group_id),
                                    item,
                                    target_path,
                                    retry_attempts,
                                    retry_delay,
                                    initial_url=file_url,
                                    skip_existing=skip_existing,
                                )
                            else:
                                logger.info(f"🚀 [自动备份] 使用 URL 流式中转: {target_file_name}, size={file_size}, target={target_path}")
                                success = await client.upload_url_stream(file_url, target_path, target_file_name, file_size)
                        else:
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
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except OSError as e:
                        logger.warning(f"⚠️ [自动备份] 清理临时文件失败: group={group_id}, file={file_name}, err={e}")

    async def handle_group_file_upload(self, event: AstrMessageEvent):
        """处理群文件上传事件（自动备份）"""
        raw_event_data = event.message_obj.raw_message
        message_list = raw_event_data.get("message") if isinstance(raw_event_data, dict) else None
        if not isinstance(message_list, list):
            return

        # 遍历消息段寻找文件段
        for segment_dict in message_list:
            if isinstance(segment_dict, dict) and segment_dict.get("type") == "file":
                data_dict = segment_dict.get("data", {})
                file_name = data_dict.get("file")
                file_id = data_dict.get("file_id")
                file_size = data_dict.get("file_size")
                file_url = data_dict.get("url")

                if not file_name or not file_id:
                    continue

                # 转换文件大小
                if isinstance(file_size, str):
                    try:
                        file_size = int(file_size)
                    except ValueError:
                        file_size = None

                # 命中文件，开始执行自动备份检查
                group_id = str(event.message_obj.group_id)
                if not group_id:
                    return

                global_cfg = self.get_global_config()
                target_path = self._get_autobackup_target_path(global_cfg, group_id)

                if not target_path:
                    return

                user_config = global_cfg
                if not self._validate_config(user_config):
                    logger.warning(f"⚠️ [自动备份] 群 {group_id} 触发了自动备份，但未找到有效的 Openlist 配置。")
                    return

                # 预先检查大小限制 (从事件数据获取)
                if file_size is not None:
                    max_size_mb = self._get_size_limit_mb(user_config, "backup_max_size", 0)
                    if max_size_mb > 0 and file_size > (max_size_mb * 1024 * 1024):
                        logger.info(f"⏭️ [自动备份] 文件 {file_name} 超过限制 {max_size_mb}MB (事件报送大小: {file_size})，跳过。")
                        return

                # 获取对应的 File 组件
                file_component = None
                for msg in event.get_messages():
                    if isinstance(msg, File):
                        file_component = msg
                        break

                if not file_component:
                    return

                # 使用配置中的备份过滤条件
                allowed_exts = self._get_extension_filter(user_config, "backup_allowed_extensions")
                if allowed_exts:
                    ext = os.path.splitext(file_name.lower())[1]
                    if ext not in allowed_exts:
                        logger.info(f"⏭️ [自动备份] 文件 {file_name} 后缀 {ext} 不在允许范围内，跳过。")
                        return

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
                        busid=data_dict.get("busid", 0),
                    )
                )
                logger.debug(f"🧵 [自动备份] 已转入后台任务: group={group_id}, file={file_name}")

                break # 已经处理了文件，跳出循环

    async def _get_group_files_recursive(
        self,
        bot,
        group_id: int,
        folder_id: str = "/",
        current_path: str = "",
        cancel_event: Optional[asyncio.Event] = None,
    ) -> List[Dict]:
        """递归获取群文件列表"""
        all_files = []
        try:
            if cancel_event and cancel_event.is_set():
                return all_files

            if folder_id == "/":
                res = await bot.api.call_action("get_group_root_files", group_id=group_id)
            else:
                res = await bot.api.call_action("get_group_files_by_folder", group_id=group_id, folder_id=folder_id)

            if not res:
                return []

            files = res.get("files", [])
            folders = res.get("folders", [])

            for f in files:
                if cancel_event and cancel_event.is_set():
                    return all_files
                f["relative_path"] = f"{current_path}/{f['file_name']}".lstrip("/")
                all_files.append(f)

            for folder in folders:
                if cancel_event and cancel_event.is_set():
                    return all_files
                sub_folder_id = folder.get("folder_id")
                sub_folder_name = folder.get("folder_name")
                if sub_folder_id:
                    sub_files = await self._get_group_files_recursive(
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
        skip_existing: bool = True,
    ) -> tuple:
        """获取群文件 URL 并上传；失败时重新获取 URL 后重试。"""
        file_id = item.get("file_id")
        file_name = item.get("_backup_target_name") or item.get("file_name")
        busid = item.get("busid", 0)
        temp_file_path = None
        upload_size = item.get("file_size")
        try:
            upload_size = int(upload_size) if upload_size is not None else None
        except (TypeError, ValueError):
            upload_size = None

        attempts = max(1, retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                if skip_existing and await self._openlist_file_matches(client, target_dir, file_name, upload_size):
                    logger.info(f"⏭️ [群备份] 目标文件已存在，停止重试: {target_dir}/{file_name}")
                    return True, ""
                if attempt == 1 and initial_url:
                    download_url = initial_url
                else:
                    url_res = await bot.api.call_action(
                        "get_group_file_url",
                        group_id=group_id,
                        file_id=file_id,
                        busid=busid,
                    )
                    download_url = url_res.get("url") if isinstance(url_res, dict) else None
                if not download_url:
                    reason = "无法获取群文件下载 URL"
                    logger.warning(f"备份文件 {file_name} 第 {attempt}/{attempts} 次失败: {reason}")
                else:
                    logger.info(
                        f"🚀 [群备份] 使用 URL 流式中转: {file_name}, "
                        f"size={upload_size}, target={target_dir}, attempt={attempt}/{attempts}"
                    )
                    if await client.upload_url_stream(download_url, target_dir, file_name, upload_size):
                        return True, ""
                    reason = "URL 流式中转上传失败"
                    logger.warning(f"备份文件 {file_name} 第 {attempt}/{attempts} 次失败: {reason}")
            except Exception as e:
                reason = str(e)
                logger.error(f"备份文件 {file_name} 第 {attempt}/{attempts} 次异常: {e}", exc_info=True)

            if attempt < attempts:
                await asyncio.sleep(max(0, retry_delay))

        if skip_existing and await self._openlist_file_matches(client, target_dir, file_name, upload_size):
            logger.info(f"⏭️ [群备份] 目标文件已存在，跳过本地临时文件备用上传: {target_dir}/{file_name}")
            return True, ""

        if not file_id:
            return False, reason

        try:
            logger.info(
                f"🧰 [群备份] URL 流式中转 {attempts} 次失败，改用本地临时文件备用上传: "
                f"{file_name}, target={target_dir}"
            )
            url_res = await bot.api.call_action(
                "get_group_file_url",
                group_id=group_id,
                file_id=file_id,
                busid=busid,
            )
            fallback_url = url_res.get("url") if isinstance(url_res, dict) else None
            if not fallback_url:
                return False, "备用上传无法获取群文件下载 URL"

            temp_dir = os.path.join(StarTools.get_data_dir("openlist"), "backup_temp")
            os.makedirs(temp_dir, exist_ok=True)
            safe_filename = self._sanitize_filename(file_name, "backup")
            temp_file_path = os.path.join(temp_dir, f"backup_{group_id}_{self._unique_suffix()}_{safe_filename}")

            if not await client.download_url_to_file(fallback_url, temp_file_path, file_name, upload_size):
                return False, "备用上传本地下载失败"

            actual_size = os.path.getsize(temp_file_path)
            if upload_size is not None and upload_size > 0 and actual_size != upload_size:
                logger.error(
                    f"备用上传本地文件大小不一致: {file_name}, actual={actual_size}, expected={upload_size}"
                )
                return False, "备用上传本地文件大小不一致"

            if skip_existing and await self._openlist_file_matches(client, target_dir, file_name, upload_size):
                logger.info(f"⏭️ [群备份] 目标文件已存在，跳过本地临时文件备用上传: {target_dir}/{file_name}")
                return True, ""

            if await client.upload_file(temp_file_path, target_dir, file_name):
                logger.info(f"✅ [群备份] 本地临时文件备用上传成功: {file_name} -> {target_dir}")
                return True, ""
            return False, "备用上传 OpenList 上传失败"
        except Exception as e:
            logger.error(f"备用上传文件 {file_name} 失败: {e}", exc_info=True)
            return False, str(e)
        finally:
            cleanup_paths = []
            if temp_file_path:
                cleanup_paths.extend([temp_file_path, f"{temp_file_path}.part"])
            for cleanup_path in cleanup_paths:
                if cleanup_path and os.path.exists(cleanup_path):
                    try:
                        os.remove(cleanup_path)
                    except OSError as e:
                        logger.warning(f"清理备份备用上传临时文件失败: {cleanup_path}, err={e}")

        return False, reason

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
            yield event.plain_result(f"🔍 正在扫描群 {group_id} 的所有文件，请稍候...")

        if items_override is not None:
            filtered_items = list(items_override)
        else:
            all_items = await self._get_group_files_recursive(bot, group_id, cancel_event=cancel_event)
            if should_cancel():
                if not is_auto:
                    yield event.plain_result(
                        f"🛑 {cancel_title}已取消\n"
                        f"📊 已处理: 总计 0, 成功 0, 跳过 0, 失败 0, 未处理 0\n"
                        f"📂 目标: {target_path}"
                    )
                else:
                    logger.info(f"🛑 [自动备份] 任务已取消。群 {group_id}: 扫描阶段取消")
                return
            if not all_items:
                if not is_auto:
                    yield event.plain_result("❌ 未找到任何群文件或获取失败。")
                return

            allowed_exts = self._get_extension_filter(user_config, "backup_allowed_extensions")
            max_size_mb = self._get_size_limit_mb(user_config, "backup_max_size", 0)
            max_size = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0

            filtered_items = []
            for item in all_items:
                name = item.get("file_name", "").lower()
                size = self._to_int_or_none(item.get("file_size"))

                if allowed_exts:
                    ext = os.path.splitext(name)[1]
                    if ext not in allowed_exts:
                        continue

                if max_size > 0 and size is not None and size > max_size:
                    continue

                filtered_items.append(item)

        original_count = len(filtered_items)
        filtered_items = self._deduplicate_backup_items(filtered_items, target_path)
        if original_count != len(filtered_items):
            logger.info(f"⏭️ [群备份] 已去重 {original_count - len(filtered_items)} 个重复群文件记录。")

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
            yield event.plain_result(f"📦 扫描完成，共发现 {total} 个文件需要备份。\n🚀 开始备份到 Openlist: {target_path}")
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

            async def get_existing_files(target_dir: str) -> Dict[str, Dict]:
                target_dir = target_dir or "/"
                async with existing_cache_lock:
                    if target_dir in existing_cache:
                        return existing_cache[target_dir]
                    list_result = await client.list_files(target_dir, per_page=0)
                    files = {}
                    if list_result is not None:
                        for existing in list_result.get("content") or []:
                            if not existing.get("is_dir", False):
                                files[existing.get("name", "")] = existing
                    existing_cache[target_dir] = files
                    return files

            async def existing_file_matches(target_dir: str, file_name: str, file_size) -> bool:
                if not skip_existing:
                    return False
                existing_files = await get_existing_files(target_dir)
                existing = existing_files.get(file_name)
                if not existing:
                    return False
                return self._existing_entry_matches(existing, file_size)

            async def remember_existing_file(target_dir: str, file_name: str, file_size):
                if not skip_existing:
                    return
                target_dir = target_dir or "/"
                async with existing_cache_lock:
                    existing_cache.setdefault(target_dir, {})[file_name] = {
                        "name": file_name,
                        "size": file_size or 0,
                        "is_dir": False,
                    }

            async def upload_task(item, idx):
                nonlocal success_count, fail_count, skipped_count
                async with semaphore:
                    if should_cancel():
                        return
                    target_dir, file_name, _ = self._backup_item_target(target_path, item)
                    original_target_dir, original_file_name, _ = self._backup_item_target(target_path, item, use_override=False)
                    if not file_name:
                        fail_count += 1
                        failed_items.append(dict(item))
                        return

                    lock_key = self._backup_item_lock_key(target_path, item)
                    target_lock = await self._acquire_target_lock(lock_key)
                    try:
                        if not await client.ensure_dir(target_dir or target_path):
                            fail_count += 1
                            failed_items.append(dict(item))
                            return

                        if should_cancel():
                            return

                        target_dir = target_dir or "/"
                        existing_files = await get_existing_files(target_dir)
                        if skip_existing:
                            existing = existing_files.get(file_name)
                            if existing and self._existing_entry_matches(existing, item.get("file_size")):
                                skipped_count += 1
                                logger.info(f"⏭️ [群备份] 跳过已存在文件: {target_dir}/{file_name}")
                                return
                            if (
                                item.get("_backup_target_name")
                                and original_target_dir == target_dir
                                and original_file_name != file_name
                            ):
                                original_existing = existing_files.get(original_file_name)
                                if original_existing and self._existing_entry_matches(original_existing, item.get("file_size")):
                                    skipped_count += 1
                                    logger.info(f"⏭️ [群备份] 跳过已存在文件: {target_dir}/{original_file_name}")
                                    return

                        resolved_name = self._resolve_backup_target_name(existing_files, file_name, item)
                        if resolved_name != file_name:
                            item = dict(item)
                            item["_backup_target_name"] = resolved_name
                            logger.info(f"📌 [群备份] 同名冲突文件改名备份: {file_name} -> {resolved_name}")
                            file_name = resolved_name

                        if should_cancel():
                            return

                        if await existing_file_matches(target_dir, file_name, item.get("file_size")):
                            skipped_count += 1
                            logger.info(f"⏭️ [群备份] 跳过已存在文件: {target_dir}/{file_name}")
                            return

                        up_res, reason = await self._upload_group_file_with_retry(
                            bot,
                            client,
                            group_id,
                            item,
                            target_dir,
                            retry_attempts,
                            retry_delay,
                            skip_existing=skip_existing,
                        )
                        if up_res:
                            success_count += 1
                            await remember_existing_file(target_dir, file_name, item.get("file_size"))
                        else:
                            fail_count += 1
                            failed_item = dict(item)
                            failed_item["_backup_fail_reason"] = reason
                            failed_items.append(failed_item)
                    except Exception as e:
                        logger.error(f"备份文件 {file_name} 失败: {e}")
                        fail_count += 1
                        failed_item = dict(item)
                        failed_item["_backup_fail_reason"] = str(e)
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
            for i in range(0, total, batch_size):
                if should_cancel():
                    cancelled = True
                    break
                batch_tasks = [upload_task(item, j) for j, item in enumerate(filtered_items[i:i+batch_size], start=i)]
                await run_batch(batch_tasks)
                logger.info(
                    f"⏳ 备份进度: {min(i+batch_size, total)}/{total} "
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
                yield event.plain_result(
                    f"🛑 {cancel_title}已取消\n"
                    f"📊 已处理: 总计 {total}, 成功 {success_count}, 跳过 {skipped_count}, "
                    f"失败 {fail_count}, 未处理 {remaining_count}\n"
                    f"📂 目标: {target_path}"
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
            yield event.plain_result(
                f"✅ 备份任务结束!\n"
                f"📊 统计: 总计 {total}, 成功 {success_count}, 跳过 {skipped_count}, 失败 {fail_count}\n"
                f"📂 目标: {target_path}"
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
