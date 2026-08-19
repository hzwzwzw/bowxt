# bowxt Agent API

这套接口用于让 Agent 与独占微信 UI 的 `bowxt serve` 分进程运行。Agent 不直接接触 AT-SPI、
键鼠或微信窗口，只消费 SQLite 中已经观察到的消息，并把发送任务提交到唯一 UI 工作线程。

## 与 kjfwd 所需能力的对应关系

| Agent 需求 | bowxt 接口 | 语义 |
| --- | --- | --- |
| 注册多个监听群/联系人 | `AgentClient.ensure_chat()` | 每个会话独立保存 `ChatType`，可同时监听多个会话 |
| 连续接收结构化消息 | `claim()` / `run_forever()` | 一次领取一个批次，不限制每周期只有一个会话或一条消息 |
| 群名、发送者、@我 | `StoredMessage.chat/sender/is_at_me` | 发送者可由可见资料卡补全；可要求有发送者后再投递 |
| 回复来源群 | `reply_text()` | 使用来源 `chat_id`，幂等异步排队 |
| 转发到参考群 | `forward_text()` | 目标会话独立配置，幂等异步排队 |
| 去重与崩溃恢复 | delivery lease + `ack()` / `nack()` | 每个 consumer 独立、至少一次投递、失败延迟重试 |
| 发送结果 | `wait_delivery()` | 区分 `pending/sent/unverified/failed`；`sent` 仍不是对端回执 |
| 图片输入 | `message_type/image_url` + `download_image()` | 只读取已通过可见图片查看器和 Ctrl+C 保存的 PNG |
| 运行日志 | `log()` | 持久化、SSE 实时推送，并在 Web IM 的 Agent 日志页查看 |

`kjfwd` 旧版依靠 UI 控件 RuntimeId 和时间拼接去重；bowxt 先把 UI 消息稳定为数据库 `seq`，
Agent 再围绕 `seq` 做租约和确认，因此进程重启后不会把“内存里见过”误当成可靠状态。

## 最小用法

```python
from bowxt import AgentClient, ChatType

agent = AgentClient("support-bot")
groups = [
    agent.ensure_chat("答疑一群", ChatType.GROUP),
    agent.ensure_chat("答疑二群", ChatType.GROUP),
]

def handle(delivery):
    message = delivery.message
    if not message.is_at_me:
        return
    result = make_answer(message.content)
    queued = agent.reply_text(delivery, result)
    agent.log(
        "info",
        "回复已进入微信发送队列",
        event="reply_queued",
        context={"source_seq": message.seq, "outgoing_seq": queued.seq},
    )

agent.run_forever(handle, chat_ids=[group.id for group in groups])
```

`run_forever()` 只在 handler 正常返回后 `ack`。异常会 `nack`，默认 5 秒后重投，并把异常写入
Agent 日志。若业务有不可重试错误，应在 handler 中记录后正常返回；不要制造永久重试循环。

## 精确控制投递

```python
deliveries = agent.claim(
    chat_ids=[groups[0].id],
    limit=8,
    timeout=20,
    lease_seconds=120,
    require_at_me=True,
    require_sender=True,
    replay_existing=False,
)

for delivery in deliveries:
    try:
        process(delivery.message)
    except Exception as exc:
        agent.nack(delivery, exc, retry_delay=15)
    else:
        agent.ack(delivery)
```

- `consumer` 是持久化消费身份；更换名称会得到一套新的独立投递进度。
- 新 consumer 默认把首次 claim 时已有的消息作为基线，只接收此后新增消息，避免 Agent 首次上线
  误回复旧对话。确需导入历史时，仅在第一次 claim 使用 `replay_existing=True`；consumer 一旦注册，
  该参数不会重置其起点。
- 一个 consumer 可由多个进程竞争领取，同一租约只会发给其中一个进程。
- 租约期间崩溃时不需要清理；到期后同一消息会再次投递，`attempt` 增加。
- `require_at_me=True` 适合 `mention_only`；不设置即可实现 `all_messages` 或自行分类。
- `is_at_me` 按 `BOWXT_MY_NAMES` 中逗号分隔的当前账号群昵称/别名判断，例如
  `BOWXT_MY_NAMES=kirotta,bowxt`。未配置时不会猜测账号名，避免误触发 Agent。
- `require_sender=True` 会暂缓没有昵称的消息。只有在群聊发送者补全已开启、且业务确实不能接受
  `sender=None` 时使用；资料卡读取失败的消息会一直等待。
- 暂停 bowxt 时，新的 claim 和发送返回 HTTP 409；已有 handler 可以完成 ack/nack 和日志写入。

## 回复、转发和投递结果

`reply_text()` 和 `forward_text()` 的默认幂等键由 consumer、来源消息 `seq` 和 `key` 组成。
相同来源上重试不会重复提交；同一来源需要发多条时，为每条提供不同的 `key`：

```python
first = agent.reply_text(delivery, "第一段", key="part-1")
second = agent.reply_text(delivery, "第二段", key="part-2")
final = agent.wait_delivery(second, timeout=40)
if final.delivery_status == "failed":
    raise RuntimeError(final.delivery_error)
```

HTTP 202 或 `pending` 只表示已持久化并进入 bowxt 队列。`sent` 表示微信可见界面中找到了发送
回显，`unverified` 表示键盘发送已执行但未找到可靠回显，`failed` 表示安全检查或 UI 操作失败。
微信 UI 不提供对端送达/已读回执。

## 日志

```python
agent.log(
    "info",
    "模型调用结束",
    event="model_completed",
    context={"latency_ms": 842, "conversation_id": "c-17"},
)
```

级别为 `debug/info/warning/error`。正文最多 20,000 字符，结构化 `context` 最多 64 KiB。
Web IM 支持实时跟随、级别过滤、全文筛选、复制和历史翻页。日志不是微信消息，不会触发任何
微信读取或发送动作。

## HTTP 协议

Python 客户端只使用标准库，底层端点也可供其他语言调用：

```text
POST /api/agents/{consumer}/claim
     {"chat_ids":[1],"limit":8,"lease_seconds":60,"timeout":20,
      "require_sender":false,"require_at_me":true,"replay_existing":false}
POST /api/agents/{consumer}/deliveries/{seq}/ack
     {"lease_token":"..."}
POST /api/agents/{consumer}/deliveries/{seq}/nack
     {"lease_token":"...","error":"...","retry_delay":5}
POST /api/agent/logs
     {"agent":"support-bot","level":"info","event":"run","message":"...","context":{}}
GET  /api/agent/logs?after=0&limit=200
GET  /api/messages?after=0&limit=200
GET  /api/messages/{seq}
```

这些端点默认只绑定 `127.0.0.1`，没有设计公网身份认证。跨主机开发应使用 SSH 隧道，不要把
8787 端口直接暴露到公网。
