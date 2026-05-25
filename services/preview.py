import os

import aiohttp
import chardet

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import StarTools

from .base import PluginService


class PreviewService(PluginService):
    """Preview service."""

    async def preview_command(self, event: AstrMessageEvent, path: str):
        """预览文件内容"""
        path = (path or "").strip()
        if not path:
            yield event.plain_result("❌ 请提供文件路径或序号")
            return
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)

        # 检查配置
        max_preview_size_mb = user_config.get("max_preview_size", 0)
        if max_preview_size_mb == -1:
            yield event.plain_result("❌ 预览功能已禁用。")
            return

        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return

        # 获取文件信息
        item = None
        path_or_num = path
        path_candidates = []
        if path_or_num.isdigit():
            number = int(path_or_num)
            item = self._get_item_by_number(user_id, number)
            if item:
                if item.get("is_dir"):
                    yield event.plain_result("❌ 无法预览目录，请指定一个文件。")
                    return
                full_path = self._get_item_full_path(user_id, item, user_config)
            else:
                yield event.plain_result(f"❌ 序号 {number} 无效")
                return
        else:
            path_candidates = self._resolve_path_candidates(user_id, path_or_num)
            full_path = path_candidates[0]

        try:
            async with self._create_openlist_client(user_config) as client:
                if not item:
                    for candidate_path in path_candidates:
                        item = await client.get_file_info(candidate_path)
                        if item:
                            full_path = candidate_path
                            break
                    if not item:
                        display_path = " / ".join(path_candidates)
                        yield event.plain_result(f"❌ 未找到文件: {display_path}")
                        return
                    if item.get("is_dir"):
                        yield event.plain_result("❌ 无法预览目录，请指定一个文件。")
                        return

                file_name = item.get("name", "")
                file_size = item.get("size", 0)
                ext = os.path.splitext(file_name)[1].lower()
                if not self._is_extension_allowed(file_name, user_config):
                    yield event.plain_result(
                        f"❌ 文件类型不允许预览: {file_name}\n"
                        f"💡 当前允许: {self._format_extension_filter(user_config)}"
                    )
                    return

                # 压缩包预览支持 (使用 API)
                archive_extensions = [".zip", ".tar", ".gz", ".7z", ".rar", ".bz2", ".xz"]
                if ext in archive_extensions:
                    yield event.plain_result(f"🔍 正在读取压缩包内容: {file_name}...")
                    archive_data = await client.list_archive_contents(full_path)
                    if archive_data and "content" in archive_data:
                        contents = archive_data["content"]
                        if not contents:
                            yield event.plain_result(f"📦 压缩包 {file_name} 为空。")
                            return

                        file_list = []
                        for f in contents:
                            prefix = "📁" if f.get("is_dir") else "📄"
                            size_str = f" ({f['size'] / 1024:.1f} KB)" if not f.get("is_dir") else ""
                            file_list.append(f"{prefix} {f['name']}{size_str}")

                        max_display = 20
                        display_list = file_list[:max_display]
                        result_text = f"📦 压缩包预览: {file_name}\n---\n" + "\n".join(display_list)
                        if len(file_list) > max_display:
                            result_text += f"\n\n...(及其他 {len(file_list) - max_display} 个文件)"

                        yield event.plain_result(result_text)
                        return
                    else:
                        yield event.plain_result(f"❌ 无法读取压缩包内容或该格式暂不支持。")
                        return

                # 检查文件大小限制
                if max_preview_size_mb > 0:
                    if file_size > max_preview_size_mb * 1024 * 1024:
                        yield event.plain_result(f"❌ 文件过大 ({file_size / (1024*1024):.2f} MB)，超过了最大预览限制 ({max_preview_size_mb} MB)。")
                        return

                yield event.plain_result(f"🔍 正在获取预览: {file_name}...")

                # 获取真实下载链接
                link = await client.get_direct_download_link(full_path)
                if not link:
                    yield event.plain_result("❌ 获取真实下载链接失败，请确认配置账号为 OpenList 管理员或具有 /api/fs/link 权限")
                    return
                download_url = link["url"]
                download_headers = self._normalize_download_headers(link.get("header", {}))

                # 下载到临时目录
                temp_dir = os.path.join(StarTools.get_data_dir("openlist"), "temp_preview")
                os.makedirs(temp_dir, exist_ok=True)
                safe_filename = self._sanitize_filename(file_name)
                temp_file_path = os.path.join(temp_dir, f"preview_{self._unique_suffix()}_{safe_filename}")

                try:
                    timeout = aiohttp.ClientTimeout(
                        total=None,
                        sock_connect=self._get_positive_int_config(user_config, "upstream_connect_timeout", 60),
                        sock_read=self._get_positive_int_config(user_config, "upstream_read_timeout", 180),
                    )
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.get(download_url, headers=download_headers) as resp:
                            if resp.status == 200:
                                with open(temp_file_path, "wb") as f:
                                    downloaded = 0
                                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                                        f.write(chunk)
                                        downloaded += len(chunk)
                                        if max_preview_size_mb > 0 and downloaded > max_preview_size_mb * 1024 * 1024:
                                            yield event.plain_result(
                                                f"❌ 文件过大 ({downloaded / (1024*1024):.2f} MB)，"
                                                f"超过了最大预览限制 ({max_preview_size_mb} MB)。"
                                            )
                                            return
                            else:
                                yield event.plain_result(f"❌ 下载文件失败: HTTP {resp.status}")
                                return

                    # 仅支持文本预览
                    text_extensions = [".txt", ".md", ".log", ".json", ".xml", ".yaml", ".yml", ".ini", ".conf", ".cfg", ".toml", ".py", ".js", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".php", ".rb", ".sh", ".bash", ".html", ".htm", ".css", ".jsx", ".tsx", ".ts", ".vue", ".sql", ".csv", ".properties", ".env"]

                    if ext in text_extensions:
                        text_length = user_config.get("text_preview_length", 1000)
                        try:
                            with open(temp_file_path, "rb") as f:
                                content_bytes = f.read(text_length * 4) # 多读一点以防编码问题

                                # 使用 chardet 检测编码
                                detection = chardet.detect(content_bytes)
                                encoding = detection.get('encoding', 'utf-8') or 'utf-8'
                                confidence = detection.get('confidence', 0)
                                logger.debug(f"文本预览编码检测: {encoding}, 置信度: {confidence:.2f}")

                                try:
                                    decoded_text = content_bytes.decode(encoding, errors='ignore').strip()
                                except (LookupError, UnicodeError):
                                    # 如果检测出的编码失败，回退到 utf-8
                                    encoding = 'utf-8'
                                    decoded_text = content_bytes.decode('utf-8', errors='ignore').strip()

                                preview_text = decoded_text[:text_length]
                                if len(decoded_text) > text_length:
                                    preview_text += "\n\n..."

                                yield event.plain_result(f"📝 文本预览:\n---\n{preview_text}")
                        except Exception as e:
                            logger.error(f"文本预览失败: {e}")
                            yield event.plain_result(f"❌ 文本解析失败: {e}")
                    else:
                        yield event.plain_result(f"❓ 该格式 ({ext}) 不在支持的文本预览列表中。")

                finally:
                    # 清理临时文件
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)

        except Exception as e:
            logger.error(f"预览失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 预览失败: {str(e)}")
