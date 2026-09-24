# 本子搜索下载（astrbot_plugin_BookDownload）

AstrBot 本子搜索下载插件，提供 NHentai / E-Hentai 自动文图搜索、作品下载与对应的 LLM 工具。

## 功能

- NHentai/E-Hentai 搜索：`nh搜索`、`eh搜索` 自动判断文字关键词或消息/引用图片。
- 搜索结果：支持将封面和作品信息渲染为亮色图卡、图文发送或纯文字发送；中文字体随插件提供。
- 作品下载：按作品 ID 或完整链接下载，支持 PDF、压缩包、逐张图片和每十张一张的长图。
- 作品详情：`nh查看`、`eh查看` 渲染亮色详情卡，展示封面、标题、Tags、Languages、Pages、Artists、Groups 及前 6 页预览。
- 每日推送：配置页开启后按本机时区定时随机选择标签搜索并推送搜索图卡，数量遵循最大结果数，默认每天 12:05；默认关闭。
- LLM 文搜图：LLM 根据对话请求调用 `book_text_search`，返回作品标题、来源、封面和链接。
- 自然语言兜底：被 @ 或唤醒后发送“搜一下碧蓝航线的本子”等明确请求时，直接使用同一文搜流程，避免模型误判为闲聊。
- LLM 图搜图：LLM 调用 `book_image_search`，优先使用当前/引用消息图片，也可提供公网 HTTPS 图片 URL。
- 反搜引擎：`eh搜索` 使用 E-Hentai 反搜；`nh搜索` 图片模式使用 SauceNAO API Key。
- AstrBot 内置管理页：可在插件页面读取和保存配置，无需编辑 JSON。

## 指令

发送 `/本子帮助` 可查看指令和调用示例。

```text
/本子帮助
/nh搜索 <关键词|附图> [页码] [图卡|图文|文字]
/eh搜索 <关键词|附图> [页码] [图卡|图文|文字]
/nh下载 <作品ID或完整链接> [pdf|压缩包|图片|长图]
/eh下载 <作品ID或完整链接> [pdf|压缩包|图片|长图]
/nh查看 <作品ID或完整链接>
/eh查看 <作品ID或完整链接>
/今日本子
```

搜索指令根据消息或引用中是否有图片自动选择反向搜图或文字搜索。NHentai 图片搜索使用 SauceNAO（需要配置 API Key），E-Hentai 图片搜索使用 E-Hentai 反搜。群聊下载图片/长图会私发给发起者。

## 配置

在 AstrBot WebUI 插件配置中设置：

- `reverse_engine`: LLM 图片搜索的默认反搜引擎，默认 `ehentai`。
- `saucenao_api_key`: `nh搜索` 图片模式必需。图片会上传至 SauceNAO。
- `ehentai_site`: `e-hentai` 或 `exhentai`。ExHentai 通常需要有效的 Cookie。
- `ehentai_cookie`: 可选 Cookie 字符串；不要发送到聊天或提交到代码仓库。
- `proxy_url`: 可选代理地址，支持 `http://`、`https://`、`socks5://` 和 `socks5h://`。
- `max_results`: 每次返回数量，范围 1-12。
- `tag_filter_enabled`: 标签正则过滤开关，默认开启；搜索、每日推送和今日本子都会应用。
- `tag_filter_regex`: 命中作品 Tags 即排除，默认 `yaoi|tomgirl|futanari|guro|scat|vore|bestiality`，可自行修改为正则。
- `max_image_mb`: 图片输入上限，范围 1-20 MB。
- `default_text_source`: LLM 文搜工具未指定来源时使用的默认来源；命令搜索由 `nh搜索` / `eh搜索` 指定。
- `result_display_mode`: 搜索结果默认展示方式，可选 `card`（图卡）、`image_text`（图文）、`text`（文字）。
- `download_format`: 默认下载发送方式，可选 `pdf`、`archive`、`images`、`long_image`。
- `download_max_pages` / `download_max_mb`: 单次下载的页数和大小上限。
- `llm_tools_enabled`: 启用/关闭两个 LLM 搜索工具。
- `daily_push_enabled`: 每日推送开关，默认关闭；关闭时不会创建定时任务。
- `daily_push_time`: 每日推送时间，格式 `HH:MM`，默认 `12:05`。
- `daily_push_source`: 每日推送来源，可选 `nhentai`、`ehentai`、`all`。
- `daily_push_tag_regex`: 标签候选正则，例如 `blowjob|stockings|lolicon`，每天随机选择一个候选。
- `daily_push_language`: Languages 筛选值，例如 `chinese`；每日推送始终使用此筛选。
- `language_filter_enabled`: 开启后普通搜索、LLM 搜索和反向搜图也会按 `daily_push_language` 校验详情 Languages。
- `daily_push_group_ids`: 推送群号，多个群号用 `|` 分隔，例如 `123456|654321`。
- `daily_push_friend_ids`: 推送私聊/Q号，多个号码用 `|` 分隔，例如 `123456|654321`。
- `daily_push_platform`: 平台标识，QQ OneBot 通常为 `aiocqhttp`。
- `今日本子`: 立即触发一次推送，只发送到当前触发指令的会话；即使每日推送开关关闭也可以手动触发，不读取定时推送目标配置。
- `daily_push_target`: 旧版完整会话配置，仅用于兼容已有配置。

## 隐私与运行要求

反向搜索会将图片发给对应搜索服务：`nh搜索` 使用 SauceNAO，`eh搜索` 使用 E-Hentai；LLM 图片工具使用 `reverse_engine` 选择的服务。LLM 图片 URL 参数仅接受公网 HTTPS 地址，不接受本地路径或内网地址。NHentai / E-Hentai 搜索结果面向成人用户，请仅在适当的 18+ 会话中启用。

插件依赖 `aiohttp`、`aiohttp-socks`、`beautifulsoup4` 和 `Pillow`，由 AstrBot 插件依赖管理安装。安装后可从 AstrBot 插件管理页打开 `本子搜索下载配置` 页面；页面保存配置会立即更新当前进程。
