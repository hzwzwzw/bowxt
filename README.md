# bowxt

`bowxt`（wx + box + bot）是一个可通过 VNC 访问的容器化 Linux 微信，同时提供安全的
桌面自动化 API、持续多会话监听、SQLite 消息持久化和一个现代 Web IM。

它只控制官方 Linux 微信的可见界面：AT-SPI 负责只读控件树，发送使用 XTest 真实键鼠事件和
系统剪贴板。项目不使用微信协议、私有 API、数据库解密、进程注入、深层控件写入或 OCR。

## 主要功能

- 固定 Ubuntu 24.04 基础镜像和官方微信 `4.1.1.8` 安装包 SHA-256；
- Xvfb + XFCE + x11vnc + noVNC，默认仅转发微信窗口；
- fcitx5 拼音和中文字体，VNC 中可直接中文输入；
- 联系人、群聊文字读取与发送，支持群内真实富文本 `@`；
- 单 UI 工作线程安全调度多个会话，外部 HTTP 调用可并发提交；
- 自动发现微信侧边栏中的可见未读会话；
- 成功观察到的收发消息写入 SQLite，重启后继续保留；
- Web IM 支持会话自动出现、手动新增、实时消息流和文字收发；
- 发送频率、单会话频率和文本长度限制默认启用。

## 快速开始

要求：Linux amd64、Docker Engine，以及能用手机确认登录的微信账号。

```bash
git clone https://github.com/hzwzwzw/bowxt.git
cd bowxt
./scripts/init.sh
```

初始化脚本会构建固定版本镜像并启动容器。随后访问：

- Web IM：<http://127.0.0.1:8787/>
- 微信单窗口 noVNC：<http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale>
- 原生 VNC：`127.0.0.1:5900`

第一次启动时在 noVNC 中扫码并用手机确认。登录状态、fcitx5 用户配置、聊天数据库和微信数据
都保存在 Docker volume `bowxt-home`；普通停止、更新镜像或替换容器不会删除它。

端口只绑定宿主 `127.0.0.1`。从另一台电脑访问时应使用 SSH 隧道，不要把端口直接暴露到公网：

```bash
ssh -L 8787:127.0.0.1:8787 -L 6080:127.0.0.1:6080 USER@DOCKER_HOST
```

## 常用管理命令

```bash
./manage.sh build               # 构建固定版本镜像
./manage.sh up                  # 启动并等待健康检查
./manage.sh open                # 打开 Web IM
./manage.sh vnc                 # 打开微信单窗口 noVNC
./manage.sh ready               # 检查微信是否登录并暴露主控件树
./manage.sh doctor              # 输出脱敏的 AT-SPI 诊断树
./manage.sh input-method        # 检查 fcitx5 和输入法环境变量
./manage.sh unit                # 容器内运行测试
./manage.sh add-chat 张三 contact
./manage.sh add-chat 测试群 group
./manage.sh logs
./manage.sh down                # 保留所有持久化数据
```

默认 `VNC_SCOPE=window`，只看到微信顶层窗口。需要处理伸出微信窗口边界的独立弹层时：

```bash
VNC_SCOPE=desktop ./manage.sh up
```

恢复单窗口：

```bash
VNC_SCOPE=window ./manage.sh up
```

`up` 检测到镜像或显示模式变化时会替换容器，但保留数据卷；微信仍可能要求在手机端再次确认。

## Web IM 与 HTTP API

Web 服务默认监听容器 `8787`。浏览器通过 SSE 接收新增会话、消息和服务状态，不需要刷新。

```text
GET    /api/status
GET    /api/chats
POST   /api/chats                     {"name":"第二联系人","chat_type":"contact"}
PATCH  /api/chats/{id}                {"chat_type":"group"}
GET    /api/chats/{id}/messages?after=0&limit=200
GET    /api/chats/{id}/messages?limit=1&recent=1
POST   /api/chats/{id}/messages       {"text":"你好"}
GET    /api/events                    text/event-stream
```

Web/API 线程不会直接操作微信。所有读取、焦点切换和发送都会进入同一个 UI 工作线程；它按会话
轮询并优先处理发送队列。因此多个客户端可以并发提交任务，但同一个微信窗口不会被多个线程
同时争抢。默认相邻 UI 轮询至少间隔 2 秒，代码拒绝低于 1.5 秒的配置。

自动新增会话采用保守路径：仅检查侧边栏中明确标记“未读”的可见行，通过普通点击打开后从
聊天标题读取完整名称，不会猜测或用空格切割“名称 + 消息预览”。自动发现的会话先标记为
`unknown`；可在 Web/API 中改为 `contact` 或 `group`。

持续服务默认不打开群成员资料卡（`BOWXT_UIA_SENDER=0`），以降低长期运行中的界面扰动。
需要群发送者昵称时可显式使用 `BOWXT_UIA_SENDER=1 ./manage.sh up`；资料卡安全退出验证仍会启用。

## Python API

```python
from bowxt import BowxtClient, ChatType

with BowxtClient(visual_direction=True, uia_sender=True) as wx:
    messages = wx.get_visible_messages("测试群", chat_type=ChatType.GROUP)
    receipt = wx.send_text("张三", "bowxt 测试", chat_type=ChatType.CONTACT)
    print(receipt.verified)
```

多会话持久服务：

```python
from bowxt import BowxtService, ChatType, SQLiteStore

service = BowxtService(SQLiteStore("messages.db"))
chat = service.add_chat("第二联系人", ChatType.CONTACT)
service.start()
service.send_text(chat.id, "你好")
service.stop()
```

命令行保留适合调试的一次性接口：

```bash
bowxt read 测试群 --type group --uia-sender
bowxt send 张三 "明确的测试消息" --type contact --yes
bowxt serve --host 127.0.0.1 --port 8787 --db ./messages.db
```

## 消息持久化

默认数据库位于 `/home/wechat/.local/share/bowxt/messages.db`。SQLite 使用 WAL，表结构包含：

- `chats`：会话名称、类型、来源、启用状态、最近消息和最近错误；
- `messages`：会话内消息 ID、方向、发送者、正文、类型、时间、观察时间、@ 状态和验证状态。

同一会话的微信消息 ID 有唯一约束。未能即时读到微信 ID 的已发送消息会先使用 `local:` ID，
后续从控件树观察到同内容的发出消息时自动合并，避免页面出现两条。

备份数据库和登录数据：

```bash
docker run --rm -v bowxt-home:/data -v "$PWD":/backup \
  ubuntu:24.04 tar -C /data -czf /backup/bowxt-home.tar.gz .
```

`./manage.sh purge-login` 会要求输入 `PURGE`，然后删除整个持久化卷；不要把它用于普通升级。

## fcitx5 中文输入

镜像安装 `fcitx5`、`fcitx5-chinese-addons`、GTK3 和 Qt5 前端，并为新数据卷配置
`keyboard-us` 与 `pinyin`。桌面会话与微信进程都设置：

```text
XMODIFIERS=@im=fcitx
GTK_IM_MODULE=fcitx
QT_IM_MODULE=fcitx
INPUT_METHOD=fcitx
```

在 VNC 中使用 `Ctrl+Space` 切换拼音。自动化发送仍优先使用临时剪贴板并在完成后恢复原内容，
不依赖输入法当前状态。

## 初始化脚本

- `scripts/init.sh`：构建并启动完整环境；
- `scripts/build.sh`：仅构建 Docker 镜像；
- `scripts/install-wechat.sh`：下载官方 deb、严格校验哈希并验证安装版本；
- `scripts/configure-fcitx5.sh`：为当前用户写入可复用的拼音默认配置；
- `scripts/bowxt-*`：容器内 X11、桌面、输入法、微信、VNC、Web 和健康检查入口。

## 开发与测试

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
PYTHONPATH=src:tests python3 -m unittest discover -s tests -v
bash -n manage.sh scripts/*
```

当前版本包含 43 项单元测试，覆盖控件选择、X11/XWayland 坐标映射、剪贴板恢复、资料卡退出、富文本 `@`、限流、
多会话调度、SQLite 重启/去重、并发调用串行化和 Web API。实机验证步骤记录在
[`VALIDATION.md`](VALIDATION.md)，开发约束见 [`AGENT.md`](AGENT.md)。

## 限制与安全边界

- “持续”表示服务长期轮询微信当前可渲染的消息；不会滚动并导出完整历史。
- 一个微信窗口在物理上只能激活一个会话，因此多会话通过单工作线程公平轮询，不是多窗口并发。
- 资料卡发送者补全会短暂打开可见资料卡，并以 `Esc` + 顶层窗口消失 + 原会话仍打开三重验证退出；
  失败后该客户端永久禁止继续输入。
- 用户正在处理通话、资料卡等未知微信弹层时，框架不会替用户关闭，而是停止输入。
- 默认仅适合自己的账号和少量已授权会话；不要用于群发、营销、陌生人触达或绕过微信限制。

许可证：AGPL-3.0-or-later。
