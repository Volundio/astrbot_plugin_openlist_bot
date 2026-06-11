import asyncio
import os
from typing import Optional

from astrbot.api import logger
from astrbot.api.star import StarTools


class PluginService:
    """Base service that delegates shared helpers/state back to the plugin."""

    GROUP_FILE_LIST_COUNT = 10000

    def __init__(self, plugin):
        self.plugin = plugin

    def __getattr__(self, name):
        return getattr(self.plugin, name)

    def _make_temp_file_path(self, temp_dir_name: str, prefix: str, file_name: str) -> str:
        temp_dir = os.path.join(StarTools.get_data_dir("openlist"), temp_dir_name)
        os.makedirs(temp_dir, exist_ok=True)
        safe_filename = self._sanitize_filename(file_name, prefix)
        return os.path.join(temp_dir, f"{prefix}_{self._unique_suffix()}_{safe_filename}")

    def _remove_file_quietly(self, file_path: str, log_prefix: str = "临时文件"):
        if not file_path or not os.path.exists(file_path):
            return
        try:
            os.remove(file_path)
        except OSError as e:
            logger.warning(f"清理{log_prefix}失败: {file_path}, err={e}")

    async def _upload_url_via_temp_file(
        self,
        client,
        source_url: str,
        target_path: str,
        file_name: str,
        file_size: Optional[int],
        temp_dir_name: str,
        temp_prefix: str,
        attempts: int = 1,
        retry_delay: int = 0,
        refresh_url=None,
        log_prefix: str = "",
    ) -> tuple:
        """Download a URL to a temp file, upload it, and always clean temp files."""
        attempts = max(1, attempts)
        current_url = source_url
        reason = "备用上传本地下载失败"
        prefix = f"{log_prefix} " if log_prefix else ""

        for attempt in range(1, attempts + 1):
            temp_file_path = self._make_temp_file_path(temp_dir_name, temp_prefix, file_name)
            try:
                if callable(refresh_url):
                    try:
                        refreshed_url = await refresh_url()
                        if refreshed_url:
                            current_url = refreshed_url
                    except Exception as e:
                        reason = str(e)
                        logger.warning(f"{prefix}备用上传刷新 URL 失败: {file_name}, attempt={attempt}/{attempts}, err={e}")

                if not current_url:
                    reason = "备用上传无法获取下载 URL"
                    logger.warning(f"{prefix}备用上传文件 {file_name} 第 {attempt}/{attempts} 次失败: {reason}")
                else:
                    logger.info(f"{prefix}本地临时文件备用上传: {file_name}, attempt={attempt}/{attempts}")
                    if not await client.download_url_to_file(current_url, temp_file_path, file_name, file_size):
                        reason = "备用上传本地下载失败"
                        logger.warning(f"{prefix}备用上传文件 {file_name} 第 {attempt}/{attempts} 次失败: {reason}")
                    else:
                        actual_size = os.path.getsize(temp_file_path)
                        if file_size is not None and file_size > 0 and actual_size != file_size:
                            reason = "备用上传本地文件大小不一致"
                            logger.error(
                                f"{prefix}{reason}: {file_name}, actual={actual_size}, expected={file_size}"
                            )
                        elif await client.upload_file(temp_file_path, target_path, file_name):
                            logger.info(f"✅ {prefix}本地临时文件备用上传成功: {file_name} -> {target_path}")
                            return True, ""
                        else:
                            reason = "备用上传 OpenList 上传失败"
                            logger.warning(f"{prefix}备用上传文件 {file_name} 第 {attempt}/{attempts} 次失败: {reason}")
            except Exception as e:
                reason = str(e)
                logger.error(f"{prefix}备用上传文件 {file_name} 第 {attempt}/{attempts} 次异常: {e}", exc_info=True)
            finally:
                for cleanup_path in (temp_file_path, f"{temp_file_path}.part"):
                    self._remove_file_quietly(cleanup_path, f"{prefix}备用上传临时文件")

            if attempt < attempts:
                await asyncio.sleep(max(0, retry_delay))

        return False, reason
