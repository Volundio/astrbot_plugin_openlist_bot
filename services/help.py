from astrbot.api.event import AstrMessageEvent

from .base import PluginService


class HelpService(PluginService):
    """Help service."""

    async def help_command(self, event: AstrMessageEvent):
        """显示帮助信息"""
        user_id = event.get_sender_id()
        user_config = self.get_user_config(user_id)
        global_cfg = self.get_global_config()
        is_user_auth_mode = bool(global_cfg.get("require_user_auth", False))

        help_text = """📚 OpenList 助手帮助
💡 您也可以使用别名 `网盘` 代替 `ol`。

---
核心导航指令
---
▶️ `ol ls [路径|序号]`
   - 浏览目录: 列出内容，若文件过多会自动分页。
     - 示例: `ol ls` / `ol ls /movies`
   - 进入子目录: `ol ls 1` (如果1是目录)
   - 获取链接: `ol ls 2` (如果2是文件，以 txt 附件发送直链)

▶️ `ol next` / `ol prev` - 下一页 / 上一页
▶️ `ol quit` - 返回上级目录（搜索后可执行本指令回到原目录）

---
文件操作指令
---
📥 `ol download <路径|序号>`
   - 直接下载文件并作为附件发送。
     - 示例: `ol download 3` / `ol download /docs/report.pdf`

🔍 `ol search <关键词> [路径]`
   - 搜索文件（依赖 OpenList 索引）。
     - 示例: `ol search 年度报告`

ℹ️ `ol info <路径|序号>`
   - 查看文件或目录的详细信息，支持列表序号。
     - 示例: `ol info 1` / `ol info /docs/report.pdf`

👁️ `ol preview <路径|序号>`
   - 预览文本文件内容或查看压缩包目录。
     - 示例: `ol preview 1` / `ol preview /data/config.txt`

📂 `ol mkdir <名称|路径>` - 新建文件夹
🗑️ `ol rm <路径|序号>` - 删除文件或文件夹（谨慎操作）

📤 `ol upload [目标路径] [闪传链接]`
   - 支持**群聊文件、私聊离线文件、图片/视频、QQ 闪传多文件集**（保留子目录结构）转存。
   - 方式一: 先发附件或闪传卡片，5 分钟内发 `ol upload` 或 `ol upload /目标目录`
   - 方式二: 直接发 `ol upload /目标目录 https://qfile.qq.com/q/xxxx`

📦 `ol backup [/目标路径] [@群号|private:@QQ号]`
   - 群聊中默认递归备份当前群文件与近期闪传；私聊中默认备份当前私聊文件与闪传。
   - 示例: `ol backup` / `ol backup /群备份 @123456` / `ol backup retry`

🔄 `ol autobackup <show|enable|disable|cancel> [@群号|private:@QQ号] [/路径]`
   - 配置群聊或私聊自动备份（支持群文件、私聊离线文件、QQ 闪传自动入盘）。
   - 群聊开启: `ol autobackup enable` 或 `ol autobackup enable @123456 /backup`
   - 私聊开启: 在私聊发 `ol autobackup enable` 或管理员发 `ol autobackup enable private:@987654 /backup`
   - 取消首次全量备份: `ol autobackup cancel [@群号|private:@QQ号]`

🚚 `ol restore <路径> [@群号]`
   - 将 OpenList 路径中的文件恢复到目标群（保持一级目录结构）或当前私聊。
   - 示例: `ol restore /backup/group_123456` / `ol restore /docs @987654`

---
插件配置指令
---
⚙️ `ol config setup` - 查看快速配置向导
⚙️ `ol config show` - 显示当前生效配置
⚙️ `ol config set <键> <值>` - 修改配置项（全局共享模式下管理员执行会直接更新全局配置）
⚙️ `ol config unset <键>` - 重置配置项 / 恢复继承全局配置
⚙️ `ol config test` - 测试与 OpenList 服务器的连接
⚙️ `ol config clear_cache` - 清除文件列表缓存"""

        if is_user_auth_mode:
            help_text += """

👤 当前模式: 用户独立认证
   - 每位用户可使用 `ol config setup` 单独配置个人 OpenList 账户，未修改项继承全局设置。"""
            if not self._validate_config(user_config):
                help_text += """

⚠️ 操作提示
   您尚未完成配置，请发送 `ol config setup` 开始配置向导。"""
        else:
            help_text += """

🌐 当前模式: 全局共享（默认）
   - 所有用户共享全局 OpenList 服务器连接；管理员可通过 WebUI 或 `ol config set` 直接修改全局设置。"""

        yield event.plain_result(help_text)
