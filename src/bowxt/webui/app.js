const state = { chats: [], activeChatId: null, messages: new Map(), loadedChats: new Set(), status: null };
const el = (id) => document.getElementById(id);
const list = el("chat-list");
const messagesEl = el("messages");
const composer = el("composer");
const input = el("message-input");
const sendButton = el("send-button");

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

function typeLabel(value) { return value === "group" ? "群聊" : value === "contact" ? "联系人" : "未分类"; }

function renderChats() {
  list.replaceChildren();
  const sorted = [...state.chats].sort((a, b) => String(b.last_message_at || b.created_at).localeCompare(String(a.last_message_at || a.created_at)));
  for (const chat of sorted) {
    const cached = state.messages.get(chat.id) || [];
    const latest = cached[cached.length - 1];
    const button = document.createElement("button");
    button.className = `chat-item${chat.id === state.activeChatId ? " active" : ""}`;
    button.type = "button";
    button.addEventListener("click", () => selectChat(chat.id));
    const avatar = document.createElement("div"); avatar.className = "avatar"; avatar.textContent = [...chat.name][0] || "微";
    const copy = document.createElement("div"); copy.className = "chat-copy";
    const top = document.createElement("div"); top.className = "chat-name-row";
    const name = document.createElement("div"); name.className = "chat-name"; name.textContent = chat.name;
    const time = document.createElement("div"); time.className = "chat-time"; time.textContent = formatTime(latest?.timestamp || chat.last_message_at);
    const preview = document.createElement("div"); preview.className = "chat-preview"; preview.textContent = chat.last_error ? `暂停：${chat.last_error}` : (latest?.content || typeLabel(chat.chat_type));
    top.append(name, time); copy.append(top, preview); button.append(avatar, copy); list.append(button);
  }
}

function renderMessages() {
  const chat = state.chats.find((item) => item.id === state.activeChatId);
  if (!chat) return;
  el("chat-title").textContent = chat.name;
  const syncLabel = !state.status?.wechat_connected ? "等待微信连接" : (chat.last_error ? "同步遇到问题" : "持续同步中");
  el("chat-meta").textContent = `${typeLabel(chat.chat_type)} · ${syncLabel}`;
  el("empty-state").hidden = true;
  messagesEl.hidden = false;
  composer.hidden = false;
  messagesEl.replaceChildren();
  for (const message of state.messages.get(chat.id) || []) {
    const row = document.createElement("div"); row.className = `message-row ${message.direction}`;
    const block = document.createElement("div"); block.className = "message-block";
    if (message.direction !== "outgoing" && message.sender) {
      const sender = document.createElement("div"); sender.className = "sender"; sender.textContent = message.sender; block.append(sender);
    }
    const bubble = document.createElement("div"); bubble.className = "bubble"; bubble.textContent = message.content;
    const time = document.createElement("div"); time.className = "message-time"; time.textContent = formatTime(message.timestamp || message.observed_at);
    block.append(bubble, time); row.append(block); messagesEl.append(row);
  }
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function selectChat(id) {
  state.activeChatId = id;
  if (!state.loadedChats.has(id)) {
    const data = await api(`/api/chats/${id}/messages?limit=500`);
    state.messages.set(id, data.messages);
    state.loadedChats.add(id);
  }
  renderChats(); renderMessages(); input.focus();
}

function updateStatus(status) {
  state.status = status;
  const connected = Boolean(status.wechat_connected);
  el("status-dot").className = connected ? "online" : "offline";
  const statusText = el("status-text");
  statusText.textContent = connected ? (status.active_chat ? `正在同步 ${status.active_chat}` : "微信已连接") : "等待微信登录";
  statusText.title = status.last_error || "";
  if (state.activeChatId !== null) renderMessages();
}

async function load() {
  const [chatData, status] = await Promise.all([api("/api/chats"), api("/api/status")]);
  state.chats = chatData.chats;
  await Promise.all(state.chats.map(async (chat) => {
    const data = await api(`/api/chats/${chat.id}/messages?limit=1&recent=1`);
    state.messages.set(chat.id, data.messages);
  }));
  updateStatus(status); renderChats();
  if (state.chats.length) await selectChat(state.chats[0].id);
  const events = new EventSource("/api/events");
  events.onmessage = async ({ data }) => {
    const event = JSON.parse(data);
    if (event.type === "status") updateStatus(event.status);
    if (event.type === "chat") {
      const index = state.chats.findIndex((item) => item.id === event.chat.id);
      if (index >= 0) state.chats[index] = event.chat; else state.chats.unshift(event.chat);
      renderChats();
      if (state.activeChatId === null) await selectChat(event.chat.id);
    }
    if (event.type === "message") {
      if (!state.messages.has(event.message.chat_id)) state.messages.set(event.message.chat_id, []);
      const values = state.messages.get(event.message.chat_id);
      if (!values.some((item) => item.seq === event.message.seq)) values.push(event.message);
      const chat = state.chats.find((item) => item.id === event.message.chat_id);
      if (chat) chat.last_message_at = event.message.timestamp || event.message.observed_at;
      renderChats(); if (state.activeChatId === event.message.chat_id) renderMessages();
    }
  };
  events.onerror = () => { el("status-text").textContent = "消息流重连中"; };
}

composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text || state.activeChatId === null) return;
  sendButton.disabled = true;
  try {
    const data = await api(`/api/chats/${state.activeChatId}/messages`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
    input.value = "";
    if (!state.messages.has(state.activeChatId)) state.messages.set(state.activeChatId, []);
    const values = state.messages.get(state.activeChatId);
    if (!values.some((item) => item.seq === data.message.seq)) values.push(data.message);
    renderMessages();
  } catch (error) { window.alert(`发送失败：${error.message}`); }
  finally { sendButton.disabled = false; input.focus(); }
});
input.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); composer.requestSubmit(); } });
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 130)}px`; });

const dialog = el("chat-dialog");
el("add-chat").addEventListener("click", () => { el("dialog-error").textContent = ""; dialog.showModal(); el("new-chat-name").focus(); });
el("close-dialog").addEventListener("click", () => dialog.close());
el("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const data = await api("/api/chats", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: el("new-chat-name").value, chat_type: el("new-chat-type").value }) });
    const index = state.chats.findIndex((item) => item.id === data.chat.id);
    if (index >= 0) state.chats[index] = data.chat; else state.chats.unshift(data.chat);
    dialog.close(); el("chat-form").reset(); renderChats(); await selectChat(data.chat.id);
  } catch (error) { el("dialog-error").textContent = error.message; }
});

load().catch((error) => { updateStatus({ wechat_connected: false, last_error: error.message }); });
