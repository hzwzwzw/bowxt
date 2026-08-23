# bowxt

[![CI](https://github.com/hzwzwzw/bowxt/actions/workflows/ci.yml/badge.svg)](https://github.com/hzwzwzw/bowxt/actions/workflows/ci.yml)

`bowxt`（wx + box + bot）是一个面向 Linux 的微信消息收发框架。它在 Docker 中运行官方
Linux 微信，通过 AT-SPI 读取可见控件树，并用真实键鼠事件完成界面操作，同时提供 Web IM、
HTTP API 和面向 Agent 的 Python 客户端。

项目不接入微信私有协议，也不要求修改微信进程。

## 功能

- 通过 noVNC 或 VNC 查看和操作容器中的官方 Linux 微信；
- 读取、发送联系人和群聊文字消息，支持群发送人、`@我`、富文本 `@成员` 和图片读取；
- 在“轮询 / 新消息唤醒 / 暂停”三种同步模式间切换；
- 多会话持久化、消息去重、异步发送队列和发送状态跟踪；
- Web IM 对话、会话添加、未读提示、历史翻页、图片显示和实时更新；
- 不登录微信也可创建模拟联系人或群聊，构造文字、图片、发送人和时间并触发正常 Agent 事件；
- 面向 Agent 的独立消费游标、批量领取、租约、ACK/NACK、崩溃重投和幂等发送；
- 独立的 Agent 日志通道，支持持久化、实时跟随、级别过滤、搜索和复制。
- Agent 可发布受限的声明式自定义面板，在 Web IM 中安全展示运行状态和业务层级数据。

## 快速开始

要求：Linux amd64、Docker Engine，以及可在手机端确认登录的微信账号。

```bash
git clone https://github.com/hzwzwzw/bowxt.git
cd bowxt
cp .env.example .env
./scripts/init.sh
```

启动后访问：

- Web IM：<http://127.0.0.1:8787/>
- 微信 noVNC：<http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale&reconnect=1&reconnect_delay=1000>
- 原生 VNC：`127.0.0.1:5900`

首次启动时在 noVNC 中登录微信并用手机确认。登录状态、微信数据和 bowxt 数据库保存在
`bowxt-home` Docker volume 中，普通停止和容器更新不会删除它。noVNC 已启用自动重连，可处理
登录窗口切换为微信主窗口时的短暂断开。

端口默认只绑定宿主机 `127.0.0.1`。远程访问建议使用 SSH 隧道：

```bash
ssh -L 8787:127.0.0.1:8787 -L 6080:127.0.0.1:6080 USER@DOCKER_HOST
```

常用管理命令：

```bash
./manage.sh build
./manage.sh up
./manage.sh ready
./manage.sh doctor
./manage.sh unit
./manage.sh logs
./manage.sh down       # 保留登录和消息数据
```

## 配置

复制 `.env.example` 为 `.env` 后按需修改：

| 配置 | 说明 |
|---|---|
| `BOWXT_SYNC_MODE` | 启动模式：`polling`、`unread` 或 `paused` |
| `BOWXT_POLL_GAP` | 会话轮询或未读检查间隔 |
| `BOWXT_ACTION_DELAY` | 普通键鼠操作间隔 |
| `BOWXT_UIA_SENDER` | 是否通过可见资料卡补全群发送人 |
| `BOWXT_MY_NAMES` | 当前账号在群内可能被 `@` 的名称，多个值用逗号分隔 |
| `VNC_SCOPE` | `window` 只转发微信，`desktop` 转发完整桌面 |
| `WEB_PORT` / `NOVNC_PORT` / `VNC_PORT` | 宿主机监听端口 |

例如：

```dotenv
BOWXT_SYNC_MODE=unread
BOWXT_MY_NAMES=kirotta,bowxt
VNC_SCOPE=window
```

`.env` 是本机配置，不会提交到 Git，也不会复制进 Docker 镜像。显式设置的环境变量可以临时
覆盖其中的值。

## Web IM

Web IM 提供三个页面：

- **对话**：点击左上角 `+` 添加联系人或群聊；支持会话切换、未读红点、历史消息翻页、图片、
  乐观发送气泡和异步发送状态。发送任务在后台串行操作微信，输入框不会等待上一条消息完成。
- **Agent 管理**：从管理员安装的插件创建多个隔离实例，在 Web IM 中配置、启动、停止、重启，
  并按实例查看日志。顶部只保留“对话”和“Agent”；日志从实例卡片弹窗打开。每个实例自动使用
  自己的实例 ID 作为 durable consumer。
- **会话权限**：每个 Agent 分别配置读、写范围，支持全部会话、指定会话、正则白名单和正则
  黑名单。实例卡片展示策略允许范围，以及 consumer 最近一次实际领取的会话。

左下角可以切换同步模式并调整轮询、键鼠间隔：

- `polling`：按顺序检查全部已启用会话；
- `unread`：优先根据会话列表的新消息标记唤醒，只跳转需要读取的会话；当前已打开的受监控
  会话会原地读取，因为微信不会为当前会话显示红点；
- `paused`：当前操作结束后停止读取、切换和发送，并拒绝新的发送请求。

通过 API 添加会话：

```bash
./manage.sh add-chat 张三 contact
./manage.sh add-chat 测试群 group
```

### 模拟会话

点击左上角 `+`，把“会话来源”改为“模拟调试会话”，即可创建模拟联系人或群聊。它使用与普通
会话相同的消息列表、输入框、持久化和 Agent 投递协议，但不会打开微信窗口或执行键鼠操作，因此
微信未登录时仍可使用。

进入模拟会话后点击“模拟接收”，可以构造文字或图片消息。时间默认当前时间，也可手工指定；模拟
群聊必须填写发送者，可选组织名；“视为 @ 当前微信账号”可用于测试 `mention_only` Agent。上传图片
会在服务端验证并转换为清晰 PNG，再通过现有图片接口提供给 Agent。Agent 或 WebIM 在模拟会话中
发送的回复会直接标记为本地已发送，不会进入微信发送队列。

模拟消息进入与微信消息相同的 SQLite 消息流、SSE 更新和 Agent claim/lease/ACK 链路。暂停模式
同样会拒绝模拟接收和回复。为避免 Agent 伪造入站事件，带 `X-Bowxt-Agent` 身份的请求不能创建
模拟会话或调用模拟接收接口。

## Agent 开发

Agent 应连接已经运行的 bowxt Web 服务，不要创建第二个直接操作微信的 UI 客户端。

```python
from bowxt import AgentClient, ChatType

agent = AgentClient("support-bot")
group = agent.ensure_chat("答疑群", ChatType.GROUP)

def handle(delivery):
    message = delivery.message
    agent.log(
        "info",
        f"收到 {message.sender} ({message.sender_organization or '个人微信'}): {message.content}",
        event="message_received",
        context={"seq": message.seq},
    )
    agent.reply_text(delivery, make_answer(message.content))

agent.run_forever(
    handle,
    chat_ids=[group.id],
    require_sender=True,
    require_at_me=True,
)
```

每个 `consumer` 拥有独立、持久化的消费进度。新 consumer 默认从当前消息末尾开始，避免 Agent
上线后回复历史消息；需要导入历史时可在第一次 `claim()` 使用 `replay_existing=True`。领取的
消息带租约，处理成功后 ACK，失败时 NACK；进程崩溃后租约到期会重新投递。
群聊消息的 `sender` 是发送者昵称；企业微信联系人资料卡提供的“企业”另存为
`sender_organization`，普通联系人则为 `None`。

`send_text()`、`reply_text()` 和 `forward_text()` 都是异步、可幂等提交。`pending` 仅表示进入
队列；最终状态可通过 `wait_delivery()` 或消息 API 查询。图片消息可使用 `download_image()`
获取 bowxt 已保存的图片。

受管 Agent 还可用 `publish_panel()` 在自己的实例卡片上添加只读自定义面板。前端只渲染 bowxt
定义的树节点，不接受 Agent 提供的 HTML、脚本或样式：

```python
agent.publish_panel("status", "运行状态", [
    {"label": "客户群", "meta": "2 个任务", "expanded": True, "children": [
        {"label": "任务 task-17", "value": "等待模型回复", "tone": "info"},
    ]},
])
```

完整接口、投递语义和更多示例见 [AGENT_API.md](AGENT_API.md)，最小可运行示例见
[examples/agent_echo.py](examples/agent_echo.py)。

### 多 Agent 插件

一个微信桌面窗口只能由 bowxt 的单个 UI worker 操作，但消息可以广播给多个 durable consumer：

```text
微信桌面窗口 -> bowxt UI worker -> SQLite 消息流
                                  |- kjfwd-prod（客户群）
                                  |- personal-secretary（私人联系人）
                                  `- 其他 Agent 实例
```

Agent 插件是管理员预先安装的目录，其中包含 `bowxt-agent.json`。Web 前端不会接受任意命令或
上传可执行代码，只允许从 `BOWXT_AGENT_PLUGIN_DIRS` 指定的受信任目录创建实例。示例 manifest：

```json
{
  "schema_version": 1,
  "id": "my-agent",
  "name": "My Agent",
  "version": "1.0.0",
  "entrypoint": ["{python}", "{plugin_dir}/app.py", "--config", "{config_path}", "--env", "{env_path}"],
  "default_config_file": "config.example.json",
  "resources": ["prompts", "skills"],
  "secret_schema": [{"name": "API_KEY", "label": "API Key", "required": true}]
}
```

多个插件目录按 `BOWXT_AGENT_PLUGIN_DIRS` 中的顺序决定优先级；相同插件 ID 只采用最先发现的副本。
这允许只读主机挂载覆盖 volume 中保留的 fallback 副本，而不会创建两个插件类型。

可用占位符为 `{python}`、`{plugin_dir}`、`{instance_dir}`、`{config_path}`、`{env_path}`。
bowxt 还会注入 `BOWXT_BASE_URL`、`BOWXT_AGENT_ID`、`BOWXT_CONSUMER` 和
`BOWXT_AGENT_DATA_DIR`，并用 `BOWXT_MANAGED=1` 标记受管进程。插件应优先读取这些变量，使每个
实例拥有唯一消费身份和独立数据目录。生产 Agent 推荐统一由该控制面运行；外部 `AgentClient`
进程主要用于开发调试或控制面故障回退。

配置与密钥保存在消息数据库同目录的实例区，并物化为权限 `0600` 的 `config.json` 和 `.env`。
API 只返回密钥是否已配置，不会返回原值。插件退出、stdout/stderr 和生命周期事件统一写入该实例日志。

不同 consumer 会各自收到符合插件 `chat_ids` 与控制面读权限交集的消息；相同 consumer 的多个
进程是竞争消费。控制面还会在受管 Agent 发送消息时执行独立的写权限。因此多个 Agent 应使用
不同实例 ID，并在 Web IM 中明确配置群聊或联系人范围。运行中的实例也可进入配置，保存时确认后
由 bowxt 停止、写入配置并重启。

## HTTP API

常用端点：

```text
GET    /api/status
GET    /api/chats
POST   /api/chats
POST   /api/simulated-chats
PATCH  /api/chats/{id}
GET    /api/chats/{id}/messages
GET    /api/chats/{id}/history?since={ISO-8601}&until={ISO-8601}
POST   /api/chats/{id}/messages
POST   /api/chats/{id}/simulate
GET    /api/messages?after={seq}
GET    /api/messages/{seq}
GET    /api/messages/{seq}/image
PATCH  /api/control
GET    /api/events

POST   /api/agents/{consumer}/claim
POST   /api/agents/{consumer}/deliveries/{seq}/ack
POST   /api/agents/{consumer}/deliveries/{seq}/nack
GET    /api/agent/logs
POST   /api/agent/logs
GET    /api/agent/plugins
GET    /api/agent/instances
POST   /api/agent/instances
GET    /api/agent/instances/{id}
PATCH  /api/agent/instances/{id}
DELETE /api/agent/instances/{id}
POST   /api/agent/instances/{id}/start
POST   /api/agent/instances/{id}/stop
POST   /api/agent/instances/{id}/restart
GET    /api/agent/instances/{id}/panels
GET    /api/agent/instances/{id}/panels/{panel_id}
PUT    /api/agent/panels/{panel_id}
DELETE /api/agent/panels/{panel_id}
```

Web/API 请求不会直接并发操作微信。所有读取、切换和发送都由一个 UI 工作线程执行，发送任务
优先于后台轮询、发送人补全和图片升级。

## 直接 Python API

一次性调试也可以直接使用 `WeChatClient`：

```python
from bowxt import ChatType, WeChatClient

with WeChatClient(visual_direction=True, uia_sender=True) as wx:
    messages = wx.get_visible_messages("测试群", chat_type=ChatType.GROUP)
    receipt = wx.send_text("张三", "你好", chat_type=ChatType.CONTACT)
    print(messages, receipt.verified)
```

命令行提供对应的调试入口：

```bash
bowxt read 测试群 --type group --uia-sender
bowxt send 张三 "测试消息" --type contact --yes
bowxt serve --host 127.0.0.1 --port 8787 --db ./messages.db
```

## 开发与测试

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
PYTHONPATH=src:tests python3 -W error::ResourceWarning -m unittest discover -s tests -v
bash -n manage.sh scripts/*
python3 -m compileall -q src tests
```

GitHub Actions 会在 Python 3.10/3.12 上运行单元测试和资源泄漏检查，构建并重新安装发布 wheel，
再构建完整微信桌面镜像。最后一个任务会启动未登录微信的新容器，通过模拟会话黑盒验证群发送人和
组织、图片、Agent 租约/ACK、幂等发送及暂停/恢复链路，因此无需在 CI 中使用真实微信账号。

实机验证记录见 [VALIDATION.md](VALIDATION.md)，开发约束见 [AGENT.md](AGENT.md)。

## 限制

- 只能读取微信当前已渲染的消息，不会自动滚动并导出完整聊天历史。
- 微信通常只公开一组消息的时间分隔，无法保证每个气泡都有独立、精确的发送时间。
- 一个微信窗口同一时刻只能操作一个会话，因此多会话收发是串行调度，不是真正的多窗口并行。
- 群发送人通常不在消息行控件树中，需要短暂打开资料卡补全；资料卡不可识别或无法退出时，本次
  补全会失败。
- 新消息唤醒依赖微信会话列表暴露的未读信息；不可见、未渲染或微信未标记的会话可能需要轮询
  模式才能及时读取。
- 图片只支持当前可见的图片气泡；文件、语音、视频、引用、红包等消息类型尚未提供完整内容接口。
- 微信界面结构或版本变化可能导致控件选择器失效；容器替换后微信也可能要求再次手机确认。

## Acknowledgement

本项目仅供技术研究与实现思路参考，不保证适合生产环境或长期兼容微信版本。

项目在设计与实现过程中参考了：

- [wx4py](https://github.com/claw-codes/wx4py)
- [RICwang/docker-wechat](https://github.com/RICwang/docker-wechat)

感谢上述项目及其贡献者。

许可证：AGPL-3.0-or-later。
