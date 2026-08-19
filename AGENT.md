# bowxt 开发代理手册

这份文件面向继续维护 bowxt 的编码代理和开发者。开始修改前先阅读 `README.md`、本文件和
与改动相关的测试。

## 不可破坏的安全边界

1. 读取只能使用公开的 AT-SPI 只读接口；不得读取或解密微信数据库。
2. 写入只能是对可见控件的真实键盘、鼠标和剪贴板操作；不得设置深层控件值。
3. 禁止协议发包、私有 API、进程注入、内存扫描、Hook 和 OCR。
4. 输入前必须验证微信活动顶层窗口、坐标映射、无未知弹层和无已有草稿；不确定就拒绝。
5. 任何框架创建的弹层必须显式退出并验证消失；验证失败后锁定该客户端的全部输入。
6. 不降低 `SafetyPolicy` 默认限流，不允许轮询间隔低于 1.5 秒或键鼠操作间隔低于 60 ms。
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
- Web 发送必须先持久化唯一 `pending` 行，再由 UI 工作线程更新同一行；不得让 HTTP 线程等待
  微信操作，也不得把连续提交并行注入同一个微信窗口。
- `paused=true` 时，工作线程不得连接、读取、发现会话或处理发送队列；当前不可中断的原子操作
  可以安全收尾。暂停期间的新发送必须明确拒绝。
- 微信只提供分组时间时，前端只能显示分组时间条，不能把同一时间复制到每个气泡下。
- 状态事件不得重建消息列表或抢走用户的滚动位置；非当前会话新消息要显示未读状态。
- 群发送者资料卡必须补全当轮所有新入站消息；没有新消息时可每轮追补一条历史记录。右侧气泡不得尝试
  打开资料卡；发送回显验证必须显式禁用发送者补全。补全昵称时应更新原消息，不得制造新副本。
- 发送队列优先于昵称补全队列；允许在两次资料卡之间插队，但当前资料卡必须完成 `Esc` 退出验证后才能切换任务。
- 仅与消息视口相交一个像素的 Qt 虚拟行是边界残留，不得解析成新消息。
- 发送路径必须保留 `last_send_timings` 分段计时，以便区分排队、切换、输入和确认延迟。
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
