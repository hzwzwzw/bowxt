from bowxt import ChatType, WeChatClient


with WeChatClient() as wx:
    receipt = wx.send_text(
        "项目群",
        "请查看这条测试消息",
        chat_type=ChatType.GROUP,
        mentions=["张三"],
    )
    print(receipt)
