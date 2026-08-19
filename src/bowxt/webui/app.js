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
};

const el = (id) => document.getElementById(id);
const list = el("chat-list");
const messagesEl = el("messages");
const composer = el("composer");
const input = el("message-input");
const sendButton = el("send-button");
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
  if (state.view === "logs") {
    el("chat-title").textContent = "Agent 日志";
    el("chat-meta").textContent = `${state.logs.length} 条已加载 · 日志持久化并实时更新`;
    return;
  }
  if (!chat) {
    el("chat-title").textContent = "选择一个会话";
    el("chat-meta").textContent = "消息通过可见微信界面安全收发";
    return;
  }
  el("chat-title").textContent = chat.name;
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
  logPanel.hidden = true;

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
      sender.textContent = message.sender;
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
    if (logLevel.value !== "all" && item.level !== logLevel.value) return false;
    if (!query) return true;
    const context = JSON.stringify(item.context || {});
    return `${item.agent} ${item.event} ${item.message} ${context}`.toLocaleLowerCase().includes(query);
  });
}

function renderLogs({ forceBottom = false, preserveHeight = null } = {}) {
  if (state.view !== "logs") return;
  renderConversationMeta(null);
  el("empty-state").hidden = true;
  messagesEl.hidden = true;
  composer.hidden = true;
  logPanel.hidden = false;
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
  const before = state.logs[0]?.seq;
  if (!before) return;
  button.disabled = true;
  button.textContent = "正在加载…";
  const oldHeight = logsEl.scrollHeight;
  try {
    const data = await api(`/api/agent/logs?before=${before}&limit=300`);
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
  el("show-logs").classList.toggle("active", view === "logs");
  renderChats();
  if (view === "logs") renderLogs({ forceBottom: true });
  else if (state.activeChatId !== null) renderMessages();
  else {
    renderConversationMeta(null);
    el("empty-state").hidden = false;
    messagesEl.hidden = true;
    composer.hidden = true;
    logPanel.hidden = true;
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
      : "等待微信登录";
  statusText.title = status.last_error || "";
  if (document.activeElement !== syncMode) syncMode.value = mode;
  el("poll-gap-label").textContent = mode === "unread" ? "红点检查间隔" : "轮询间隔";
  el("mode-help").textContent = mode === "unread"
    ? "仅在侧边栏出现未读标记时切换会话；当前会话只读不跳转。"
    : mode === "paused" ? "暂停后不读取、切换或发送消息。" : "轮询模式会依次打开所有已监听会话。";
  input.disabled = paused;
  sendButton.disabled = paused;
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
  if (state.view === "logs") renderLogs();
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
  const [chatData, status, logData, streamHead] = await Promise.all([
    api("/api/chats"),
    api("/api/status"),
    api("/api/agent/logs?limit=300&recent=1"),
    api("/api/messages?limit=1&recent=1"),
  ]);
  state.messageCursor = Number(streamHead.messages[0]?.seq || 0);
  state.chats = chatData.chats;
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
      if (state.view === "logs") renderLogs();
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
el("show-logs").addEventListener("click", () => setView("logs"));
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
el("add-chat").addEventListener("click", () => {
  el("dialog-error").textContent = "";
  dialog.showModal();
  el("new-chat-name").focus();
});
el("close-dialog").addEventListener("click", () => dialog.close());
el("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const data = await api("/api/chats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: el("new-chat-name").value, chat_type: el("new-chat-type").value }),
    });
    const index = state.chats.findIndex((item) => item.id === data.chat.id);
    if (index >= 0) state.chats[index] = data.chat;
    else state.chats.unshift(data.chat);
    dialog.close();
    el("chat-form").reset();
    renderChats();
    await selectChat(data.chat.id);
  } catch (error) {
    el("dialog-error").textContent = error.message;
  }
});

load().catch((error) => { updateStatus({ wechat_connected: false, last_error: error.message }); });
