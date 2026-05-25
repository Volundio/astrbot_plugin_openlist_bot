import asyncio
import os
from typing import Dict

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File
from astrbot.api.star import StarTools

from .base import PluginService


class DownloadService(PluginService):
    """Download service."""

    async def _download_file(self, event: AstrMessageEvent, file_item: Dict, user_config: Dict, full_path_override: str = None):
        """下载文件并作为附件发送给用户"""
        user_id = event.get_sender_id()
        file_name = file_item.get("name", "")
        file_size = file_item.get("size", 0)
        file_path = full_path_override or ""
        temp_file_path = None
        sent_file = False
        if not self._is_extension_allowed(file_name, user_config):
            yield event.plain_result(
                f"❌ 文件类型不允许下载: {file_name}\n"
                f"💡 当前允许: {self._format_extension_filter(user_config)}"
            )
            return
        max_download_size_mb = self._get_size_limit_mb(user_config, "max_download_size", 50)
        max_download_size = max_download_size_mb * 1024 * 1024
        if max_download_size_mb > 0 and file_size > max_download_size:
            size_mb = file_size / (1024 * 1024)
            yield event.plain_result(f"❌ 文件过大: {size_mb:.1f}MB > {max_download_size_mb}MB\n💡 请使用 /ol ls 获取下载链接")
            return
        try:
            if full_path_override:
                file_path = full_path_override
            else:
                file_path = self._get_item_full_path(user_id, file_item, user_config)

            async with self._create_openlist_client(user_config) as client:
                link = await client.get_direct_download_link(file_path)
                if not link:
                    yield event.plain_result("❌ 无法获取真实下载链接，请确认配置账号为 OpenList 管理员或具有 /api/fs/link 权限")
                    return
                download_url = link["url"]
                download_headers = self._normalize_download_headers(link.get("header", {}))
                link_size = link.get("content_length")
                try:
                    link_size = int(link_size) if link_size is not None else 0
                except (TypeError, ValueError):
                    link_size = 0
                if not file_size and link_size > 0:
                    file_size = link_size
                if max_download_size_mb > 0 and link_size > max_download_size:
                    size_mb = link_size / (1024 * 1024)
                    yield event.plain_result(f"❌ 文件过大: {size_mb:.1f}MB > {max_download_size_mb}MB\n💡 请使用 /ol ls 获取下载链接")
                    return
                downloads_dir = os.path.join(StarTools.get_data_dir("openlist"), "downloads")
                os.makedirs(downloads_dir, exist_ok=True)
                safe_filename = self._sanitize_filename(file_name)
                temp_file_path = os.path.join(downloads_dir, f"{user_id}_{self._unique_suffix()}_{safe_filename}")
                yield event.plain_result(f"📥 开始下载: {file_name}\n💾 大小: {self._format_file_size(file_size)}")
                timeout = aiohttp.ClientTimeout(
                    total=None,
                    sock_connect=self._get_positive_int_config(user_config, "upstream_connect_timeout", 60),
                    sock_read=self._get_positive_int_config(user_config, "upstream_read_timeout", 180),
                )
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(download_url, headers=download_headers) as response:
                        if response.status == 200:
                            with open(temp_file_path, "wb") as f:
                                downloaded = 0
                                async for chunk in response.content.iter_chunked(8192):
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    if max_download_size_mb > 0 and downloaded > max_download_size:
                                        size_mb = downloaded / (1024 * 1024)
                                        yield event.plain_result(
                                            f"❌ 文件过大: {size_mb:.1f}MB > {max_download_size_mb}MB\n"
                                            f"💡 请使用 /ol ls 获取下载链接"
                                        )
                                        return
                                    if (
                                        self._get_bool_config(user_config, "debug_transfer_logging", False)
                                        and file_size > 10 * 1024 * 1024
                                        and downloaded % (10 * 1024 * 1024) < 8192
                                    ):
                                        progress = (downloaded / file_size) * 100
                                        logger.info(
                                            f"下载进度: {file_name} {progress:.1f}% "
                                            f"({self._format_file_size(downloaded)}/{self._format_file_size(file_size)})"
                                        )
                            yield event.plain_result(f"✅ 下载完成，正在发送文件...")
                            file_component = File(name=file_name, file=temp_file_path)
                            yield event.chain_result([file_component])
                            sent_file = True
                            asyncio.create_task(self._cleanup_temp_file(temp_file_path))
                        else:
                            error_text = await response.text()
                            logger.error(f"用户 {user_id} 下载文件失败 - HTTP状态: {response.status}, 响应: {error_text}, 文件: {file_name}, URL: {download_url}")
                            yield event.plain_result(f"❌ 下载失败: HTTP {response.status}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
        except Exception as e:
            logger.error(f"用户 {user_id} 下载文件失败: {e}, 文件: {file_name}, 路径: {file_path}", exc_info=True)
            yield event.plain_result(f"❌ 下载失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
        finally:
            if temp_file_path and not sent_file and os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except OSError as e:
                    logger.debug(f"清理下载临时文件失败: {temp_file_path}, err={e}")

    async def _get_and_send_download_link(self, event: AstrMessageEvent, item: Dict, user_config: Dict, full_path: str = None):
        """获取指定项目的文件链接并发送"""
        user_id = event.get_sender_id()
        yield event.plain_result(f"🔗 正在获取文件链接: {item.get('name', '')}...")

        # 如果提供了 full_path，则直接使用；否则，根据 item 信息构建路径
        if full_path:
            file_path = full_path
        else:
            file_path = self._get_item_full_path(user_id, item, user_config)

        file_name = item.get("name", "")
        if not self._is_extension_allowed(file_name, user_config):
            yield event.plain_result(
                f"❌ 文件类型不允许获取链接: {file_name}\n"
                f"💡 当前允许: {self._format_extension_filter(user_config)}"
            )
            return

        try:
            async with self._create_openlist_client(user_config) as client:
                download_url = await client.get_download_url(file_path)
                if download_url:
                    name = item.get("name", "")
                    size = item.get("size", 0)
                    async for result in self._send_download_link_txt(event, name, size, file_path, download_url):
                        yield result
                else:
                    logger.warning(f"用户 {user_id} 无法获取下载链接 - 路径: {file_path}, 文件名: {item.get('name', '')}")
                    yield event.plain_result(f"❌ 无法获取下载链接，文件可能不存在或为目录: {file_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 获取下载链接失败: {e}, 路径: {file_path}, 文件名: {item.get('name', '')}", exc_info=True)
            yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
