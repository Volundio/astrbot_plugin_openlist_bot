import asyncio
import os
import posixpath
import re
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
        """URL 中转上传自动重试；可在重试时刷新平台文件 URL。"""
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
        return False

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
            return self._parse_cq_segments(message)
        return []

    def _get_raw_message_segments(self, event: AstrMessageEvent) -> List[Dict]:
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        message = self._read_value(raw_message, "message")
        return self._normalize_message_segments(message)

    def _extract_reply_message_id(self, event: AstrMessageEvent) -> Optional[str]:
        for segment in self._get_raw_message_segments(event):
            if segment.get("type") == "reply":
                data = segment.get("data") or {}
                reply_id = data.get("id") or data.get("message_id")
                if reply_id not in (None, ""):
                    return str(reply_id)

        try:
            components = event.get_messages()
        except Exception:
            components = []

        for component in components or []:
            class_name = component.__class__.__name__.lower()
            if "reply" not in class_name:
                continue
            for attr in ("id", "message_id", "reply_id"):
                value = getattr(component, attr, None)
                if value not in (None, ""):
                    return str(value)

        raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
        for key in ("reply_id", "source_msg_id", "message_id"):
            value = self._read_value(raw_message, key)
            if key == "message_id":
                continue
            if value not in (None, ""):
                return str(value)
        return None

    async def _get_replied_message(self, event: AstrMessageEvent, reply_id: str) -> Optional[Dict]:
        try:
            try:
                message_id = int(reply_id)
            except (TypeError, ValueError):
                message_id = reply_id
            result = await event.bot.api.call_action("get_msg", message_id=message_id)
            return result if isinstance(result, dict) else None
        except Exception as e:
            logger.error(f"获取引用消息失败: reply_id={reply_id}, err={e}", exc_info=True)
            return None

    def _extract_upload_segments(self, replied_message: Dict) -> List[Dict]:
        message = replied_message.get("message") if isinstance(replied_message, dict) else None
        segments = self._normalize_message_segments(message)
        return [segment for segment in segments if segment.get("type") in self.UPLOAD_SEGMENT_TYPES]

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

    def _get_message_group_id(self, event: AstrMessageEvent, replied_message: Dict):
        group_id = self._read_value(replied_message, "group_id")
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

    async def _build_upload_item(self, event: AstrMessageEvent, replied_message: Dict, segment: Dict) -> Dict:
        segment_type = segment.get("type")
        data = segment.get("data") or {}
        file_value = str(data.get("file") or "")
        source_url = str(data.get("url") or "").strip()
        if not source_url and self._is_http_url(file_value):
            source_url = file_value

        file_id = data.get("file_id")
        busid = self._as_int(data.get("busid")) or 0
        group_id = self._get_message_group_id(event, replied_message)
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
        """上传引用消息中的文件、图片或视频。"""
        user_id = event.get_sender_id()
        nav_key = self._get_navigation_state_key(event)
        target = (target or "").strip()
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return

        reply_id = self._extract_reply_message_id(event)
        if not reply_id:
            yield event.plain_result("❌ 请回复一条包含图片、视频或文件的消息后发送 /ol upload [目标目录]")
            return

        target_path = self._resolve_target_path(nav_key, target)
        replied_message = await self._get_replied_message(event, reply_id)
        if not replied_message:
            yield event.plain_result("❌ 无法获取被引用消息，请确认该消息仍可被 OneBot 查询。")
            return

        upload_segments = self._extract_upload_segments(replied_message)
        if not upload_segments:
            yield event.plain_result("❌ 被引用消息中没有可上传的图片、视频或文件。")
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
                    yield event.plain_result(f"❌ 无法访问上传目标目录: {target_path}")
                    return

                yield event.plain_result(f"📤 准备上传引用消息中的 {total} 个文件\n📂 目标: {target_path}")

                for index, segment in enumerate(upload_segments, start=1):
                    item = await self._build_upload_item(event, replied_message, segment)
                    file_name = item["name"]
                    file_size = item["size"]
                    source_url = item["url"]

                    if not source_url:
                        fail_count += 1
                        yield event.plain_result(f"❌ 无法获取引用文件下载地址: {file_name}")
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
                                f"💡 当前 max_upload_size={max_upload_size_mb}MB；如需允许未知大小文件，请将 max_upload_size 设为 0。"
                            )
                            continue
                        if file_size > max_upload_size:
                            fail_count += 1
                            size_mb = file_size / (1024 * 1024)
                            yield event.plain_result(f"❌ 文件过大: {file_name} {size_mb:.1f}MB > {max_upload_size_mb}MB")
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
                    f"✅ 引用上传完成!\n"
                    f"📊 统计: 总计 {total}, 成功 {success_count}, 失败 {fail_count}\n"
                    f"📂 目标: {target_path}"
                )
        except Exception as e:
            logger.error(f"用户 {user_id} 引用上传失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
