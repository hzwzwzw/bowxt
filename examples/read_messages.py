from bowxt import ChatType, WeChatClient


with WeChatClient(my_names=["我的群昵称"], uia_sender=True) as wx:
    for message in wx.get_visible_messages("项目群", chat_type=ChatType.GROUP):
        print(message.sender, message.direction.value, message.type.value, message.content)
