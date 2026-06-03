import posixpath

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .base import PluginService


class BrowseService(PluginService):
    """Browse service."""

    async def list_files(self, event: AstrMessageEvent, path: str = ""):
        """列出文件和目录，或获取文件链接"""
        user_id = event.get_sender_id()
        nav_key = self._get_navigation_state_key(event)
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return
        path = (path or "").strip()
        target_path = self._resolve_target_path(nav_key, path)
        path_candidates = [target_path]
        if path.isdigit():
            number = int(path)
            item = self._get_item_by_number(nav_key, number)
            if item:
                if item.get("is_dir", False):
                    target_path = self._get_item_full_path(nav_key, item, user_config)
                    path_candidates = [target_path]
                else:
                    async for result in self._get_and_send_download_link(event, item, user_config):
                        yield result
                    return
            else:
                yield event.plain_result(f"❌ 序号 {number} 无效，请使用 /ol ls 查看当前目录")
                return
        else:
            path_candidates = self._resolve_path_candidates(nav_key, path)
        try:
            cache_enabled = str(user_config.get("enable_cache", True)).lower() not in ("false", "0", "no", "off")
            cache_duration = self._get_cache_duration_seconds(user_config)
            async with self._create_openlist_client(user_config) as client:
                for candidate_path in path_candidates:
                    file_info = await client.get_file_info(candidate_path)
                    if file_info and not file_info.get("is_dir", False):
                        async for result in self._get_and_send_download_link(event, file_info, user_config, full_path=candidate_path):
                            yield result
                        return

                    list_result = None
                    if cache_enabled:
                        list_result = self.cache_manager.get_cache(
                            user_config["openlist_url"],
                            candidate_path,
                            user_id,
                            cache_duration,
                        )
                    if list_result is None:
                        list_result = await client.list_files(candidate_path, per_page=0)
                        if list_result is not None and cache_enabled:
                            self.cache_manager.set_cache(
                                user_config["openlist_url"],
                                candidate_path,
                                user_id,
                                list_result,
                            )
                    if list_result is not None:
                        files = list_result.get("content") or []
                        self._update_user_navigation_state(nav_key, candidate_path, files)
                        formatted_list = self._format_file_list(files, candidate_path, user_config, nav_key)
                        yield event.plain_result(formatted_list)
                        return

                display_path = " / ".join(path_candidates)
                logger.warning(f"用户 {user_id} 无法访问路径候选: {display_path}")
                yield event.plain_result(f"❌ 无法访问路径: {display_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 列出文件失败: {e}, 路径候选: {path_candidates}", exc_info=True)
            yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    async def next_page(self, event: AstrMessageEvent):
        """下一页"""
        user_id = event.get_sender_id()
        nav_key = self._get_navigation_state_key(event)
        user_config = self.get_user_config(user_id)
        nav_state = self._get_user_navigation_state(nav_key)
        if not nav_state.get("items"):
            yield event.plain_result("🤔 没有可供翻页的列表，请先使用 /ol ls 查看一个目录。")
            return
        current_page = nav_state.get("current_page", 1)
        all_items = nav_state.get("items", [])
        max_files_per_page = user_config.get("max_display_files", 20)
        total_pages = (len(all_items) + max_files_per_page - 1) // max_files_per_page

        if current_page < total_pages:
            nav_state["current_page"] += 1
        else:
            yield event.plain_result("➡️ 已经是最后一页了。")
            return

        formatted_list = self._format_file_list(
            all_items, nav_state["current_path"], user_config, nav_key
        )
        yield event.plain_result(formatted_list)

    async def prev_page(self, event: AstrMessageEvent):
        """上一页"""
        user_id = event.get_sender_id()
        nav_key = self._get_navigation_state_key(event)
        user_config = self.get_user_config(user_id)
        nav_state = self._get_user_navigation_state(nav_key)
        if not nav_state.get("items"):
            yield event.plain_result("🤔 没有可供翻页的列表，请先使用 /ol ls 查看一个目录。")
            return
        current_page = nav_state.get("current_page", 1)
        all_items = nav_state.get("items", [])
        max_files_per_page = user_config.get("max_display_files", 20)
        total_pages = (len(all_items) + max_files_per_page - 1) // max_files_per_page

        if current_page > 1:
            nav_state["current_page"] -= 1
        else:
            yield event.plain_result("⬅️ 已经是第一页了。")
            return

        formatted_list = self._format_file_list(
            all_items, nav_state["current_path"], user_config, nav_key
        )
        yield event.plain_result(formatted_list)

    async def search_files(self, event: AstrMessageEvent, keyword: str, path: str = "/"):
        """搜索文件"""
        if not keyword:
            yield event.plain_result("❌ 请提供搜索关键词")
            return
        user_id = event.get_sender_id()
        nav_key = self._get_navigation_state_key(event)
        path = self._resolve_target_path(nav_key, path)
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return
        try:
            yield event.plain_result(f'🔍 正在搜索 "{keyword}"...')
            async with self._create_openlist_client(user_config) as client:
                files = await client.search_files(keyword, path)
                if files:
                    search_title = f'🔍 搜索 "{keyword}"'
                    self._update_user_navigation_state(nav_key, search_title, files)

                    # 使用通用的列表格式化函数显示第一页
                    formatted_list = self._format_file_list(files, search_title, user_config, nav_key)
                    yield event.plain_result(formatted_list)
                else:
                    yield event.plain_result(f"🔍 未找到包含 '{keyword}' 的文件")
        except Exception as e:
            logger.error(f"用户 {user_id} 搜索文件失败: {e}, 关键词: {keyword}, 路径: {path}", exc_info=True)
            yield event.plain_result(f"❌ 搜索失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    async def file_info(self, event: AstrMessageEvent, path: str):
        """获取文件详细信息"""
        path = (path or "").strip()
        if not path:
            yield event.plain_result("❌ 请提供文件路径")
            return
        user_id = event.get_sender_id()
        nav_key = self._get_navigation_state_key(event)
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return
        path_candidates = self._resolve_path_candidates(nav_key, path)
        target_path = path_candidates[0]
        try:
            async with self._create_openlist_client(user_config) as client:
                file_info = None
                for candidate_path in path_candidates:
                    file_info = await client.get_file_info(candidate_path)
                    if file_info:
                        target_path = candidate_path
                        break
                if file_info:
                    name = file_info.get("name", "")
                    size = file_info.get("size", 0)
                    modified = file_info.get("modified", "")
                    is_dir = file_info.get("is_dir", False)
                    provider = file_info.get("provider", "")
                    download_url = None
                    info_text = f"📋 文件信息\n\n"
                    info_text += f"📄 名称: {name}\n"
                    info_text += f"📁 类型: {'目录' if is_dir else '文件'}\n"
                    info_text += f"📍 路径: {target_path}\n"
                    if not is_dir: info_text += f"💾 大小: {self._format_file_size(size)}\n"
                    if modified: info_text += f"📅 修改时间: {modified.replace('T', ' ').split('.')[0]}\n"
                    if provider: info_text += f"🔗 存储: {provider}\n"
                    if not is_dir:
                        if self._is_extension_allowed(name, user_config):
                            download_url = await client.get_download_url(target_path)
                            if download_url:
                                info_text += "\n🔗 下载链接将作为 txt 附件发送。"
                        else:
                            info_text += f"\n🔗 下载链接: 文件类型不允许（当前允许: {self._format_extension_filter(user_config)}）"
                    yield event.plain_result(info_text)
                    if download_url:
                        async for result in self._send_download_link_txt(event, name, size, target_path, download_url):
                            yield result
                else:
                    display_path = " / ".join(path_candidates)
                    logger.warning(f"用户 {user_id} 文件不存在: {display_path}")
                    yield event.plain_result(f"❌ 文件不存在: {display_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 获取文件信息失败: {e}, 路径候选: {path_candidates}", exc_info=True)
            yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    async def get_download_link(self, event: AstrMessageEvent, path: str):
        """直接下载指定的文件"""
        path = (path or "").strip()
        if not path:
            yield event.plain_result("❌ 请提供文件路径或序号")
            return
        user_id = event.get_sender_id()
        nav_key = self._get_navigation_state_key(event)
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return

        item_to_download = None
        full_path_override = None

        if path.isdigit():
            number = int(path)
            item = self._get_item_by_number(nav_key, number)
            if item:
                if item.get("is_dir", False):
                    yield event.plain_result(f"❌ 序号 {number} 是目录，无法下载。")
                    return
                item_to_download = item
            else:
                yield event.plain_result(f"❌ 序号 {number} 无效。")
                return
        else:
            path_candidates = self._resolve_path_candidates(nav_key, path)
            try:
                async with self._create_openlist_client(user_config) as client:
                    for candidate_path in path_candidates:
                        file_info = await client.get_file_info(candidate_path)
                        if file_info and not file_info.get("is_dir", False):
                            item_to_download = file_info
                            full_path_override = candidate_path
                            break
                    if not item_to_download:
                        display_path = " / ".join(path_candidates)
                        yield event.plain_result(f"❌ 无法下载，文件不存在或路径为目录: {display_path}")
                        return
            except Exception as e:
                logger.error(f"用户 {user_id} 获取文件信息失败: {e}, 路径候选: {path_candidates}", exc_info=True)
                yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
                return

        if item_to_download:
            yield event.plain_result(f"📥 正在准备下载文件: {item_to_download.get('name', '')}...")
            async for result in self._download_file(event, item_to_download, user_config, full_path_override=full_path_override):
                yield result

    async def quit_navigation(self, event: AstrMessageEvent):
        """返回上级目录"""
        user_id = event.get_sender_id()
        nav_key = self._get_navigation_state_key(event)
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return
        nav_state = self._get_user_navigation_state(nav_key)
        if not nav_state["parent_paths"]:
            yield event.plain_result("📂 已经在根目录，无法继续回退。")
            return
        previous_path = nav_state["parent_paths"].pop()
        try:
            async with self._create_openlist_client(user_config) as client:
                result = await client.list_files(previous_path)
                if result is not None:
                    files = result.get("content") or []
                    nav_state["current_path"] = previous_path
                    nav_state["items"] = files
                    formatted_list = self._format_file_list(files, previous_path, user_config, nav_key)
                    yield event.plain_result(f"⬅️ 已返回上级目录\n\n{formatted_list}")
                else:
                    logger.warning(f"用户 {user_id} 无法访问上级目录: {previous_path}")
                    yield event.plain_result(f"❌ 无法访问上级目录: {previous_path}")
        except Exception as e:
            logger.error(f"用户 {user_id} 回退目录失败: {e}, 目标路径: {previous_path}", exc_info=True)
            yield event.plain_result(f"❌ 回退失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")

    async def remove_command(self, event: AstrMessageEvent, path: str):
        """删除文件或文件夹"""
        path = (path or "").strip()
        if not path:
            yield event.plain_result("❌ 请提供文件路径或序号")
            return
        user_id = event.get_sender_id()
        nav_key = self._get_navigation_state_key(event)
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return

        target_dir = None
        target_names = []
        display_name = ""

        if path.isdigit():
            number = int(path)
            item = self._get_item_by_number(nav_key, number)
            if item:
                full_path = self._get_item_full_path(nav_key, item, user_config)
                if full_path == "/":
                    yield event.plain_result("❌ 不允许删除根目录。")
                    return
                target_dir = posixpath.dirname(full_path) or "/"
                target_names = [posixpath.basename(full_path)]
                display_name = full_path
            else:
                yield event.plain_result(f"❌ 序号 {number} 无效。")
                return
        else:
            full_path = self._resolve_target_path(nav_key, path)
            if full_path == "/":
                yield event.plain_result("❌ 不允许删除根目录。")
                return
            target_dir = posixpath.dirname(full_path) or "/"
            target_names = [posixpath.basename(full_path)]
            display_name = full_path

        try:
            async with self._create_openlist_client(user_config) as client:
                success = await client.remove(target_dir, target_names)
                if success:
                    yield event.plain_result(f"✅ 已删除: {display_name}")
                    self.cache_manager.clear_cache(user_id)

                    # 检查是否删除了当前路径或其父目录
                    nav_state = self._get_user_navigation_state(nav_key)
                    current_path = nav_state["current_path"]

                    # 构建被删除项目的完整路径列表
                    deleted_full_paths = []
                    for name in target_names:
                        p = f"{target_dir.rstrip('/')}/{name}"
                        if not p.startswith("/"): p = "/" + p
                        deleted_full_paths.append(p)

                    # 如果当前路径被删除（或当前路径是其子目录），返回根目录
                    is_current_path_deleted = False
                    for deleted_path in deleted_full_paths:
                        if current_path == deleted_path or current_path.startswith(deleted_path + "/"):
                            is_current_path_deleted = True
                            break

                    if is_current_path_deleted:
                        # 返回根目录并刷新
                        result = await client.list_files("/")
                        if result is not None:
                            files = result.get("content") or []
                            self.user_navigation_state[nav_key] = {
                                "current_path": "/",
                                "items": files,
                                "parent_paths": [],
                                "current_page": 1,
                            }
                            yield event.plain_result("⚠️ 当前目录已被删除，已自动返回根目录。")
                    elif target_dir == current_path:
                        # 如果在当前目录下删除了某个项目，刷新当前目录
                        result = await client.list_files(current_path)
                        if result is not None:
                            files = result.get("content") or []
                            self._update_user_navigation_state(nav_key, current_path, files)
                else:
                    yield event.plain_result(f"❌ 删除失败，请检查权限或路径是否正确")
        except Exception as e:
            logger.error(f"用户 {user_id} 删除失败: {e}, 路径: {path}", exc_info=True)
            yield event.plain_result(f"❌ 删除失败: {str(e)}")

    async def mkdir_command(self, event: AstrMessageEvent, name: str):
        """创建文件夹"""
        name = (name or "").strip()
        if not name:
            yield event.plain_result("❌ 请提供文件夹名称或路径")
            return
        user_id = event.get_sender_id()
        nav_key = self._get_navigation_state_key(event)
        user_config = self.get_user_config(user_id)
        if not self._validate_config(user_config):
            yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
            return

        full_path = self._resolve_target_path(nav_key, name)
        if full_path == "/":
            yield event.plain_result("❌ 不允许创建根目录。")
            return

        try:
            async with self._create_openlist_client(user_config) as client:
                success = await client.mkdir(full_path)
                if success:
                    yield event.plain_result(f"✅ 已创建文件夹: {name}")
                    self.cache_manager.clear_cache(user_id)
                    # 如果在当前目录下创建，刷新列表
                    nav_state = self._get_user_navigation_state(nav_key)
                    current_path = self._normalize_openlist_path(nav_state["current_path"])
                    # 检查创建的文件夹是否在当前目录下（直接子目录）
                    parent_path = posixpath.dirname(full_path) or "/"
                    if parent_path == current_path.rstrip("/") or (current_path == "/" and parent_path == "/"):
                        result = await client.list_files(current_path)
                        if result:
                            files = result.get("content") or []
                            self._update_user_navigation_state(nav_key, current_path, files)
                else:
                    yield event.plain_result(f"❌ 创建文件夹失败")
        except Exception as e:
            logger.error(f"用户 {user_id} 创建文件夹失败: {e}, 名称: {name}", exc_info=True)
            yield event.plain_result(f"❌ 创建失败: {str(e)}")
