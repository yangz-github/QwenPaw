# 文档：https://qwenpaw.agentscope.io/docs/config

## 方式一：pip 安装
如果你更习惯自行管理 Python 环境（需 Python >= 3.10, < 3.14）：

复制
pip install qwenpaw
可选：先创建并激活虚拟环境再安装（python -m venv .venv，Linux/macOS 下 source .venv/bin/activate，Windows 下 .venv\Scripts\Activate.ps1）。安装后会提供 qwenpaw 命令。

然后按下方 步骤二：初始化 和 步骤三：启动服务 操作。

可选：先创建并激活虚拟环境再安装（python -m venv .venv，Linux/macOS 下 source .venv/bin/activate，Windows 下 .venv\Scripts\Activate.ps1）。安装后会提供 qwenpaw 命令。

然后按下方 步骤二：初始化 和 步骤三：启动服务 操作。

## 步骤二：初始化
在工作目录（默认 ~/.qwenpaw）下生成 config.json 与 HEARTBEAT.md。两种方式：
快速用默认配置（不交互，适合先跑起来再改配置）：

复制
qwenpaw init --defaults
交互式初始化（按提示填写心跳间隔、投递目标、活跃时段，并可顺带配置频道与 Skills）：

复制
qwenpaw init
详见 CLI - 快速上手。
若已有配置想覆盖，可使用 qwenpaw init --force（会提示确认）。 初始化后若尚未启用频道，接入钉钉、飞书、QQ 等需在 频道配置 中按文档填写。

## 步骤三：启动服务

复制
qwenpaw app
服务默认监听 127.0.0.1:8088。若已配置频道，QwenPaw 会在对应 app 内回复；若尚未配置，也可先完成本节再前往频道配置。