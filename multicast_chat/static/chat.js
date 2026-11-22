"use strict";

const $ = (id) => document.getElementById(id);
const textEncoder = new TextEncoder();
const fileRows = new Map();
const timeFormat = new Intl.DateTimeFormat("zh-CN", {timeStyle: "medium"});
const units = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 1});
let state;
let connected = false;
let uploading = false;
let lastMessages = "";
let lastPeople = "";
let lastContacts = "";

function element(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}

function notice(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

async function action(operation, value = {}) {
  notice("");
  try {
    const response = await fetch(`/api/${operation}`, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Local-Chat": "1"},
      body: JSON.stringify(value),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    return true;
  } catch (error) {
    notice(error.message);
    return false;
  }
}

function readableSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${units.format(bytes / 1024)} KiB`;
  return `${units.format(bytes / 1024 ** 2)} MiB`;
}

function renderPeople() {
  const key = JSON.stringify([state.members, state.pending, state.mode, state.id, state.transitioning]);
  if (key === lastPeople) return;
  lastPeople = key;
  $("members").replaceChildren(...state.members.map((member) => {
    const row = element("li", undefined, "member");
    row.dataset.id = member.id;
    const name = element("div", member.name, "member-name");
    if (member.id === state.id) name.append(element("span", "（你）", "muted"));
    if (member.owner) name.append(element("span", "房主", "owner"));
    const status = element("span", member.online ? "在线" : "离线",
      member.online ? "status online" : "status offline");
    row.append(name, status);
    if (state.mode === "owner" && !member.owner) {
      const remove = element("button", "移除", "quiet danger compact");
      remove.type = "button";
      remove.disabled = state.transitioning;
      remove.setAttribute("aria-label", `移除 ${member.name}`);
      remove.addEventListener("click", () => action("remove", {id: member.id}));
      row.append(remove);
    }
    return row;
  }));
  $("approvals").hidden = state.mode !== "owner" || !state.pending.length;
  $("pending").replaceChildren(...state.pending.map((member) => {
    const row = element("li", undefined, "request");
    const identity = element("div", member.name);
    identity.append(element("small", member.id.slice(0, 12), "fingerprint"));
    const approve = element("button", "批准", "compact");
    approve.type = "button";
    approve.disabled = state.transitioning;
    approve.setAttribute("aria-label", `批准 ${member.name}`);
    approve.addEventListener("click", () => action("approve", {id: member.id}));
    row.append(identity, approve);
    return row;
  }));
}

function renderMessages() {
  const key = state.chat.map((chat) => chat.id).join("|");
  if (key === lastMessages && $("messages").childElementCount) return;
  lastMessages = key;
  const area = $("messages");
  const atEnd = area.scrollHeight - area.clientHeight - area.scrollTop < 80;
  area.replaceChildren(...state.chat.map((chat) => {
    const article = element("article", undefined, chat.mine ? "message mine" : "message");
    article.dataset.id = chat.id;
    const meta = element("div", undefined, "meta");
    const time = element("time", timeFormat.format(chat.time));
    time.dateTime = new Date(chat.time).toISOString();
    meta.append(element("strong", chat.sender), time);
    article.append(meta, element("p", chat.text));
    return article;
  }));
  if (!state.chat.length) area.append(element("p", "暂无消息", "empty"));
  if (atEnd) area.scrollTop = area.scrollHeight;
}

function renderContacts() {
  const key = state.contacts.map((contact) => contact.id).join("|");
  if (key === lastContacts) return;
  lastContacts = key;
  $("outside-messages").hidden = !state.contacts.length;
  $("contacts").replaceChildren(...state.contacts.map((contact) => {
    const row = element("li", undefined, "contact");
    row.append(element("strong", contact.sender, "muted"), element("p", contact.text));
    return row;
  }));
}

const statuses = {
  sending: "正在组播", waiting: "等待接收方校验", receiving: "正在接收",
  verifying: "正在校验", complete: "已完成", failed: "失败",
};

function renderFiles() {
  const list = $("files");
  const keys = new Set(state.files.map((file) => file.id));
  for (const [key, row] of fileRows) {
    if (!keys.has(key)) {
      row.node.remove();
      fileRows.delete(key);
    }
  }
  $("file-empty")?.remove();
  if (!state.files.length) {
    const empty = element("li", "暂无文件", "empty");
    empty.id = "file-empty";
    list.append(empty);
  }
  for (const file of [...state.files].reverse()) {
    let row = fileRows.get(file.id);
    if (!row) {
      const node = element("li", undefined, "file-row");
      node.dataset.id = file.id;
      const header = element("div", undefined, "file-header");
      const title = element("strong", file.name);
      const status = element("span", undefined, "file-status");
      const progress = element("progress");
      progress.setAttribute("aria-label", `${file.name} 传输进度`);
      const details = element("p", undefined, "file-details");
      const actions = element("div", undefined, "file-actions");
      header.append(title, status);
      node.append(header, progress, details, actions);
      list.prepend(node);
      row = {node, status, progress, details, actions};
      fileRows.set(file.id, row);
    }
    row.node.dataset.status = file.status;
    row.status.textContent = `${file.outgoing ? "发送" : "接收"} · ${statuses[file.status]}`;
    row.progress.max = file.size || 1;
    row.progress.value = file.size ? file.bytes : (file.status === "complete" ? 1 : 0);
    const confirmations = file.expected === null ? "" : ` · ${file.confirmed}/${file.expected} 位在线成员已校验`;
    row.details.textContent = file.error || `${readableSize(file.bytes)} / ${readableSize(file.size)}${confirmations}`;
    if (file.download && !row.actions.childElementCount) {
      const url = `/api/files/${file.id}`;
      const link = element("a", "下载");
      link.href = url + "/download";
      link.download = file.name;
      row.actions.append(link);
      if (file.mime && !file.outgoing) {
        const player = element(file.mime.startsWith("audio/") ? "audio" : "video");
        player.setAttribute("aria-label", `播放 ${file.name}`);
        player.controls = true;
        player.preload = "metadata";
        player.playsInline = true;
        player.src = url + "/play";
        row.actions.append(player);
      }
    }
  }
}

function render() {
  if (!state) return;
  const lobby = ["idle", "removed", "closed"].includes(state.mode);
  const waiting = state.mode === "waiting";
  const canSend = connected && state.ready;
  $("identity").textContent = `节点 ${state.name} · ${connected ? "已连接" : "正在重连"}`;
  const dataGroup = state.dataGroup ? `  ·  房间组 ${state.dataGroup}` : "";
  $("network").textContent = `会合组 ${state.group}${dataGroup}  /  ${state.interface}`;
  $("lobby").hidden = !lobby;
  $("room").hidden = lobby;
  const contact = $("outside-contact");
  const contactSlot = $(waiting ? "waiting-contact" : "lobby-contact");
  if (contact.parentElement !== contactSlot) {
    contactSlot.append(contact);
    $("contact-label").textContent = waiting ? "公开留言" : "公开留言（填写上方房间邀请）";
  }
  $("copy").hidden = !state.invite;
  $("copy-value").value = state.invite || "";
  $("copy-value").hidden = !state.invite || $("copy-value").hidden;
  $("leave").textContent = state.mode === "owner" ? "关闭房间" :
    waiting ? "取消申请" : "退出房间";
  $("leave").disabled = state.closing;
  $("room-status").textContent = state.closing ? "正在关闭房间" : waiting ? "等待房主批准加入" :
    !connected ? "正在连接本地节点" : state.ready ? "房间已就绪" : "正在同步成员和密钥";
  $("epoch").textContent = state.epoch ? `密钥版本 ${state.epoch}` : "";
  $("file-limit").textContent = `单文件上限 ${readableSize(state.fileLimit)}`;
  $("send").disabled = !canSend;
  $("file").disabled = !canSend || uploading;
  $("send-file").disabled = !canSend || uploading;
  renderPeople();
  renderMessages();
  renderContacts();
  renderFiles();
}

$("create").addEventListener("click", () => action("create"));
$("leave").addEventListener("click", () => action("leave"));
$("join-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action("join", {invite: $("invitation").value.trim()});
});
$("contact-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const waiting = state.mode === "waiting";
  const invite = $("invitation").value.trim();
  const field = $("contact-text");
  const text = field.value;
  if (!waiting && !invite) {
    notice("请先粘贴邀请");
    return;
  }
  if (textEncoder.encode(text).length > state.contactLimit) {
    notice(`留言最多 ${state.contactLimit} 个 UTF-8 字节`);
    return;
  }
  if (await action("contact", waiting ? {text} : {invite, text}) && field.value === text) {
    field.value = "";
    notice("公开留言已组播");
  }
});
$("copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(state.invite);
    notice("邀请已复制");
  } catch {
    $("copy-value").hidden = false;
    $("copy-value").select();
    notice("可选中并复制邀请文本");
  }
});
$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const field = $("chat-text");
  const text = field.value;
  if (textEncoder.encode(text).length > state.chatLimit) {
    notice(`消息最多 ${state.chatLimit} 个 UTF-8 字节`);
    return;
  }
  if (await action("chat", {text}) && field.value === text) field.value = "";
});
$("file-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const file = $("file").files[0];
  if (!file || uploading) return;
  if (file.size > state.fileLimit) {
    notice("文件超过大小上限");
    return;
  }
  uploading = true;
  notice("");
  render();
  const progress = $("upload-progress");
  progress.hidden = false;
  progress.value = 0;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/files");
  xhr.responseType = "json";
  xhr.setRequestHeader("X-Local-Chat", "1");
  xhr.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
  xhr.upload.onprogress = (event) => {
    if (event.lengthComputable) {
      progress.max = event.total;
      progress.value = event.loaded;
    }
  };
  xhr.onloadend = () => {
    uploading = false;
    progress.hidden = true;
    if (xhr.status === 202) $("file").value = "";
    else notice(xhr.response?.error || "上传失败，请重试");
    render();
  };
  xhr.send(file);
});

const events = new EventSource("/api/events");
events.addEventListener("state", (event) => {
  const next = JSON.parse(event.data);
  if (next.mode !== state?.mode && next.mode === "removed") notice("房主已移除你。重新加入需要批准。");
  if (next.mode !== state?.mode && next.mode === "closed") notice("房主已关闭房间。");
  state = next;
  connected = true;
  render();
});
events.onerror = () => {
  connected = false;
  $("identity").textContent = "正在重连本地节点";
  render();
};
