const apiBaseUrl = "/api";
const chatState = { history: [] };

// Surfaces otherwise-invisible script errors on mobile/in-app browsers where nobody has
// devtools open — shows in the login overlay's debug box so a screenshot is enough to
// diagnose a remote report instead of "it doesn't work" with no further information.
window.addEventListener("error", (event) => {
  const overlay = document.getElementById("loginOverlay");
  const debugNode = document.getElementById("loginDebugInfo");
  if (!overlay || !debugNode) return;
  overlay.hidden = false;
  debugNode.hidden = false;
  debugNode.textContent = `${new Date().toISOString()}\nscript error: ${event.message}\nat ${event.filename}:${event.lineno}:${event.colno}`;
});

// Only meaningful in AUTH_MODE=wecom: identity there comes from a bearer token minted by
// /api/auth/wecom/callback (see app/wecom.py), not a server-side session/cookie, so the
// frontend has to carry it itself. Dev/OIDC modes never populate this and are unaffected.
const WECOM_TOKEN_KEY = "wecom_token";
function getStoredWecomToken() {
  try { return localStorage.getItem(WECOM_TOKEN_KEY); } catch (_) { return null; }
}
function authHeaders() {
  const token = getStoredWecomToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
function captureWecomTokenFromUrl() {
  // Returns true only once we're sure the token is actually persisted, so the caller can
  // force a hard reload — some in-app browsers (WeCom's embedded webview included) keep a
  // stale JS/DOM state alive across an OAuth redirect chain in ways a normal soft-refresh
  // of this same page load doesn't fully clear. Reloading from a clean, token-free URL is
  // the one thing that reliably works regardless of what that webview did on the way here.
  const match = location.hash.match(/wecom_token=([^&]+)/);
  if (!match) return false;
  try {
    localStorage.setItem(WECOM_TOKEN_KEY, decodeURIComponent(match[1]));
  } catch (_) {
    return false;
  }
  try { history.replaceState(null, "", location.pathname + location.search); } catch (_) {}
  return true;
}

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
}[char]));

const inlineMarkdown = (text) =>
  text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`(.+?)`/g, "<code>$1</code>");

// Minimal Markdown renderer for model output: bold, inline code, and "- "
// bullet lists. Escapes first so nothing from the model or documents can
// inject HTML; only the markup this function adds is trusted.
function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  const parts = [];
  let listBuffer = [];
  const flushList = () => {
    if (!listBuffer.length) return;
    parts.push(`<ul class="bubble-list">${listBuffer.join("")}</ul>`);
    listBuffer = [];
  };
  lines.forEach((line, index) => {
    const listMatch = line.match(/^[-*]\s+(.+)/);
    if (listMatch) {
      listBuffer.push(`<li>${inlineMarkdown(listMatch[1])}</li>`);
      return;
    }
    flushList();
    parts.push(inlineMarkdown(line));
    if (index < lines.length - 1) parts.push("<br>");
  });
  flushList();
  return parts.join("");
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2600);
}

async function jsonRequest(url, options = {}) {
  const headers = { ...authHeaders(), ...(options.headers || {}) };
  const response = await fetch(url, { ...options, headers });
  if (!response.ok) {
    let message = "请求失败";
    try { message = (await response.json()).detail || message; } catch (_) {}
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

async function checkHealth() {
  try {
    const health = await jsonRequest(`${apiBaseUrl}/health`);
    $("#statusDot").classList.toggle("ok", health.status === "ok");
    $("#statusText").textContent = health.status === "ok" ? "服务运行正常" : "部分服务异常";
    $("#statusDetail").textContent = health.model_ready ? `${health.vector_backend} · 模型已连接` : "请配置模型密钥";
  } catch (_) {
    $("#statusText").textContent = "无法连接服务";
    $("#statusDetail").textContent = "请检查后台进程";
  }
}

let currentRoles = [];
let currentPermissions = [];
const isSuperAdmin = () => currentRoles.includes("super_admin");

function showLoginOverlay(debugInfo) {
  const overlay = $("#loginOverlay");
  if (overlay) overlay.hidden = false;
  const debugNode = $("#loginDebugInfo");
  if (debugNode && debugInfo) {
    debugNode.hidden = false;
    debugNode.textContent = `${new Date().toISOString()}\n${debugInfo}`;
  }
  initWecomQr();
}

let wecomQrRequested = false;
// Desktop WeCom hands external chat links to the system browser instead of opening them in
// an in-client webview, so the snsapi_base flow the "手机企业微信登录" button uses (which
// requires being inside that webview) can't work there. This renders WeCom's own QR-scan
// widget as the desktop-compatible alternative; scanning it lands on the exact same
// /api/auth/wecom/callback endpoint, so no separate success-handling path is needed.
async function initWecomQr() {
  if (wecomQrRequested) return;
  wecomQrRequested = true;
  try {
    const config = await jsonRequest(`${apiBaseUrl}/auth/wecom/qr-config`);
    // The widget's iframe is cross-origin (open.work.weixin.qq.com), so our own stylesheet
    // can't reach inside it — WeCom instead accepts a small CSS override via `href` as a
    // data: URI, using its documented class names to strip its default title text/border
    // down to just the QR code so it doesn't look like a foreign widget bolted onto the page.
    const qrOverrideCss = [
      ".impowerBox .title{display:none}",
      // WeCom's own caption under the QR duplicates the "电脑端：用企业微信扫一扫" text
      // this page already shows below the widget, so it's hidden rather than restyled.
      ".impowerBox .status{display:none}",
      ".impowerBox .qrcode{width:200px;border:none;margin:0 auto}",
      ".impowerBox .info{width:200px;margin:0 auto}",
      ".status_icon{display:none!important}",
    ].join("");
    const script = document.createElement("script");
    script.src = "https://wwcdn.weixin.qq.com/node/wework/wwopen/js/wwLogin-1.2.7.js";
    script.onload = () => {
      new WwLogin({
        id: "wecomQrContainer",
        appid: config.corp_id,
        agentid: config.agent_id,
        redirect_uri: config.redirect_uri,
        state: config.state,
        href: `data:text/css;base64,${btoa(qrOverrideCss)}`,
      });
    };
    document.head.appendChild(script);
  } catch (_) {
    // Not configured on this deployment (e.g. dev/oidc mode) — the mobile button still works.
  }
}

$("#logoutButton").addEventListener("click", () => {
  try { localStorage.removeItem(WECOM_TOKEN_KEY); } catch (_) {}
  location.reload();
});

function hideLoginOverlay() {
  const overlay = $("#loginOverlay");
  if (overlay) overlay.hidden = true;
}

function applyPermissionGatedNav() {
  document.querySelectorAll("[data-requires-permission]").forEach((button) => {
    const required = button.dataset.requiresPermission;
    const allowed = currentPermissions.includes(required);
    button.hidden = !allowed;
    // If the view the user was looking at just got hidden out from under them (e.g. a
    // logout/login as a different account), fall back to the chat view instead of leaving
    // a hidden section marked "active".
    if (!allowed && button.classList.contains("active")) {
      button.classList.remove("active");
      $(`#${button.dataset.view}View`).classList.remove("active");
      $('.nav-item[data-view="chat"]').classList.add("active");
      $("#chatView").classList.add("active");
    }
  });
}

async function loadIdentity() {
  try {
    const identity = await jsonRequest(`${apiBaseUrl}/me`);
    $("#identityName").textContent = identity.display_name;
    $("#identityGroups").textContent = identity.groups.length ? identity.groups.join(" · ") : "无访问组";
    currentRoles = identity.roles || [];
    currentPermissions = identity.permissions || [];
    applyPermissionGatedNav();
    $("#logoutButton").hidden = false;
    hideLoginOverlay();
  } catch (error) {
    $("#identityName").textContent = "未登录";
    $("#identityGroups").textContent = "";
    $("#logoutButton").hidden = true;
    currentPermissions = [];
    applyPermissionGatedNav();
    // A first-ever visit with no token yet is the routine, expected path into this catch
    // block — showing raw error/debug text for that would just be noise. Only surface the
    // debug box when a token WAS present and still failed, since that's the case worth
    // actually diagnosing (expired session, server issue, etc.).
    const hadToken = !!getStoredWecomToken();
    if (error.status === 401) {
      try { localStorage.removeItem(WECOM_TOKEN_KEY); } catch (_) {}
    }
    showLoginOverlay(
      hadToken
        ? [`url: ${location.href}`, `httpStatus: ${error.status}`, `message: ${error.message}`].join("\n")
        : null
    );
  }
}

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".view").forEach((view) => view.classList.remove("active"));
    button.classList.add("active");
    $(`#${button.dataset.view}View`).classList.add("active");
    if (button.dataset.view === "documents") loadDocuments();
    if (button.dataset.view === "roles") loadRoles();
  });
});

function addMessage(role, content, citations = [], fallback = false) {
  const welcome = $(".welcome-card");
  if (welcome) welcome.remove();
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role}`;
  const citationHtml = citations.length ? `<div class="citations">${citations.map((item, index) =>
    `<div class="citation"><strong>[${index + 1}] ${escapeHtml(item.title)}</strong>${item.page_number ? ` · 第 ${item.page_number} 页` : ""}<br>${escapeHtml(item.excerpt)}</div>`
  ).join("")}</div>` : "";
  const notice = fallback
    ? '<div class="fallback-notice">⚠️ 非企业官方文档内容（AI 通用回答，仅供参考）</div>'
    : "";
  wrapper.innerHTML = `${notice}<div class="bubble">${escapeHtml(content)}${citationHtml}</div>`;
  $("#messages").appendChild(wrapper);
  wrapper.scrollIntoView({ behavior: "smooth", block: "end" });
}

async function ask(question) {
  const welcome = $(".welcome-card");
  if (welcome) welcome.remove();
  addMessage("user", question);
  const previousHistory = chatState.history.slice(-8);
  chatState.history.push({ role: "user", content: question });

  const wrapper = document.createElement("div");
  wrapper.className = "message assistant";
  wrapper.innerHTML = '<div class="bubble">正在检索知识库……</div><button class="chat-stop" type="button">停止</button>';
  $("#messages").appendChild(wrapper);
  const bubble = wrapper.querySelector(".bubble");
  const stopButton = wrapper.querySelector(".chat-stop");

  const controller = new AbortController();
  let traceId = null;
  let citations = [];
  let fallback = false;
  let fullAnswer = "";
  let started = false;

  stopButton.addEventListener("click", async () => {
    controller.abort();
    stopButton.remove();
    if (traceId) {
      try { await jsonRequest(`${apiBaseUrl}/chat/${traceId}/cancel`, { method: "POST" }); } catch (_) {}
    }
  });

  function handleFrame(frame) {
    let eventName = "message";
    let data = "";
    for (const line of frame.split("\n")) {
      if (line.startsWith("event: ")) eventName = line.slice(7).trim();
      else if (line.startsWith("data: ")) data += line.slice(6);
    }
    if (!data) return;
    let payload;
    try { payload = JSON.parse(data); } catch (_) { return; }
    if (payload.event === "metadata") {
      traceId = payload.trace_id;
      citations = payload.citations || [];
    } else if (payload.event === "token") {
      if (!started) {
        bubble.textContent = "";
        started = true;
      }
      fullAnswer += payload.content;
      bubble.innerHTML = renderMarkdown(fullAnswer);
      wrapper.scrollIntoView({ behavior: "smooth", block: "end" });
    } else if (payload.event === "done") {
      fallback = Boolean(payload.fallback);
      if (payload.cancelled) bubble.innerHTML = renderMarkdown(fullAnswer || "已停止生成。");
    } else if (payload.event === "error") {
      throw new Error(payload.detail || "流式回答出错");
    }
  }

  try {
    const knowledgeBaseIds = Array.from(chatKnowledgeSelection);
    const response = await fetch(`${apiBaseUrl}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ question, conversation_history: previousHistory, knowledge_base_ids: knowledgeBaseIds }),
      signal: controller.signal,
    });
    if (!response.ok) {
      let message = "请求失败";
      try { message = (await response.json()).detail || message; } catch (_) {}
      throw new Error(message);
    }
    if (!response.body) throw new Error("无响应流");
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        handleFrame(frame);
      }
    }
  } catch (error) {
    if (error.name === "AbortError") {
      bubble.innerHTML = renderMarkdown(fullAnswer || "已停止生成。");
    } else {
      bubble.textContent = `暂时无法回答：${error.message}`;
    }
  } finally {
    stopButton.remove();
  }

  if (fallback) {
    const notice = document.createElement("div");
    notice.className = "fallback-notice";
    notice.textContent = "⚠️ 非企业官方文档内容（AI 通用回答，仅供参考）";
    wrapper.insertBefore(notice, bubble);
  }
  if (citations.length) {
    const citationHtml = `<div class="citations">${citations.map((item, index) =>
      `<div class="citation"><strong>[${index + 1}] ${escapeHtml(item.title)}</strong>${item.page_number ? ` · 第 ${item.page_number} 页` : ""}<br>${escapeHtml(item.excerpt)}</div>`
    ).join("")}</div>`;
    bubble.insertAdjacentHTML("beforeend", citationHtml);
  }
  if (fullAnswer) {
    chatState.history.push({ role: "assistant", content: fullAnswer });
  }
}

const QUESTION_MIN_HEIGHT = 54;

function autoResizeQuestion() {
  const input = $("#question");
  input.style.height = "auto";
  input.style.height = `${Math.max(input.scrollHeight, QUESTION_MIN_HEIGHT)}px`;
}

$("#question").addEventListener("input", autoResizeQuestion);
$("#question").addEventListener("keydown", (event) => {
  // isComposing guards against IME (拼音/五笔 etc.) users pressing Enter to
  // confirm a candidate word, which must not submit the form mid-typing.
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("#chatForm").requestSubmit();
  }
});

$("#chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#question");
  const question = input.value.trim();
  if (!question) return;
  const sendButton = event.target.querySelector("button[type=submit]");
  input.value = "";
  autoResizeQuestion();
  sendButton.disabled = true;
  try {
    await ask(question);
  } finally {
    sendButton.disabled = false;
  }
});

document.querySelectorAll("[data-question]").forEach((button) => {
  button.addEventListener("click", () => ask(button.dataset.question));
});

$("#file").addEventListener("change", (event) => {
  const files = event.target.files;
  const label = $("#dropzoneText");
  label.textContent = !files.length
    ? "选择或拖入文档，可批量多选"
    : files.length === 1
      ? files[0].name
      : `已选择 ${files.length} 个文件`;
});

$("#uploadForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const files = Array.from($("#file").files);
  if (!files.length) return;
  const button = form.querySelector("button[type=submit]");
  const isBatch = files.length > 1;
  const knowledgeBaseIds = Array.from($("#uploadKnowledgeBases").querySelectorAll("input:checked")).map((input) => input.value);
  const department = form.department.value.trim();
  const deptAccessGroups = Array.from($("#uploadAccessDepartments").querySelectorAll("input:checked")).map((input) => `dept_${input.value}`);
  const customAccessGroups = form.access_groups.value.split(",").map((item) => item.trim()).filter(Boolean);
  const accessGroups = [...deptAccessGroups, ...customAccessGroups].join(",");
  button.disabled = true;
  $("#uploadMessage").textContent = "";
  const jobIds = [];
  let failedCount = 0;
  try {
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      button.textContent = isBatch
        ? `正在上传 ${index + 1}/${files.length} 个文件……`
        : "正在解析并建立索引……";
      const formData = new FormData();
      formData.append("file", file);
      if (department) formData.append("department", department);
      if (accessGroups) formData.append("access_groups", accessGroups);
      if (knowledgeBaseIds.length) formData.append("knowledge_base_ids", knowledgeBaseIds.join(","));
      try {
        const accepted = await jsonRequest(`${apiBaseUrl}/documents`, { method: "POST", body: formData });
        jobIds.push(accepted.job.id);
      } catch (error) {
        failedCount += 1;
        toast(`${file.name} 上传失败：${error.message}`);
      }
    }
    form.reset();
    $("#dropzoneText").textContent = "选择或拖入文档，可批量多选";
    await loadDocuments();
    if (jobIds.length && isBatch) {
      $("#uploadMessage").textContent = `${jobIds.length}/${files.length} 个文件已进入后台索引队列。`;
      toast(`${jobIds.length}/${files.length} 个文件已进入后台索引队列`);
      pollJobsBatch(jobIds);
    } else if (jobIds.length) {
      $("#uploadMessage").textContent = `上传成功，后台任务 ${jobIds[0].slice(0, 8)} 正在建立索引。`;
      toast("文档已进入后台处理队列");
      pollJob(jobIds[0]);
    } else if (failedCount) {
      $("#uploadMessage").textContent = "全部文件上传失败，请检查后重试。";
    }
  } finally {
    button.disabled = false;
    button.textContent = "上传并建立索引";
  }
});

const DOCUMENT_STATUS_LABELS = {
  queued: ["排队中", "pending"],
  processing: ["处理中", "pending"],
  ready: ["已就绪", "ok"],
  failed: ["失败", "error"],
  cancelled: ["已取消", "muted"],
  expired: ["已过期", "muted"],
};

function describeDocumentStatus(status) {
  const [label, tone] = DOCUMENT_STATUS_LABELS[status] || [status, "muted"];
  return `<span class="doc-status doc-status-${tone}"><span class="doc-status-dot"></span>${escapeHtml(label)}</span>`;
}

let cachedDocuments = [];
let documentSearchQuery = "";
const GROUP_COLLAPSE_THRESHOLD = 8;

const FILE_TYPE_BADGES = {
  pdf: ["PDF", "pdf"],
  doc: ["DOC", "docx"],
  docx: ["DOCX", "docx"],
  ppt: ["PPT", "pptx"],
  pptx: ["PPTX", "pptx"],
  png: ["IMG", "image"],
  jpg: ["IMG", "image"],
  jpeg: ["IMG", "image"],
  md: ["MD", "md"],
  txt: ["TXT", "txt"],
};

function fileTypeBadge(filename) {
  const extension = (filename.split(".").pop() || "").toLowerCase();
  const [label, tone] = FILE_TYPE_BADGES[extension] || [extension.toUpperCase() || "FILE", "default"];
  return `<span class="doc-icon doc-icon-${tone}">${escapeHtml(label)}</span>`;
}

function renderDocRow(documentRecord) {
  return `
    <div class="doc-row">
      ${fileTypeBadge(documentRecord.filename)}
      <div class="doc-info"><strong>${escapeHtml(documentRecord.title)}</strong><small>${describeDocumentStatus(documentRecord.status)} · ${documentRecord.chunk_count} 个片段 · ${escapeHtml(documentRecord.access_groups.length ? documentRecord.access_groups.join(", ") : "全员可见")}</small></div>
      <div class="doc-row-actions">
        <button class="ghost small" data-edit-doc="${documentRecord.id}" title="修改所属部门、访问组、所属知识库">编辑</button>
        <button class="ghost small" data-update-version="${documentRecord.id}" title="替换这份文档的内容，标题/权限/知识库归属保持不变">更新版本</button>
        <button class="delete" data-delete="${documentRecord.id}" title="删除">删除</button>
      </div>
    </div>`;
}

function renderGroupedDocuments(documents) {
  const groups = new Map();
  for (const documentRecord of documents) {
    const ids = documentRecord.knowledge_base_ids.length ? documentRecord.knowledge_base_ids : ["__default__"];
    for (const id of ids) {
      if (!groups.has(id)) groups.set(id, []);
      groups.get(id).push(documentRecord);
    }
  }
  const orderedIds = cachedKnowledgeBases.map((knowledgeBase) => knowledgeBase.id).filter((id) => groups.has(id));
  if (groups.has("__default__")) orderedIds.push("__default__");
  for (const id of groups.keys()) if (!orderedIds.includes(id)) orderedIds.push(id);

  return orderedIds.map((id) => {
    const docs = groups.get(id);
    // Large groups start collapsed so the page isn't one long scroll; a search
    // always shows its matches expanded regardless of group size.
    const collapsed = docs.length > GROUP_COLLAPSE_THRESHOLD && !documentSearchQuery;
    return `
    <div class="doc-group${collapsed ? " collapsed" : ""}">
      <button type="button" class="doc-group-header"><span class="doc-group-caret">▾</span>${escapeHtml(knowledgeBaseName(id))}<span class="doc-group-count">${docs.length}</span></button>
      <div class="doc-group-body">${docs.map(renderDocRow).join("")}</div>
    </div>`;
  }).join("");
}

function renderDocumentList() {
  const query = documentSearchQuery;
  const filtered = query
    ? cachedDocuments.filter((documentRecord) =>
        `${documentRecord.title} ${documentRecord.filename}`.toLowerCase().includes(query)
      )
    : cachedDocuments;
  $("#documentList").innerHTML = filtered.length
    ? renderGroupedDocuments(filtered)
    : `<p class="empty">${query ? "没有匹配的文档" : "暂无文档"}</p>`;
  document.querySelectorAll(".doc-group-header").forEach((header) => {
    header.addEventListener("click", () => header.parentElement.classList.toggle("collapsed"));
  });
  document.querySelectorAll("[data-delete]").forEach((button) => button.addEventListener("click", async () => {
    if (!confirm("确定删除这份文档及其索引吗？")) return;
    button.disabled = true;
    button.textContent = "删除中…";
    try {
      await jsonRequest(`${apiBaseUrl}/documents/${button.dataset.delete}`, { method: "DELETE" });
      toast("文档已删除");
      loadDocuments();
    } catch (error) {
      toast(error.message);
      button.disabled = false;
      button.textContent = "删除";
    }
  }));
  document.querySelectorAll("[data-update-version]").forEach((button) => button.addEventListener("click", () => {
    pendingVersionDocId = button.dataset.updateVersion;
    $("#versionFileInput").click();
  }));
  document.querySelectorAll("[data-edit-doc]").forEach((button) => button.addEventListener("click", () => {
    const documentRecord = cachedDocuments.find((item) => item.id === button.dataset.editDoc);
    if (documentRecord) openEditDocModal(documentRecord);
  }));
}

function openEditDocModal(documentRecord) {
  $("#editDocModal").dataset.documentId = documentRecord.id;
  $("#editDocDepartment").value = documentRecord.department || "";
  const deptIds = documentRecord.access_groups
    .filter((group) => group.startsWith("dept_"))
    .map((group) => group.slice(5));
  const customGroups = documentRecord.access_groups.filter((group) => !group.startsWith("dept_"));
  $("#editDocAccessGroups").value = customGroups.join(", ");
  renderDeptCheckboxList($("#editDocAccessDepartments"), deptIds);
  renderKbCheckboxList($("#editDocKnowledgeBases"), documentRecord.knowledge_base_ids);
  $("#editDocMessage").textContent = "";
  $("#editDocModal").hidden = false;
}

function closeEditDocModal() {
  $("#editDocModal").hidden = true;
  delete $("#editDocModal").dataset.documentId;
}

$("#editDocCancel").addEventListener("click", closeEditDocModal);

$("#editDocSave").addEventListener("click", async () => {
  const documentId = $("#editDocModal").dataset.documentId;
  if (!documentId) return;
  const department = $("#editDocDepartment").value.trim();
  const deptAccessGroups = Array.from($("#editDocAccessDepartments").querySelectorAll("input:checked")).map((input) => `dept_${input.value}`);
  const customAccessGroups = $("#editDocAccessGroups").value.split(",").map((item) => item.trim()).filter(Boolean);
  const accessGroups = [...deptAccessGroups, ...customAccessGroups];
  const knowledgeBaseIds = Array.from($("#editDocKnowledgeBases").querySelectorAll("input:checked")).map((input) => input.value);
  const button = $("#editDocSave");
  button.disabled = true;
  try {
    await jsonRequest(`${apiBaseUrl}/documents/${documentId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        // The backend treats an omitted/null department as "leave unchanged" (it can't tell
        // the difference), so clearing the field has to send an empty string, not null —
        // the service layer's own `department.strip() or None` is what turns that into a
        // real NULL in the database.
        department,
        access_groups: accessGroups,
        knowledge_base_ids: knowledgeBaseIds,
      }),
    });
    toast("文档信息已更新");
    closeEditDocModal();
    await loadDocuments();
  } catch (error) {
    $("#editDocMessage").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

let pendingVersionDocId = null;

$("#versionFileInput").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  const documentId = pendingVersionDocId;
  pendingVersionDocId = null;
  if (!file || !documentId) return;
  const button = document.querySelector(`[data-update-version="${documentId}"]`);
  if (button) {
    button.disabled = true;
    button.textContent = "上传中…";
  }
  try {
    const formData = new FormData();
    formData.append("file", file);
    const accepted = await jsonRequest(`${apiBaseUrl}/documents/${documentId}/versions`, {
      method: "POST",
      body: formData,
    });
    toast("新版本已上传，正在重新建立索引");
    pollJob(accepted.job.id);
    await loadDocuments();
  } catch (error) {
    toast(error.message);
    if (button) {
      button.disabled = false;
      button.textContent = "更新版本";
    }
  } finally {
    event.target.value = "";
  }
});

$("#documentSearch").addEventListener("input", (event) => {
  documentSearchQuery = event.target.value.trim().toLowerCase();
  renderDocumentList();
});

function knowledgeBaseName(id) {
  if (id === "__default__") return "默认知识库";
  const knowledgeBase = cachedKnowledgeBases.find((item) => item.id === id);
  return knowledgeBase ? knowledgeBase.name : "未知知识库";
}

function renderKbList() {
  const container = $("#kbList");
  if (!container) return;
  if (!cachedKnowledgeBases.length) {
    container.innerHTML = '<span class="kb-list-empty">还没有自建知识库，文档会显示在下面的"默认知识库"分组里</span>';
    return;
  }
  container.innerHTML = cachedKnowledgeBases.map((knowledgeBase) => {
    const count = cachedDocuments.filter((documentRecord) => documentRecord.knowledge_base_ids.includes(knowledgeBase.id)).length;
    return `<span class="kb-list-item"><button type="button" class="kb-list-name" data-rename-kb="${knowledgeBase.id}" title="点击修改名称">${escapeHtml(knowledgeBase.name)}</button><b>${count}</b><button type="button" class="kb-list-remove" data-delete-kb="${knowledgeBase.id}" aria-label="删除知识库 ${escapeHtml(knowledgeBase.name)}">×</button></span>`;
  }).join("");
  container.querySelectorAll("[data-delete-kb]").forEach((button) => {
    button.addEventListener("click", async () => {
      const knowledgeBase = cachedKnowledgeBases.find((item) => item.id === button.dataset.deleteKb);
      if (!confirm(`确定删除知识库"${knowledgeBase ? knowledgeBase.name : ""}"吗？`)) return;
      button.disabled = true;
      try {
        await jsonRequest(`${apiBaseUrl}/knowledge-bases/${button.dataset.deleteKb}`, { method: "DELETE" });
        toast("知识库已删除");
        await loadKnowledgeBases();
        await loadDocuments();
      } catch (error) {
        toast(error.message);
        button.disabled = false;
      }
    });
  });
  container.querySelectorAll("[data-rename-kb]").forEach((button) => {
    button.addEventListener("click", async () => {
      const knowledgeBase = cachedKnowledgeBases.find((item) => item.id === button.dataset.renameKb);
      if (!knowledgeBase) return;
      const nextName = prompt("修改知识库名称", knowledgeBase.name);
      if (nextName === null) return;
      const trimmed = nextName.trim();
      if (!trimmed || trimmed === knowledgeBase.name) return;
      try {
        await jsonRequest(`${apiBaseUrl}/knowledge-bases/${knowledgeBase.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: trimmed }),
        });
        toast("知识库名称已更新");
        await loadKnowledgeBases();
      } catch (error) {
        toast(error.message);
      }
    });
  });
}

async function loadDocuments() {
  try {
    cachedDocuments = await jsonRequest(`${apiBaseUrl}/documents`);
    $("#documentCount").textContent = `${cachedDocuments.length} 份文档`;
    renderKbList();
    renderDocumentList();
  } catch (error) {
    $("#documentList").innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

async function pollJob(jobId) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    try {
      const job = await jsonRequest(`${apiBaseUrl}/jobs/${jobId}`);
      $("#uploadMessage").textContent = `索引进度 ${job.progress}% · ${job.status}`;
      if (["completed", "failed", "cancelled"].includes(job.status)) {
        if (job.status === "completed") toast("文档索引完成");
        if (job.status === "failed") toast(`索引失败：${job.error_message || "未知错误"}`);
        loadDocuments();
        return;
      }
    } catch (_) { return; }
  }
  $("#uploadMessage").textContent = "后台处理超过 90 秒，请稍后点击“刷新”查看状态。";
  toast("索引仍在后台处理，可稍后刷新资料列表");
}

async function pollJobsBatch(jobIds) {
  const pending = new Set(jobIds);
  let completed = 0;
  let failed = 0;
  for (let attempt = 0; attempt < 60 && pending.size; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await Promise.all(
      Array.from(pending).map(async (jobId) => {
        try {
          const job = await jsonRequest(`${apiBaseUrl}/jobs/${jobId}`);
          if (["completed", "failed", "cancelled"].includes(job.status)) {
            pending.delete(jobId);
            if (job.status === "completed") completed += 1;
            if (job.status === "failed") failed += 1;
          }
        } catch (_) {
          pending.delete(jobId);
        }
      })
    );
    $("#uploadMessage").textContent = pending.size
      ? `批量索引进行中：剩余 ${pending.size}/${jobIds.length} 个文件……`
      : "";
    await loadDocuments();
  }
  if (pending.size) {
    toast(`批量索引仍有 ${pending.size} 个文件在后台处理，可稍后刷新资料列表`);
    return;
  }
  toast(failed ? `批量索引完成：${completed} 个成功，${failed} 个失败` : `批量索引完成：${jobIds.length} 个文件全部成功`);
}

const chatKnowledgeSelection = new Set();
let cachedKnowledgeBases = [];
let cachedDepartments = [];

function renderKbChips() {
  const chips = $("#kbChips");
  if (chatKnowledgeSelection.size === 0) {
    chips.innerHTML = '<span class="kb-placeholder">全部知识库</span>';
    return;
  }
  chips.innerHTML = cachedKnowledgeBases
    .filter((knowledgeBase) => chatKnowledgeSelection.has(knowledgeBase.id))
    .map((knowledgeBase) => `
      <span class="kb-chip">${escapeHtml(knowledgeBase.name)}<button type="button" class="kb-chip-remove" data-remove="${knowledgeBase.id}" aria-label="移除 ${escapeHtml(knowledgeBase.name)}">×</button></span>
    `).join("");
  chips.querySelectorAll("[data-remove]").forEach((removeButton) => {
    removeButton.addEventListener("click", (event) => {
      event.stopPropagation();
      chatKnowledgeSelection.delete(removeButton.dataset.remove);
      renderKbChips();
      renderKbSelectMenu();
    });
  });
}

function renderKbSelectMenu() {
  const menu = $("#kbSelectMenu");
  if (!cachedKnowledgeBases.length) {
    menu.innerHTML = '<p class="kb-select-empty">暂无知识库，先在“文档管理”中新建</p>';
    return;
  }
  const allSelected = chatKnowledgeSelection.size === 0;
  menu.innerHTML = `
    <button type="button" class="kb-option kb-option-all${allSelected ? " selected" : ""}" data-all>全部知识库</button>
    <div class="kb-select-divider"></div>
    ${cachedKnowledgeBases.map((knowledgeBase) => `
      <button type="button" class="kb-option${chatKnowledgeSelection.has(knowledgeBase.id) ? " selected" : ""}" data-id="${knowledgeBase.id}">${escapeHtml(knowledgeBase.name)}</button>`).join("")}
  `;
  menu.querySelector("[data-all]").addEventListener("click", () => {
    chatKnowledgeSelection.clear();
    renderKbSelectMenu();
    renderKbChips();
  });
  menu.querySelectorAll("[data-id]").forEach((option) => {
    option.addEventListener("click", () => {
      const id = option.dataset.id;
      if (chatKnowledgeSelection.has(id)) chatKnowledgeSelection.delete(id);
      else chatKnowledgeSelection.add(id);
      renderKbSelectMenu();
      renderKbChips();
    });
  });
}

function closeKbSelectMenu() {
  $("#kbSelectMenu").hidden = true;
  $("#kbSelectTrigger").setAttribute("aria-expanded", "false");
}

$("#kbSelectTrigger").addEventListener("click", (event) => {
  event.stopPropagation();
  const menu = $("#kbSelectMenu");
  const willOpen = menu.hidden;
  menu.hidden = !willOpen;
  $("#kbSelectTrigger").setAttribute("aria-expanded", String(willOpen));
});
$("#kbSelectTrigger").addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    $("#kbSelectTrigger").click();
  }
});
document.addEventListener("click", (event) => {
  const wrapper = $("#kbSelect");
  if (wrapper && !wrapper.contains(event.target)) closeKbSelectMenu();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeKbSelectMenu();
});

function renderKbCheckboxList(container, selectedIds = []) {
  if (!cachedKnowledgeBases.length) {
    container.innerHTML = '<p class="kb-checkbox-empty">暂无知识库，可先在上方新建</p>';
    return;
  }
  container.innerHTML = cachedKnowledgeBases
    .map((knowledgeBase) => {
      const checked = selectedIds.includes(knowledgeBase.id) ? " checked" : "";
      return `<label class="kb-checkbox-item"><input type="checkbox" value="${knowledgeBase.id}"${checked} />${escapeHtml(knowledgeBase.name)}</label>`;
    })
    .join("");
}

async function loadKnowledgeBases() {
  try {
    cachedKnowledgeBases = await jsonRequest(`${apiBaseUrl}/knowledge-bases`);
    renderKbCheckboxList($("#uploadKnowledgeBases"));
    renderKbSelectMenu();
    renderKbChips();
  } catch (_) {}
}

// Populates a <datalist> so "所属部门" is a real pick-from-the-org-chart field instead of
// free text that drifts from WeCom's actual department names — but stays an <input>, not a
// <select>, so it degrades to plain free text with zero extra logic when this isn't
// configured (dev/oidc mode, or the request just fails): an empty datalist is a no-op.
function renderDeptCheckboxList(container, selectedIds = []) {
  if (!container) return;
  if (!cachedDepartments.length) {
    container.innerHTML = '<p class="kb-checkbox-empty">未读取到企业微信部门列表</p>';
    return;
  }
  const selected = new Set(selectedIds.map(String));
  container.innerHTML = cachedDepartments
    .map((department) => {
      const checked = selected.has(String(department.id)) ? " checked" : "";
      return `<label class="kb-checkbox-item"><input type="checkbox" value="${department.id}"${checked} />${escapeHtml(department.name)}</label>`;
    })
    .join("");
}

async function loadWeComDepartments() {
  try {
    cachedDepartments = await jsonRequest(`${apiBaseUrl}/wecom/departments`);
    $("#departmentOptions").innerHTML = cachedDepartments
      .map((department) => `<option value="${escapeHtml(department.name)}"></option>`)
      .join("");
    renderDeptCheckboxList($("#uploadAccessDepartments"));
  } catch (_) {}
}

$("#createKnowledgeBase").addEventListener("click", async (event) => {
  const name = $("#newKnowledgeBaseName").value.trim();
  if (!name) return toast("请输入知识库名称");
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await jsonRequest(`${apiBaseUrl}/knowledge-bases`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, routing_keywords: [] }),
    });
    $("#newKnowledgeBaseName").value = "";
    toast("知识库已创建");
    loadKnowledgeBases();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#refreshDocuments").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  await loadDocuments();
  button.disabled = false;
});

let cachedPermissions = [];
let cachedRoles = [];

function renderPermissionList() {
  $("#permissionList").innerHTML = cachedPermissions.length
    ? cachedPermissions.map((permission) =>
        `<span class="kb-list-item"><b>${escapeHtml(permission.category)}</b>${escapeHtml(permission.code)} — ${escapeHtml(permission.description)}</span>`
      ).join("")
    : '<p class="kb-list-empty">暂无权限数据</p>';
}

function renderRoleList() {
  const editable = isSuperAdmin();
  $("#roleCount").textContent = `${cachedRoles.length} 个角色`;
  $("#roleList").innerHTML = cachedRoles.length
    ? cachedRoles.map((role) => {
        const canEditThis = editable && !role.is_system;
        const chips = cachedPermissions.map((permission) => {
          const checked = role.permission_codes.includes(permission.code);
          const classes = ["perm-chip"];
          if (checked) classes.push("checked");
          if (canEditThis) classes.push("editable"); else if (!checked) classes.push("disabled");
          const attr = canEditThis ? ` data-toggle-permission="${role.id}" data-code="${permission.code}"` : "";
          return `<span class="${classes.join(" ")}"${attr}>${escapeHtml(permission.code)}</span>`;
        }).join("");
        const deleteButton = canEditThis
          ? `<button class="delete small" data-delete-role="${role.id}" title="删除角色">删除</button>`
          : "";
        return `
          <div class="role-row">
            <div class="role-row-head">
              <div class="role-name"><strong>${escapeHtml(role.name)}</strong><span class="role-badge${role.is_system ? " system" : ""}">${role.is_system ? "系统角色" : "自定义"}</span></div>
              ${deleteButton}
            </div>
            <p class="role-desc">${escapeHtml(role.description || "无说明")}</p>
            <div class="role-permissions">${chips}</div>
          </div>`;
      }).join("")
    : '<p class="empty">暂无角色</p>';
}

async function loadRoles() {
  const canManage = isSuperAdmin();
  $("#roleCapabilityNote").textContent = canManage ? "超级管理员" : "只读";
  $("#createRoleSubmit").disabled = !canManage;
  $("#createRoleHint").textContent = canManage
    ? "权限范围限定在你所在的租户内，系统预置角色不能重命名或删除。"
    : "仅超级管理员可以创建自定义角色，当前身份没有 role.manage 权限。";
  $("#assignUserRoleSubmit").disabled = !canManage;
  $("#assignUserRoleHint").textContent = canManage
    ? "填企业微信 userid（通讯录里的\"帐号\"字段），指定后该账号下次登录即生效。super_admin 不能在这里分配，只能改服务器配置。"
    : "仅超级管理员可以分配角色，当前身份没有 role.manage 权限。";
  try {
    [cachedPermissions, cachedRoles] = await Promise.all([
      jsonRequest(`${apiBaseUrl}/permissions`),
      jsonRequest(`${apiBaseUrl}/roles`),
    ]);
    renderPermissionList();
    renderRoleList();
    renderRoleSelectOptions();
  } catch (error) {
    $("#roleList").innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
  if (canManage) await loadUserRoles();
}

function renderRoleSelectOptions() {
  // super_admin is deliberately never offered here — it can only be granted through server
  // config (WECOM_SUPER_ADMIN_USERIDS), matching the backend's own refusal to assign it.
  const options = cachedRoles.filter((role) => role.name !== "super_admin");
  $("#assignRoleSelect").innerHTML = options
    .map((role) => `<option value="${role.id}">${escapeHtml(role.name)}</option>`)
    .join("");
}

let cachedUserRoles = [];

function renderUserRoleList() {
  $("#userRoleCount").textContent = `${cachedUserRoles.length} 条`;
  $("#userRoleList").innerHTML = cachedUserRoles.length
    ? cachedUserRoles
        .map(
          (assignment) => `
          <div class="doc-row" style="grid-template-columns:1fr auto">
            <div class="doc-info"><strong>${escapeHtml(assignment.user_id)}</strong><small>${escapeHtml(assignment.role_name)}</small></div>
            <button class="delete" data-remove-assignment="${assignment.id}" title="移除">删除</button>
          </div>`
        )
        .join("")
    : '<p class="empty">还没有给任何账号分配过角色</p>';
}

async function loadUserRoles() {
  try {
    cachedUserRoles = await jsonRequest(`${apiBaseUrl}/user-roles`);
    renderUserRoleList();
  } catch (error) {
    $("#userRoleList").innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

$("#assignUserRoleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const userId = $("#assignUserId").value.trim();
  const roleId = $("#assignRoleSelect").value;
  if (!userId) return toast("请输入企业微信 userid");
  if (!roleId) return toast("没有可分配的角色");
  const button = $("#assignUserRoleSubmit");
  button.disabled = true;
  try {
    await jsonRequest(`${apiBaseUrl}/user-roles`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, role_id: roleId }),
    });
    $("#assignUserId").value = "";
    toast("已分配");
    await loadUserRoles();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = !isSuperAdmin();
  }
});

$("#userRoleList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-remove-assignment]");
  if (!button) return;
  if (!confirm("确认移除这条角色分配？")) return;
  button.disabled = true;
  try {
    await jsonRequest(`${apiBaseUrl}/user-roles/${button.dataset.removeAssignment}`, {
      method: "DELETE",
    });
    toast("已移除");
    await loadUserRoles();
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
});

$("#refreshUserRoles").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  await loadUserRoles();
  button.disabled = false;
});

$("#createRoleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = $("#newRoleName").value.trim();
  if (!name) return toast("请输入角色名称");
  const description = $("#newRoleDescription").value.trim();
  const button = $("#createRoleSubmit");
  button.disabled = true;
  try {
    await jsonRequest(`${apiBaseUrl}/roles`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description: description || null }),
    });
    $("#newRoleName").value = "";
    $("#newRoleDescription").value = "";
    toast("角色已创建");
    await loadRoles();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = isSuperAdmin() ? false : true;
  }
});

$("#roleList").addEventListener("click", async (event) => {
  const deleteButton = event.target.closest("[data-delete-role]");
  if (deleteButton) {
    if (!confirm("确认删除这个角色？")) return;
    deleteButton.disabled = true;
    try {
      await jsonRequest(`${apiBaseUrl}/roles/${deleteButton.dataset.deleteRole}`, { method: "DELETE" });
      toast("角色已删除");
      await loadRoles();
    } catch (error) {
      toast(error.message);
      deleteButton.disabled = false;
    }
    return;
  }
  const chip = event.target.closest("[data-toggle-permission]");
  if (chip) {
    const roleId = chip.dataset.togglePermission;
    const role = cachedRoles.find((item) => item.id === roleId);
    if (!role) return;
    const nextCodes = role.permission_codes.includes(chip.dataset.code)
      ? role.permission_codes.filter((code) => code !== chip.dataset.code)
      : [...role.permission_codes, chip.dataset.code];
    const row = chip.closest(".role-row");
    row.querySelectorAll(".perm-chip").forEach((node) => (node.style.pointerEvents = "none"));
    try {
      const updated = await jsonRequest(`${apiBaseUrl}/roles/${roleId}/permissions`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ permission_codes: nextCodes }),
      });
      role.permission_codes = updated.permission_codes;
      renderRoleList();
    } catch (error) {
      toast(error.message);
      row.querySelectorAll(".perm-chip").forEach((node) => (node.style.pointerEvents = ""));
    }
  }
});

$("#refreshRoles").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  await loadRoles();
  button.disabled = false;
});

if (!captureWecomTokenFromUrl()) {
  const wecomAuthError = new URLSearchParams(location.search).get("auth_error");
  if (wecomAuthError) toast(`登录失败：${wecomAuthError}`);
  autoResizeQuestion();
  checkHealth();
  loadIdentity();
  loadKnowledgeBases();
  loadWeComDepartments();
} else {
  location.reload();
}
