# bowxt 开发代理手册

这份文件面向继续维护 bowxt 的编码代理和开发者。开始修改前先阅读 `README.md`、本文件和
与改动相关的测试。

## 不可破坏的安全边界

1. 读取只能使用公开的 AT-SPI 只读接口；不得读取或解密微信数据库。
2. 写入只能是对可见控件的真实键盘、鼠标和剪贴板操作；不得设置深层控件值。
3. 禁止协议发包、私有 API、进程注入、内存扫描、Hook 和 OCR。
4. 输入前必须验证微信活动顶层窗口、坐标映射、无未知弹层和无已有草稿；不确定就拒绝。
5. 任何框架创建的弹层必须显式退出并验证消失；验证失败后锁定该客户端的全部输入。
6. 不降低 `SafetyPolicy` 默认限流，不允许轮询间隔低于 1.5 秒。
7. 实机发送测试只能发给任务明确允许的账号/群，并使用可辨识的低频测试文字。
8. 不删除 `bowxt-home`，除非用户明确要求清除登录和消息数据。

## 架构约束

- `client.py` 是一次性 UI API；`accessibility.py` 保持只读，`input.py` 是唯一输入实现。
- `service.py` 的单一工作线程独占 `WeChatClient`。HTTP/SSE 线程只能排队，不得直接碰 UI。
- 每个会话单独保存 `ChatType`，不能让群聊和联系人共享一个全局类型。
- `store.py` 的 schema 变更必须向后兼容；SQLite 连接必须在每次操作后关闭。
- 消息入库以 `(chat_id, message_id)` 去重；发送后暂存的 `local:` 行要能与可见回显合并。
- 自动发现只能处理明确未读的可见会话行，并通过打开会话后读取标题确认名称。
- Web UI 不得使用 `innerHTML` 渲染用户消息，避免持久型 XSS；使用 `textContent`。
- noVNC、VNC 和 Web 默认只绑定 `127.0.0.1`。

## 修改后的最低验证

```bash
PYTHONPATH=src:tests python3 -W error::ResourceWarning -m unittest discover -s tests -v
python3 -m compileall -q src tests
bash -n manage.sh scripts/*
docker build -t bowxt:dev .
docker run --rm --entrypoint /bin/sh bowxt:dev -lc \
  'cd /opt/bowxt && PYTHONPATH=src:tests python3 -m unittest discover -s tests'
```

涉及容器桌面时还要验证 `fcitx5`、微信、noVNC、Web 四个服务；涉及收发时先 `doctor`、再只读，
最后只发送一条明确测试消息。持续监听至少覆盖两个联系人和一个群，并检查 SQLite 中按会话保存。

## 发布检查

- README、API 示例、Docker 端口和实际脚本一致；
- 源码、wheel、sdist 中不存在旧项目名；
- 不提交数据库、微信数据、二维码、聊天截图、cookie 或调试日志；
- 固定上游镜像摘要、微信版本与 deb SHA-256；
- Git 工作区只包含项目文件，测试全部通过后再提交和推送。
