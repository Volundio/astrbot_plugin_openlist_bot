import asyncio
import os
from typing import Dict, Optional

from astrbot.api import logger

from ..lib.onebot_files import (
    fetch_flash_file_url,
    is_http_url,
    resolve_group_file_source,
    resolve_private_file_source,
    to_int_or_none,
)
from .base import PluginService


class BackupUploader(PluginService):
    """Upload one group/private/flash file to OpenList with retry and local temp fallback."""

    async def _resolve_item_source(
        self,
        bot,
        group_id,
        item: Dict,
        allow_initial_url: bool = True,
    ) -> Dict[str, str]:
        """Resolve {'url': ..., 'local_path': ...} for group, private, or flash backup items."""
        local_path = item.get("local_path") or ""
        if isinstance(local_path, str) and local_path and os.path.isfile(local_path):
            return {"url": "", "local_path": local_path}

        initial_url = (item.get("url") or item.get("file_url") or "") if allow_initial_url else ""
        if is_http_url(initial_url):
            return {"url": initial_url.strip(), "local_path": ""}

        source_type = str(item.get("source_type") or "").lower()
        file_id = item.get("file_id")
        busid = to_int_or_none(item.get("busid")) or 0

        if source_type == "flash" or item.get("fileset_id"):
            fileset_id = str(item.get("fileset_id") or "")
            if fileset_id:
                url = await fetch_flash_file_url(bot, fileset_id, item)
                if url:
                    return {"url": url, "local_path": ""}
            return {"url": "", "local_path": ""}

        if source_type == "private" or (not group_id and item.get("user_id")):
            user_id = item.get("user_id")
            file_hash = item.get("file_hash")
            return await resolve_private_file_source(
                bot,
                user_id,
                str(file_id) if file_id else None,
                file_hash=file_hash,
            )

        return await resolve_group_file_source(
            bot,
            group_id,
            str(file_id) if file_id else None,
            busid=busid,
        )

    async def upload_group_file_with_retry(
        self,
        bot,
        client,
        group_id,
        item: Dict,
        target_dir: str,
        retry_attempts: int,
        retry_delay: int,
        initial_url: Optional[str] = None,
    ) -> tuple:
        file_id = item.get("file_id")
        fileset_id = item.get("fileset_id")
        source_type = str(item.get("source_type") or ("group" if group_id else "private")).lower()
        file_name = item.get("_backup_target_name") or item.get("file_name") or item.get("name")
        upload_size = to_int_or_none(item.get("file_size", item.get("size")))
        tag = "[闪传备份]" if source_type == "flash" else ("[私聊备份]" if source_type == "private" else "[群备份]")

        attempts = max(1, retry_attempts)
        reason = "URL 流式中转上传失败"
        last_url = initial_url or item.get("url") or item.get("file_url") or ""

        for attempt in range(1, attempts + 1):
            try:
                if attempt == 1 and is_http_url(last_url):
                    resolved = {"url": last_url.strip(), "local_path": ""}
                else:
                    resolved = await self._resolve_item_source(
                        bot,
                        group_id,
                        item,
                        allow_initial_url=(attempt == 1),
                    )
                local_path = resolved.get("local_path") or ""
                download_url = resolved.get("url") or ""
                if download_url:
                    last_url = download_url

                if local_path and os.path.isfile(local_path):
                    if await client.upload_file(local_path, target_dir, file_name):
                        return True, ""
                    reason = "本地文件上传失败"
                    logger.warning(f"{tag} 文件 {file_name} 第 {attempt}/{attempts} 次失败: {reason}")
                elif not download_url:
                    reason = "无法获取文件下载 URL"
                    logger.warning(f"{tag} 文件 {file_name} 第 {attempt}/{attempts} 次失败: {reason}")
                else:
                    logger.info(
                        f"🚀 {tag} 使用 URL 流式中转: {file_name}, "
                        f"size={upload_size}, target={target_dir}, attempt={attempt}/{attempts}"
                    )
                    if await client.upload_url_stream(download_url, target_dir, file_name, upload_size):
                        return True, ""
                    reason = "URL 流式中转上传失败"
                    logger.warning(f"{tag} 文件 {file_name} 第 {attempt}/{attempts} 次失败: {reason}")
            except Exception as e:
                reason = self._short_backup_fail_reason(e)
                logger.error(f"{tag} 文件 {file_name} 第 {attempt}/{attempts} 次异常: {reason}", exc_info=True)

            if self._is_permanent_group_file_error(reason):
                break
            if attempt < attempts:
                await asyncio.sleep(max(0, retry_delay))

        can_refresh = bool(file_id or fileset_id)
        if not can_refresh and not is_http_url(last_url):
            return False, reason

        if self._is_permanent_group_file_error(reason):
            logger.warning(
                f"⏭️ {tag} 源文件已不存在或过期，跳过本地临时文件备用上传: {file_name}, reason={reason}"
            )
            return False, reason

        logger.info(
            f"🧰 {tag} URL 流式中转 {attempts} 次失败，改用本地临时文件备用上传: "
            f"{file_name}, target={target_dir}"
        )

        async def refresh_url():
            if not can_refresh:
                return last_url
            return await self._resolve_item_source(bot, group_id, item, allow_initial_url=False)

        prefix_id = group_id or item.get("user_id") or "flash"
        return await self._upload_url_via_temp_file(
            client,
            last_url if not can_refresh else "",
            target_dir,
            file_name,
            upload_size,
            temp_dir_name="backup_temp",
            temp_prefix=f"backup_{prefix_id}",
            attempts=attempts,
            retry_delay=retry_delay,
            refresh_url=refresh_url,
            log_prefix=tag,
        )
