import asyncio
import os
import posixpath
import re
import time
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..lib.client import OpenlistClient
from .base import PluginService


class UploadService(PluginService):
    """Upload service."""

    CQ_SEGMENT_RE = re.compile(r"\[CQ:([a-zA-Z0-9_]+)(?:,([^\]]*))?\]")
    UPLOAD_SEGMENT_TYPES = {"file", "image", "video"}
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
            if attempt > 1 and callable(refresh_url):
                try:
                    refreshed_url = await refresh_url()
                    if refreshed_url:
                        current_url = refreshed_url
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

    def _cq_unescape(self, value: str) -> str:
        return (
            value
            .replace("&#91;", "[")
            .replace("&#93;", "]")
            .replace("&#44;", ",")
            .replace("&amp;", "&")
        )

    def _parse_cq_segments(self, message: str) -> List[Dict]:
        segments = []
        for match in self.CQ_SEGMENT_RE.finditer(message or ""):
            segment_type = match.group(1)
            raw_params = match.group(2) or ""
            data = {}
            for part in raw_params.split(","):
                if not part or "=" not in part:
                    continue
                key, value = part.split("=", 1)
                data[key] = self._cq_unescape(value)
            segments.append({"type": segment_type, "data": data})
        return segments

    def _normalize_message_segments(self, message) -> List[Dict]:
        if isinstance(message, list):
            return [segment for segment in message if isinstance(segment, dict)]
        if isinstance(message, dict) and message.get("type"):
            return [message]
        if isinstance(message, str):
            if "[CQ:" not in message:
                return []
            return self._parse_cq_segments(message)
        return []

    def _extract_upload_segments(self, message_data: Dict) -> List[Dict]:
        message = message_data.get("message") if isinstance(message_data, dict) else None
        segments = self._normalize_message_segments(message)
        return [segment for segment in segments if segment.get("type") in self.UPLOAD_SEGMENT_TYPES]

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

    def _is_self_message(self, raw_message: Dict) -> bool:
        if self._read_value(raw_message, "post_type") == "message_sent":
            return True
        self_id = self._read_value(raw_message, "self_id")
        user_id = self._read_value(raw_message, "user_id")
        return self_id not in (None, "") and str(self_id) == str(user_id)

    async def remember_uploadable_message(self, event: AstrMessageEvent):
        """记录同会话最近的附件消息，供 ol upload 使用。"""
        self._prune_recent_upload_messages()
        raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw_message, dict):
            return
        if self._is_self_message(raw_message):
            return

        segments = self._normalize_message_segments(raw_message.get("message"))
        upload_segments = [segment for segment in segments if segment.get("type") in self.UPLOAD_SEGMENT_TYPES]
        if not upload_segments:
            return

        nav_key = self._get_navigation_state_key(event)
        cached_message = {
            "group_id": raw_message.get("group_id"),
            "message_id": raw_message.get("message_id"),
            "message": upload_segments,
        }
        if cached_message.get("group_id") in (None, ""):
            group_id = self._get_event_group_id(event)
            if group_id not in (None, ""):
                cached_message["group_id"] = group_id

        self.recent_upload_messages[nav_key] = {
            "timestamp": time.time(),
            "message": cached_message,
        }
        self._prune_recent_upload_messages()
        logger.debug(
            f"已记录最近可上传附件消息: session={nav_key}, "
            f"segments={len(upload_segments)}, message_id={cached_message.get('message_id')}"
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

    def _as_int(self, value) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _is_http_url(self, value: str) -> bool:
        return isinstance(value, str) and value.startswith(("http://", "https://"))

    def _basename_from_url(self, url: str) -> str:
        if not self._is_http_url(url):
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
        file_value = data.get("file") or ""
        if not candidate and file_value and not self._is_http_url(file_value):
            candidate = file_value
        if not candidate:
            candidate = self._basename_from_url(file_value) or self._basename_from_url(source_url)

        fallback = f"{segment_type}_{self._unique_suffix()}"
        safe_name = self._sanitize_filename(str(candidate), fallback)
        root, ext = os.path.splitext(safe_name)
        ext = ext.lower()

        if segment_type == "image" and (not ext or ext == ".image"):
            return f"{root or fallback}.jpg"
        if segment_type == "video" and not ext:
            return f"{safe_name}.mp4"
        return safe_name

    def _get_message_group_id(self, event: AstrMessageEvent, message_data: Dict):
        group_id = self._read_value(message_data, "group_id")
        if group_id not in (None, ""):
            return group_id
        return getattr(getattr(event, "message_obj", None), "group_id", None)

    async def _get_group_file_url(self, event: AstrMessageEvent, group_id, file_id: str, busid: int = 0) -> Optional[str]:
        if not group_id or not file_id:
            return None
        try:
            url_res = await event.bot.api.call_action(
                "get_group_file_url",
                group_id=int(group_id),
                file_id=file_id,
                busid=busid or 0,
            )
            return url_res.get("url") if isinstance(url_res, dict) else None
        except Exception as e:
            logger.warning(f"获取群文件 URL 失败: group={group_id}, file_id={file_id}, err={e}")
            return None

    async def _build_upload_item(self, event: AstrMessageEvent, message_data: Dict, segment: Dict) -> Dict:
        segment_type = segment.get("type")
        data = segment.get("data") or {}
        file_value = str(data.get("file") or "")
        source_url = str(data.get("url") or "").strip()
        if not source_url and self._is_http_url(file_value):
            source_url = file_value

        file_id = data.get("file_id")
        busid = self._as_int(data.get("busid")) or 0
        group_id = self._get_message_group_id(event, message_data)
        if segment_type == "file" and not source_url and file_id:
            source_url = await self._get_group_file_url(event, group_id, file_id, busid) or ""

        file_name = self._build_segment_filename(segment_type, data, source_url)
        file_size = self._as_int(data.get("file_size"))
        if file_size is None:
            file_size = self._as_int(data.get("size"))

        async def refresh_url():
            if segment_type != "file" or not file_id:
                return None
            return await self._get_group_file_url(event, group_id, file_id, busid)

        return {
            "type": segment_type,
            "name": file_name,
            "size": file_size,
            "url": source_url,
            "refresh_url": refresh_url if segment_type == "file" and file_id else None,
        }

    async def _probe_url_size(self, source_url: str, user_config: Dict) -> Optional[int]:
        timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=self._get_positive_int_config(user_config, "upstream_connect_timeout", 60),
            sock_read=self._get_positive_int_config(user_config, "upstream_read_timeout", 180),
        )
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.head(source_url, allow_redirects=True) as response:
                    size = self._as_int(response.headers.get("Content-Length"))
                    if response.status < 400 and size is not None:
                        return size
        except Exception as e:
            logger.debug(f"HEAD 探测文件大小失败，尝试 Range GET: url={source_url}, err={e}")

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(source_url, headers={"Range": "bytes=0-0"}) as response:
                    content_range = response.headers.get("Content-Range", "")
                    if "/" in content_range:
                        size = self._as_int(content_range.rsplit("/", 1)[-1])
                        if size is not None:
                            return size
                    return self._as_int(response.headers.get("Content-Length"))
        except Exception as e:
            logger.warning(f"探测文件大小失败: url={source_url}, err={e}")
            return None

    async def upload_command(self, event: AstrMessageEvent, target: str = ""):
        """上传最近附件消息中的文件、图片或视频。"""
        user_id = event.get_sender_id()
        nav_key = self._get_navigation_state_key(event)
        target = (target or "").strip()
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

        target_path = self._resolve_target_path(nav_key, target)
        upload_message = self._get_recent_upload_message(event)
        if not upload_message:
            yield event.plain_result(self._format_upload_usage_tip("没有找到可上传的最近附件消息"))
            return

        upload_segments = self._extract_upload_segments(upload_message)
        if not upload_segments:
            yield event.plain_result(self._format_upload_usage_tip("最近附件消息中没有可上传的图片、视频或文件"))
            return

        max_upload_size_mb = self._get_size_limit_mb(user_config, "max_upload_size", 100)
        max_upload_size = max_upload_size_mb * 1024 * 1024
        total = len(upload_segments)
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

                yield event.plain_result(f"📤 准备上传最近附件消息中的 {total} 个文件\n📂 目标: {target_path}")

                for index, segment in enumerate(upload_segments, start=1):
                    item = await self._build_upload_item(event, upload_message, segment)
                    file_name = item["name"]
                    file_size = item["size"]
                    source_url = item["url"]

                    if not source_url:
                        fail_count += 1
                        yield event.plain_result(
                            f"❌ 无法获取附件下载地址：{file_name}\n"
                            "提示：请重新发送该附件后再执行 ol upload，或检查当前 OneBot 适配器是否提供附件 URL。"
                        )
                        continue

                    if not self._is_extension_allowed(file_name, user_config):
                        fail_count += 1
                        yield event.plain_result(
                            f"❌ 文件类型不允许上传: {file_name}\n"
                            f"💡 当前允许: {self._format_extension_filter(user_config)}"
                        )
                        continue

                    if file_size is None and max_upload_size_mb > 0:
                        file_size = await self._probe_url_size(source_url, user_config)

                    if max_upload_size_mb > 0:
                        if file_size is None:
                            fail_count += 1
                            yield event.plain_result(
                                f"❌ 无法确认文件大小: {file_name}\n"
                                f"提示：当前上传大小限制为 {max_upload_size_mb}MB。"
                            )
                            continue
                        if file_size > max_upload_size:
                            fail_count += 1
                            size_mb = file_size / (1024 * 1024)
                            yield event.plain_result(
                                f"❌ 文件过大：{file_name} {size_mb:.1f}MB > {max_upload_size_mb}MB\n"
                                f"提示：当前上传大小限制为 {max_upload_size_mb}MB。"
                            )
                            continue

                    size_text = self._format_file_size(file_size) if file_size is not None else "未知"
                    yield event.plain_result(f"📤 正在上传 ({index}/{total}): {file_name}\n💾 大小: {size_text}")
                    success = await self._upload_url_stream_with_retry(
                        client,
                        source_url,
                        target_path,
                        file_name,
                        file_size,
                        user_config,
                        refresh_url=item["refresh_url"],
                    )
                    if success:
                        success_count += 1
                    else:
                        fail_count += 1
                        yield event.plain_result(f"❌ 上传失败: {file_name}")

                if success_count:
                    self.cache_manager.clear_cache(user_id)
                    result = await client.list_files(target_path)
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
            logger.error(f"用户 {user_id} 最近附件上传失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
