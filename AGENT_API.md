# bowxt Agent API

这套接口用于让 Agent 与独占微信 UI 的 `bowxt serve` 分进程运行。Agent 不直接接触 AT-SPI、
键鼠或微信窗口，只消费 SQLite 中已经观察到的消息，并把发送任务提交到唯一 UI 工作线程。

## 与 kjfwd 所需能力的对应关系

| Agent 需求 | bowxt 接口 | 语义 |
| --- | --- | --- |
| 注册多个监听群/联系人 | `AgentClient.ensure_chat()` | 每个会话独立保存 `ChatType`，可同时监听多个会话 |
| 连续接收结构化消息 | `claim()` / `run_forever()` | 一次领取一个批次，不限制每周期只有一个会话或一条消息 |
| 查询已持久化的时间窗历史 | `get_history()` | 按会话查询指定时间范围，最长 31 天；受 Agent 读权限约束 |
| 群名、发送者、组织、@我 | `StoredMessage.chat/sender/sender_organization/is_at_me` | 昵称和企业组织可由可见资料卡补全；可要求有发送者后再投递 |
| 回复来源群 | `reply_text()` | 使用来源 `chat_id`，幂等异步排队 |
| 转发到参考群 | `forward_text()` | 目标会话独立配置，幂等异步排队 |
| 去重与崩溃恢复 | delivery lease + `ack()` / `nack()` | 每个 consumer 独立、至少一次投递、失败延迟重试 |
| 发送结果 | `wait_delivery()` | 区分 `pending/sent/unverified/failed`；`sent` 仍不是对端回执 |
| 图片输入 | `message_type/image_url` + `download_image()` | 只读取已通过可见图片查看器和 Ctrl+C 保存的 PNG |
| 运行日志 | `log()` | 持久化、SSE 实时推送，并从 Agent 卡片的“查看日志”弹窗查看 |
| 自定义状态展示 | `publish_panel()` | 发布受限的声明式树，在 Agent 卡片中打开；不执行插件 HTML/JS |

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
- 企业微信联系人资料卡中的“企业”会写入 `sender_organization`；普通微信联系人或资料卡未提供企业
  字段时为 `None`。同一张资料卡只读取一次即可同时补全昵称和组织，不会因组织为空而反复打开。
- 暂停 bowxt 时，新的 claim 和发送返回 HTTP 409；已有 handler 可以完成 ack/nack 和日志写入。

### 使用模拟会话调试

WebIM 可创建 `source=simulation` 的联系人或群聊，并通过“模拟接收”构造文字或图片。模拟入站消息
会像真实微信消息一样获得持久化 `seq`，并进入本节所述的 claim、租约和 ACK/NACK 链路；模拟群聊
同样携带 `sender`、可选 `sender_organization` 和 `is_at_me`。Agent 对模拟会话调用
`reply_text()` 或 `send_text()` 时，回复会直接保存为 `sent/verified`，不依赖微信登录，也不会执行
键鼠操作。

创建和注入只用于人工调试，不属于 Agent 权限：携带 `X-Bowxt-Agent` 的客户端不能调用
`POST /api/simulated-chats` 或 `POST /api/chats/{id}/simulate`。Agent 仍只通过正常消息消费接口看到
这些事件。

## 持久化群聊历史

```python
messages = agent.get_history("答疑一群", duration_seconds=3600)
for message in messages:
    print(message.timestamp or message.observed_at, message.sender, message.content)
```

`get_history()` 自动分页读取时间窗内的全部持久化消息，包含发送人、组织名、方向、类型和
`is_at_me`。传入的 `until` 必须是带时区的 `datetime`；不传时以当前 UTC 时间为终点。

这不是微信服务端历史导出。它只能返回 bowxt 已经从可见 UI 观测并写入 SQLite 的消息。
受管 Agent 会按自身的会话读权限过滤，无权会话不能查询。

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
Web IM 的 Agent 日志弹窗支持实时跟随、级别过滤、全文筛选、复制和历史翻页。日志不是微信消息，不会触发任何
微信读取或发送动作。

## 自定义面板

受管 Agent 可以在自己的 Web IM 卡片上发布一个或多个只读面板：

```python
agent.publish_panel(
    "active-conversations",
    "会话信息",
    [{
        "id": "group:客户群",
        "label": "客户群",
        "meta": "1 个活跃会话",
        "expanded": True,
        "children": [{
            "id": "conversation:abc",
            "label": "abc",
            "meta": "打印机问题",
            "children": [{
                "label": "12:30 · 张三",
                "meta": "某组织",
                "value": "打印机脱机",
                "tone": "neutral",
            }],
        }],
    }],
    empty_text="当前没有活跃会话",
)
```

协议固定为 `version=1, type=tree`。节点支持 `id`、`label`、`meta`、`value`、`tone`、
`expanded` 和 `children`；`tone` 可取 `neutral/info/success/warning/danger`。最多 1,000 个节点、
8 层、编码后 256 KiB。服务端拒绝未知字段，Web IM 全部通过 DOM `textContent` 渲染，不接受
HTML、Markdown、脚本、链接动作或插件 CSS。面板更新持久化并通过 SSE 通知前端；停止 Agent 后
保留最后一次快照，实例状态和更新时间会明确显示其新鲜度。`delete_panel()` 可移除面板。

面板身份只能来自 `X-Bowxt-Agent`，请求体不能指定或冒充其他实例；当前仅受管实例可以发布。
发布面板只读 Agent 自己的数据并写 bowxt SQLite，不触发微信读取、切换或键鼠发送。

## HTTP 协议

Python 客户端只使用标准库，底层端点也可供其他语言调用：

```text
POST /api/simulated-chats
     {"name":"本地答疑群","chat_type":"group"}
POST /api/chats/{id}/simulate
     {"text":"@kirotta 测试问题","sender":"测试用户",
      "sender_organization":"测试组织","is_at_me":true,
      "timestamp":"2026-08-24T13:00:00+08:00"}
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
GET  /api/chats/{id}/history?since={ISO-8601}&until={ISO-8601}&after=0&limit=1000
PUT  /api/agent/panels/{panel_id}
     {"title":"运行状态","document":{"version":1,"type":"tree","nodes":[]}}
DELETE /api/agent/panels/{panel_id}
GET  /api/agent/instances/{id}/panels/{panel_id}
```

这些端点默认只绑定 `127.0.0.1`，没有设计公网身份认证。跨主机开发应使用 SSH 隧道，不要把
8787 端口直接暴露到公网。

## 受管插件实例

独立运行的 `AgentClient` 与受管插件使用同一投递协议。受管模式只增加实例配置和进程生命周期，
不会让 Agent 直接接触微信 UI。

```text
GET    /api/agent/plugins
GET    /api/agent/instances
POST   /api/agent/instances
       {"plugin_id":"kjfwd-bot","id":"kjfwd-prod","name":"客户群答疑",
        "config":{},"secrets":{"API_KEY":"..."},"autostart":true,
        "permissions":{
          "read":{"mode":"selected","chat_ids":[1,2],"patterns":[]},
          "write":{"mode":"regex_allow","chat_ids":[],"patterns":["^客户群"]}
        }}
GET    /api/agent/instances/{id}
PATCH  /api/agent/instances/{id}
       {"name":"新名称","config":{},"secrets":{"API_KEY":"..."},"autostart":false,
        "permissions":{"read":{"mode":"all"},"write":{"mode":"all"}},
        "restart":true}
DELETE /api/agent/instances/{id}
POST   /api/agent/instances/{id}/start   {}
POST   /api/agent/instances/{id}/stop    {}
POST   /api/agent/instances/{id}/restart {}
GET    /api/agent/logs?agent={id}&recent=1
```

会话权限的 `mode` 支持 `all`、`selected`、`regex_allow` 和 `regex_deny`，读写策略相互独立。
控制面会在消息领取、消息详情/图片读取和发送入队边界执行策略；实例卡片同时展示策略当前匹配的
会话和 consumer 最近一次实际 claim 的会话。受管 `AgentClient` 自动携带 `X-Bowxt-Agent` 标识。

运行中的实例可在 Web IM 中进入配置；保存时需确认重启，API 对应传 `"restart": true`。不传该字段
仍会拒绝热改，避免配置文件与运行进程不一致。删除运行实例仍需先停止。更新时省略密钥或传空的 `secrets` 会保留已有密钥；
API 响应只包含 `{"configured": true}`，不回传原值。实例 ID 会通过 `BOWXT_CONSUMER` 注入，
插件应让它覆盖静态配置中的 consumer，确保多个 Agent 拥有独立投递进度。受管进程还会收到
`BOWXT_MANAGED=1`；插件可据此拒绝未经控制面授权的普通外部启动。生产实例推荐使用受管模式，
独立 `AgentClient` 进程只作为开发或故障回退入口。
