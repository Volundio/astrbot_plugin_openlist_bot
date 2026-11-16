import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from astrbot_plugin_openlist_bot.lib.client import OpenlistClient
from astrbot_plugin_openlist_bot.lib.onebot_files import (
    extract_flash_descriptors_from_segments,
    normalize_message_segments,
    sanitize_relative_subpath,
)
from astrbot_plugin_openlist_bot.main import OpenlistPlugin


class MockAstrBotConfig(dict):
    def __init__(self, initial=None):
        super().__init__(initial or {"global_settings": {}})
        self.saved_count = 0

    def save_config(self):
        self.saved_count += 1


class FakeBot:
    def __init__(self, handler):
        self.calls = []

        async def call_action(action, **params):
            self.calls.append((action, params))
            return await handler(action, params)

        self.api = type("FakeApi", (), {"call_action": staticmethod(call_action)})()


class FakeOpenlistClient:
    def __init__(self):
        self.dirs = {"/", "/archive", "/movies", "/backup"}
        self.files_by_dir = {
            "/": [{"name": "archive", "is_dir": True, "size": 0}],
            "/archive": [],
            "/movies": [],
            "/backup": [],
        }
        self.uploaded_streams = []
        self.uploaded_files = []
        self.mkdir_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_file_info(self, path: str, log_error: bool = True):
        if path in self.dirs:
            return {"name": os.path.basename(path) or "/", "is_dir": True, "size": 0}
        parent = os.path.dirname(path.rstrip("/")) or "/"
        fname = os.path.basename(path)
        for f in self.files_by_dir.get(parent, []):
            if f.get("name") == fname:
                return f
        return None

    async def get_download_url(self, path: str, prefer_public: bool = True, file_info=None):
        info = file_info or await self.get_file_info(path)
        if info and not info.get("is_dir", True):
            return info.get("raw_url") or f"http://127.0.0.1:5244/d{path}"
        return None

    async def list_files(self, path: str = "/", page: int = 1, per_page: int = 0, refresh: bool = False, log_error: bool = True):
        if path not in self.dirs:
            return None
        return {"content": list(self.files_by_dir.get(path, []))}

    async def mkdir(self, path: str, log_error: bool = True) -> bool:
        self.mkdir_calls.append(path)
        self.dirs.add(path)
        self.files_by_dir.setdefault(path, [])
        return True

    async def ensure_dir(self, path: str) -> bool:
        parts = [p for p in path.strip("/").split("/") if p]
        cur = ""
        for p in parts:
            cur = f"{cur}/{p}"
            if cur not in self.dirs:
                await self.mkdir(cur)
        return True

    async def upload_url_stream(self, source_url: str, target_path: str, filename: str, file_size=None) -> bool:
        self.uploaded_streams.append((source_url, target_path, filename, file_size))
        self.files_by_dir.setdefault(target_path, []).append(
            {"name": filename, "is_dir": False, "size": file_size or 128}
        )
        return True

    async def upload_file(self, file_path: str, target_path: str, filename: str = None) -> bool:
        fname = filename or os.path.basename(file_path)
        size = os.path.getsize(file_path) if os.path.exists(file_path) else 64
        self.uploaded_files.append((file_path, target_path, fname, size))
        self.files_by_dir.setdefault(target_path, []).append(
            {"name": fname, "is_dir": False, "size": size}
        )
        return True


async def _collect_results(async_gen):
    out = []
    async for item in async_gen:
        out.append(item)
    return out


def test_schema_defaults_to_global_shared_mode():
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    items = schema["global_settings"]["items"]
    assert items["require_user_auth"]["default"] is False
    assert "slider" in items["cache_duration"]
    assert "minimum" not in items["cache_duration"]
    assert "private_backup_default_path" in items
    assert "private_autobackup_default_path" in items


def test_config_global_mode_direct_edit_and_user_mode_override():
    cfg = MockAstrBotConfig({"global_settings": {"default_openlist_url": "http://init:5244", "require_user_auth": False}})
    plugin = OpenlistPlugin(Context(), config=cfg)

    async def run():
        # 1. 普通用户在全局模式下不能修改全局配置
        normal_evt = AstrMessageEvent(sender_id="2001", is_admin=False, role="member")
        res = await _collect_results(plugin.config_command(normal_evt, "set", "openlist_url", "http://evil:5244"))
        assert "权限不足" in res[0]["text"]
        assert plugin.get_user_config("2001")["openlist_url"] == "http://init:5244"

        # 2. 管理员在全局模式下直接修改全局配置并同步 WebUI save_config
        admin_evt = AstrMessageEvent(sender_id="1001", is_admin=True, role="owner")
        res = await _collect_results(plugin.config_command(admin_evt, "set", "openlist_url", "http://global-new:5244"))
        assert "已更新全局配置" in res[0]["text"]
        assert cfg.saved_count == 1
        assert cfg["global_settings"]["default_openlist_url"] == "http://global-new:5244"
        assert plugin.get_user_config("2001")["openlist_url"] == "http://global-new:5244"

        # 3. 切换到用户独立模式后，用户可将 max_preview_size 显式设为 0 覆盖全局的 10
        cfg["global_settings"]["require_user_auth"] = True
        cfg["global_settings"]["max_preview_size"] = 10
        user_evt = AstrMessageEvent(sender_id="3003", is_admin=False)
        await _collect_results(plugin.config_command(user_evt, "set", "max_preview_size", "0"))
        assert plugin.get_user_config("3003")["max_preview_size"] == 0

        # 4. unset 恢复继承全局配置
        await _collect_results(plugin.config_command(user_evt, "unset", "max_preview_size"))
        assert plugin.get_user_config("3003")["max_preview_size"] == 10

    asyncio.run(run())


def test_root_help_does_not_hijack_unwoken_group_chat():
    cfg = MockAstrBotConfig({"global_settings": {"default_openlist_url": "http://127.0.0.1:5244"}})
    plugin = OpenlistPlugin(Context(wake_prefix=["/"]), config=cfg)

    async def run():
        # 未唤醒的群聊普通消息 "网盘" 不应触发拦截
        unwoken = AstrMessageEvent(group_id=123456, message_str="网盘", is_at_or_wake_command=False)
        res = await _collect_results(plugin.handle_openlist_root_help(unwoken))
        assert res == []
        assert unwoken.stopped is False

        # 已唤醒的 "/ol" 正常拦截并返回帮助
        woken = AstrMessageEvent(group_id=123456, message_str="/ol", is_at_or_wake_command=True)
        res = await _collect_results(plugin.handle_openlist_root_help(woken))
        assert len(res) == 1
        assert "OpenList 助手帮助" in res[0]["text"]
        assert woken.stopped is True

    asyncio.run(run())


def test_filename_truncation_preserves_extension_and_blocks_traversal():
    plugin = OpenlistPlugin(Context(), config=MockAstrBotConfig())
    long_name = "2026年度非常重要的项目归档报告(最终签署版)[v3.2]_" + ("A" * 180) + ".pdf"

    local_safe = plugin._sanitize_filename(long_name, max_length=100)
    assert len(local_safe) <= 100
    assert local_safe.endswith(".pdf")

    remote_safe = plugin._sanitize_remote_filename(long_name, max_length=120)
    assert len(remote_safe) <= 120
    assert remote_safe.endswith(".pdf")
    assert "(最终签署版)[v3.2]" in remote_safe

    escaped = sanitize_relative_subpath("../../etc/passwd/../相册/01.jpg")
    assert ".." not in escaped
    assert escaped.endswith("相册/01.jpg")


def test_navigation_quit_page_reset_and_search_isolation():
    cfg = MockAstrBotConfig({"global_settings": {"default_openlist_url": "http://127.0.0.1:5244", "max_display_files": 5}})
    plugin = OpenlistPlugin(Context(), config=cfg)
    fake_client = FakeOpenlistClient()
    fake_client.dirs.update({"/parent", "/parent/child"})
    fake_client.files_by_dir["/parent"] = [
        {"name": "child", "is_dir": True, "size": 0},
        {"name": "a.txt", "is_dir": False, "size": 12},
    ]
    fake_client.files_by_dir["/parent/child"] = [
        {"name": f"f_{i}.txt", "is_dir": False, "size": 10} for i in range(15)
    ]
    fake_client.search_files = AsyncMock(return_value=[
        {"name": "found.pdf", "parent": "/parent", "is_dir": False, "size": 99}
    ])
    plugin._create_openlist_client = lambda _uc: fake_client

    async def run():
        evt = AstrMessageEvent(sender_id="1001", group_id=888)
        await _collect_results(plugin.list_files(evt, "/parent"))
        await _collect_results(plugin.list_files(evt, "1"))  # 进入 /parent/child
        await _collect_results(plugin.next_page(evt))
        await _collect_results(plugin.next_page(evt))  # 第 3 页

        # 返回上级 /parent（仅 2 项，共 1 页），页码应重置为 1 而非显示空白第 3 页
        quit_res = await _collect_results(plugin.quit_navigation(evt))
        assert "第 1 / 1 页" in quit_res[0]["text"]
        assert "a.txt" in quit_res[0]["text"]

        # 执行搜索不应覆盖真实 current_path，quit 应直接退回 /parent
        await _collect_results(plugin.search_files(evt, "found", "/"))
        nav_key = plugin._get_navigation_state_key(evt)
        assert plugin._get_user_navigation_state(nav_key)["current_path"] == "/parent"
        quit_from_search = await _collect_results(plugin.quit_navigation(evt))
        assert "已退出搜索并返回目录" in quit_from_search[0]["text"]
        assert "📁 /parent" in quit_from_search[0]["text"]

    asyncio.run(run())


def test_openlist_client_ensure_dir_avoids_mkdir_on_mount_point():
    client = OpenlistClient("http://127.0.0.1:5244", token="tok")
    existing_dirs = {"/", "/夸克"}
    mkdir_called = []

    async def fake_get_info(path, log_error=True):
        if path in existing_dirs:
            return {"name": os.path.basename(path) or "/", "is_dir": True}
        return None

    async def fake_mkdir(path, log_error=True):
        mkdir_called.append(path)
        if path == "/夸克":
            # 模拟挂载点根目录调用 mkdir 返回 500 失败
            return False
        existing_dirs.add(path)
        return True

    client.get_file_info = fake_get_info
    client.mkdir = fake_mkdir

    async def run():
        ok = await client.ensure_dir("/夸克/群备份/group_100")
        assert ok is True
        assert "/夸克" not in mkdir_called
        assert mkdir_called == ["/夸克/群备份", "/夸克/群备份/group_100"]

    asyncio.run(run())


def test_preview_rejects_unsupported_extension_before_download():
    cfg = MockAstrBotConfig({"global_settings": {"default_openlist_url": "http://127.0.0.1:5244", "max_preview_size": 0}})
    plugin = OpenlistPlugin(Context(), config=cfg)
    fake_client = FakeOpenlistClient()
    fake_client.files_by_dir["/movies"] = [{"name": "huge.mp4", "is_dir": False, "size": 5 * 1024 * 1024 * 1024}]
    link_spy = AsyncMock(return_value={"url": "http://127.0.0.1/huge.mp4", "header": {}})
    fake_client.get_direct_download_link = link_spy
    plugin._create_openlist_client = lambda _uc: fake_client

    async def run():
        evt = AstrMessageEvent(sender_id="1001")
        res = await _collect_results(plugin.preview_command(evt, "/movies/huge.mp4"))
        assert "不在支持的文本或压缩包预览列表中" in res[0]["text"]
        assert link_spy.await_count == 0

    asyncio.run(run())


def test_qq_flash_transfer_multifile_upload_from_card_and_link():
    cfg = MockAstrBotConfig({"global_settings": {"default_openlist_url": "http://127.0.0.1:5244"}})
    plugin = OpenlistPlugin(Context(), config=cfg)
    fake_client = FakeOpenlistClient()
    plugin._create_openlist_client = lambda _uc: fake_client

    async def bot_handler(action, params):
        if action == "get_fileset_id":
            assert params.get("share_code") == "ShareCodeXYZ"
            return {"fileset_id": "fs_multi_99"}
        if action == "get_flash_file_list":
            assert params.get("fileset_id") == "fs_multi_99"
            return {
                "file_list": [
                    {"file_name": "说明(v1).txt", "file_size": 512, "cli_file_index": 0},
                    {
                        "file_name": "设计图",
                        "is_dir": True,
                        "files": [
                            {"file_name": "首页.png", "file_size": 2048, "cli_file_index": 1},
                            {"file_name": "详情页.png", "file_size": 4096, "cli_file_index": 2},
                        ],
                    },
                ]
            }
        if action == "get_flash_file_url":
            idx = params.get("file_index", 0)
            return {"url": f"https://cdn.qfile.qq.com/dl/{params['fileset_id']}/{idx}"}
        return {}

    bot = FakeBot(bot_handler)

    async def run():
        # 场景 A: 先发 JSON 闪传卡片，再发 ol upload /archive
        card_json = json.dumps({
            "app": "com.tencent.flashtransfer",
            "meta": {"detail": {"qqdocurl": "https://qfile.qq.com/q/ShareCodeXYZ?from=aio"}},
        })
        card_evt = AstrMessageEvent(
            sender_id="5001",
            bot=bot,
            raw_message={
                "post_type": "message",
                "user_id": 5001,
                "self_id": 99999,
                "message": [{"type": "json", "data": {"data": card_json}}],
            },
        )
        await plugin.remember_recent_upload_message(card_evt)

        cmd_evt = AstrMessageEvent(sender_id="5001", bot=bot, message_str="/ol upload /archive")
        res = await _collect_results(plugin.upload_command(cmd_evt, "/archive"))
        summary = res[-1]["text"]
        assert "总计 3, 成功 3, 失败 0" in summary
        uploaded_targets = [(t_dir, fname) for _, t_dir, fname, _ in fake_client.uploaded_streams]
        assert ("/archive", "说明(v1).txt") in uploaded_targets
        assert ("/archive/设计图", "首页.png") in uploaded_targets
        assert ("/archive/设计图", "详情页.png") in uploaded_targets

        # 场景 B: 直接命令一行传闪传链接 ol upload /movies https://qfile.qq.com/q/ShareCodeXYZ
        direct_evt = AstrMessageEvent(sender_id="5001", bot=bot)
        res2 = await _collect_results(plugin.upload_command(direct_evt, "/movies", "https://qfile.qq.com/q/ShareCodeXYZ"))
        assert "总计 3, 成功 3, 失败 0" in res2[-1]["text"]

    asyncio.run(run())


def test_private_chat_file_upload_and_offline_file_notice():
    cfg = MockAstrBotConfig({"global_settings": {"default_openlist_url": "http://127.0.0.1:5244"}})
    plugin = OpenlistPlugin(Context(), config=cfg)
    fake_client = FakeOpenlistClient()
    plugin._create_openlist_client = lambda _uc: fake_client

    async def bot_handler(action, params):
        if action == "get_private_file_url":
            return {"url": f"https://c2cpicdw.qpic.cn/private/{params['file_id']}"}
        return {}

    bot = FakeBot(bot_handler)

    async def run():
        # 1. 私聊 file 消息段（无内联 url，需调用 get_private_file_url）
        priv_msg_evt = AstrMessageEvent(
            sender_id="6001",
            bot=bot,
            raw_message={
                "post_type": "message",
                "message_type": "private",
                "user_id": 6001,
                "self_id": 99999,
                "message": [{"type": "file", "data": {"name": "合同草案.docx", "file_id": "priv_fid_1", "file_size": 1024}}],
            },
        )
        await plugin.remember_recent_upload_message(priv_msg_evt)
        up_evt = AstrMessageEvent(sender_id="6001", bot=bot)
        res = await _collect_results(plugin.upload_command(up_evt, "/archive"))
        assert "总计 1, 成功 1, 失败 0" in res[-1]["text"]
        assert fake_client.uploaded_streams[-1][2] == "合同草案.docx"

        # 2. OneBot offline_file 私聊离线文件通知
        offline_evt = AstrMessageEvent(
            sender_id="6002",
            bot=bot,
            raw_message={
                "post_type": "notice",
                "notice_type": "offline_file",
                "user_id": 6002,
                "self_id": 99999,
                "file": {"name": "离线压缩包.zip", "size": 4096, "url": "https://cdn.qq.com/offline.zip"},
            },
        )
        await plugin.remember_recent_upload_message(offline_evt)
        up_evt2 = AstrMessageEvent(sender_id="6002", bot=bot)
        res2 = await _collect_results(plugin.upload_command(up_evt2, "/archive"))
        assert "总计 1, 成功 1, 失败 0" in res2[-1]["text"]
        assert fake_client.uploaded_streams[-1][2] == "离线压缩包.zip"

    asyncio.run(run())


def test_autobackup_blocks_self_upload_and_deduplicates_dual_events():
    cfg = MockAstrBotConfig({"global_settings": {"default_openlist_url": "http://127.0.0.1:5244"}})
    plugin = OpenlistPlugin(Context(), config=cfg)
    fake_client = FakeOpenlistClient()
    plugin._create_openlist_client = lambda _uc: fake_client
    plugin.global_config_manager.save_runtime_fields({
        "autobackup_groups": ["777:/backup/group_777"],
        "autobackup_private_users": ["8001:/backup/private_8001"],
    })

    async def bot_handler(action, params):
        if action == "get_group_file_url":
            return {"url": f"https://group.cdn.qq.com/{params['file_id']}"}
        if action == "get_private_file_url":
            return {"url": f"https://priv.cdn.qq.com/{params['file_id']}"}
        if action == "get_flash_file_list":
            return [{"file_name": "闪传视频.mp4", "file_size": 8192, "cli_file_index": 0}]
        if action == "get_flash_file_url":
            return {"url": "https://flash.cdn.qq.com/v.mp4"}
        return {}

    bot = FakeBot(bot_handler)

    async def run():
        # 1. 机器人自身发送文件产生的 group_upload（user_id == self_id）必须被忽略
        self_notice = AstrMessageEvent(
            sender_id="99999",
            self_id="99999",
            group_id=777,
            bot=bot,
            raw_message={
                "post_type": "notice",
                "notice_type": "group_upload",
                "group_id": 777,
                "user_id": 99999,
                "self_id": 99999,
                "file": {"id": "/self_txt", "name": "link_download_link.txt", "size": 120, "busid": 102},
            },
        )
        await plugin.handle_group_file_upload(self_notice)
        await asyncio.sleep(0.05)
        assert fake_client.uploaded_streams == []

        # 2. 同一群文件同时触发 message(file) 与 notice(group_upload)，只应自动备份 1 次
        msg_evt = AstrMessageEvent(
            sender_id="2002",
            self_id="99999",
            group_id=777,
            bot=bot,
            raw_message={
                "post_type": "message",
                "group_id": 777,
                "user_id": 2002,
                "self_id": 99999,
                "message": [{"type": "file", "data": {"name": "周报.pdf", "file_id": "/fid_weekly", "file_size": 500}}],
            },
        )
        notice_evt = AstrMessageEvent(
            sender_id="2002",
            self_id="99999",
            group_id=777,
            bot=bot,
            raw_message={
                "post_type": "notice",
                "notice_type": "group_upload",
                "group_id": 777,
                "user_id": 2002,
                "self_id": 99999,
                "file": {"id": "/fid_weekly", "name": "周报.pdf", "size": 500, "busid": 102},
            },
        )
        await plugin.handle_group_file_upload(msg_evt)
        await plugin.handle_group_file_upload(notice_evt)
        await asyncio.gather(*list(plugin._background_tasks))
        assert len(fake_client.uploaded_streams) == 1
        assert fake_client.uploaded_streams[0][1:] == ("/backup/group_777", "周报.pdf", 500)

        # 3. 私聊自动备份：收到 flashtransfer 消息段时自动转存到 /backup/private_8001
        priv_flash_evt = AstrMessageEvent(
            sender_id="8001",
            self_id="99999",
            bot=bot,
            raw_message={
                "post_type": "message",
                "message_type": "private",
                "user_id": 8001,
                "self_id": 99999,
                "message": [{"type": "flashtransfer", "data": {"fileSetId": "fs_auto_1"}}],
            },
        )
        await plugin.handle_group_file_upload(priv_flash_evt)
        await asyncio.gather(*list(plugin._background_tasks))
        assert fake_client.uploaded_streams[-1][1:] == ("/backup/private_8001", "闪传视频.mp4", 8192)

    asyncio.run(run())


def test_private_manual_backup_and_retry():
    cfg = MockAstrBotConfig({"global_settings": {"default_openlist_url": "http://127.0.0.1:5244", "backup_retry_attempts": 1, "backup_retry_delay": 0}})
    plugin = OpenlistPlugin(Context(), config=cfg)
    fake_client = FakeOpenlistClient()
    fail_once = {"flag": True}
    orig_upload_stream = fake_client.upload_url_stream

    async def flaky_upload(url, target_dir, filename, file_size=None):
        if filename == "flaky.pdf" and fail_once["flag"]:
            return False
        return await orig_upload_stream(url, target_dir, filename, file_size)

    fake_client.upload_url_stream = flaky_upload
    fake_client.download_url_to_file = AsyncMock(return_value=False)
    plugin._create_openlist_client = lambda _uc: fake_client

    async def bot_handler(action, params):
        if action == "get_friend_msg_history":
            return {
                "messages": [
                    {
                        "user_id": 9001,
                        "self_id": 99999,
                        "message": [{"type": "file", "data": {"name": "ok.txt", "file_id": "pf_1", "file_size": 100}}],
                    },
                    {
                        "user_id": 9001,
                        "self_id": 99999,
                        "message": [{"type": "flashtransfer", "data": {"fileSetId": "fs_priv_1"}}],
                    },
                ]
            }
        if action == "get_private_file_url":
            return {"url": "https://priv.cdn.qq.com/ok.txt"}
        if action == "get_flash_file_list":
            return [{"file_name": "flaky.pdf", "file_size": 200, "cli_file_index": 0}]
        if action == "get_flash_file_url":
            return {"url": "https://flash.cdn.qq.com/flaky.pdf"}
        return {}

    bot = FakeBot(bot_handler)

    async def run():
        priv_evt = AstrMessageEvent(sender_id="9001", bot=bot)
        res = await _collect_results(plugin.backup_command(priv_evt))
        assert "成功 1, 跳过 0, 失败 1" in res[-1]["text"]
        assert "flaky.pdf" in res[-1]["text"]

        # 第二次使用 ol backup retry 恢复正常并成功上传失败的闪传项
        fail_once["flag"] = False
        retry_res = await _collect_results(plugin.backup_command(priv_evt, "retry"))
        assert "成功 1, 跳过 0, 失败 0" in retry_res[-1]["text"]

    asyncio.run(run())


def test_cq_and_flash_segment_parsing():
    segs = normalize_message_segments(
        "看看这个闪传 [CQ:flashtransfer,fileSetId=abc_123] 还有链接 https://qfile.qq.com/q/Zz9988"
    )
    flashes = extract_flash_descriptors_from_segments(segs, include_plain_text=True)
    assert len(flashes) == 2
    assert flashes[0]["fileset_id"] == "abc_123"
    assert flashes[1]["share_code"] == "Zz9988"


def test_http_client_token_cache_and_401_relogin_and_stream_preview():
    from aiohttp import web

    login_calls = {"count": 0}
    list_calls = {"count": 0}

    async def run():
        async def handle_login(request):
            login_calls["count"] += 1
            return web.json_response({"code": 200, "data": {"token": f"jwt_tok_{login_calls['count']}"}})

        async def handle_list(request):
            list_calls["count"] += 1
            auth = request.headers.get("Authorization", "")
            # 模拟第 2 次请求时旧 token 失效返回 401，验证自动重登重试
            if list_calls["count"] == 2 and auth == "jwt_tok_1":
                return web.json_response({"code": 401, "message": "token expired"})
            return web.json_response({"code": 200, "data": {"content": [{"name": "readme.txt", "is_dir": False, "size": 50000}]}})

        async def handle_raw_text(request):
            range_hdr = request.headers.get("Range", "")
            full_data = ("Hello OpenList 预览测试! " * 2000).encode("utf-8")
            if range_hdr.startswith("bytes=0-"):
                end = int(range_hdr.split("-")[1])
                return web.Response(status=206, body=full_data[: end + 1])
            return web.Response(status=200, body=full_data)

        app = web.Application()
        app.router.add_post("/api/auth/login", handle_login)
        app.router.add_post("/api/fs/list", handle_list)
        app.router.add_get("/raw/readme.txt", handle_raw_text)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"

        try:
            # 第一次创建客户端：触发 1 次登录
            async with OpenlistClient(base_url, username="admin", password="pwd") as c1:
                res1 = await c1.list_files("/")
                assert res1 is not None
                assert login_calls["count"] == 1

            # 第二次创建客户端：命中内存缓存，不重复登录；遇到业务 401 自动重登重试成功
            async with OpenlistClient(base_url, username="admin", password="pwd") as c2:
                assert login_calls["count"] == 1
                res2 = await c2.list_files("/")
                assert res2 is not None
                assert login_calls["count"] == 2

            # 测试 PreviewService 流式 Range 文本预览无需落盘
            cfg = MockAstrBotConfig({
                "global_settings": {
                    "default_openlist_url": base_url,
                    "text_preview_length": 120,
                    "max_preview_size": 10,
                }
            })
            plugin = OpenlistPlugin(Context(), config=cfg)

            class PreviewStubClient(FakeOpenlistClient):
                async def get_file_info(self, path, log_error=True):
                    return {"name": "readme.txt", "is_dir": False, "size": 50000}

                async def get_direct_download_link(self, path):
                    return {"url": f"{base_url}/raw/readme.txt", "header": {}}

            plugin._create_openlist_client = lambda _uc: PreviewStubClient()
            evt = AstrMessageEvent(sender_id="1001")
            prev_res = await _collect_results(plugin.preview_command(evt, "/readme.txt"))
            assert "📝 文本预览:" in prev_res[-1]["text"]
            assert "Hello OpenList 预览测试!" in prev_res[-1]["text"]
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_restore_group_folder_params_and_base64_cross_container_fallback(tmp_path):
    from aiohttp import web

    async def run():
        async def handle_dl(_req):
            return web.Response(body=b"restore content")

        app = web.Application()
        app.router.add_get("/dl/doc.txt", handle_dl)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        dl_url = f"http://127.0.0.1:{port}/dl/doc.txt"

        created_folder_params = {}
        upload_attempts = []

        async def bot_handler(action, params):
            if action == "get_group_root_files":
                if created_folder_params:
                    return {"folders": [{"folder_name": "资料夹", "folder_id": "/folder_01"}]}
                return {"folders": []}
            if action == "create_group_file_folder":
                created_folder_params.update(params)
                return {}
            if action == "upload_group_file":
                upload_attempts.append(params["file"])
                # 第一次模拟跨 Docker 容器无法读取本地路径失败，第二次 base64:// 成功
                if not str(params["file"]).startswith("base64://"):
                    raise RuntimeError("ENOENT: no such file in NapCat container")
                return {}
            return {}

        bot = FakeBot(bot_handler)
        cfg = MockAstrBotConfig({"global_settings": {"default_openlist_url": "http://127.0.0.1:5244"}})
        plugin = OpenlistPlugin(Context(), config=cfg)

        class RestoreStubClient(FakeOpenlistClient):
            async def get_file_info(self, path, log_error=True):
                if path == "/backup":
                    return {"name": "backup", "is_dir": True}
                return None

            async def list_files(self, path="/", page=1, per_page=0, refresh=False, log_error=True):
                if path == "/backup":
                    return {"content": [{"name": "资料夹", "is_dir": True, "size": 0}]}
                if path == "/backup/资料夹":
                    return {"content": [{"name": "doc.txt", "is_dir": False, "size": 15}]}
                return {"content": []}

            async def get_direct_download_link(self, path):
                return {"url": dl_url, "header": {}}

        plugin._create_openlist_client = lambda _uc: RestoreStubClient()
        try:
            evt = AstrMessageEvent(sender_id="1001", group_id=555666, bot=bot)
            res = await _collect_results(plugin.restore_command(evt, "/backup"))
            assert "总计 1, 成功 1, 失败 0" in res[-1]["text"]
            assert created_folder_params.get("name") == "资料夹"
            assert created_folder_params.get("folder_name") == "资料夹"
            assert created_folder_params.get("parent_id") == "/"
            assert len(upload_attempts) == 2
            assert upload_attempts[1].startswith("base64://")
            await plugin.terminate()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_private_autobackup_command_and_info_by_number():
    cfg = MockAstrBotConfig({"global_settings": {"default_openlist_url": "http://127.0.0.1:5244"}})
    plugin = OpenlistPlugin(Context(), config=cfg)
    fake_client = FakeOpenlistClient()
    fake_client.files_by_dir["/"] = [{"name": "notes.txt", "is_dir": False, "size": 256, "raw_url": "http://127.0.0.1:5244/d/notes.txt"}]
    plugin._create_openlist_client = lambda _uc: fake_client

    async def bot_handler(action, params):
        if action == "get_friend_msg_history":
            return {"messages": []}
        return {}

    bot = FakeBot(bot_handler)

    async def run():
        admin_priv_evt = AstrMessageEvent(sender_id="7788", is_admin=True, bot=bot)
        # 开启私聊自动备份
        en_res = await _collect_results(plugin.autobackup_command(admin_priv_evt, "enable", "/backup/priv_7788"))
        assert "私聊用户 7788 自动备份已开启" in en_res[0]["text"]

        # 幂等再次执行 enable 不重复全量备份
        en_again = await _collect_results(plugin.autobackup_command(admin_priv_evt, "enable"))
        assert "不会重复执行首次全量备份" in en_again[0]["text"]

        # show 展示私聊启用状态
        show_res = await _collect_results(plugin.autobackup_command(admin_priv_evt, "show"))
        assert "用户 7788 -> /backup/priv_7788" in show_res[0]["text"]

        # disable 关闭私聊自动备份
        dis_res = await _collect_results(plugin.autobackup_command(admin_priv_evt, "disable"))
        assert "已禁用" in dis_res[0]["text"]

        # ol info 支持序号
        await _collect_results(plugin.list_files(admin_priv_evt, "/"))
        info_res = await _collect_results(plugin.file_info(admin_priv_evt, "1"))
        assert "notes.txt" in info_res[0]["text"]
        await plugin.terminate()

    asyncio.run(run())
