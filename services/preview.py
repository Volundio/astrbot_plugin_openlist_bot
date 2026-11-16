import os

import aiohttp

try:
    import chardet
except ImportError:  # pragma: no cover - optional fallback when chardet is absent
    chardet = None

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .base import PluginService


class PreviewService(PluginService):
    """Preview service."""

    ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".7z", ".rar", ".bz2", ".xz"}
    TEXT_EXTENSIONS = {
        ".txt", ".md", ".log", ".json", ".xml", ".yaml", ".yml", ".ini", ".conf",
        ".cfg", ".toml", ".py", ".js", ".java", ".c", ".cpp", ".h", ".go", ".rs",
        ".php", ".rb", ".sh", ".bash", ".html", ".htm", ".css", ".jsx", ".tsx",
        ".ts", ".vue", ".sql", ".csv", ".properties", ".env",
    }

    def _decode_preview_bytes(self, content_bytes: bytes) -> str:
        if not content_bytes:
            return ""
        if chardet is not None:
            try:
                detection = chardet.detect(content_bytes)
                encoding = detection.get("encoding") or "utf-8"
                confidence = detection.get("confidence", 0)
                logger.debug(f"文本预览编码检测: {encoding}, 置信度: {confidence:.2f}")
                return content_bytes.decode(encoding, errors="ignore").strip()
            except (LookupError, UnicodeError, Exception):
                pass
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return content_bytes.decode(enc).strip()
            except UnicodeDecodeError:
                continue
        return content_bytes.decode("utf-8", errors="ignore").strip()

    async def preview_command(self, event: AstrMessageEvent, path: str = ""):
        """预览文件内容"""
        path = (path or "").strip()
        if not path:
            yield event.plain_result(self._format_usage_tip(
                "缺少文件路径或序号",
                "ol preview <路径|序号>",
                ["ol preview 1", "ol preview /data/config.txt"],
                "支持文本文件预览和压缩包目录查看；序号来自当前会话最近一次列表。",
            ))
            return
        user_id = event.get_sender_id()
        nav_key = self._get_navigation_state_key(event)
        user_config = self.get_user_config(user_id)

        max_preview_size_mb = int(user_config.get("max_preview_size", 10) or 0)
        if max_preview_size_mb == -1:
            yield event.plain_result(
                "❌ 预览功能已禁用。\n"
                "提示：如需重新启用，可发送 ol config set max_preview_size 0，或设置具体 MB 限制。"
            )
            return

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

        item = None
        path_or_num = path
        path_candidates = []
        if path_or_num.isdigit():
            number = int(path_or_num)
            item = self._get_item_by_number(nav_key, number)
            if item:
                if item.get("is_dir"):
                    yield event.plain_result(self._format_usage_tip(
                        f"序号 {number} 是目录，无法预览",
                        "ol preview <文件路径|文件序号>",
                        ["ol preview 1", "ol preview /data/config.txt"],
                        "如需查看目录内容，请使用 ol ls 序号。",
                    ))
                    return
                full_path = self._get_item_full_path(nav_key, item, user_config)
            else:
                yield event.plain_result(self._format_usage_tip(
                    f"序号 {number} 无效",
                    "ol preview <路径|序号>",
                    ["ol ls", "ol preview 1", "ol preview /data/config.txt"],
                    "序号来自当前会话最近一次 ol ls 或 ol search 的列表。",
                ))
                return
        else:
            path_candidates = self._resolve_path_candidates(nav_key, path_or_num)
            full_path = path_candidates[0]

        try:
            async with self._create_openlist_client(user_config) as client:
                if not item:
                    multi = len(path_candidates) > 1
                    for idx, candidate_path in enumerate(path_candidates):
                        item = await client.get_file_info(
                            candidate_path,
                            log_error=not multi or idx == len(path_candidates) - 1,
                        )
                        if item:
                            full_path = candidate_path
                            break
                    if not item:
                        display_path = " / ".join(path_candidates)
                        yield event.plain_result(self._format_usage_tip(
                            f"未找到文件：{display_path}",
                            "ol preview <文件路径|文件序号>",
                            ["ol preview 1", "ol preview /data/config.txt"],
                            "不以 / 开头时，会优先按当前目录的相对路径解析。",
                        ))
                        return
                    if item.get("is_dir"):
                        yield event.plain_result(self._format_usage_tip(
                            "无法预览目录",
                            "ol preview <文件路径|文件序号>",
                            ["ol preview 1", "ol preview /data/config.txt"],
                            "目录请使用 ol ls 查看；预览仅支持文件。",
                        ))
                        return

                file_name = item.get("name", "")
                file_size = item.get("size", 0) or 0
                ext = os.path.splitext(file_name)[1].lower()
                if not self._is_extension_allowed(file_name, user_config):
                    yield event.plain_result(
                        f"❌ 文件类型不允许预览: {file_name}\n"
                        f"💡 当前允许: {self._format_extension_filter(user_config)}"
                    )
                    return

                # 1. 压缩包预览支持 (使用 API)
                if ext in self.ARCHIVE_EXTENSIONS:
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
                            size_str = f" ({(f.get('size') or 0) / 1024:.1f} KB)" if not f.get("is_dir") else ""
                            file_list.append(f"{prefix} {f.get('name', '')}{size_str}")

                        max_display = 20
                        display_list = file_list[:max_display]
                        result_text = f"📦 压缩包预览: {file_name}\n---\n" + "\n".join(display_list)
                        if len(file_list) > max_display:
                            result_text += f"\n\n...(及其他 {len(file_list) - max_display} 个文件)"

                        yield event.plain_result(result_text)
                        return
                    yield event.plain_result("❌ 无法读取压缩包内容或该格式暂不支持。")
                    return

                # 2. 前置判断文本后缀，避免先完整下载不支持的大文件再报错
                if ext not in self.TEXT_EXTENSIONS:
                    yield event.plain_result(
                        f"❓ 该格式 ({ext or '无扩展名'}) 不在支持的文本或压缩包预览列表中。\n"
                        "示例：ol preview /data/config.txt\n"
                        "提示：如需下载该文件，请使用 ol download <路径|序号>。"
                    )
                    return

                if max_preview_size_mb > 0 and file_size > max_preview_size_mb * 1024 * 1024:
                    yield event.plain_result(
                        f"❌ 文件过大：{file_size / (1024 * 1024):.2f}MB > {max_preview_size_mb}MB\n"
                        "请使用 ol ls 获取下载链接。"
                    )
                    return

                yield event.plain_result(f"🔍 正在获取预览: {file_name}...")

                link = await client.get_direct_download_link(full_path)
                if not link:
                    yield event.plain_result("❌ 获取真实下载链接失败，请确认配置账号为 OpenList 管理员或具有 /api/fs/link 权限")
                    return
                download_url = link["url"]
                download_headers = self._normalize_download_headers(link.get("header", {}))

                text_length = self._get_positive_int_config(user_config, "text_preview_length", 1000)
                max_read_bytes = max(4096, text_length * 4)
                if "Range" not in download_headers and "range" not in download_headers:
                    download_headers = dict(download_headers)
                    download_headers["Range"] = f"bytes=0-{max_read_bytes - 1}"

                timeout = aiohttp.ClientTimeout(
                    total=60,
                    sock_connect=self._get_positive_int_config(user_config, "upstream_connect_timeout", 60),
                    sock_read=self._get_positive_int_config(user_config, "upstream_read_timeout", 180),
                )
                buffer = bytearray()
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(download_url, headers=download_headers) as resp:
                        if resp.status not in (200, 206):
                            yield event.plain_result(f"❌ 下载文件失败: HTTP {resp.status}")
                            return
                        async for chunk in resp.content.iter_chunked(16384):
                            if not chunk:
                                continue
                            remaining = max_read_bytes - len(buffer)
                            buffer.extend(chunk[:remaining])
                            if len(buffer) >= max_read_bytes:
                                break

                decoded_text = self._decode_preview_bytes(bytes(buffer))
                preview_text = decoded_text[:text_length]
                if len(decoded_text) > text_length or file_size > len(buffer):
                    preview_text += "\n\n..."
                yield event.plain_result(f"📝 文本预览:\n---\n{preview_text}")

        except Exception as e:
            logger.error(f"预览失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 预览失败: {str(e)}")
