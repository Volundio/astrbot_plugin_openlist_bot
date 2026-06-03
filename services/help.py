from astrbot.api.event import AstrMessageEvent

from .base import PluginService


class HelpService(PluginService):
    """Help service."""

    async def help_command(self, event: AstrMessageEvent):
        """显示帮助信息"""
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        global_cfg = self.get_global_config()
        is_user_auth_mode = global_cfg.get("require_user_auth", True)

        help_text = f"""📚 OpenList 助手帮助
💡 您也可以使用别名 `/网盘` 代替 `/ol`。

---
核心导航指令
---
▶️ `/ol ls [路径|序号]`
   - 浏览目录: 列出内容，若文件过多会自动分页。
     - 示例: `/ol ls` 或 `/ol ls /movies`
   - 进入子目录:
     - 示例: `/ol ls 1` (如果1是目录)
   - 获取链接: 获取文件的下载链接，并以 txt 附件发送。
     - 示例: `/ol ls 2` (如果2是文件)

▶️ `/ol next` - 下一页
▶️ `/ol prev` - 上一页

▶️ `/ol quit`
   - 返回到上级目录。

---
文件操作指令
---
📥 `/ol download <路径|序号>`
   - 直接下载: 将文件作为附件发送给您。
     - 示例: `/ol download 3` (下载列表中的3号文件)
     - 示例: `/ol download /docs/report.pdf`

🔍 `/ol search <关键词> [路径]`
   - 搜索文件。注意：搜索依赖服务器索引，可能不是最新的。
     - 示例: `/ol search "年度报告"`

ℹ️ `/ol info <路径>`
   - 查看文件或目录的详细信息，不支持序号。
     - 示例: `/ol info /docs/report.pdf`

👁️ `/ol preview <路径|序号>`
   - 预览内容: 支持文本文件内容预览或压缩包目录查看。
     - 示例: `/ol preview 1`
     - 示例: `/ol preview /data/config.txt`

📂 `/ol mkdir <名称|路径>`
   - 新建文件夹: 在当前目录或指定路径创建。
     - 示例: `/ol mkdir new_folder`

🗑️ `/ol rm <路径|序号>`
   - 删除项目: 删除文件或文件夹（谨慎操作）。
     - 示例: `/ol rm 4`
     - 示例: `/ol rm /tmp/old_file.txt`

📤 `/ol upload [路径]`
   - 先发送图片、视频或文件，再在 5 分钟内发送本指令上传最近附件。
   - `/ol upload`: 上传到当前目录。
   - `/ol upload /目标目录`: 上传到指定目录。
   - `/ol upload 子目录`: 上传到当前目录下的子目录。

📦 `/ol backup [/目标路径] [@群号]`
   - 将指定群聊的所有文件递归备份到 Openlist。
   - 示例: `/ol backup /群备份 @123456`
   - 重试失败项: `/ol backup retry`
   - 提示: 路径须以 `/` 开头，群号须以 `@` 开头。未指定路径时使用 `backup_default_path`。
   - 权限: 当前群可直接操作；指定其他群时需要您是目标群群主或管理员。

🔄 `/ol autobackup <enable|disable> [@群号] [/路径]`
   - 配置群文件自动备份（新上传文件自动同步）。
   - 示例: `/ol autobackup enable` (开启当前群备份到默认路径，并立即执行一次全量备份)
   - 示例: `/ol autobackup enable @123456 /backup` (指定群号和路径)
   - 示例: `/ol autobackup disable @123456` (禁用指定群的自动备份)
   - 提示: 禁用时无需提供路径。路径须以 `/` 开头，群号须以 `@` 开头。

🚚 `/ol restore <路径> [@群号]`
   - 将 Openlist 路径中的文件恢复（发送）到目标群组或私聊。
   - 示例: `/ol restore /backup/group_123456` (恢复到当前会话)
   - 示例: `/ol restore /docs @987654` (恢复到指定群)
   - 提示: 目标为群组时会尝试保持一级目录结构。
   - 权限: 当前群可直接操作；指定其他群时需要您是目标群群主或管理员。

---
插件配置指令
---
⚙️ `/ol config setup` - 推荐新用户使用，启动交互式配置向导。
⚙️ `/ol config show` - 显示您当前的配置。
⚙️ `/ol config set <键> <值>` - 修改配置项。
⚙️ `/ol config test` - 测试与服务器的连接。
⚙️ `/ol config clear_cache` - 清除文件列表缓存。
"""

        if is_user_auth_mode:
            help_text += f"""

👤 当前模式: 用户独立认证
   - 每位用户都需要使用 `/ol config setup` 单独配置自己的 Openlist 账户信息。"""

            if not self._validate_config(user_config):
                help_text += f"""

⚠️ 操作提示
   您尚未完成配置，请发送 `/ol config setup` 开始配置向导。"""
        else:
            help_text += f"""

🌐 当前模式: 全局共享
   - 所有用户共享管理员预设的 Openlist 服务器连接，无需单独配置。"""

        help_text += f"""

💡 通用提示:
1.  路径区分大小写，以 `/` 开头表示根目录。
2.  `ls` 获取链接，`download` 直接发送文件。
3.  管理员可在机器人后台的插件配置页面调整全局设置。"""

        yield event.plain_result(help_text)
