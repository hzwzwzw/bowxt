# bowxt

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
- 面向 Agent 的独立消费游标、批量领取、租约、ACK/NACK、崩溃重投和幂等发送；
- 独立的 Agent 日志通道，支持持久化、实时跟随、级别过滤、搜索和复制。

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

Web IM 提供两个页面：

- **对话**：点击左上角 `+` 添加联系人或群聊；支持会话切换、未读红点、历史消息翻页、图片、
  乐观发送气泡和异步发送状态。发送任务在后台串行操作微信，输入框不会等待上一条消息完成。
- **Agent 日志**：显示 Agent 通过日志 API 写入的持久化日志，支持实时跟随、级别过滤、全文
  搜索、复制和向前加载历史。日志与微信消息完全分开，不会生成聊天气泡或触发微信操作。

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
        f"收到 {message.sender}: {message.content}",
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

`send_text()`、`reply_text()` 和 `forward_text()` 都是异步、可幂等提交。`pending` 仅表示进入
队列；最终状态可通过 `wait_delivery()` 或消息 API 查询。图片消息可使用 `download_image()`
获取 bowxt 已保存的图片。

完整接口、投递语义和更多示例见 [AGENT_API.md](AGENT_API.md)，最小可运行示例见
[examples/agent_echo.py](examples/agent_echo.py)。

## HTTP API

常用端点：

```text
GET    /api/status
GET    /api/chats
POST   /api/chats
PATCH  /api/chats/{id}
GET    /api/chats/{id}/messages
POST   /api/chats/{id}/messages
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
