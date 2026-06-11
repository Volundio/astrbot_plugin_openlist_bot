import asyncio
from typing import Dict, Optional

from astrbot.api import logger

from .base import PluginService


class BackupUploader(PluginService):
    """Upload one group file to OpenList with retry and local temp fallback."""

    async def upload_group_file_with_retry(
        self,
        bot,
        client,
        group_id: int,
        item: Dict,
        target_dir: str,
        retry_attempts: int,
        retry_delay: int,
        initial_url: Optional[str] = None,
    ) -> tuple:
        file_id = item.get("file_id")
        file_name = item.get("_backup_target_name") or item.get("file_name")
        busid = item.get("busid", 0)
        upload_size = item.get("file_size")
        try:
            upload_size = int(upload_size) if upload_size is not None else None
        except (TypeError, ValueError):
            upload_size = None

        attempts = max(1, retry_attempts)
        reason = "URL 流式中转上传失败"
        for attempt in range(1, attempts + 1):
            try:
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
                reason = self._short_backup_fail_reason(e)
                logger.error(f"备份文件 {file_name} 第 {attempt}/{attempts} 次异常: {reason}", exc_info=True)

            if attempt < attempts:
                await asyncio.sleep(max(0, retry_delay))

        if not file_id:
            return False, reason

        if self._is_permanent_group_file_error(reason):
            logger.warning(
                f"⏭️ [群备份] 群文件已不存在，跳过本地临时文件备用上传: {file_name}, reason={reason}"
            )
            return False, reason

        logger.info(
            f"🧰 [群备份] URL 流式中转 {attempts} 次失败，改用本地临时文件备用上传: "
            f"{file_name}, target={target_dir}"
        )

        async def refresh_url():
            url_res = await bot.api.call_action(
                "get_group_file_url",
                group_id=group_id,
                file_id=file_id,
                busid=busid,
            )
            return url_res.get("url") if isinstance(url_res, dict) else None

        return await self._upload_url_via_temp_file(
            client,
            "",
            target_dir,
            file_name,
            upload_size,
            temp_dir_name="backup_temp",
            temp_prefix=f"backup_{group_id}",
            attempts=attempts,
            retry_delay=retry_delay,
            refresh_url=refresh_url,
            log_prefix="[群备份]",
        )
