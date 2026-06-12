<div align="center">

# <img src="https://raw.githubusercontent.com/OpenListTeam/Logo/main/logo.svg" width="32" height="32" style="vertical-align: middle;"> OpenList 助手

<i>🚀 跨越终端，触手可及的网盘管理专家</i>

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

</div>

## ✨ 简介

一款为 [**AstrBot**](https://github.com/AstrBotDevs/AstrBot) 设计的 [**OpenList**](https://github.com/OpenListTeam/OpenList) 文件管理插件。它将强大的网盘管理功能带入聊天界面，让您可以像聊天一样轻松列出、搜索、下载、上传和备份文件，支持智能导航、文件预览、群文件自动备份、失败重试等多种高级特性。

本项目参考 [**Foolllll-J/astrbot_plugin_openlistfile**](https://github.com/Foolllll-J/astrbot_plugin_openlistfile) 进行重构二次开发，在保留原有业务能力的基础上，重新梳理了项目结构、配置逻辑、下载/上传/备份流程和异常处理。

---

## ✨ 功能特性

* 📁 **智能导航** - 序号快速导航，支持进入文件夹、返回上级目录和分页浏览，并按会话隔离状态。
* 📥 **直接下载** - 通过 OpenList API 获取真实下载地址，并使用 AstrBot `File` 组件发送文件。
* 🔗 **链接获取** - 下载链接会以 txt 附件发送，避免长链接被平台自动转成图片。
* 📤 **文件上传** - 先发送图片、视频或文件，再使用 `ol upload` 上传同会话最近 5 分钟内的附件消息。
* 🔍 **文件搜索** - 支持在指定目录中搜索目标文件。
* 📋 **文件信息** - 查看文件详细信息，并可附带下载链接 txt 附件。
* 📦 **备份恢复** - 支持群文件递归备份、失败重试、跳过重复文件、同名冲突改名和恢复文件到群。
* 🔄 **自动备份** - 支持群文件新增自动备份，开启时会立即执行一次全量备份。
* 👁️ **内容预览** - 支持文本文件预览和压缩包内容查看。
* ⚙️ **灵活设置** - 支持全局设置和用户独立设置两种模式。
* 🧰 **传输调优** - 上传分块、超时、重试和诊断日志均可配置。
* 🎨 **美化显示** - 智能文件图标，直观的信息展示。

---

## 🔧 设置方式

### 🔌 两种设置模式

#### 1. 全局设置模式（默认推荐）

* 所有用户共享同一个 OpenList 服务器连接。
* 管理员在 AstrBot WebUI 中统一设置。
* 适合团队共享同一个文件服务器的场景。

#### 2. 用户独立设置模式

* 每个用户拥有独立的 OpenList 连接设置。
* 用户设置互不干扰，支持连接不同的 OpenList 服务器。
* 启用 `require_user_auth` 后，每位用户需要自行配置连接信息。

### 🖥️ WebUI 全局设置

首次加载后，请在 AstrBot 后台 -> 插件 页面找到本插件进行设置。

常用全局配置项：

| 配置项 | 说明 |
| :--- | :--- |
| `default_openlist_url` | OpenList API 地址（机器人访问）。普通部署直接填公网地址；Docker/内网部署可填内网地址 |
| `public_openlist_url` | 对外下载地址（可选）。仅 API 地址为内网、但发给用户的链接需要公网访问时填写 |
| `default_username` | 默认用户名，留空表示匿名访问 |
| `default_password` | 默认密码 |
| `default_token` | 默认访问 Token，优先级高于用户名密码 |
| `fixed_base_directory` | 路径前缀修正（高级，可选）。仅列表路径与真实下载路径不一致时填写 |
| `require_user_auth` | 是否要求每个用户独立配置 |
| `allowed_extensions` | 允许的文件扩展名，留空表示不限制 |
| `backup_default_path` | 手动备份默认目录 |
| `autobackup_default_path` | 自动备份默认目录 |
| `autobackup_groups` | 启用自动备份的群号列表 |

> 如果只配置了用户名和密码，插件会自动登录 OpenList 并获取 Token；不需要手动填写 Token。
>
> 大多数公网单地址部署只需要填写 `default_openlist_url`，`public_openlist_url` 和 `fixed_base_directory` 都可以留空。

### 💬 用户设置（聊天界面）

#### 快速设置向导

```
ol config setup
```

#### 手动设置

**Bash**

```
# 显示当前设置
ol config show

# 设置 OpenList API 地址（机器人访问）
ol config set openlist_url http://your-server:5244

# 设置用户名（可选）
ol config set username your_username

# 设置密码（可选）
ol config set password your_password

# 设置访问 Token（可选，优先级高于用户名密码）
ol config set token your_token

# 设置对外下载地址（可选；普通部署留空）
ol config set public_openlist_url https://your-public-domain

# 设置路径前缀修正（高级可选；普通部署留空）
ol config set fixed_base_directory /夸克

# 设置允许的文件扩展名（留空表示不限制）
ol config set allowed_extensions .txt,.pdf,.mp4

# 设置最大下载 / 上传 / 预览大小（MB，0 表示不限制）
ol config set max_download_size 50
ol config set max_upload_size 100
ol config set max_preview_size 10

# 测试连接
ol config test

# 清理文件列表缓存
ol config clear_cache
```

#### 传输与备份调优

**Bash**

```
# 普通上传单文件重试：总尝试次数 3，每次间隔 5 秒
ol config set upload_retry_attempts 3
ol config set upload_retry_delay 5

# 传输调优
ol config set upload_chunk_size_mb 4
ol config set upload_progress_step_mb 64
ol config set upstream_connect_timeout 60
ol config set upstream_read_timeout 180
ol config set openlist_connect_timeout 30
ol config set openlist_upload_response_timeout 3000

# 开启上传/下载/DNS 诊断日志（默认关闭）
ol config set debug_transfer_logging true

# 设置手动备份默认目录（支持 {group_id} 占位符）
ol config set backup_default_path /backup/group_{group_id}

# 备份时跳过同目录内同名且大小一致的文件（默认开启）
# 同目录同名但大小不同的文件会自动改名备份，避免覆盖
ol config set backup_skip_existing true

# 设置备份单文件重试：总尝试次数 3，每次间隔 5 秒
ol config set backup_retry_attempts 3
ol config set backup_retry_delay 5
```

---

## 📖 使用指南

### 📝 指令列表

插件支持主指令 `ol` 及其别名 `网盘`。以下是常用指令及其对应的中文别名：

> 发送 `ol` 或 `ol help` 可查看完整帮助。子命令缺少参数或参数格式错误时，插件会返回对应的简短用法、示例和必要提示。

| 指令 | 中文别名 | 指令示例 | 说明 |
| :--- | :--- | :--- | :--- |
| `ol ls` | `网盘 列表`, `网盘 直链` | `ol ls /` | 列出文件/获取下载链接（txt 附件） |
| `ol config` | `网盘 配置` | `ol config show` | 配置插件参数 |
| `ol next` | `网盘 下一页` | `ol next` | 列表翻页（下一页） |
| `ol prev` | `网盘 上一页` | `ol prev` | 列表翻页（上一页） |
| `ol search` | `网盘 搜索` | `ol search "关键词"` | 搜索文件 |
| `ol info` | `网盘 信息` | `ol info /path/file` | 查看文件/目录详细信息 |
| `ol download` | `网盘 下载` | `ol download 1` | 直接下载文件并发送 |
| `ol upload` | `网盘 上传` | `ol upload /path` | 上传最近附件消息中的图片、视频或文件 |
| `ol backup` | `网盘 备份` | `ol backup /path @群号` | 手动备份群文件 |
| `ol autobackup` | `网盘 自动备份` | `ol autobackup enable` | 配置自动备份，支持取消首次全量备份 |
| `ol restore` | `网盘 恢复` | `ol restore /path @群号` | 从网盘恢复文件 |
| `ol preview` | `网盘 预览` | `ol preview 1` | 预览文本或压缩包 |
| `ol rm` | `网盘 删除` | `ol rm 1` | 删除文件或目录 |
| `ol mkdir` | `网盘 新建` | `ol mkdir folder` | 创建新目录 |
| `ol quit` | `网盘 上一级`, `网盘 返回` | `ol quit` | 返回上级目录 |
| `ol help` | `网盘 帮助` | `ol help` | 显示帮助信息 |

### 📂 浏览与导航

**Bash**

```
# 查看帮助文档
ol help

# 列出根目录文件
ol ls /

# 使用序号进入子目录
ol ls 1          # 如果 1 号是目录，则进入该目录

# 如果序号对应文件，则获取下载链接 txt 附件
ol ls 2

# 翻页
ol next          # 查看下一页
ol prev          # 查看上一页

# 返回上级目录
ol quit

# 路径方式
ol ls /movies    # 列出 /movies 目录的内容
```

### 🔍 文件搜索与信息

**Bash**

```
# 搜索文件（注意：依赖服务器索引，结果可能非最新）
ol search "年度报告"

# 在指定目录搜索
ol search "年度报告" /documents

# 查看文件信息
ol info /movies/Inception.mkv

# 预览文件内容（支持文本和压缩包）
ol preview 2
ol preview /data/config.txt

# 新建文件夹
ol mkdir my_folder
ol mkdir /data/new_dir

# 删除文件或文件夹（谨慎操作）
ol rm 3
ol rm /temp/stale_file.txt
```

### 📥 下载与上传

**Bash**

```
# 方式一：获取下载链接（txt 附件）
ol ls 2
ol ls /movies/Inception.mkv

# 方式二：直接下载文件
ol download 2
ol download /movies/Inception.mkv

# 先发送图片、视频或文件，再上传到当前目录
ol upload

# 先发送图片、视频或文件，再上传到指定目录
ol upload /movies

# 先发送图片、视频或文件，再上传到当前目录下的子目录
ol upload clips
```

说明：

* `ol ls` 获取文件链接时，会将下载链接写入 txt 附件发送，避免长文本被平台转成图片。
* `ol download` 会先通过 OpenList API 获取真实下载链接，再下载到本地临时文件并用 `File` 组件发送。
* `ol upload` 使用同会话、同一发送者 5 分钟内最近一条附件消息；不依赖 QQ/OneBot 的引用回复解析。
* 最近附件缓存只保存消息元数据和 URL，不保存文件内容；缓存最多保留 500 个会话条目，并忽略机器人自己发出的消息。
* 上传使用平台提供的文件 URL 进行流式中转；群文件没有 URL 时会尝试通过 OneBot 群文件接口获取下载地址。
* 用户上传的 URL 流式中转多次失败后，会改用本地临时文件备用上传：先完整下载到 `upload_temp`，校验大小后再上传；无论成功或失败，都会清理临时文件。
* 浏览列表、分页和序号操作按会话隔离；同一用户在不同群聊或私聊中使用不会串用序号状态。

### 📦 备份与恢复

**Bash**

```
# 手动备份群文件到 OpenList
# 用法：ol backup [@群号] [/目标路径]
ol backup @123456789 /backup/group_files
ol backup /my_backup
ol backup

# 只重试上次备份失败的文件
ol backup retry

# 自动备份设置
ol autobackup
ol autobackup enable
ol autobackup enable @123456789 /backup/group_{group_id}
ol autobackup disable
ol autobackup disable @123456789
ol autobackup cancel
ol autobackup cancel @123456789

# 从 OpenList 恢复文件到群
# 用法：ol restore /来源路径 [@目标群号]
ol restore /backup/important_file @123456789
ol restore /backup/folder
```

说明：

* `ol backup` 未指定路径时使用 `backup_default_path`。
* `ol backup` 支持 `@群号` 和 `/目标路径` 两个参数，顺序不限。
* `backup_default_path`、`autobackup_default_path` 支持 `{group_id}`、`{gid}`、`{group}` 占位符。
* 开始上传前会先检查并创建本轮备份涉及的 OpenList 目标目录。
* 全量备份会显式请求最多 10000 个根目录或文件夹条目，避免 NapCat 群文件列表接口默认只返回 50 项；扫描消息会分别显示接口返回数、过滤数、去重数和最终待备份数。
* 备份默认只会跳过同目录中同名且大小一致的文件，不会跨目录检查重复文件。
* 判断已存在文件时会刷新 OpenList 目标目录；如果目录列表获取失败，本轮该目录不会反复请求列表，会跳过已存在检测并继续备份。
* 同目录同名但大小不同的文件会自动改名备份，避免覆盖。例如 `file.mp4` 会保存为 `file [文件标识].mp4`。
* 群文件 URL 流式中转多次失败后，会改用本地临时文件备用上传：先完整下载到 `backup_temp`，校验大小后再上传；备用上传也会按 `backup_retry_attempts` 重试并重新获取群文件 URL，无论成功或失败都会清理临时文件。
* 如果 QQ/NapCat 返回群文件已不存在（`code=-103`），插件会直接记录失败原因，不会进入本地临时文件备用上传。
* 备份失败项会保存到临时失败清单，结束消息会列出失败文件和原因，使用 `ol backup retry` 可只重试失败项。
* `ol autobackup enable` 开启自动备份后，会立即执行一次全量备份。
* 后续群文件上传会通过 OneBot `group_upload` 通知、普通消息 `file` 段或 AstrBot `File` 组件触发自动备份；如果事件提供 `file_id`，插件会直接获取群文件下载 URL 并上传。
* `ol autobackup cancel [@群号]` 可中途取消 `enable` 触发的首次全量备份。
* `ol autobackup disable` 只会关闭后续自动备份；如果首次全量备份正在执行，需要另行发送 `cancel`。
* 自动备份配置需要群主或管理员权限。
* 当前群内备份/恢复可直接操作；如果指定 `@其他群号`，需要您是目标群的群主或管理员。
* `ol restore /` 支持从 OpenList 根目录恢复文件。

---

## 📜 项目说明

### ⚙️ 配置说明

首次加载后，请在 AstrBot 后台 -> 插件 页面找到本插件进行设置。所有配置项都有详细的说明和提示。

### 📂 文件存储结构

```
data/plugins_data/openlist/
├── global_config.json          # 全局设置文件
├── users/                      # 用户设置目录
│   ├── user1.json              # 用户 1 的设置
│   ├── user2.json              # 用户 2 的设置
│   └── ...
├── cache/                      # 文件列表缓存目录
│   ├── abc123.json             # 缓存文件 (MD5 命名)
│   └── ...
├── downloads/                  # 临时下载/恢复目录
│   ├── user123_xxx_file.txt    # 临时下载文件
│   └── ...
├── links/                      # 下载链接 txt 临时目录
├── temp_preview/               # 文件预览临时目录
├── upload_temp/                # 用户上传备用上传临时目录
├── backup_temp/                # 群文件备用上传临时目录
└── backup_retry/               # 备份失败重试清单
```

### 🧱 重构后的源码结构

```
astrbot_plugin_openlist_bot/
├── main.py                     # 插件入口、命令注册和通用工具
├── lib/
│   ├── cache.py                # 文件列表缓存
│   ├── client.py               # OpenList API 客户端
│   └── config.py               # 配置默认值和校验规则
└── services/
    ├── base.py                 # 服务基类与共享临时文件工具
    ├── backup.py               # 备份与自动备份任务编排
    ├── backup_scanner.py       # 群文件扫描、过滤和去重
    ├── backup_target.py        # 备份目标路径、同名处理和已存在判断
    ├── backup_uploader.py      # 群文件上传重试和备用上传
    ├── browse.py               # 浏览、搜索、删除、新建
    ├── config_command.py       # 配置命令
    ├── download.py             # 下载与链接发送
    ├── preview.py              # 文件预览
    ├── restore.py              # 恢复文件
    ├── upload.py               # 最近附件上传
    └── help.py                 # 帮助信息
```

AstrBot 插件要求入口类和命令注册保留在 `main.py` 中，因此重构后仍由 `main.py` 注册命令，具体业务逻辑拆分到 `services/`。

---

## 🛠️ 故障排除

### ❓ 常见问题

**Q: 提示“❌ 请先配置 OpenList 连接信息”**

A: 这是因为您处于“用户独立设置模式”，或全局 OpenList 地址尚未设置。请运行 `ol config setup` 设置向导，或在 WebUI 中配置默认 OpenList 服务器地址。

**Q: 只配置了账号密码，没有配置 Token，可以用吗？**

A: 可以。插件会在创建 OpenList 客户端时自动登录并获取 Token。Token 是可选项，并且优先级高于用户名密码。

**Q: 为什么下载链接要作为 txt 附件发送？**

A: AstrBot 配置会把长文本消息转换成图片，导致直链不可复制。插件会将链接写入 txt 附件发送，避免这个问题。

**Q: 只发送 `ol` 时会显示什么？**

A: 插件会直接显示整理过的帮助信息，避免 AstrBot 指令组默认树形提示过长、参数类型噪声过多的问题。

**Q: `fixed_base_directory` 这个参数有用吗？**

A: 有用，但它是高级兼容项，普通部署请留空。它只用于修正 `/d` 下载链接和 `/api/fs/link` 真实下载链接的路径前缀。典型场景是：OpenList 列表里看到的文件路径是 `/video/a.mp4`，但真实下载接口要求的路径是 `/夸克/video/a.mp4`，这时才需要填写 `/夸克`。它不是默认浏览目录，也不是备份目录；填错会导致下载链接或直接下载失败。

**Q: 为什么 `search` 搜不到文件，但 `ls` 能看到？**

A: 这是因为 `search` 依赖服务器的**搜索索引**，而 `ls` 是实时列出文件。如果文件是新添加的，服务器索引可能尚未更新。请联系您的 OpenList 服务器管理员，在后台对相应存储**手动更新索引**。

**Q: `/api/fs/link` 返回 403 或提示不是管理员怎么办？**

A: 部分 OpenList 权限配置要求管理员才能调用真实下载链接接口。请确认插件配置的账号具有对应权限；如果只需要链接，可使用 `ol ls 文件路径` 获取普通下载链接 txt 附件。

**Q: 自动备份提示“权限不足”**

A: `ol autobackup enable/disable/cancel` 需要群主或管理员权限。指定其他群号时，还需要您是目标群群主或管理员。

**Q: 连接测试失败**

A: 请检查：

1. 服务器地址是否正确（包含 `http://` 或 `https://`）；
2. AstrBot 所在设备网络是否能访问到该地址；
3. 用户名、密码或 Token 是否正确；
4. 如果使用公网下载链接，请确认 `public_openlist_url` 是否可访问。

### ✅ 设置验证

使用以下指令验证设置：

**Bash**

```
ol config show    # 查看当前设置
ol config test    # 测试连接
ol ls /           # 测试文件列表
```

---

## 🔄 版本历史

详见 [CHANGELOG.md](./CHANGELOG.md)。

---

## 🙏 致谢

本项目参考 [astrbot_plugin_openlistfile](https://github.com/Foolllll-J/astrbot_plugin_openlistfile) 进行重构二次开发，在此向原作者表示衷心感谢！


---

## ❤️ 支持

* [AstrBot 帮助文档](https://docs.astrbot.app/)
* 如果您在使用中遇到问题，欢迎在仓库提交 Issue。

---

<div align="center">

**如果本插件对你有帮助，欢迎点个 ⭐ Star 支持一下！**

</div>
