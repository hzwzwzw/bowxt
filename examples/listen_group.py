from bowxt import ChatType, WeChatClient


def on_message(message):
    print(f"[{message.chat}] {message.sender}: {message.content}")
    return None


with WeChatClient(my_names=["我的群昵称"], uia_sender=True) as wx:
    wx.listen(
        ["项目群"],
        on_message,
        chat_type=ChatType.GROUP,
        poll_interval=3.0,
        auto_reply=False,
        block=True,
    )
