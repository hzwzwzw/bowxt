"""Minimal durable Agent example; run only against an authorized test chat."""

from bowxt import AgentClient, ChatType


agent = AgentClient("example-echo")
chat = agent.ensure_chat("测试群", ChatType.GROUP)


def handle(delivery):
    message = delivery.message
    agent.log(
        "info",
        f"收到 {message.sender or '未知成员'}: {message.content}",
        event="message_received",
        context={"message_seq": message.seq, "chat": message.chat},
    )
    agent.reply_text(delivery, f"已收到：{message.content}")


agent.run_forever(handle, chat_ids=[chat.id], require_at_me=True)
