import asyncio
import os
import posixpath
import time
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File

from ..lib.client import OpenlistClient
from ..lib.onebot_files import (
    FLASH_SEGMENT_TYPES,
    MEDIA_UPLOAD_SEGMENT_TYPES,
    extract_flash_descriptors_from_segments,
    extract_flash_descriptors_from_text,
    is_http_url,
    is_self_event,
    normalize_message_segments,
    read_field,
    resolve_flash_transfer_files,
    resolve_group_file_source,
    resolve_media_segment_source,
    resolve_private_file_source,
    sanitize_relative_subpath,
    to_int_or_none,
)
from .base import PluginService


class UploadService(PluginService):
    """Upload service supporting group/private attachments, offline_file notices, and QQ Flash Transfer."""

    UPLOAD_SEGMENT_TYPES = MEDIA_UPLOAD_SEGMENT_TYPES | FLASH_SEGMENT_TYPES
    RECENT_UPLOAD_TTL_SECONDS = 300
    MAX_RECENT_UPLOAD_MESSAGES = 500

    async def _upload_file_with_retry(
        self,
        client: OpenlistClient,
        file_path: str,
        target_path: str,
        file_name: str,
        user_config: Dict,
    ) -> bool:
        """本地文件上传自动重试。"""
        attempts, retry_delay = self._get_retry_config(user_config, "upload")
        for attempt in range(1, attempts + 1):
            if await client.upload_file(file_path, target_path, file_name):
                return True
            logger.warning(f"上传文件 {file_name} 第 {attempt}/{attempts} 次失败。")
            if attempt < attempts:
                await asyncio.sleep(retry_delay)
        return False

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
        """URL 中转上传自动重试；全部失败后改用本地临时文件备用上传。"""
        attempts, retry_delay = self._get_retry_config(user_config, "upload")
        current_url = source_url
        for attempt in range(1, attempts + 1):
            if (attempt > 1 or not current_url) and callable(refresh_url):
                try:
                    refreshed = await refresh_url()
                    if isinstance(refreshed, dict):
                        if refreshed.get("local_path") and os.path.isfile(refreshed["local_path"]):
                            return await self._upload_file_with_retry(
                                client, refreshed["local_path"], target_path, file_name, user_config
                            )
                        if refreshed.get("url"):
                            current_url = refreshed["url"]
                    elif refreshed:
                        current_url = refreshed
                except Exception as e:
                    logger.warning(f"刷新上传 URL 失败: {file_name}, attempt={attempt}/{attempts}, err={e}")

            if current_url and await client.upload_url_stream(current_url, target_path, file_name, file_size):
                return True
            logger.warning(f"URL 中转上传 {file_name} 第 {attempt}/{attempts} 次失败。")
            if attempt < attempts:
                await asyncio.sleep(retry_delay)

        logger.info(f"🧰 URL 中转上传 {file_name} {attempts} 次失败，改用本地临时文件备用上传。")
        success, _ = await self._upload_url_via_temp_file(
            client,
            current_url,
            target_path,
            file_name,
            file_size,
            temp_dir_name="upload_temp",
            temp_prefix="upload",
            attempts=1,
            retry_delay=retry_delay,
            refresh_url=refresh_url,
        )
        return success

    def _prune_recent_upload_messages(self):
        now = time.time()
        expired_keys = [
            key for key, cached in self.recent_upload_messages.items()
            if now - cached.get("timestamp", 0) > self.RECENT_UPLOAD_TTL_SECONDS
        ]
        for key in expired_keys:
            self.recent_upload_messages.pop(key, None)
        overflow = len(self.recent_upload_messages) - self.MAX_RECENT_UPLOAD_MESSAGES
        if overflow <= 0:
            return
        oldest_keys = sorted(
            self.recent_upload_messages,
            key=lambda key: self.recent_upload_messages[key].get("timestamp", 0),
        )[:overflow]
        for key in oldest_keys:
            self.recent_upload_messages.pop(key, None)

    def _extract_uploadable_entries_from_segments(
        self,
        segments: List[Dict],
        include_plain_flash_links: bool = True,
    ) -> tuple:
        """返回 (media_segments, flash_descriptors)。"""
        media_segments = [
            seg for seg in segments
            if isinstance(seg, dict) and str(seg.get("type") or "").lower() in MEDIA_UPLOAD_SEGMENT_TYPES
        ]
        flash_descriptors = extract_flash_descriptors_from_segments(
            segments,
            include_plain_text=include_plain_flash_links,
        )
        return media_segments, flash_descriptors

    def _extract_component_fallbacks(self, event: AstrMessageEvent) -> List[Dict]:
        """当平台未提供 OneBot raw_message 段时，从 AstrBot 消息组件提取可上传附件。"""
        get_messages = getattr(event, "get_messages", None)
        if not callable(get_messages):
            return []
        segments = []
        for comp in get_messages() or []:
            comp_cls = type(comp).__name__.lower()
            if isinstance(comp, File) or comp_cls == "file":
                seg_type = "file"
            elif comp_cls in ("image", "video"):
                seg_type = comp_cls
            else:
                continue
            segments.append({
                "type": seg_type,
                "data": {
                    "name": read_field(comp, "name") or "",
                    "file": read_field(comp, "file") or "",
                    "file_id": read_field(comp, "file_id") or "",
                    "url": read_field(comp, "url") or "",
                    "file_size": read_field(comp, "file_size") or read_field(comp, "size"),
                    "busid": read_field(comp, "busid", 0),
                },
                "_component": comp,
            })
        return segments

    async def remember_uploadable_message(self, event: AstrMessageEvent):
        """记录同会话最近的附件消息、私聊离线文件通知及 QQ 闪传消息，供 ol upload 使用。"""
        message_obj = getattr(event, "message_obj", None)
        raw_message = read_field(message_obj, "raw_message")
        if is_self_event(event, raw_message):
            return

        is_cmd = self._is_openlist_command_message(event)
        group_id = self._get_event_group_id(event)
        user_id = str(event.get_sender_id() or read_field(raw_message, "user_id") or "")

        media_segments: List[Dict] = []
        flash_descriptors: List[Dict] = []

        # 1. 处理 OneBot 通知事件（offline_file 私聊离线文件 / group_upload 群文件上传）
        notice_type = read_field(raw_message, "notice_type")
        if notice_type in ("offline_file", "group_upload"):
            file_info = read_field(raw_message, "file", {}) or {}
            file_name = (
                read_field(file_info, "name")
                or read_field(file_info, "file_name")
                or read_field(file_info, "file")
            )
            file_id = (
                read_field(file_info, "id")
                or read_field(file_info, "file_id")
                or read_field(raw_message, "file_id")
            )
            file_url = read_field(file_info, "url") or read_field(raw_message, "url") or ""
            file_size = to_int_or_none(
                read_field(file_info, "size", read_field(raw_message, "file_size"))
            )
            busid = to_int_or_none(read_field(file_info, "busid", read_field(raw_message, "busid"))) or 0
            if file_name or file_id or file_url:
                media_segments.append({
                    "type": "file",
                    "data": {
                        "name": file_name or "",
                        "file": file_name or "",
                        "file_id": file_id or "",
                        "url": file_url,
                        "file_size": file_size,
                        "busid": busid,
                        "file_hash": read_field(file_info, "file_hash") or read_field(file_info, "hash"),
                    },
                })
                if notice_type == "offline_file":
                    group_id = None
                    if not user_id:
                        user_id = str(read_field(raw_message, "user_id") or "")
                elif notice_type == "group_upload" and not group_id:
                    group_id = read_field(raw_message, "group_id")

        # 2. 处理普通消息段与闪传卡片/链接
        if not media_segments and isinstance(raw_message, dict):
            segments = normalize_message_segments(raw_message.get("message"))
            media_segments, flash_descriptors = self._extract_uploadable_entries_from_segments(
                segments,
                include_plain_flash_links=not is_cmd,
            )

        # 3. 兜底：从 AstrBot 消息组件提取附件或从纯文本提取闪传链接
        if not media_segments and not flash_descriptors:
            comp_segments = self._extract_component_fallbacks(event)
            if comp_segments:
                media_segments = comp_segments
            elif not is_cmd:
                flash_descriptors = extract_flash_descriptors_from_text(getattr(event, "message_str", "") or "")

        if not media_segments and not flash_descriptors:
            return

        # 同步记录私聊文件/闪传历史，供私聊 ol backup 手动扫描补充
        if group_id in (None, "") and user_id and hasattr(self, "backup_service"):
            self.backup_service.record_private_transfer_cache(
                user_id=user_id,
                media_segments=media_segments,
                flash_descriptors=flash_descriptors,
            )

        nav_key = f"group:{group_id}:user:{user_id}" if group_id not in (None, "") else f"private:user:{user_id}"
        cached_message = {
            "group_id": group_id,
            "user_id": user_id,
            "message_id": read_field(raw_message, "message_id"),
            "message": media_segments,
            "flash_descriptors": flash_descriptors,
        }

        self._prune_recent_upload_messages()
        self.recent_upload_messages[nav_key] = {
            "timestamp": time.time(),
            "message": cached_message,
        }
        logger.debug(
            f"已记录最近可上传消息: session={nav_key}, "
            f"segments={len(media_segments)}, flash={len(flash_descriptors)}, "
            f"message_id={cached_message.get('message_id')}"
        )

    def _get_recent_upload_message(self, event: AstrMessageEvent) -> Optional[Dict]:
        self._prune_recent_upload_messages()
        nav_key = self._get_navigation_state_key(event)
        cached = self.recent_upload_messages.get(nav_key)
        if not cached:
            return None
        if time.time() - cached.get("timestamp", 0) > self.RECENT_UPLOAD_TTL_SECONDS:
            self.recent_upload_messages.pop(nav_key, None)
            return None
        message = cached.get("message")
        return message if isinstance(message, dict) else None

    def _basename_from_url(self, url: str) -> str:
        if not is_http_url(url):
            return ""
        path = unquote(urlparse(url).path or "")
        return posixpath.basename(path.rstrip("/"))

    def _build_segment_filename(self, segment_type: str, data: Dict, source_url: str) -> str:
        candidate = (
            data.get("name")
            or data.get("filename")
            or data.get("file_name")
            or ""
        )
        file_value = str(data.get("file") or "")
        if not candidate and file_value and not is_http_url(file_value):
            candidate = os.path.basename(file_value.replace("\\", "/"))
        if not candidate:
            candidate = self._basename_from_url(file_value) or self._basename_from_url(source_url)

        fallback = f"{segment_type}_{self._unique_suffix()}"
        safe_name = self._sanitize_remote_filename(str(candidate), fallback=fallback)
        root, ext = os.path.splitext(safe_name)
        ext_lower = ext.lower()

        if segment_type == "image" and (not ext_lower or ext_lower == ".image"):
            return f"{root or fallback}.jpg"
        if segment_type == "video" and not ext_lower:
            return f"{safe_name}.mp4"
        return safe_name

    def _get_message_group_id(self, event: AstrMessageEvent, message_data: Dict):
        group_id = self._read_value(message_data, "group_id")
        if group_id not in (None, ""):
            return group_id
        return self._get_event_group_id(event)

    async def _build_upload_item(self, event: AstrMessageEvent, message_data: Dict, segment: Dict) -> Dict:
        segment_type = str(segment.get("type") or "").lower()
        data = segment.get("data") or {}
        file_component = segment.get("_component")
        file_value = str(data.get("file") or "")
        source_url = str(data.get("url") or "").strip()
        if not source_url and is_http_url(file_value):
            source_url = file_value.strip()

        local_path = ""
        if not source_url and file_value and os.path.isfile(file_value):
            local_path = file_value

        file_id = data.get("file_id") or data.get("id")
        file_hash = data.get("file_hash") or data.get("hash")
        busid = to_int_or_none(data.get("busid")) or 0
        group_id = self._get_message_group_id(event, message_data)
        user_id = self._read_value(message_data, "user_id") or event.get_sender_id()

        if segment_type == "file" and not source_url and not local_path and file_id:
            if group_id not in (None, ""):
                resolved = await resolve_group_file_source(event, group_id, str(file_id), busid=busid)
            else:
                resolved = await resolve_private_file_source(
                    event, user_id, str(file_id), file_hash=file_hash
                )
            source_url = resolved.get("url", "")
            local_path = resolved.get("local_path", "")
        elif segment_type in ("image", "video") and not source_url and not local_path:
            resolved = await resolve_media_segment_source(event, segment_type, data)
            source_url = resolved.get("url", "")
            local_path = resolved.get("local_path", "")

        component_temp_file = False
        if not source_url and not local_path and file_component is not None:
            get_file_fn = getattr(file_component, "get_file", None)
            if callable(get_file_fn):
                try:
                    fetched = await get_file_fn()
                    if fetched and os.path.isfile(fetched):
                        local_path = fetched
                        component_temp_file = True
                except Exception as e:
                    logger.warning(f"从消息组件获取本地附件失败: {e}")

        file_name = self._build_segment_filename(segment_type, data, source_url)
        file_size = to_int_or_none(data.get("file_size"))
        if file_size is None:
            file_size = to_int_or_none(data.get("size"))
        if file_size is None and local_path and os.path.isfile(local_path):
            file_size = os.path.getsize(local_path)

        async def refresh_url():
            if segment_type == "file" and file_id:
                if group_id not in (None, ""):
                    return await resolve_group_file_source(event, group_id, str(file_id), busid=busid)
                return await resolve_private_file_source(event, user_id, str(file_id), file_hash=file_hash)
            if segment_type in ("image", "video"):
                return await resolve_media_segment_source(event, segment_type, data)
            return None

        return {
            "type": segment_type,
            "name": file_name,
            "relative_path": file_name,
            "size": file_size,
            "url": source_url,
            "local_path": local_path,
            "cleanup_local": component_temp_file,
            "refresh_url": refresh_url if (file_id or segment_type in ("image", "video")) else None,
            "is_flash": False,
        }

    async def _probe_url_size(self, source_url: str, user_config: Dict) -> Optional[int]:
        timeout = aiohttp.ClientTimeout(
            total=30,
            sock_connect=self._get_positive_int_config(user_config, "upstream_connect_timeout", 60),
            sock_read=self._get_positive_int_config(user_config, "upstream_read_timeout", 180),
        )
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.head(source_url, allow_redirects=True) as response:
                    size = to_int_or_none(response.headers.get("Content-Length"))
                    if response.status < 400 and size is not None:
                        return size
        except Exception as e:
            logger.debug(f"HEAD 探测文件大小失败，尝试 Range GET: url={source_url}, err={e}")

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(source_url, headers={"Range": "bytes=0-0"}) as response:
                    content_range = response.headers.get("Content-Range", "")
                    if "/" in content_range:
                        size = to_int_or_none(content_range.rsplit("/", 1)[-1])
                        if size is not None:
                            return size
                    return to_int_or_none(response.headers.get("Content-Length"))
        except Exception as e:
            logger.warning(f"探测文件大小失败: url={source_url}, err={e}")
            return None

    def _parse_upload_args(self, target: str, extra: str) -> tuple:
        """
        解析 ol upload [目标目录] [闪传链接/fileset:xxx] 参数（顺序自适应）。
        返回 (target_dir_arg, direct_flash_descriptors, error_message)。
        """
        args = [a.strip() for a in (target or "", extra or "") if (a or "").strip()]
        target_dir_arg = ""
        direct_flash_descriptors: List[Dict] = []

        for arg in args:
            flash_from_text = extract_flash_descriptors_from_text(arg)
            if flash_from_text:
                direct_flash_descriptors.extend(flash_from_text)
                continue
            if arg.lower().startswith(("fileset:", "flash:")):
                fs_id = arg.split(":", 1)[1].strip()
                if fs_id:
                    direct_flash_descriptors.append({
                        "fileset_id": fs_id,
                        "share_code": None,
                        "url": "",
                        "title": "",
                    })
                    continue
            if not target_dir_arg:
                target_dir_arg = arg
            else:
                return "", [], f"无法识别的多余参数：{arg}"

        return target_dir_arg, direct_flash_descriptors, ""

    async def upload_command(self, event: AstrMessageEvent, target: str = "", extra: str = ""):
        """上传最近附件消息中的群文件、私聊文件、图片/视频或 QQ 闪传多文件。"""
        user_id = event.get_sender_id()
        nav_key = self._get_navigation_state_key(event)
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

        target_dir_arg, direct_flash_descriptors, arg_error = self._parse_upload_args(target, extra)
        if arg_error:
            yield event.plain_result(self._format_upload_usage_tip(arg_error))
            return

        target_path = self._resolve_target_path(nav_key, target_dir_arg)

        media_segments: List[Dict] = []
        flash_descriptors: List[Dict] = list(direct_flash_descriptors)

        if not flash_descriptors:
            upload_message = self._get_recent_upload_message(event)
            if not upload_message:
                yield event.plain_result(self._format_upload_usage_tip("没有找到可上传的最近附件或闪传消息"))
                return
            media_segments = list(upload_message.get("message") or [])
            flash_descriptors = list(upload_message.get("flash_descriptors") or [])
            if not media_segments and not flash_descriptors:
                # 兼容旧版缓存结构
                raw_segs = normalize_message_segments(upload_message.get("message"))
                media_segments, flash_descriptors = self._extract_uploadable_entries_from_segments(raw_segs)
        else:
            upload_message = {"group_id": self._get_event_group_id(event), "user_id": user_id}

        if not media_segments and not flash_descriptors:
            yield event.plain_result(self._format_upload_usage_tip("最近消息中没有可上传的图片、视频、文件或 QQ 闪传"))
            return

        # 解析所有待上传条目（包含展开 QQ 闪传多文件集）
        upload_items: List[Dict] = []
        for seg in media_segments:
            upload_items.append(await self._build_upload_item(event, upload_message, seg))

        if flash_descriptors:
            yield event.plain_result(f"⚡ 正在解析 QQ 闪传文件集（共 {len(flash_descriptors)} 个闪传任务）...")
            for desc in flash_descriptors:
                fileset_id, flash_files = await resolve_flash_transfer_files(event, desc)
                if not flash_files:
                    label = desc.get("fileset_id") or desc.get("share_code") or desc.get("url") or "未知闪传"
                    yield event.plain_result(
                        f"❌ 无法解析 QQ 闪传文件列表：{label}\n"
                        "提示：请确认当前 OneBot 协议端（如 NapCat）支持闪传扩展接口，且闪传未过期。"
                    )
                    continue
                for f_item in flash_files:
                    rel_path = sanitize_relative_subpath(
                        f_item.get("relative_path") or f_item.get("file_name") or "flash_file",
                        fallback_name=f_item.get("file_name") or "flash_file",
                    )
                    upload_items.append({
                        "type": "flashtransfer",
                        "name": posixpath.basename(rel_path) or f_item.get("file_name") or "flash_file",
                        "relative_path": rel_path,
                        "size": f_item.get("file_size"),
                        "url": f_item.get("url") or "",
                        "local_path": f_item.get("local_path") or "",
                        "cleanup_local": False,
                        "refresh_url": f_item.get("refresh_url"),
                        "is_flash": True,
                        "fileset_id": fileset_id,
                    })

        if not upload_items:
            return

        max_upload_size_mb = self._get_size_limit_mb(user_config, "max_upload_size", 100)
        max_upload_size = max_upload_size_mb * 1024 * 1024
        total = len(upload_items)
        success_count = 0
        fail_count = 0

        try:
            async with self._create_openlist_client(user_config) as client:
                result = await client.list_files(target_path, per_page=1)
                if result is None:
                    yield event.plain_result(self._format_usage_tip(
                        f"无法访问上传目标目录：{target_path}",
                        "ol upload [OpenList目标目录]",
                        [
                            "ol upload",
                            "ol upload /movies",
                            "ol upload clips",
                        ],
                        "请确认目标目录存在，并且当前 OpenList 账号有写入权限。",
                    ))
                    return

                ensured_dirs = {self._normalize_openlist_path(target_path)}
                yield event.plain_result(f"📤 准备转存/上传共 {total} 个文件\n📂 目标根目录: {target_path}")

                for index, item in enumerate(upload_items, start=1):
                    file_name = item["name"]
                    rel_path = item.get("relative_path") or file_name
                    rel_dir = posixpath.dirname(rel_path)
                    item_target_dir = (
                        self._normalize_openlist_path(f"{target_path.rstrip('/')}/{rel_dir}")
                        if rel_dir
                        else self._normalize_openlist_path(target_path)
                    )
                    file_size = item["size"]
                    source_url = item["url"]
                    local_path = item.get("local_path") or ""
                    cleanup_local = bool(item.get("cleanup_local"))

                    try:
                        if not source_url and not local_path and callable(item.get("refresh_url")):
                            refreshed = await item["refresh_url"]()
                            if isinstance(refreshed, dict):
                                source_url = refreshed.get("url") or ""
                                local_path = refreshed.get("local_path") or ""
                            elif isinstance(refreshed, str):
                                source_url = refreshed

                        if not source_url and not local_path:
                            fail_count += 1
                            yield event.plain_result(
                                f"❌ 无法获取附件下载地址：{rel_path}\n"
                                "提示：请重新发送该附件后再执行 ol upload，或检查当前 OneBot 协议端是否支持对应文件直链接口。"
                            )
                            continue

                        if not self._is_extension_allowed(file_name, user_config):
                            fail_count += 1
                            yield event.plain_result(
                                f"❌ 文件类型不允许上传: {rel_path}\n"
                                f"💡 当前允许: {self._format_extension_filter(user_config)}"
                            )
                            continue

                        if file_size is None and local_path and os.path.isfile(local_path):
                            file_size = os.path.getsize(local_path)
                        if file_size is None and source_url and max_upload_size_mb > 0:
                            file_size = await self._probe_url_size(source_url, user_config)

                        if max_upload_size_mb > 0:
                            if file_size is None:
                                fail_count += 1
                                yield event.plain_result(
                                    f"❌ 无法确认文件大小: {rel_path}\n"
                                    f"提示：当前上传大小限制为 {max_upload_size_mb}MB。"
                                )
                                continue
                            if file_size > max_upload_size:
                                fail_count += 1
                                size_mb = file_size / (1024 * 1024)
                                yield event.plain_result(
                                    f"❌ 文件过大：{rel_path} {size_mb:.1f}MB > {max_upload_size_mb}MB\n"
                                    f"提示：当前上传大小限制为 {max_upload_size_mb}MB。"
                                )
                                continue

                        if item_target_dir not in ensured_dirs:
                            if not await client.ensure_dir(item_target_dir):
                                fail_count += 1
                                yield event.plain_result(f"❌ 创建子目录失败：{item_target_dir}")
                                continue
                            ensured_dirs.add(item_target_dir)

                        size_text = self._format_file_size(file_size) if file_size is not None else "未知"
                        if total <= 5 or index == 1 or index == total or index % 5 == 0:
                            yield event.plain_result(f"📤 正在上传 ({index}/{total}): {rel_path}\n💾 大小: {size_text}")

                        if local_path and os.path.isfile(local_path):
                            success = await self._upload_file_with_retry(
                                client,
                                local_path,
                                item_target_dir,
                                file_name,
                                user_config,
                            )
                        else:
                            success = await self._upload_url_stream_with_retry(
                                client,
                                source_url,
                                item_target_dir,
                                file_name,
                                file_size,
                                user_config,
                                refresh_url=item.get("refresh_url"),
                            )

                        if success:
                            success_count += 1
                        else:
                            fail_count += 1
                            yield event.plain_result(f"❌ 上传失败: {rel_path}")
                    finally:
                        if cleanup_local and local_path:
                            self._remove_file_quietly(local_path, "上传组件临时文件")

                if success_count:
                    self._invalidate_openlist_cache(user_config, target_path, user_id)
                    result = await client.list_files(target_path, per_page=0, refresh=True)
                    if result:
                        files = result.get("content", [])
                        self._update_user_navigation_state(nav_key, target_path, files)
                        formatted_list = self._format_file_list(files, target_path, user_config, nav_key)
                        yield event.plain_result(f"📁 当前目录已更新:\n\n{formatted_list}")

                yield event.plain_result(
                    f"✅ 上传完成!\n"
                    f"📊 统计: 总计 {total}, 成功 {success_count}, 失败 {fail_count}\n"
                    f"📂 目标: {target_path}"
                )
        except Exception as e:
            logger.error(f"用户 {user_id} 最近附件/闪传上传失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
