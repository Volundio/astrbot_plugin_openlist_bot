import asyncio
import os
import time
from typing import Dict, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File, Image

from ..lib.client import OpenlistClient
from .base import PluginService


class UploadService(PluginService):
    """Upload service."""

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

    async def _upload_file(self, event: AstrMessageEvent, file_component: File, user_config: Dict):
        user_id = event.get_sender_id()
        upload_state_key = self._get_upload_state_key(event)
        upload_state = self._get_user_upload_state(upload_state_key)
        target_path = upload_state["target_path"]

        file_name = None
        raw_file_id = None
        raw_file_size = None
        raw_file_url = None
        raw_busid = 0
        component_name = getattr(file_component, "name", None)
        component_url = getattr(file_component, "url", None)
        component_file = getattr(file_component, "file_", None)
        raw_event_data = event.message_obj.raw_message
        message_list = raw_event_data.get("message") if isinstance(raw_event_data, dict) else None
        if isinstance(message_list, list):
            for segment_dict in message_list:
                if isinstance(segment_dict, dict) and segment_dict.get("type") == "file":
                    data_dict = segment_dict.get("data", {})
                    file_name = data_dict.get("file")
                    raw_file_id = data_dict.get("file_id")
                    raw_file_size = data_dict.get("file_size")
                    raw_file_url = data_dict.get("url")
                    raw_busid = data_dict.get("busid", 0)
                    if file_name:
                        break

        file_name = file_name or component_name
        if not file_name:
            yield event.plain_result("出现异常，请稍后尝试上传")
            logger.warning(f"用户 {user_id} 上传文件失败：无法从原始消息中解析出有效的文件名。")
            return
        if not self._is_extension_allowed(file_name, user_config):
            yield event.plain_result(
                f"❌ 文件类型不允许上传: {file_name}\n"
                f"💡 当前允许: {self._format_extension_filter(user_config)}"
            )
            return

        raw_file_size_int = None
        if raw_file_size not in (None, ""):
            try:
                raw_file_size_int = int(raw_file_size)
            except (TypeError, ValueError):
                logger.warning(f"用户 {user_id} 上传文件大小解析失败: name={file_name}, raw_size={raw_file_size}")

        try:
            logger.info(
                f"用户 {user_id} 准备处理上传文件: name={file_name}, target={target_path}, "
                f"raw_size={raw_file_size}, file_id={raw_file_id}, raw_has_url={bool(raw_file_url)}, "
                f"component_name={component_name}, component_has_url={bool(component_url)}, "
                f"component_file={component_file}"
            )
            max_upload_size_mb = self._get_size_limit_mb(user_config, "max_upload_size", 100)
            max_upload_size = max_upload_size_mb * 1024 * 1024
            if max_upload_size_mb > 0 and raw_file_size_int is not None and raw_file_size_int > max_upload_size:
                size_mb = raw_file_size_int / (1024 * 1024)
                yield event.plain_result(f"❌ 文件过大: {size_mb:.1f}MB > {max_upload_size_mb}MB")
                return

            upload_url = raw_file_url or component_url
            if upload_url and (raw_file_size_int is not None or max_upload_size_mb == 0):
                yield event.plain_result(f"📤 开始上传: {file_name}\n💾 大小: {self._format_file_size(raw_file_size_int) if raw_file_size_int is not None else '未知'}\n📂 目标: {target_path}")
                logger.info(
                    f"用户 {user_id} 使用 URL 流式中转上传: name={file_name}, "
                    f"size={raw_file_size_int}, target={target_path}, openlist_url={user_config.get('openlist_url')}"
                )
                async def refresh_upload_url():
                    group_id = getattr(event.message_obj, "group_id", None)
                    if not group_id or not raw_file_id:
                        return None
                    url_res = await event.bot.api.call_action(
                        "get_group_file_url",
                        group_id=int(group_id),
                        file_id=raw_file_id,
                        busid=raw_busid or 0,
                    )
                    return url_res.get("url") if isinstance(url_res, dict) else None

                async with self._create_openlist_client(user_config) as client:
                    success = await self._upload_url_stream_with_retry(
                        client,
                        upload_url,
                        target_path,
                        file_name,
                        raw_file_size_int,
                        user_config,
                        refresh_url=refresh_upload_url,
                    )
                    if success:
                        yield event.plain_result(f"✅ 上传成功!\n📄 文件: {file_name}\n📂 路径: {target_path}")
                        self.cache_manager.clear_cache(user_id)
                        self._set_user_upload_waiting(upload_state_key, False)
                        result = await client.list_files(target_path)
                        if result:
                            files = result.get("content", [])
                            self._update_user_navigation_state(user_id, target_path, files)
                            formatted_list = self._format_file_list(files, target_path, user_config, user_id)
                            yield event.plain_result(f"📁 当前目录已更新:\n\n{formatted_list}")
                    else:
                        yield event.plain_result("❌ 上传失败，请检查网络连接和权限\n💡 提示: 管理员可在后台日志中查看详细错误信息")
                return

            if upload_url and raw_file_size_int is None and max_upload_size_mb > 0:
                logger.warning(f"用户 {user_id} 上传文件缺少有效大小，无法预先执行大小限制，回退到本地临时文件上传: name={file_name}")

            yield event.plain_result(f"📥 正在获取文件: {file_name}\n💾 大小: {self._format_file_size(raw_file_size_int) if raw_file_size_int is not None else '未知'}")
            get_file_started_at = time.monotonic()
            file_path = await file_component.get_file()
            get_file_elapsed = time.monotonic() - get_file_started_at

            if not file_path or not os.path.exists(file_path):
                logger.error(
                    f"用户 {user_id} 获取上传文件失败: name={file_name}, returned_path={file_path}, "
                    f"elapsed={get_file_elapsed:.2f}s"
                )
                yield event.plain_result("❌ 无法获取文件，请重新发送")
                return

            try:
                file_size = os.path.getsize(file_path)
                logger.info(
                    f"用户 {user_id} 获取上传文件完成: name={file_name}, local_path={file_path}, "
                    f"actual_size={file_size}, elapsed={get_file_elapsed:.2f}s"
                )
                if max_upload_size_mb > 0 and file_size > max_upload_size:
                    size_mb = file_size / (1024 * 1024)
                    yield event.plain_result(f"❌ 文件过大: {size_mb:.1f}MB > {max_upload_size_mb}MB")
                    return

                yield event.plain_result(f"📤 开始上传: {file_name}\n💾 大小: {self._format_file_size(file_size)}\n📂 目标: {target_path}")
                logger.info(
                    f"用户 {user_id} 开始调用 OpenList 上传: name={file_name}, local_path={file_path}, "
                    f"target={target_path}, openlist_url={user_config.get('openlist_url')}"
                )
                async with self._create_openlist_client(user_config) as client:
                    success = await self._upload_file_with_retry(client, file_path, target_path, file_name, user_config)
                    if success:
                        yield event.plain_result(f"✅ 上传成功!\n📄 文件: {file_name}\n📂 路径: {target_path}")
                        self.cache_manager.clear_cache(user_id)
                        self._set_user_upload_waiting(upload_state_key, False)
                        result = await client.list_files(target_path)
                        if result:
                            files = result.get("content", [])
                            self._update_user_navigation_state(user_id, target_path, files)
                            formatted_list = self._format_file_list(files, target_path, user_config, user_id)
                            yield event.plain_result(f"📁 当前目录已更新:\n\n{formatted_list}")
                    else:
                        yield event.plain_result(f"❌ 上传失败，请检查网络连接和权限\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)
        except Exception as e:
            logger.error(f"用户 {user_id} 上传文件失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            self._set_user_upload_waiting(upload_state_key, False)

    async def _upload_image(self, event: AstrMessageEvent, image_component: Image, user_config: Dict):
        """上传图片到Openlist"""
        user_id = event.get_sender_id()
        upload_state_key = self._get_upload_state_key(event)
        upload_state = self._get_user_upload_state(upload_state_key)
        target_path = upload_state["target_path"]
        try:
            image_path = await image_component.convert_to_file_path()
            if not image_path or not os.path.exists(image_path):
                yield event.plain_result("❌ 无法获取图片文件，请重新发送")
                return

            try:
                if image_path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
                    ext = os.path.splitext(image_path)[1]
                else:
                    ext = ".jpg"
                filename = f"image_{self._unique_suffix()}{ext}"
                if not self._is_extension_allowed(filename, user_config):
                    yield event.plain_result(
                        f"❌ 图片类型不允许上传: {filename}\n"
                        f"💡 当前允许: {self._format_extension_filter(user_config)}"
                    )
                    return
                file_size = os.path.getsize(image_path)
                max_upload_size_mb = self._get_size_limit_mb(user_config, "max_upload_size", 100)
                max_upload_size = max_upload_size_mb * 1024 * 1024
                if max_upload_size_mb > 0 and file_size > max_upload_size:
                    size_mb = file_size / (1024 * 1024)
                    yield event.plain_result(f"❌ 图片过大: {size_mb:.1f}MB > {max_upload_size_mb}MB")
                    return
                yield event.plain_result(f"📤 开始上传图片: {filename}\n💾 大小: {self._format_file_size(file_size)}\n📂 目标: {target_path}")
                async with self._create_openlist_client(user_config) as client:
                    success = await self._upload_file_with_retry(client, image_path, target_path, filename, user_config)
                    if success:
                        yield event.plain_result(f"✅ 图片上传成功!\n📄 文件: {filename}\n📂 路径: {target_path}")
                        self.cache_manager.clear_cache(user_id)
                        self._set_user_upload_waiting(upload_state_key, False)
                        result = await client.list_files(target_path)
                        if result:
                            files = result.get("content", [])
                            self._update_user_navigation_state(user_id, target_path, files)
                            formatted_list = self._format_file_list(files, target_path, user_config, user_id)
                            yield event.plain_result(f"📁 当前目录已更新:\n\n{formatted_list}")
                    else:
                        yield event.plain_result(f"❌ 上传失败，请检查网络连接和权限\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            finally:
                if os.path.exists(image_path):
                    os.remove(image_path)
        except Exception as e:
            logger.error(f"用户 {user_id} 上传图片失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            self._set_user_upload_waiting(upload_state_key, False)

    async def handle_file_message(self, event: AstrMessageEvent):
        """处理文件消息"""
        if not isinstance(event, AstrMessageEvent): return

        if not self._is_regular_message_event(event):
            return

        messages = event.get_messages()
        file_components = [msg for msg in messages if isinstance(msg, (File, Image))]
        if not file_components:
            return

        user_id = event.get_sender_id()
        upload_state_key = self._get_upload_state_key(event)
        upload_state = self._get_user_upload_state(upload_state_key)
        if not upload_state["waiting"]: return

        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息")
            self._set_user_upload_waiting(upload_state_key, False)
            return

        file_component = file_components[0]
        if isinstance(file_component, Image):
            async for result in self._upload_image(event, file_component, user_config):
                yield result
        else:
            async for result in self._upload_file(event, file_component, user_config):
                yield result

    async def upload_command(self, event: AstrMessageEvent, target: str = ""):
        """上传文件命令"""
        user_id = event.get_sender_id()
        upload_state_key = self._get_upload_state_key(event)
        target = (target or "").strip()
        if target.lower() in ("cancel", "取消"):
            upload_state = self._get_user_upload_state(upload_state_key)
            if upload_state["waiting"]:
                self._set_user_upload_waiting(upload_state_key, False)
                yield event.plain_result("✅ 已取消上传模式")
            else:
                yield event.plain_result("❌ 当前不在上传模式")
            return

        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return

        upload_timeout_minutes = self._get_upload_mode_timeout_minutes(user_config)
        target_path = self._resolve_target_path(user_id, target)
        try:
            async with self._create_openlist_client(user_config) as client:
                result = await client.list_files(target_path, per_page=1)
                if result is None:
                    yield event.plain_result(f"❌ 无法访问上传目标目录: {target_path}")
                    return
        except Exception as e:
            logger.error(f"用户 {user_id} 检查上传目标目录失败: {e}, 路径: {target_path}", exc_info=True)
            yield event.plain_result(f"❌ 无法访问上传目标目录: {target_path}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            return

        self._set_user_upload_waiting(upload_state_key, True, target_path)
        upload_text = f"""📤 上传模式已启动

📂 目标目录: {target_path}

💡 请直接发送文件或图片，系统会自动上传到此目录

⏰ 上传模式将在{upload_timeout_minutes}分钟后自动取消

📋 支持的操作:

• 直接发送文件 - 上传文件

• 直接发送图片 - 上传图片

• /ol upload 路径 - 切换上传目标目录

• /ol upload cancel - 取消上传模式

• /ol ls - 查看当前目录"""
        yield event.plain_result(upload_text)
        async def auto_cancel_upload():
            await asyncio.sleep(upload_timeout_minutes * 60)
            upload_state = self._get_user_upload_state(upload_state_key)
            if upload_state["waiting"] and upload_state.get("target_path") == target_path:
                self._set_user_upload_waiting(upload_state_key, False)
                logger.info(f"用户 {user_id} 在会话 {upload_state_key} 的上传模式已自动取消（超时{upload_timeout_minutes}分钟）")
        asyncio.create_task(auto_cancel_upload())
