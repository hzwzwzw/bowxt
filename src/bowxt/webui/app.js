const state = {
  chats: [],
  activeChatId: null,
  messages: new Map(),
  loadedChats: new Set(),
  olderMessages: new Map(),
  unreadChats: new Set(),
  status: null,
  view: "chat",
  messageCursor: 0,
  logs: [],
  logsLoaded: false,
  olderLogs: false,
  logCursor: 0,
  activeLogAgent: null,
  plugins: [],
  agents: [],
  activeCustomPanel: null,
};

const el = (id) => document.getElementById(id);
const list = el("chat-list");
const messagesEl = el("messages");
const composer = el("composer");
const input = el("message-input");
const sendButton = el("send-button");
const simulateReceive = el("simulate-receive");
const syncMode = el("sync-mode");
const pollGap = el("poll-gap");
const pollGapValue = el("poll-gap-value");
const actionDelay = el("action-delay");
const actionDelayValue = el("action-delay-value");
const logPanel = el("log-panel");
const logsEl = el("logs");
const logSearch = el("log-search");
const logLevel = el("log-level");
const logFollow = el("log-follow");
const agentPanel = el("agent-panel");
const agentList = el("agent-list");

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
}

function formatLogTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "--:--:--" : new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
}

function typeLabel(value) {
  return value === "group" ? "群聊" : value === "contact" ? "联系人" : "未分类";
}

function localDateTimeValue(date = new Date()) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 19);
}

function upsertMessage(message) {
  if (!state.messages.has(message.chat_id)) state.messages.set(message.chat_id, []);
  const values = state.messages.get(message.chat_id);
  const index = values.findIndex((item) => item.seq === message.seq || item.message_id === message.message_id || (message.client_id && item.client_id === message.client_id));
  if (index >= 0) values[index] = { ...values[index], ...message };
  else values.push(message);
  values.sort((a, b) => {
    const aSeq = typeof a.seq === "number" ? a.seq : Number.MAX_SAFE_INTEGER;
    const bSeq = typeof b.seq === "number" ? b.seq : Number.MAX_SAFE_INTEGER;
    return aSeq - bSeq;
  });
  return index < 0;
}

function upsertLog(value) {
  const index = state.logs.findIndex((item) => item.seq === value.seq);
  if (index >= 0) state.logs[index] = { ...state.logs[index], ...value };
  else state.logs.push(value);
  state.logs.sort((a, b) => a.seq - b.seq);
  state.logCursor = Math.max(state.logCursor, Number(value.seq) || 0);
  return index < 0;
}

function renderChats() {
  list.replaceChildren();
  const sorted = [...state.chats].sort((a, b) => String(b.last_message_at || b.created_at).localeCompare(String(a.last_message_at || a.created_at)));
  for (const chat of sorted) {
    const cached = state.messages.get(chat.id) || [];
    const latest = cached[cached.length - 1];
    const button = document.createElement("button");
    button.className = `chat-item${chat.id === state.activeChatId && state.view === "chat" ? " active" : ""}`;
    button.type = "button";
    button.setAttribute("aria-label", `${chat.name}${state.unreadChats.has(chat.id) ? "，有新消息" : ""}`);
    button.addEventListener("click", () => selectChat(chat.id));

    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = [...chat.name][0] || "微";
    const avatarWrap = document.createElement("div");
    avatarWrap.className = "avatar-wrap";
    avatarWrap.append(avatar);
    if (state.unreadChats.has(chat.id)) {
      const unread = document.createElement("span");
      unread.className = "unread-dot";
      unread.setAttribute("aria-label", "有新消息");
      avatarWrap.append(unread);
    }

    const copy = document.createElement("div");
    copy.className = "chat-copy";
    const top = document.createElement("div");
    top.className = "chat-name-row";
    const name = document.createElement("div");
    name.className = "chat-name";
    name.textContent = chat.name;
    if (chat.source === "simulation") {
      const badge = document.createElement("span");
      badge.className = "simulation-badge";
      badge.textContent = "模拟";
      name.append(badge);
      avatar.classList.add("simulation-avatar");
    }
    const time = document.createElement("div");
    time.className = "chat-time";
    time.textContent = formatTime(latest?.timestamp || latest?.observed_at || chat.last_message_at);
    const preview = document.createElement("div");
    preview.className = "chat-preview";
    preview.textContent = chat.last_error ? `暂停：${chat.last_error}` : (latest?.message_type === "image" ? "[图片]" : (latest?.content || typeLabel(chat.chat_type)));
    top.append(name, time);
    copy.append(top, preview);
    button.append(avatarWrap, copy);
    list.append(button);
  }
}

function renderConversationMeta(chat) {
  if (state.view === "agents") {
    const running = state.agents.filter((item) => item.status?.state === "running").length;
    el("chat-title").textContent = "Agent 管理";
    el("chat-meta").textContent = `${state.agents.length} 个实例 · ${running} 个运行中`;
    return;
  }
  if (!chat) {
    el("chat-title").textContent = "选择一个会话";
    el("chat-meta").textContent = "消息通过可见微信界面安全收发";
    return;
  }
  el("chat-title").textContent = chat.name;
  if (chat.source === "simulation") {
    el("chat-meta").textContent = `模拟${typeLabel(chat.chat_type)} · 本地调试，不操作微信`;
    return;
  }
  const mode = state.status?.mode || (state.status?.paused ? "paused" : "polling");
  const activeLabel = mode === "paused" ? "已暂停" : mode === "unread" ? "新消息唤醒" : "持续轮询";
  const syncLabel = !state.status?.wechat_connected ? "等待微信连接" : (chat.last_error ? "同步遇到问题" : activeLabel);
  el("chat-meta").textContent = `${typeLabel(chat.chat_type)} · ${syncLabel}`;
}

function renderMessages({ forceBottom = false, preserveHeight = null } = {}) {
  const chat = state.chats.find((item) => item.id === state.activeChatId);
  if (!chat || state.view !== "chat") return;
  renderConversationMeta(chat);
  el("empty-state").hidden = true;
  messagesEl.hidden = false;
  composer.hidden = false;
  const simulated = chat.source === "simulation";
  simulateReceive.hidden = !simulated;
  input.placeholder = simulated
    ? "输入本地回复，Enter 发送，Shift+Enter 换行"
    : "输入消息，Enter 发送，Shift+Enter 换行";

  const distanceFromBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
  const wasNearBottom = distanceFromBottom < 80;
  const previousScrollTop = messagesEl.scrollTop;
  messagesEl.replaceChildren();

  if (state.olderMessages.get(chat.id)) {
    const older = document.createElement("button");
    older.type = "button";
    older.className = "load-older";
    older.textContent = "加载更早消息";
    older.addEventListener("click", () => loadOlderMessages(chat.id, older));
    messagesEl.append(older);
  }

  let previousTime = "";
  for (const message of state.messages.get(chat.id) || []) {
    const timeLabel = message.timestamp ? formatTime(message.timestamp) : "";
    if (timeLabel && timeLabel !== previousTime) {
      const divider = document.createElement("div");
      divider.className = "message-time-divider";
      divider.textContent = timeLabel;
      messagesEl.append(divider);
      previousTime = timeLabel;
    }
    const row = document.createElement("div");
    row.className = `message-row ${message.direction}`;
    const block = document.createElement("div");
    block.className = "message-block";
    if (message.direction !== "outgoing" && message.sender) {
      const sender = document.createElement("div");
      sender.className = "sender";
      sender.textContent = message.sender_organization
        ? `${message.sender} · ${message.sender_organization}`
        : message.sender;
      block.append(sender);
    }
    const bubbleLine = document.createElement("div");
    bubbleLine.className = "bubble-line";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (message.message_type === "image") {
      bubble.classList.add("image-bubble");
      if (message.image_url) {
        const link = document.createElement("a");
        link.href = message.image_url;
        link.target = "_blank";
        link.rel = "noopener";
        link.title = "打开图片";
        const image = document.createElement("img");
        image.src = message.image_url;
        image.alt = message.content || "微信图片";
        image.loading = "lazy";
        if (message.image_width) image.width = message.image_width;
        if (message.image_height) image.height = message.image_height;
        link.append(image);
        bubble.append(link);
      } else {
        const placeholder = document.createElement("span");
        placeholder.className = "image-placeholder";
        placeholder.textContent = "图片暂不可见";
        bubble.append(placeholder);
      }
    } else {
      bubble.textContent = message.content;
    }
    bubbleLine.append(bubble);
    if (message.direction === "outgoing" && message.delivery_status === "pending") {
      const pending = document.createElement("button");
      pending.className = "delivery-state pending";
      pending.type = "button";
      pending.disabled = true;
      pending.setAttribute("aria-label", "发送中");
      pending.title = "正在等待微信界面发送";
      bubbleLine.append(pending);
    } else if (message.direction === "outgoing" && message.delivery_status === "failed") {
      const failed = document.createElement("button");
      failed.className = "delivery-state failed";
      failed.type = "button";
      failed.textContent = "!";
      failed.setAttribute("aria-label", "发送失败");
      failed.title = message.delivery_error || "发送失败";
      bubbleLine.append(failed);
    }
    block.append(bubbleLine);
    row.append(block);
    messagesEl.append(row);
  }

  if (preserveHeight !== null) messagesEl.scrollTop = messagesEl.scrollHeight - preserveHeight;
  else if (forceBottom || wasNearBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
  else messagesEl.scrollTop = previousScrollTop;
}

async function loadOlderMessages(chatId, button) {
  const values = state.messages.get(chatId) || [];
  const before = values.find((item) => typeof item.seq === "number")?.seq;
  if (!before) return;
  button.disabled = true;
  button.textContent = "正在加载…";
  const oldHeight = messagesEl.scrollHeight;
  try {
    const data = await api(`/api/chats/${chatId}/messages?before=${before}&limit=200`);
    for (const message of data.messages) upsertMessage(message);
    state.olderMessages.set(chatId, data.messages.length === 200);
    renderMessages({ preserveHeight: oldHeight });
  } catch (error) {
    button.disabled = false;
    button.textContent = `加载失败：${error.message}`;
  }
}

function filteredLogs() {
  const query = logSearch.value.trim().toLocaleLowerCase();
  return state.logs.filter((item) => {
    if (state.activeLogAgent && item.agent !== state.activeLogAgent) return false;
    if (logLevel.value !== "all" && item.level !== logLevel.value) return false;
    if (!query) return true;
    const context = JSON.stringify(item.context || {});
    return `${item.agent} ${item.event} ${item.message} ${context}`.toLocaleLowerCase().includes(query);
  });
}

function agentStateLabel(value) {
  return value === "running" ? "运行中" : value === "stopping" ? "停止中" : value === "failed" ? "异常退出" : "已停止";
}

function policySummary(instance, capability) {
  const policy = instance.permissions?.[capability] || { mode: "all" };
  const matches = instance.access?.[capability] || [];
  const names = matches.slice(0, 3).map((chat) => chat.name).join("、");
  const suffix = matches.length > 3 ? ` 等 ${matches.length} 个` : (names || "无匹配会话");
  if (policy.mode === "all") return `所有会话（当前 ${matches.length} 个）`;
  if (policy.mode === "selected") return `指定：${suffix}`;
  if (policy.mode === "regex_allow") return `正则白名单：${suffix}`;
  return `正则黑名单外：${suffix}`;
}

function renderAgents() {
  if (state.view !== "agents") return;
  renderConversationMeta(null);
  el("empty-state").hidden = true;
  messagesEl.hidden = true;
  composer.hidden = true;
  agentPanel.hidden = false;
  agentList.replaceChildren();
  if (!state.agents.length) {
    const empty = document.createElement("div");
    empty.className = "agent-empty";
    empty.textContent = state.plugins.length ? "还没有 Agent 实例。点击“添加 Agent”从已安装插件创建。" : "尚未安装 Agent 插件。请先将插件目录挂载到 bowxt。";
    agentList.append(empty);
    return;
  }
  for (const instance of state.agents) {
    const card = document.createElement("article");
    card.className = "agent-card";
    const heading = document.createElement("div");
    heading.className = "agent-card-heading";
    const copy = document.createElement("div");
    const title = document.createElement("h4");
    title.textContent = instance.name;
    const identity = document.createElement("p");
    identity.textContent = `${instance.plugin?.name || instance.plugin_id} · ${instance.id}`;
    copy.append(title, identity);
    const badge = document.createElement("span");
    badge.className = `agent-state ${instance.status?.state || "stopped"}`;
    badge.textContent = agentStateLabel(instance.status?.state);
    heading.append(copy, badge);
    const description = document.createElement("p");
    description.className = "agent-description";
    description.textContent = instance.plugin?.description || "插件当前不可用，请检查挂载目录。";
    const meta = document.createElement("div");
    meta.className = "agent-meta";
    meta.textContent = `${instance.autostart ? "自动启动" : "手动启动"}${instance.status?.pid ? ` · PID ${instance.status.pid}` : ""}`;
    const scope = document.createElement("section");
    scope.className = "agent-scope";
    const claimed = instance.consumer_activity?.chats || [];
    const claimTitle = document.createElement("strong");
    claimTitle.textContent = "consumer 实际监听";
    const claimValue = document.createElement("div");
    claimValue.className = "agent-chat-chips";
    if (claimed.length) {
      for (const chat of claimed) {
        const chip = document.createElement("span");
        chip.textContent = chat.name;
        chip.title = `会话 ID ${chat.id}`;
        claimValue.append(chip);
      }
    } else {
      const emptyScope = document.createElement("span");
      emptyScope.className = "scope-empty";
      emptyScope.textContent = "尚未领取消息";
      claimValue.append(emptyScope);
    }
    const policy = document.createElement("p");
    policy.textContent = `可读：${policySummary(instance, "read")}\n可写：${policySummary(instance, "write")}`;
    scope.append(claimTitle, claimValue, policy);
    const actions = document.createElement("div");
    actions.className = "agent-actions";
    const running = ["running", "stopping"].includes(instance.status?.state);
    const start = document.createElement("button");
    start.type = "button";
    start.textContent = running ? "停止" : "启动";
    start.disabled = instance.status?.state === "stopping" || !instance.plugin_available;
    start.addEventListener("click", () => controlAgent(instance.id, running ? "stop" : "start", start));
    const restart = document.createElement("button");
    restart.type = "button";
    restart.textContent = "重启";
    restart.disabled = !running || instance.status?.state === "stopping";
    restart.addEventListener("click", () => controlAgent(instance.id, "restart", restart));
    const configure = document.createElement("button");
    configure.type = "button";
    configure.textContent = "配置";
    configure.addEventListener("click", () => openAgentDialog(instance));
    const logs = document.createElement("button");
    logs.type = "button";
    logs.textContent = "查看日志";
    logs.addEventListener("click", () => openAgentLogs(instance));
    actions.append(start, restart, configure, logs);
    for (const panel of instance.panels || []) {
      const custom = document.createElement("button");
      custom.type = "button";
      custom.className = "panel-action";
      custom.textContent = panel.title;
      custom.title = `自定义面板 · 更新于 ${formatLogTime(panel.updated_at)}`;
      custom.addEventListener("click", () => openCustomPanel(instance, panel));
      actions.append(custom);
    }
    card.append(heading, description, meta, scope, actions);
    agentList.append(card);
  }
}

function panelHeading(node) {
  const heading = document.createElement("span");
  heading.className = "panel-node-heading";
  const label = document.createElement("span");
  label.className = "panel-node-label";
  label.textContent = node.label;
  heading.append(label);
  if (node.meta) {
    const meta = document.createElement("span");
    meta.className = "panel-node-meta";
    meta.textContent = node.meta;
    heading.append(meta);
  }
  return heading;
}

function renderPanelNode(node) {
  const children = Array.isArray(node.children) ? node.children : [];
  const root = document.createElement(children.length ? "details" : "section");
  root.className = `${children.length ? "panel-branch" : "panel-leaf"} tone-${node.tone || "neutral"}`;
  if (node.id) root.dataset.nodeId = node.id;
  if (children.length) {
    root.open = Boolean(node.expanded);
    const summary = document.createElement("summary");
    summary.append(panelHeading(node));
    root.append(summary);
  } else {
    root.append(panelHeading(node));
  }
  if (node.value) {
    const value = document.createElement("p");
    value.className = "panel-node-value";
    value.textContent = node.value;
    root.append(value);
  }
  if (children.length) {
    const nested = document.createElement("div");
    nested.className = "panel-children";
    for (const child of children) nested.append(renderPanelNode(child));
    root.append(nested);
  }
  return root;
}

function renderCustomPanel(panel) {
  const content = el("agent-custom-panel-content");
  content.replaceChildren();
  const documentValue = panel.document || {};
  if (documentValue.type !== "tree" || !Array.isArray(documentValue.nodes)) {
    const invalid = document.createElement("div");
    invalid.className = "custom-panel-empty";
    invalid.textContent = "无法展示：不支持的面板格式";
    content.append(invalid);
    return;
  }
  if (!documentValue.nodes.length) {
    const empty = document.createElement("div");
    empty.className = "custom-panel-empty";
    empty.textContent = documentValue.empty_text || "暂无数据";
    content.append(empty);
    return;
  }
  const tree = document.createElement("div");
  tree.className = "panel-tree";
  for (const node of documentValue.nodes) tree.append(renderPanelNode(node));
  content.append(tree);
}

async function openCustomPanel(instance, summary) {
  state.activeCustomPanel = { agent: instance.id, panelId: summary.id };
  el("agent-custom-panel-title").textContent = `${instance.name} · ${summary.title}`;
  el("agent-custom-panel-meta").textContent = "正在加载 Agent 面板…";
  const content = el("agent-custom-panel-content");
  content.replaceChildren();
  const loading = document.createElement("div");
  loading.className = "custom-panel-empty";
  loading.textContent = "正在加载…";
  content.append(loading);
  if (!el("agent-custom-panel-dialog").open) el("agent-custom-panel-dialog").showModal();
  try {
    const data = await api(`/api/agent/instances/${encodeURIComponent(instance.id)}/panels/${encodeURIComponent(summary.id)}`);
    if (state.activeCustomPanel?.agent !== instance.id || state.activeCustomPanel?.panelId !== summary.id) return;
    el("agent-custom-panel-title").textContent = `${instance.name} · ${data.panel.title}`;
    el("agent-custom-panel-meta").textContent = `更新于 ${formatLogTime(data.panel.updated_at)} · 声明式只读面板`;
    renderCustomPanel(data.panel);
  } catch (error) {
    loading.textContent = `面板加载失败：${error.message}`;
  }
}

async function refreshAgents() {
  const data = await api("/api/agent/instances");
  state.agents = data.instances;
  if (state.view === "agents") renderAgents();
}

async function openAgentLogs(instance) {
  state.activeLogAgent = instance.id;
  state.olderLogs = false;
  el("agent-log-title").textContent = `${instance.name} · 日志`;
  el("agent-log-dialog").showModal();
  logsEl.replaceChildren();
  const loading = document.createElement("div");
  loading.className = "log-empty";
  loading.textContent = "正在加载日志…";
  logsEl.append(loading);
  try {
    const data = await api(`/api/agent/logs?agent=${encodeURIComponent(instance.id)}&limit=300&recent=1`);
    for (const item of data.logs) upsertLog(item);
    state.olderLogs = data.logs.length === 300;
    renderLogs({ forceBottom: true });
  } catch (error) {
    loading.textContent = `日志加载失败：${error.message}`;
  }
}

async function controlAgent(instanceId, action, button) {
  button.disabled = true;
  try {
    await api(`/api/agent/instances/${encodeURIComponent(instanceId)}/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    await refreshAgents();
  } catch (error) {
    button.textContent = `失败：${error.message}`;
    setTimeout(() => renderAgents(), 1600);
  }
}

function renderLogs({ forceBottom = false, preserveHeight = null } = {}) {
  if (!el("agent-log-dialog").open) return;
  const oldScrollTop = logsEl.scrollTop;
  const wasNearBottom = logsEl.scrollHeight - logsEl.scrollTop - logsEl.clientHeight < 80;
  logsEl.replaceChildren();

  if (state.olderLogs) {
    const older = document.createElement("button");
    older.type = "button";
    older.className = "load-older";
    older.textContent = "加载更早日志";
    older.addEventListener("click", () => loadOlderLogs(older));
    logsEl.append(older);
  }

  const values = filteredLogs();
  if (!values.length) {
    const empty = document.createElement("div");
    empty.className = "log-empty";
    empty.textContent = state.logs.length ? "没有符合筛选条件的日志" : "尚无 Agent 日志\nAgentClient.log() 写入后会实时显示在这里";
    logsEl.append(empty);
  }
  for (const item of values) {
    const row = document.createElement("div");
    row.className = "log-row";
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = formatLogTime(item.created_at);
    const level = document.createElement("span");
    level.className = `log-level ${item.level}`;
    level.textContent = item.level;
    const source = document.createElement("span");
    source.className = "log-source";
    source.textContent = item.agent;
    source.title = item.agent;
    const content = document.createElement("div");
    content.className = "log-content";
    const event = document.createElement("span");
    event.className = "log-event";
    event.textContent = item.event;
    const message = document.createElement("span");
    message.textContent = item.message;
    content.append(event, message);
    if (item.context && Object.keys(item.context).length) {
      const context = document.createElement("div");
      context.className = "log-context";
      context.textContent = JSON.stringify(item.context, null, 2);
      content.append(context);
    }
    row.append(time, level, source, content);
    logsEl.append(row);
  }

  if (preserveHeight !== null) logsEl.scrollTop = logsEl.scrollHeight - preserveHeight;
  else if (forceBottom || (wasNearBottom && logFollow.checked)) logsEl.scrollTop = logsEl.scrollHeight;
  else logsEl.scrollTop = oldScrollTop;
}

async function loadOlderLogs(button) {
  const visible = state.logs.filter((item) => item.agent === state.activeLogAgent);
  const before = visible[0]?.seq;
  if (!before) return;
  button.disabled = true;
  button.textContent = "正在加载…";
  const oldHeight = logsEl.scrollHeight;
  try {
    const data = await api(`/api/agent/logs?agent=${encodeURIComponent(state.activeLogAgent)}&before=${before}&limit=300`);
    for (const item of data.logs) upsertLog(item);
    state.olderLogs = data.logs.length === 300;
    renderLogs({ preserveHeight: oldHeight });
  } catch (error) {
    button.disabled = false;
    button.textContent = `加载失败：${error.message}`;
  }
}

function setView(view) {
  state.view = view;
  el("show-chat").classList.toggle("active", view === "chat");
  el("show-agents").classList.toggle("active", view === "agents");
  renderChats();
  agentPanel.hidden = view !== "agents";
  if (view === "agents") renderAgents();
  else if (state.activeChatId !== null) renderMessages();
  else {
    renderConversationMeta(null);
    el("empty-state").hidden = false;
    messagesEl.hidden = true;
    composer.hidden = true;
    agentPanel.hidden = true;
  }
}

async function selectChat(id) {
  state.activeChatId = id;
  state.unreadChats.delete(id);
  if (!state.loadedChats.has(id)) {
    const data = await api(`/api/chats/${id}/messages?limit=200&recent=1`);
    state.messages.set(id, []);
    for (const message of data.messages) upsertMessage(message);
    state.olderMessages.set(id, data.messages.length === 200);
    state.loadedChats.add(id);
  }
  setView("chat");
  renderMessages({ forceBottom: true });
  input.focus();
}

function updateStatus(status) {
  state.status = status;
  const connected = Boolean(status.wechat_connected);
  const mode = status.mode || (status.paused ? "paused" : "polling");
  const paused = mode === "paused";
  el("status-dot").className = paused ? "paused" : (connected ? "online" : "offline");
  const statusText = el("status-text");
  statusText.textContent = paused
    ? (status.active_chat ? "当前操作结束后暂停" : "已暂停所有微信操作")
    : connected
      ? (status.active_chat ? `正在读取 ${status.active_chat}` : (mode === "unread" ? "等待微信新消息" : "微信已连接，持续轮询"))
      : "微信未连接，模拟会话可用";
  statusText.title = status.last_error || "";
  if (document.activeElement !== syncMode) syncMode.value = mode;
  el("poll-gap-label").textContent = mode === "unread" ? "红点检查间隔" : "轮询间隔";
  el("mode-help").textContent = mode === "unread"
    ? "仅在侧边栏出现未读标记时切换会话；当前会话只读不跳转。"
    : mode === "paused" ? "暂停后不读取、切换或发送消息。" : "轮询模式会依次打开所有已监听会话。";
  input.disabled = paused;
  sendButton.disabled = paused;
  simulateReceive.disabled = paused;
  el("queue-depth").textContent = `队列 ${status.queue_depth || 0}`;
  if (document.activeElement !== pollGap) pollGap.value = String(status.poll_gap || 1.5);
  pollGapValue.textContent = `${Number(status.poll_gap || pollGap.value).toFixed(1)} 秒`;
  if (document.activeElement !== actionDelay) actionDelay.value = String(status.action_delay || 0.12);
  actionDelayValue.textContent = `${Math.round(Number(status.action_delay || actionDelay.value) * 1000)} ms`;
  const timing = status.last_send_timings;
  const timingEl = el("send-timing");
  if (timing) {
    const seconds = (key) => Number(timing[key] || 0).toFixed(1);
    timingEl.textContent = `上次发送 ${seconds("end_to_end_s")}s：排队 ${seconds("queue_wait_s")} / 切换 ${seconds("open_chat_s")} / 输入 ${seconds("input_to_enter_s")} / 确认 ${seconds("verify_s")}`;
    timingEl.hidden = false;
  } else timingEl.hidden = true;
  renderConversationMeta(state.chats.find((item) => item.id === state.activeChatId));
}

function processMessage(message, { markUnread = true } = {}) {
  const isNew = upsertMessage(message);
  if (isNew && markUnread && message.chat_id !== state.activeChatId && message.delivery_status === "observed" && message.direction !== "outgoing") {
    state.unreadChats.add(message.chat_id);
  }
  const chat = state.chats.find((item) => item.id === message.chat_id);
  if (chat) chat.last_message_at = message.timestamp || message.observed_at;
  renderChats();
  if (state.view === "chat" && state.activeChatId === message.chat_id) renderMessages();
}

async function syncDurableMessages() {
  while (true) {
    const data = await api(`/api/messages?after=${state.messageCursor}&limit=1000`);
    for (const message of data.messages) {
      processMessage(message);
      state.messageCursor = Math.max(state.messageCursor, Number(message.seq) || 0);
    }
    if (data.messages.length < 1000) return;
  }
}

async function syncDurableLogs() {
  while (true) {
    const data = await api(`/api/agent/logs?after=${state.logCursor}&limit=1000`);
    for (const item of data.logs) upsertLog(item);
    if (data.logs.length < 1000) break;
  }
  if (el("agent-log-dialog").open) renderLogs();
}

async function refreshLoadedChats() {
  const loaded = [...state.loadedChats];
  await Promise.all(loaded.map(async (chatId) => {
    const data = await api(`/api/chats/${chatId}/messages?limit=200&recent=1`);
    for (const message of data.messages) upsertMessage(message);
  }));
  if (state.view === "chat" && state.activeChatId !== null) renderMessages();
}

async function load() {
  const [chatData, status, logData, streamHead, pluginData, agentData] = await Promise.all([
    api("/api/chats"),
    api("/api/status"),
    api("/api/agent/logs?limit=300&recent=1"),
    api("/api/messages?limit=1&recent=1"),
    api("/api/agent/plugins"),
    api("/api/agent/instances"),
  ]);
  state.messageCursor = Number(streamHead.messages[0]?.seq || 0);
  state.chats = chatData.chats;
  state.plugins = pluginData.plugins;
  state.agents = agentData.instances;
  for (const item of logData.logs) upsertLog(item);
  state.logsLoaded = true;
  state.olderLogs = logData.logs.length === 300;
  await Promise.all(state.chats.map(async (chat) => {
    const data = await api(`/api/chats/${chat.id}/messages?limit=1&recent=1`);
    state.messages.set(chat.id, []);
    for (const message of data.messages) upsertMessage(message);
  }));
  updateStatus(status);
  renderChats();
  if (state.chats.length) await selectChat(state.chats[0].id);

  const events = new EventSource("/api/events");
  events.onopen = () => {
    Promise.all([syncDurableMessages(), syncDurableLogs(), refreshLoadedChats()]).catch((error) => {
      el("status-text").textContent = `补拉失败：${error.message}`;
    });
  };
  events.onmessage = ({ data }) => {
    const event = JSON.parse(data);
    if (event.type === "status") updateStatus(event.status);
    if (event.type === "chat") {
      const index = state.chats.findIndex((item) => item.id === event.chat.id);
      if (index >= 0) state.chats[index] = event.chat;
      else state.chats.unshift(event.chat);
      renderChats();
      if (state.activeChatId === null) selectChat(event.chat.id);
    }
    if (event.type === "message") processMessage(event.message);
    if (event.type === "agent_log") {
      upsertLog(event.log);
      if (el("agent-log-dialog").open) renderLogs();
    }
    if (event.type === "agent_instance") {
      const index = state.agents.findIndex((item) => item.id === event.instance.id);
      if (index >= 0) state.agents[index] = event.instance;
      else state.agents.push(event.instance);
      if (state.view === "agents") renderAgents();
    }
    if (event.type === "agent_instance_removed") {
      state.agents = state.agents.filter((item) => item.id !== event.instance_id);
      if (state.view === "agents") renderAgents();
    }
    if (event.type === "agent_panel") {
      const instance = state.agents.find((item) => item.id === event.panel.agent);
      if (instance) {
        instance.panels = instance.panels || [];
        const index = instance.panels.findIndex((item) => item.id === event.panel.id);
        if (index >= 0) instance.panels[index] = event.panel;
        else instance.panels.push(event.panel);
        if (state.view === "agents") renderAgents();
      }
      if (state.activeCustomPanel?.agent === event.panel.agent && state.activeCustomPanel?.panelId === event.panel.id) {
        openCustomPanel(instance || { id: event.panel.agent, name: event.panel.agent }, event.panel);
      }
    }
    if (event.type === "agent_panel_removed") {
      const instance = state.agents.find((item) => item.id === event.agent);
      if (instance) instance.panels = (instance.panels || []).filter((item) => item.id !== event.panel_id);
      if (state.activeCustomPanel?.agent === event.agent && state.activeCustomPanel?.panelId === event.panel_id) {
        el("agent-custom-panel-dialog").close();
        state.activeCustomPanel = null;
      }
      if (state.view === "agents") renderAgents();
    }
  };
  events.onerror = () => { el("status-text").textContent = "消息流重连中"; };
}

composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text || state.activeChatId === null || state.status?.paused) return;
  const chatId = state.activeChatId;
  const clientId = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`).replace(/[^A-Za-z0-9_-]/g, "");
  const now = new Date().toISOString();
  const optimistic = {
    seq: `ui:${clientId}`,
    message_id: `local:${clientId}`,
    client_id: clientId,
    chat_id: chatId,
    content: text,
    direction: "outgoing",
    timestamp: now,
    observed_at: now,
    delivery_status: "pending",
    delivery_error: null,
  };
  input.value = "";
  input.style.height = "auto";
  upsertMessage(optimistic);
  renderChats();
  if (state.activeChatId === chatId) renderMessages({ forceBottom: true });
  input.focus();
  try {
    const data = await api(`/api/chats/${chatId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, client_id: clientId }),
    });
    upsertMessage(data.message);
  } catch (error) {
    upsertMessage({ ...optimistic, delivery_status: "failed", delivery_error: error.message });
  }
  renderChats();
  if (state.activeChatId === chatId) renderMessages();
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    composer.requestSubmit();
  }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 130)}px`;
  input.style.overflowY = input.scrollHeight > 130 ? "auto" : "hidden";
});

el("show-chat").addEventListener("click", () => setView("chat"));
el("show-agents").addEventListener("click", () => setView("agents"));
el("close-agent-custom-panel-dialog").addEventListener("click", () => {
  el("agent-custom-panel-dialog").close();
  state.activeCustomPanel = null;
});
el("agent-custom-panel-dialog").addEventListener("close", () => {
  state.activeCustomPanel = null;
});
logSearch.addEventListener("input", () => renderLogs());
logLevel.addEventListener("change", () => renderLogs());
el("copy-logs").addEventListener("click", async () => {
  const text = filteredLogs().map((item) => `${item.created_at} ${item.level.toUpperCase()} ${item.agent} ${item.event} ${item.message}${Object.keys(item.context || {}).length ? ` ${JSON.stringify(item.context)}` : ""}`).join("\n");
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    el("copy-logs").textContent = "已复制";
    setTimeout(() => { el("copy-logs").textContent = "复制可见日志"; }, 1200);
  } catch (error) {
    el("copy-logs").textContent = `复制失败`;
  }
});

syncMode.addEventListener("change", async () => {
  syncMode.disabled = true;
  try {
    updateStatus(await api("/api/control", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: syncMode.value }),
    }));
  } catch (error) {
    el("status-text").textContent = `控制失败：${error.message}`;
  } finally {
    syncMode.disabled = false;
  }
});

pollGap.addEventListener("input", () => { pollGapValue.textContent = `${Number(pollGap.value).toFixed(1)} 秒`; });
pollGap.addEventListener("change", async () => {
  try {
    updateStatus(await api("/api/control", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ poll_gap: Number(pollGap.value) }),
    }));
  } catch (error) {
    el("status-text").textContent = `速度设置失败：${error.message}`;
  }
});

actionDelay.addEventListener("input", () => { actionDelayValue.textContent = `${Math.round(Number(actionDelay.value) * 1000)} ms`; });
actionDelay.addEventListener("change", async () => {
  try {
    updateStatus(await api("/api/control", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action_delay: Number(actionDelay.value) }),
    }));
  } catch (error) {
    el("status-text").textContent = `键鼠速度设置失败：${error.message}`;
  }
});

const dialog = el("chat-dialog");

function updateChatDialogMode() {
  const simulated = el("new-chat-source").value === "simulation";
  const unknown = el("new-chat-unknown");
  unknown.disabled = simulated;
  if (simulated && el("new-chat-type").value === "unknown") {
    el("new-chat-type").value = "contact";
  }
  el("chat-dialog-help").textContent = simulated
    ? "本地模拟会话不依赖微信登录，可用于触发和观察 Agent"
    : "名称必须与微信中的联系人或群聊完全一致";
  el("create-chat-submit").textContent = simulated ? "创建模拟会话" : "开始监听";
}

el("add-chat").addEventListener("click", () => {
  el("dialog-error").textContent = "";
  updateChatDialogMode();
  dialog.showModal();
  el("new-chat-name").focus();
});
el("close-dialog").addEventListener("click", () => dialog.close());
el("new-chat-source").addEventListener("change", updateChatDialogMode);
el("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const simulated = el("new-chat-source").value === "simulation";
    const data = await api(simulated ? "/api/simulated-chats" : "/api/chats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: el("new-chat-name").value, chat_type: el("new-chat-type").value }),
    });
    const index = state.chats.findIndex((item) => item.id === data.chat.id);
    if (index >= 0) state.chats[index] = data.chat;
    else state.chats.unshift(data.chat);
    dialog.close();
    el("chat-form").reset();
    updateChatDialogMode();
    renderChats();
    await selectChat(data.chat.id);
  } catch (error) {
    el("dialog-error").textContent = error.message;
  }
});

const simulationDialog = el("simulation-dialog");

function updateSimulationMessageType() {
  const image = el("simulation-message-type").value === "image";
  el("simulation-text-field").hidden = image;
  el("simulation-image-field").hidden = !image;
}

function openSimulationDialog() {
  const chat = state.chats.find((item) => item.id === state.activeChatId);
  if (!chat || chat.source !== "simulation") return;
  el("simulation-form").reset();
  el("simulation-error").textContent = "";
  el("simulation-time").value = localDateTimeValue();
  el("simulation-at-me").checked = true;
  const isGroup = chat.chat_type === "group";
  el("simulation-sender-fields").hidden = !isGroup;
  el("simulation-sender").required = isGroup;
  el("simulation-chat-label").textContent = `${chat.name} · 消息会进入正常 Agent 投递链路，不操作微信`;
  updateSimulationMessageType();
  simulationDialog.showModal();
  (isGroup ? el("simulation-sender") : el("simulation-text")).focus();
}

function fileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error || new Error("读取图片失败"));
    reader.readAsDataURL(file);
  });
}

simulateReceive.addEventListener("click", openSimulationDialog);
el("close-simulation-dialog").addEventListener("click", () => simulationDialog.close());
el("simulation-message-type").addEventListener("change", updateSimulationMessageType);
el("simulation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const chat = state.chats.find((item) => item.id === state.activeChatId);
  if (!chat || chat.source !== "simulation") return;
  const submit = el("simulation-submit");
  submit.disabled = true;
  el("simulation-error").textContent = "";
  try {
    const kind = el("simulation-message-type").value;
    const payload = {
      sender: chat.chat_type === "group" ? el("simulation-sender").value : undefined,
      sender_organization: chat.chat_type === "group" ? el("simulation-organization").value : undefined,
      is_at_me: el("simulation-at-me").checked,
    };
    const localTime = el("simulation-time").value;
    if (localTime) payload.timestamp = new Date(localTime).toISOString();
    if (kind === "image") {
      const file = el("simulation-image").files[0];
      if (!file) throw new Error("请选择一张图片");
      if (file.size > 10 * 1024 * 1024) throw new Error("图片不能超过 10 MiB");
      payload.image = {
        data: await fileAsBase64(file),
        mime_type: file.type || "application/octet-stream",
        name: file.name,
      };
    } else {
      payload.text = el("simulation-text").value.trim();
      if (!payload.text) throw new Error("模拟文字消息不能为空");
    }
    const data = await api(`/api/chats/${chat.id}/simulate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    processMessage(data.message, { markUnread: false });
    simulationDialog.close();
    renderMessages({ forceBottom: true });
  } catch (error) {
    el("simulation-error").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

const agentDialog = el("agent-dialog");
let editingAgentId = null;

function defaultPermissions() {
  return {
    read: { mode: "all", chat_ids: [], patterns: [] },
    write: { mode: "all", chat_ids: [], patterns: [] },
  };
}

function updatePermissionVisibility(capability) {
  const mode = el(`agent-${capability}-mode`).value;
  el(`agent-${capability}-chats`).hidden = mode !== "selected";
  el(`agent-${capability}-pattern-field`).hidden = !mode.startsWith("regex_");
}

function renderPermissionEditor(capability, policy) {
  const value = policy || defaultPermissions()[capability];
  el(`agent-${capability}-mode`).value = value.mode || "all";
  el(`agent-${capability}-patterns`).value = (value.patterns || []).join("\n");
  const selected = new Set((value.chat_ids || []).map(Number));
  const root = el(`agent-${capability}-chats`);
  root.replaceChildren();
  if (!state.chats.length) {
    const empty = document.createElement("p");
    empty.className = "field-help";
    empty.textContent = "当前还没有会话。";
    root.append(empty);
  }
  for (const chat of state.chats) {
    const label = document.createElement("label");
    label.className = "permission-chat";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = String(chat.id);
    checkbox.checked = selected.has(chat.id);
    const name = document.createElement("span");
    name.textContent = chat.name;
    const id = document.createElement("small");
    id.textContent = `#${chat.id}`;
    label.append(checkbox, name, id);
    root.append(label);
  }
  updatePermissionVisibility(capability);
}

function readPermissionEditor(capability) {
  const mode = el(`agent-${capability}-mode`).value;
  const chatIds = [...el(`agent-${capability}-chats`).querySelectorAll("input:checked")]
    .map((input) => Number(input.value));
  const patterns = el(`agent-${capability}-patterns`).value
    .split("\n").map((value) => value.trim()).filter(Boolean);
  for (const pattern of patterns) new RegExp(pattern);
  return { mode, chat_ids: chatIds, patterns };
}

function selectedAgentPlugin() {
  return state.plugins.find((item) => item.id === el("agent-plugin").value);
}

function renderAgentSecretFields(plugin, instance = null) {
  const root = el("agent-secrets");
  root.replaceChildren();
  for (const field of plugin?.secret_schema || []) {
    const label = document.createElement("label");
    label.textContent = field.label || field.name;
    const input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "new-password";
    input.dataset.secret = field.name;
    input.placeholder = instance?.secrets?.[field.name]?.configured ? "已配置；留空保持不变" : (field.required ? "必填" : "可选");
    input.required = Boolean(field.required && !instance?.secrets?.[field.name]?.configured);
    label.append(input);
    root.append(label);
  }
  if (!root.children.length) {
    const empty = document.createElement("p");
    empty.className = "field-help";
    empty.textContent = "此插件没有声明密钥字段。";
    root.append(empty);
  }
}

function updateAgentPluginDefaults() {
  if (editingAgentId) return;
  const plugin = selectedAgentPlugin();
  el("agent-config").value = JSON.stringify(plugin?.default_config || {}, null, 2);
  el("agent-config-help").textContent = plugin?.config_schema?.help || "配置以 JSON 保存到实例私有目录。";
  renderAgentSecretFields(plugin);
}

function openAgentDialog(instance = null) {
  editingAgentId = instance?.id || null;
  el("agent-dialog-error").textContent = "";
  el("agent-dialog-title").textContent = instance ? `配置 ${instance.name}` : "添加 Agent";
  const pluginSelect = el("agent-plugin");
  pluginSelect.replaceChildren();
  for (const plugin of state.plugins) pluginSelect.append(new Option(`${plugin.name} (${plugin.version})`, plugin.id));
  pluginSelect.disabled = Boolean(instance);
  el("agent-id").disabled = Boolean(instance);
  if (instance) {
    pluginSelect.value = instance.plugin_id;
    el("agent-id").value = instance.id;
    el("agent-name").value = instance.name;
    el("agent-config").value = JSON.stringify(instance.config || {}, null, 2);
    el("agent-autostart").checked = Boolean(instance.autostart);
    const plugin = state.plugins.find((item) => item.id === instance.plugin_id);
    el("agent-config-help").textContent = plugin?.config_schema?.help || "配置以 JSON 保存到实例私有目录。";
    renderAgentSecretFields(plugin, instance);
    renderPermissionEditor("read", instance.permissions?.read);
    renderPermissionEditor("write", instance.permissions?.write);
  } else {
    el("agent-id").value = "";
    el("agent-name").value = "";
    el("agent-autostart").checked = false;
    updateAgentPluginDefaults();
    renderPermissionEditor("read", defaultPermissions().read);
    renderPermissionEditor("write", defaultPermissions().write);
  }
  agentDialog.showModal();
}

el("add-agent").addEventListener("click", () => {
  if (!state.plugins.length) return;
  openAgentDialog();
});
el("close-agent-dialog").addEventListener("click", () => agentDialog.close());
el("agent-plugin").addEventListener("change", updateAgentPluginDefaults);
for (const capability of ["read", "write"]) {
  el(`agent-${capability}-mode`).addEventListener("change", () => updatePermissionVisibility(capability));
}
el("agent-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorEl = el("agent-dialog-error");
  errorEl.textContent = "";
  let config;
  try {
    config = JSON.parse(el("agent-config").value);
    if (!config || Array.isArray(config) || typeof config !== "object") throw new Error("配置必须是 JSON 对象");
  } catch (error) {
    errorEl.textContent = `配置 JSON 无效：${error.message}`;
    return;
  }
  const secrets = {};
  for (const input of el("agent-secrets").querySelectorAll("input[data-secret]")) {
    if (input.value) secrets[input.dataset.secret] = input.value;
  }
  let permissions;
  try {
    permissions = {
      read: readPermissionEditor("read"),
      write: readPermissionEditor("write"),
    };
  } catch (error) {
    errorEl.textContent = `会话权限中的正则表达式无效：${error.message}`;
    return;
  }
  const payload = {
    name: el("agent-name").value.trim(), config, secrets,
    permissions,
    autostart: el("agent-autostart").checked,
  };
  if (!editingAgentId) {
    payload.id = el("agent-id").value.trim();
    payload.plugin_id = el("agent-plugin").value;
  }
  const editingInstance = state.agents.find((item) => item.id === editingAgentId);
  if (editingInstance?.status?.state === "running") {
    const confirmed = window.confirm(
      `${editingInstance.name} 正在运行。保存配置需要停止并重启 Agent，处理中但尚未确认的消息会在租约到期后重新投递。是否继续？`
    );
    if (!confirmed) return;
    payload.restart = true;
  }
  try {
    await api(editingAgentId ? `/api/agent/instances/${encodeURIComponent(editingAgentId)}` : "/api/agent/instances", {
      method: editingAgentId ? "PATCH" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    agentDialog.close();
    await refreshAgents();
  } catch (error) {
    errorEl.textContent = error.message;
  }
});

el("close-agent-log-dialog").addEventListener("click", () => el("agent-log-dialog").close());

load().catch((error) => { updateStatus({ wechat_connected: false, last_error: error.message }); });
