<p align="center">
	<img src="logo.png" alt="nanobot GUI Logo" width="160" />
</p>

<h1 align="center">nanobot GUI</h1>

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![PySide6](https://img.shields.io/badge/UI-PySide6-41CD52?logo=qt&logoColor=white)
![Platforms](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-555)

`nanobot GUI` 是一个基于 PySide6 的桌面界面程序，免安装直接使用，包含Skill，MCP，Channel等管理功能。

## 项目目的

- 提供一个轻量、可视化的桌面客户端，降低对 nanobot 相关能力的使用门槛。
- 通过统一界面管理 Skill、MCP 与 Channel，减少多端切换与手动配置成本。

## 核心功能

- Skill 管理：查看、维护与组织可用技能。
- MCP 管理：集中管理 MCP 配置与连接能力。
- Channel 管理：统一处理不同 Channel 的接入与使用。
- 跨平台运行：支持 macOS、Linux、Windows。

## 快速开始

启动后程序会自动完成初始化（创建/刷新配置与工作区模板），主界面左侧包含：`Gateway`、`Skills`、`Providers`、`Channels`、`MCP`、`Cron`、`Others`。

### 1) 启动 nanobot

1. 打开左侧 `Gateway` 页面。
2. 设置端口（默认读取当前配置）。
3. 点击 `Start Gateway`。
4. 在日志区确认出现启动日志，状态变为运行中。

### 2) 配置核心功能

- `Providers`：填写各 Provider 的 `apiKey` / `apiBase` 和默认模型，点击 `Save All Providers`。
- `Channels`：按 JSON 编辑渠道配置，点击 `Format JSON` 校验后点击 `Save JSON`。
- `MCP`：添加或修改 `name`、`command`、`args`、`env(JSON)`、`url`，点击 `Save`。
- `Skills`：点击 `Refresh` 查看技能，或输入名称后点击 `Create in workspace/skills` 新建技能目录。
- `Others`：配置工作区路径、温度、工具限制、自动启动等，点击 `Save Advanced Settings`。

### 3) 配置定时任务

在 `Cron` 页面可添加、执行、启停与删除任务；任务数据保存在本地数据目录。

## 开发与打包

<details>
<summary>开发和打包修改内容（点击展开）</summary>

### 本地开发

```bash
uv sync
uv run python app.py
```

### 使用 PyInstaller 打包

> 当前仓库已提供两个 spec 文件：
>
> - `app.spec`：Windows / Linux
> - `app-macos.spec`：macOS

### 1) 安装 PyInstaller（如未安装）

```bash
uv add pyinstaller
```

### 2) 打包

Windows / Linux：

```bash
uv run pyinstaller app.spec
```

macOS：

```bash
uv run pyinstaller app-macos.spec
```

产物输出目录：`dist/`


## 目录结构

```text
.
├── .github/
│   └── workflows/
│       └── release-pyinstaller.yml
├── app.py
├── app.spec
├── app-macos.spec
├── pyproject.toml
├── scripts/
│   └── zip_release_asset.py
├── LICENSE
└── README.md
```


</details>

## License

MIT
