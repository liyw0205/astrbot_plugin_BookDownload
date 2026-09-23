# astrbot_plugin_BookDownload

AstrBot 图书下载插件的基础仓库。

## 当前状态

仓库已包含可被 AstrBot 识别的最小插件入口。具体的书源、检索方式、版权策略、下载格式和存储位置尚未约定，因此当前版本不实现实际下载功能。

## 目录结构

```text
.
├── _conf_schema.json  # 插件配置 schema
├── main.py            # AstrBot 插件入口
├── metadata.yaml      # 插件元数据
└── __init__.py
```

## 开发

将此目录放入 AstrBot 的插件目录后即可被加载。后续新增命令或配置时，请同步更新 `README.md`、`metadata.yaml` 和 `_conf_schema.json`。

