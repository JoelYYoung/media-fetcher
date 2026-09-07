const state = { jobs: [], profiles: [], modes: [], polling: null };
const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (response.status === 401) { location.href = "/login"; throw new Error("请重新登录"); }
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(body.detail || body || `请求失败 (${response.status})`);
  return body;
}

function escapeText(value) { return value == null ? "" : String(value); }
function bytes(value) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}
function duration(value) {
  if (!value) return "";
  const total = Math.round(value), hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60), seconds = total % 60;
  return hours ? `${hours}:${String(minutes).padStart(2,"0")}:${String(seconds).padStart(2,"0")}` : `${minutes}:${String(seconds).padStart(2,"0")}`;
}
function statusLabel(status) {
  return {queued:"等待中", probing:"正在识别", downloading:"下载中", processing:"处理中", completed:"已完成", failed:"失败", cancelled:"已取消"}[status] || status;
}
function transcriptLabel(status) {
  return {queued:"等待转写", preparing:"正在提取音频", transcribing:"语音识别中", completed:"转写完成", failed:"转写失败"}[status] || "";
}
function modeLabel(mode) { return state.modes.find((item) => item.id === mode)?.label || mode; }

function renderJobs() {
  const root = $("#jobs"); root.replaceChildren();
  $("#emptyState").classList.toggle("hidden", state.jobs.length > 0);
  const active = state.jobs.some((job) => ["queued","probing","downloading","processing"].includes(job.status) || ["queued","preparing","transcribing"].includes(job.transcript_status));
  $("#workerState").textContent = active ? "任务运行中" : "队列空闲";
  for (const job of state.jobs) {
    const node = $("#jobTemplate").content.firstElementChild.cloneNode(true);
    const site = job.site === "youtube" ? "YT" : job.site === "bilibili" ? "BILI" : "WEB";
    node.querySelector(".site-mark").textContent = site;
    node.querySelector(".job-title").textContent = job.title || job.source_host || "等待识别媒体";
    const meta = [modeLabel(job.mode), job.item_count > 1 ? `${job.item_count} 项` : duration(job.duration), job.output_size ? bytes(job.output_size) : ""].filter(Boolean);
    node.querySelector(".job-meta").textContent = meta.join(" · ");
    const pill = node.querySelector(".status-pill"); pill.textContent = statusLabel(job.status); pill.classList.add(job.status);
    const percent = Math.max(0, Math.min(100, Math.round((job.progress || 0) * 100)));
    node.querySelector(".progress-bar").style.width = `${percent}%`;
    node.querySelector(".progress-percent").textContent = `${percent}%`;
    let detail = job.downloaded_bytes ? bytes(job.downloaded_bytes) : "";
    if (job.total_bytes) detail += ` / ${bytes(job.total_bytes)}`;
    if (job.speed) detail += ` · ${bytes(job.speed)}/s`;
    if (job.eta) detail += ` · 剩余 ${duration(job.eta)}`;
    node.querySelector(".progress-detail").textContent = detail || statusLabel(job.status);
    if (job.error) { const error = node.querySelector(".job-error"); error.textContent = job.error; error.classList.remove("hidden"); }
    const subtitleFiles = job.subtitle_files || [];
    if (job.status === "completed" && job.subtitles) {
      const subtitleState = node.querySelector(".subtitle-state"); subtitleState.classList.remove("hidden");
      subtitleState.textContent = subtitleFiles.length ? (job.playlist ? `已获取 ${subtitleFiles.length} 个字幕文件，并打包进下载文件` : `已获取 ${subtitleFiles.length} 个中英文字幕文件`) : "该媒体源没有提供可用的中英文字幕";
      subtitleState.classList.toggle("missing", !subtitleFiles.length);
    }
    if (job.transcript_status) {
      const transcriptState = node.querySelector(".transcript-state");
      const transcriptPercent = Math.max(0, Math.min(100, Math.round((job.transcript_progress || 0) * 100)));
      transcriptState.classList.remove("hidden");
      transcriptState.classList.toggle("failed", job.transcript_status === "failed");
      transcriptState.classList.toggle("completed", job.transcript_status === "completed");
      node.querySelector(".transcript-phase").textContent = job.transcript_error || job.transcript_phase || transcriptLabel(job.transcript_status);
      node.querySelector(".transcript-percent").textContent = ["queued","preparing","transcribing"].includes(job.transcript_status) ? `${transcriptPercent}%` : "";
    }
    const actions = node.querySelector(".job-actions");
    if (job.status === "completed") {
      const link = document.createElement("a"); link.className = "download"; link.href = `/api/jobs/${job.id}/download`; link.textContent = "下载文件"; actions.append(link);
      if (!job.playlist) {
        for (const subtitle of subtitleFiles) {
          const subtitleLink = document.createElement("a"); subtitleLink.className = "subtitle-download"; subtitleLink.href = `/api/jobs/${job.id}/subtitles/${subtitle.id}`; subtitleLink.textContent = `${subtitle.label} · ${subtitle.format}`; subtitleLink.title = subtitle.name; actions.append(subtitleLink);
        }
      }
      if (!job.playlist) {
        if (job.transcript_status === "completed") {
          actions.append(actionButton("查看文字", () => openTranscript(job)));
          const transcriptDownload = document.createElement("a"); transcriptDownload.href = `/api/jobs/${job.id}/transcript/download`; transcriptDownload.textContent = "下载文字"; actions.append(transcriptDownload);
        } else if (["queued","preparing","transcribing"].includes(job.transcript_status)) {
          const transcribing = actionButton(`${transcriptLabel(job.transcript_status)} ${Math.round((job.transcript_progress || 0) * 100)}%`, () => {});
          transcribing.disabled = true; actions.append(transcribing);
        } else {
          actions.append(actionButton(job.transcript_status === "failed" ? "重新转写" : "语音转文字", () => startTranscription(job.id)));
        }
      }
    }
    if (["queued","probing","downloading","processing"].includes(job.status)) {
      actions.append(actionButton("取消", () => mutateJob(job.id, "cancel")));
    } else if (!["queued","preparing","transcribing"].includes(job.transcript_status)) {
      actions.append(actionButton("删除", () => mutateJob(job.id, "delete")));
    }
    root.append(node);
  }
  if (active && !state.polling) state.polling = setInterval(refreshJobs, 1800);
  if (!active && state.polling) { clearInterval(state.polling); state.polling = null; }
}

async function startTranscription(id) {
  try {
    const updated = await api(`/api/jobs/${id}/transcribe`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({language:"auto"})});
    state.jobs = state.jobs.map((job) => job.id === id ? updated : job); renderJobs(); showMessage("已加入本机语音转写队列", true);
  } catch (error) { showMessage(error.message); }
}

async function openTranscript(job) {
  try {
    const result = await api(`/api/jobs/${job.id}/transcript`);
    $("#transcriptTitle").textContent = job.title || "语音转文字";
    $("#transcriptMeta").textContent = `本机 Whisper 模型 · ${result.segments} 个音频分段 · ${result.language === "auto" ? "自动识别语言" : result.language.toUpperCase()}`;
    $("#transcriptText").textContent = result.text || "未识别到清晰语音。";
    $("#transcriptText").dataset.raw = result.text;
    $("#downloadTranscript").href = `/api/jobs/${job.id}/transcript/download`;
    $("#transcriptDialog").showModal();
  } catch (error) { showMessage(error.message); }
}

function actionButton(label, handler) {
  const button = document.createElement("button"); button.type = "button"; button.textContent = label;
  button.addEventListener("click", handler); return button;
}
async function mutateJob(id, action) {
  try {
    await api(`/api/jobs/${id}${action === "cancel" ? "/cancel" : ""}`, { method: action === "cancel" ? "POST" : "DELETE" });
    await refreshJobs();
  } catch (error) { showMessage(error.message); }
}
async function refreshJobs() {
  try { state.jobs = await api("/api/jobs"); renderJobs(); } catch (error) { showMessage(error.message); }
}
function showMessage(message, good = false) {
  const target = $("#formMessage"); target.textContent = message; target.style.color = good ? "var(--accent)" : "var(--bad)"; target.classList.remove("hidden");
  setTimeout(() => target.classList.add("hidden"), 6000);
}

function renderProfiles() {
  const root = $("#profiles"); root.replaceChildren();
  for (const profile of state.profiles) {
    const card = document.createElement("section"); card.className = "profile";
    const head = document.createElement("div"); head.className = "profile-head";
    const title = document.createElement("h3"); title.textContent = profile.label;
    const badge = document.createElement("span"); badge.className = `auth-state ${profile.has_cookies ? "on" : ""}`; badge.textContent = profile.has_cookies ? "Cookie 已配置" : "匿名模式";
    head.append(title, badge); card.append(head);

    const cookieGrid = document.createElement("div"); cookieGrid.className = "profile-grid";
    const cookieLabel = document.createElement("label"); cookieLabel.textContent = "Netscape cookies.txt";
    const file = document.createElement("input"); file.type = "file"; file.accept = ".txt,text/plain"; cookieLabel.append(file);
    const upload = actionButton("导入 Cookie", async () => {
      if (!file.files[0]) return;
      const body = new FormData(); body.append("file", file.files[0]);
      try { state.profiles = await api(`/api/profiles/${profile.site}/cookies`, {method:"POST", body}); renderProfiles(); showMessage(`${profile.label} Cookie 已加密保存`, true); }
      catch (error) { showMessage(error.message); }
    });
    cookieGrid.append(cookieLabel, upload); card.append(cookieGrid);
    if (profile.has_cookies) {
      const remove = actionButton("移除已保存 Cookie", async () => {
        state.profiles = await api(`/api/profiles/${profile.site}/cookies`, {method:"DELETE"}); renderProfiles();
      }); remove.style.marginTop = "8px"; card.append(remove);
    }

    const proxyGrid = document.createElement("div"); proxyGrid.className = "profile-grid";
    const proxyLabel = document.createElement("label"); proxyLabel.textContent = "固定出口代理（可选）";
    const proxy = document.createElement("input"); proxy.type = "text"; proxy.placeholder = profile.has_proxy ? `已配置：${profile.proxy}（输入新值可替换）` : "socks5://user:pass@host:port"; proxyLabel.append(proxy);
    const save = actionButton("保存代理", async () => {
      if (!proxy.value && profile.has_proxy) { showMessage("请输入新代理；如要清除请使用“移除代理”"); return; }
      try { state.profiles = await api(`/api/profiles/${profile.site}/proxy`, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({proxy:proxy.value})}); renderProfiles(); showMessage(`${profile.label} 代理已更新`, true); }
      catch (error) { showMessage(error.message); }
    });
    proxyGrid.append(proxyLabel, save); card.append(proxyGrid);
    if (profile.has_proxy) {
      const removeProxy = actionButton("移除代理", async () => {
        state.profiles = await api(`/api/profiles/${profile.site}/proxy`, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({proxy:""})}); renderProfiles();
      }); removeProxy.style.marginTop = "8px"; card.append(removeProxy);
    }
    const help = document.createElement("p"); help.className = "profile-help"; help.textContent = profile.site === "youtube" ? "建议从一次性无痕会话导出仅 youtube.com 的 Cookie；账号请求可能触发 YouTube 风控。" : "只会保留 bilibili.com / bilibili.tv 范围内的 Cookie。"; card.append(help);
    root.append(card);
  }
}

async function init() {
  try {
    const data = await api("/api/bootstrap");
    state.jobs = data.jobs; state.profiles = data.profiles; state.modes = data.modes;
    for (const mode of state.modes) { const option = document.createElement("option"); option.value = mode.id; option.textContent = mode.label; $("#mode").append(option); }
    renderJobs(); renderProfiles();
  } catch (error) { showMessage(error.message); }
}

$("#jobForm").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = $("#submitButton"); button.disabled = true;
  try {
    const job = await api("/api/jobs", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({url:$("#url").value, mode:$("#mode").value, playlist:$("#playlist").checked, subtitles:$("#subtitles").checked, rights_confirmed:$("#rights").checked})});
    state.jobs.unshift(job); renderJobs(); $("#url").value = ""; showMessage("任务已加入队列", true);
  } catch (error) { showMessage(error.message); } finally { button.disabled = false; }
});
$("#mode").addEventListener("change", () => { if (!$("#mode").value.startsWith("video_")) $("#subtitles").checked = false; $("#subtitles").disabled = !$("#mode").value.startsWith("video_"); });
$("#settingsButton").addEventListener("click", () => $("#settingsDialog").showModal());
$("#closeSettings").addEventListener("click", () => $("#settingsDialog").close());
$("#closeTranscript").addEventListener("click", () => $("#transcriptDialog").close());
$("#copyTranscript").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#transcriptText").dataset.raw || ""); showMessage("转写文字已复制", true); }
  catch { showMessage("浏览器未允许复制，请手动选择文字"); }
});
$("#refreshButton").addEventListener("click", refreshJobs);
init();
