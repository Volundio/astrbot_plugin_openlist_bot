import asyncio
import base64
import os

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import File

from ..lib.onebot_files import call_onebot_action, get_bot_caller, maybe_int_id
from .base import PluginService


class RestoreService(PluginService):
    """Restore service."""

    MAX_BASE64_FALLBACK_BYTES = 50 * 1024 * 1024

    async def _get_group_root_folders(self, event: AstrMessageEvent, group_id) -> dict:
        """Return root group-file folders keyed by folder name."""
        folders = {}
        if not get_bot_caller(event):
            return folders
        try:
            root_files = await call_onebot_action(
                event,
                "get_group_root_files",
                group_id=maybe_int_id(group_id),
                file_count=self.GROUP_FILE_LIST_COUNT,
            )
        except Exception as e:
            logger.warning(f"获取群根目录文件夹列表失败: {e}")
            return folders

        if isinstance(root_files, dict):
            for folder in root_files.get("folders") or []:
                folder_name = folder.get("folder_name") or folder.get("name")
                folder_id = folder.get("folder_id") or folder.get("id")
                if folder_name and folder_id:
                    folders[folder_name] = folder_id
        return folders

    async def _upload_file_to_group(
        self,
        event: AstrMessageEvent,
        target_group_id,
        temp_file_path: str,
        file_name: str,
        folder_id: str = None,
    ) -> bool:
        """上传文件至群文件，兼容本地同机路径与跨 Docker 容器 base64/消息组件降级。"""
        abs_path = os.path.abspath(temp_file_path)
        gid = maybe_int_id(target_group_id)

        if get_bot_caller(event):
            try:
                await call_onebot_action(
                    event,
                    "upload_group_file",
                    group_id=gid,
                    file=abs_path,
                    name=file_name,
                    folder=folder_id,
                    folder_id=folder_id,
                )
                return True
            except Exception as first_err:
                logger.warning(f"路径方式上传群文件失败，尝试跨容器兼容方式 ({file_name}): {first_err}")
                try:
                    file_size = os.path.getsize(temp_file_path)
                    if file_size <= self.MAX_BASE64_FALLBACK_BYTES:
                        with open(temp_file_path, "rb") as f:
                            b64_str = base64.b64encode(f.read()).decode("ascii")
                        await call_onebot_action(
                            event,
                            "upload_group_file",
                            group_id=gid,
                            file=f"base64://{b64_str}",
                            name=file_name,
                            folder=folder_id,
                            folder_id=folder_id,
                        )
                        return True
                except Exception as b64_err:
                    logger.warning(f"base64 方式上传群文件失败 ({file_name}): {b64_err}")

        # 若目标就是当前会话所在群，可降级用 AstrBot File 消息组件发送
        current_gid = self._get_event_group_id(event)
        if current_gid not in (None, "") and str(current_gid) == str(target_group_id):
            try:
                await event.send(MessageChain([File(name=file_name, file=temp_file_path)]))
                return True
            except Exception as send_err:
                logger.error(f"通过消息组件发送群文件失败 ({file_name}): {send_err}")

        return False

    async def restore_command(self, event: AstrMessageEvent, path: str = "", target: str = ""):
        """将 Openlist 路径中的文件恢复到群组或私聊"""
        path = (path or "").strip()
        if not path:
            yield event.plain_result(self._format_restore_usage_tip("缺少 OpenList 来源路径"))
            return
        path = self._normalize_openlist_path(path)
        user_id = event.get_sender_id()
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

        target_group_id = None
        if target:
            if target.startswith("@"):
                gid_text = target[1:].strip()
                if not gid_text:
                    yield event.plain_result(self._format_restore_usage_tip(f"群号格式错误：{target}"))
                    return
                target_group_id = maybe_int_id(gid_text)
            else:
                yield event.plain_result(self._format_restore_usage_tip(f"无法识别目标参数：{target}"))
                return

        if target_group_id is None:
            event_group_id = self._get_event_group_id(event)
            if event_group_id not in (None, ""):
                target_group_id = maybe_int_id(event_group_id)

        is_group = target_group_id is not None
        if is_group and await self._deny_if_no_target_group_permission(event, target_group_id, "恢复文件"):
            yield event.plain_result(self._format_usage_tip(
                "权限不足：无法恢复到目标群",
                "ol restore <OpenList来源路径> [@目标群号]",
                [
                    "ol restore /backup/group_123456",
                    "ol restore /docs @987654",
                ],
                "只能恢复到当前群，或由目标群群主/管理员指定 @群号。",
            ))
            return

        target_desc = f"群 {target_group_id}" if is_group else "私聊会话"

        yield event.plain_result(f"🚀 正在启动恢复任务...\n📂 来源路径: {path}\n🎯 目标: {target_desc}")

        try:
            async with self._create_openlist_client(user_config) as client:
                files_to_restore = []
                base_path = self._normalize_openlist_path(path)

                async def collect(current_path):
                    res = await client.list_files(current_path, per_page=0)
                    if not res:
                        return
                    for item in res.get("content", []):
                        full_item_path = self._normalize_openlist_path(f"{current_path.rstrip('/')}/{item['name']}")
                        if item.get("is_dir"):
                            await collect(full_item_path)
                        else:
                            item["full_path"] = full_item_path
                            rel = full_item_path.lstrip("/") if base_path == "/" else full_item_path[len(base_path):].lstrip("/")
                            item["relative_path"] = rel
                            files_to_restore.append(item)

                file_info = await client.get_file_info(path)
                if not file_info:
                    yield event.plain_result(f"❌ 路径不存在: {path}")
                    return

                if file_info.get("is_dir"):
                    await collect(base_path)
                else:
                    file_info["full_path"] = path
                    file_info["relative_path"] = file_info["name"]
                    files_to_restore.append(file_info)

                if not files_to_restore:
                    yield event.plain_result("📂 路径下没有可恢复的文件。")
                    return

                total = len(files_to_restore)
                yield event.plain_result(f"📦 找到 {total} 个文件，开始下载并发送...")

                created_folders = {}
                if is_group:
                    created_folders.update(await self._get_group_root_folders(event, target_group_id))

                success_count = 0
                fail_count = 0
                max_download_size_mb = self._get_size_limit_mb(user_config, "max_download_size", 50)
                max_download_size = max_download_size_mb * 1024 * 1024

                for i, item in enumerate(files_to_restore, 1):
                    file_name = item["name"]
                    full_path = item["full_path"]
                    rel_path = item["relative_path"]
                    temp_file_path = None
                    keep_temp_async = False

                    try:
                        if not self._is_extension_allowed(file_name, user_config):
                            logger.info(f"跳过恢复文件 {file_name}: 后缀不在允许范围内。")
                            fail_count += 1
                            continue

                        item_size = item.get("size", 0)
                        if max_download_size_mb > 0 and item_size and item_size > max_download_size:
                            logger.info(f"跳过恢复文件 {file_name}: 大小 {item_size} 超过限制 {max_download_size_mb}MB。")
                            fail_count += 1
                            continue

                        link = await client.get_direct_download_link(full_path)
                        if not link:
                            logger.warning(f"无法获取真实下载链接: {full_path}")
                            fail_count += 1
                            continue
                        download_url = link["url"]
                        download_headers = self._normalize_download_headers(link.get("header", {}))
                        link_size = link.get("content_length")
                        try:
                            link_size = int(link_size) if link_size is not None else 0
                        except (TypeError, ValueError):
                            link_size = 0
                        if max_download_size_mb > 0 and link_size > max_download_size:
                            logger.info(f"跳过恢复文件 {file_name}: 下载链接大小 {link_size} 超过限制 {max_download_size_mb}MB。")
                            fail_count += 1
                            continue

                        temp_file_path = self._make_temp_file_path("downloads", "restore", file_name)

                        timeout = aiohttp.ClientTimeout(
                            total=None,
                            sock_connect=self._get_positive_int_config(user_config, "upstream_connect_timeout", 60),
                            sock_read=self._get_positive_int_config(user_config, "upstream_read_timeout", 180),
                        )
                        download_too_large = False
                        downloaded = 0
                        async with aiohttp.ClientSession(timeout=timeout) as session:
                            async with session.get(download_url, headers=download_headers) as response:
                                if response.status == 200:
                                    with open(temp_file_path, "wb") as f:
                                        async for chunk in response.content.iter_chunked(65536):
                                            f.write(chunk)
                                            downloaded += len(chunk)
                                            if max_download_size_mb > 0 and downloaded > max_download_size:
                                                download_too_large = True
                                                break
                                else:
                                    logger.error(f"下载失败 {file_name}: HTTP {response.status}")
                                    fail_count += 1
                                    continue
                        if download_too_large:
                            logger.info(
                                f"跳过恢复文件 {file_name}: 实际下载大小 {downloaded} "
                                f"超过限制 {max_download_size_mb}MB。"
                            )
                            fail_count += 1
                            continue

                        if is_group:
                            folder_id = None
                            if "/" in rel_path and get_bot_caller(event):
                                folder_name = rel_path.split("/")[0]
                                if folder_name not in created_folders:
                                    try:
                                        # 同时传 name、folder_name 和 parent_id="/" 兼容 go-cqhttp / NapCat / Lagrange / LLOneBot
                                        await call_onebot_action(
                                            event,
                                            "create_group_file_folder",
                                            group_id=maybe_int_id(target_group_id),
                                            name=folder_name,
                                            folder_name=folder_name,
                                            parent_id="/",
                                        )
                                        created_folders.update(await self._get_group_root_folders(event, target_group_id))
                                    except Exception as e:
                                        created_folders.update(await self._get_group_root_folders(event, target_group_id))
                                        if folder_name not in created_folders:
                                            logger.error(f"无法获取群文件夹 {folder_name} 的 ID; create_error={e}")

                                folder_id = created_folders.get(folder_name)

                            if await self._upload_file_to_group(
                                event, target_group_id, temp_file_path, file_name, folder_id=folder_id
                            ):
                                success_count += 1
                                keep_temp_async = True
                                self._spawn_background_task(self._cleanup_temp_file(temp_file_path))
                            else:
                                fail_count += 1
                        else:
                            try:
                                file_comp = File(name=file_name, file=temp_file_path)
                                await event.send(MessageChain([file_comp]))
                                success_count += 1
                                keep_temp_async = True
                                self._spawn_background_task(self._cleanup_temp_file(temp_file_path))
                                await asyncio.sleep(1)
                            except Exception as e:
                                logger.error(f"私聊发送文件 {file_name} 失败: {e}")
                                fail_count += 1

                        if i % 5 == 0 or i == total:
                            logger.info(f"🔄 恢复进度: {i}/{total} (成功: {success_count}, 失败: {fail_count})")

                    except Exception as e:
                        logger.error(f"处理文件 {file_name} 时发生错误: {e}")
                        fail_count += 1
                    finally:
                        if temp_file_path and not keep_temp_async:
                            self._remove_file_quietly(temp_file_path, "恢复临时文件")

                yield event.plain_result(f"✅ 恢复任务完成!\n📊 统计: 总计 {total}, 成功 {success_count}, 失败 {fail_count}\n🎯 目标: {target_desc}")

        except Exception as e:
            logger.error(f"恢复任务失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 恢复失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
