/* ==========================================================================
   蕴 · 猫娘伴友 —— 前端逻辑（仿微信布局）
   左：导航栏（头像可点换） 中：列表栏 右：主内容（聊天 / 笔记 / 记忆 / 设置）
   ========================================================================== */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const messagesEl = $("#messages");
const listBody = $("#list-body");
const inputEl = $("#input");
const sendBtn = $("#btn-send");
const player = $("#player");
const hintEl = $("#hint");

const AP = { urls: {}, appearance: {}, persona: {}, styles: [] };

let view = "chat";
let busy = false;
let currentNekoMsg = null;
let currentRaw = "";
let NOTES = [];
let MEMORIES = [];
let activeNoteId = null;
let lastMessagePreview = "";

/* ---------------- 小工具 ---------------- */

function esc(t) {
  const d = document.createElement("div");
  d.textContent = t == null ? "" : String(t);
  return d.innerHTML;
}

/* ---------------- 颜文字 / 表情包 ---------------- */

const KAOMOJI = [
  "(・ω・)", "(￣▽￣)", "(=｀ω´=)", "(；一_一)", "(´・ω・`)", "(￣ω￣;)",
  "(・∀・)", "(๑´ω`๑)", "(>_<)", "(；´д｀)", "(*´▽`*)", "(¬_¬)",
  "(=①ω①=)", "(´-ω-`)", "(≧∇≦)", "(๑•̀ㅂ•́)و", "(´･ω･`)", "(・_・)",
  "(￣ε￣)", "(´；ω；`)", "(＾▽＾)", "(・ω・)ノ", "(；・∀・)", "(=￣ω￣=)",
  "(¬‿¬)", "(๑•̀ω•́)ノ", "(´∀｀)", "(・へ・)", "(￣ー￣)", "(＠_＠)",
];
const EMOJI = "😺 😹 😻 😼 😽 🙀 😿 😾 🐾 🐟 🥛 💤 ❤️ 💔 😳 😐 🤔 😏 ✅ ❌ 🎉 🔥 ✨ 🌙 ☕ 🍰".split(" ");
let STICKERS = [];
const stickerUrls = new Map();   // key -> 图片地址（key 可能是中文文件名）

/** 把 [表情:名字] / [图片:...] 渲染成图片；其余按纯文本转义。 */
function renderText(text) {
  let html = esc(text);
  html = html.replace(/\[表情:([^\]]+)\]/g, (m, key) => {
    const url = stickerUrls.get(key);
    return url ? `<img class="sticker-inline" src="${esc(url)}" alt="${esc(key)}">` : m;
  });
  html = html.replace(/\[图片:([^\]]+)\]/g, (m, u) =>
    /^(\/api\/|https?:\/\/)/.test(u)
      ? `<img class="sticker-inline" src="${u}" alt="表情" loading="lazy">`
      : m);
  // 流式输出时 [学习:主题] 会先冒出来，等后端学完才被替换成结果。
  // 直接显示原始标记很出戏，这里先渲染成一个提示。
  html = html.replace(/\[学习:([^\]]+)\]/g,
    (m, t) => `<span class="study-mark">🔍 正在去学「${t}」…</span>`);
  return html;
}

function isStickerOnly(text) {
  const t = String(text || "").trim();
  return /^\[表情:[^\]]+\]$/.test(t) || /^\[图片:[^\]]+\]$/.test(t);
}

async function refreshStickers() {
  try {
    const d = await (await fetch("/api/stickers")).json();
    STICKERS = d.stickers || [];
    stickerUrls.clear();
    STICKERS.forEach((s) => stickerUrls.set(s.key, s.url));
  } catch { /* 拿不到就不渲染表情包，不影响聊天 */ }
}

/** 导入用户自己的表情包（可多选）。 */
function pickStickerFiles() {
  const inp = document.createElement("input");
  inp.type = "file";
  inp.accept = "image/png,image/jpeg,image/webp,image/gif,image/bmp";
  inp.multiple = true;
  inp.addEventListener("change", async () => {
    const files = Array.from(inp.files || []);
    if (!files.length) return;
    const bar = $("#sticker-note");
    if (bar) bar.textContent = `正在导入 ${files.length} 张…`;
    const items = [];
    const skipped = [];
    for (const f of files) {
      if (f.size > 8 * 1024 * 1024) { skipped.push(`${f.name}（超过 8MB）`); continue; }
      try {
        items.push({ name: f.name, data_url: await readFileAsDataURL(f) });
      } catch { skipped.push(`${f.name}（读取失败）`); }
    }
    try {
      const d = await (await fetch("/api/stickers/import", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: items }),
      })).json();
      if (!d.ok) throw new Error(d.detail || "导入失败");
      await refreshStickers();
      renderEmojiPanel("sticker");
      const note = $("#sticker-note");
      if (note) {
        const bad = (d.failed || []).length + skipped.length;
        note.textContent = `已导入 ${(d.added || []).length} 张` + (bad ? `，${bad} 张被跳过` : "");
      }
    } catch (err) {
      const note = $("#sticker-note");
      if (note) note.textContent = "导入失败：" + err.message;
    }
  });
  inp.click();
}

async function deleteSticker(name) {
  try {
    const d = await (await fetch("/api/stickers/local/" + encodeURIComponent(name), {
      method: "DELETE",
    })).json();
    if (!d.ok) throw new Error(d.detail || "删除失败");
    await refreshStickers();
    renderEmojiPanel("sticker");
  } catch (err) {
    const note = $("#sticker-note");
    if (note) note.textContent = "删除失败：" + err.message;
  }
}

function renderEmojiPanel(tab) {
  const body = $("#emoji-body");
  if (tab === "sticker") {
    // 表情包全部来自用户导入（早期那 12 个程序生成的表情包已按用户要求删掉）
    body.innerHTML = `
      <div class="sticker-bar">
        <button class="ghost-btn" id="sticker-import">＋ 导入我的表情包</button>
        <span class="hint" id="sticker-note">png / jpg / webp / gif，可一次多选；点图直接发出去</span>
      </div>
      <div class="sticker-grid">${
        STICKERS.length
          ? STICKERS.map((s) => `<span class="st-wrap">
              <img class="st" data-sticker="${esc(s.key)}" src="${esc(s.url)}" title="${esc(s.label)}" alt="${esc(s.label)}">
              <button class="st-del" data-del="${esc(s.name)}" title="删除这张">×</button>
            </span>`).join("")
          : `<span class="hint">还没有表情包，点左边按钮导入你自己的</span>`
      }</div>`;
    $("#sticker-import").addEventListener("click", pickStickerFiles);
    body.querySelectorAll("[data-del]").forEach((b) => {
      b.addEventListener("click", (e) => { e.stopPropagation(); deleteSticker(b.dataset.del); });
    });
  } else if (tab === "kaomoji") {
    body.innerHTML = KAOMOJI.map((k) => `<span class="km" data-insert="${esc(k)}">${esc(k)}</span>`).join("");
  } else if (tab === "emoji") {
    body.innerHTML = EMOJI.map((e) => `<span class="km" data-insert="${esc(e)}">${esc(e)}</span>`).join("");
  } else {
    body.innerHTML = `
      <div class="search-row">
        <input id="sticker-q" placeholder="搜表情包，例如：猫猫震惊" autocomplete="off">
        <button class="send-btn" id="sticker-go" style="padding:5px 14px">搜索</button>
      </div>
      <div class="sticker-grid" id="sticker-results"><span class="hint">输入关键词后回车。图片来自必应图片搜索</span></div>`;
    const go = async () => {
      const q = $("#sticker-q").value.trim();
      if (!q) return;
      const out = $("#sticker-results");
      out.innerHTML = `<span class="hint">搜索中…</span>`;
      try {
        const d = await (await fetch("/api/stickers/search?q=" + encodeURIComponent(q))).json();
        const items = d.stickers || [];
        out.innerHTML = items.length
          ? items.map((s) => `<img class="st" data-net="${esc(s.url)}" src="${esc(s.thumb)}" title="点一下发出去">`).join("")
          : `<span class="hint">没搜到，换个词试试</span>`;
      } catch {
        out.innerHTML = `<span class="hint">搜索失败（可能是网络不通）</span>`;
      }
    };
    $("#sticker-go").addEventListener("click", go);
    $("#sticker-q").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); go(); }
    });
    setTimeout(() => $("#sticker-q").focus({ preventScroll: true }), 0);
  }
}

function insertIntoInput(text) {
  const el = $("#input");
  const at = typeof el.selectionStart === "number" ? el.selectionStart : el.value.length;
  el.value = el.value.slice(0, at) + text + el.value.slice(at);
  el.focus({ preventScroll: true });
  const pos = at + text.length;
  try { el.setSelectionRange(pos, pos); } catch { /* ignore */ }
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 140) + "px";
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const now = new Date();
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return d.toDateString() === now.toDateString() ? hm : `${d.getMonth() + 1}/${d.getDate()}`;
}

function setHint(t) { hintEl.textContent = t || ""; }
function nekoName() { return AP.persona.name || "蕴"; }
function userName() { return AP.persona.call_user || "同行者"; }

function avatarHTML(kind) {
  const url = kind === "user" ? AP.urls.user_avatar : AP.urls.neko_avatar;
  const cls = kind === "user" ? "avatar user-av" : "avatar";
  if (url) return `<div class="${cls}"><img src="${esc(url)}" alt=""></div>`;
  const fb = kind === "user" ? "我" : nekoName().slice(0, 1);
  return `<div class="${cls}"><span>${esc(fb)}</span></div>`;
}

/* ---------------- 外观 ---------------- */

function applyAppearance() {
  const theme = AP.appearance.theme || "wechat";
  document.documentElement.dataset.theme = theme;
  $$('input[name="theme"]').forEach((r) => { r.checked = r.value === theme; });

  const railImg = $("#neko-avatar-img");
  if (AP.urls.neko_avatar) {
    railImg.src = AP.urls.neko_avatar;
    railImg.classList.add("show");
  } else {
    railImg.removeAttribute("src");
    railImg.classList.remove("show");
  }
  $("#neko-avatar-fallback").textContent = nekoName().slice(0, 1);

  const tn = $("#thumb-neko");
  if (AP.urls.neko_avatar) { tn.src = AP.urls.neko_avatar; tn.style.visibility = "visible"; }
  else { tn.removeAttribute("src"); tn.style.visibility = "hidden"; }
  const tu = $("#thumb-user");
  if (AP.urls.user_avatar) { tu.src = AP.urls.user_avatar; tu.style.visibility = "visible"; }
  else { tu.removeAttribute("src"); tu.style.visibility = "hidden"; }

  const bg = AP.urls.chat_background;
  const opacity = AP.appearance.background_opacity ?? 1;
  const root = document.documentElement.style;
  if (bg) {
    root.setProperty("--chat-bg-image", `url("${bg}")`);
    root.setProperty("--chat-bg-opacity", String(opacity));
    $("#thumb-bg").style.backgroundImage = `url("${bg}")`;
  } else {
    root.setProperty("--chat-bg-image", "none");
    root.setProperty("--chat-bg-opacity", "1");
    $("#thumb-bg").style.backgroundImage = "";
  }
  $("#bg-opacity").value = String(Math.round(opacity * 100));
  $("#bg-opacity-val").textContent = `${Math.round(opacity * 100)}%`;

  document.title = `${nekoName()} · 猫娘伴友`;
  $("#chat-title").textContent = nekoName();
  $("#chat-sub").textContent = `${AP.persona.style || ""} · 称呼你「${userName()}」`;
  $("#pf-name").value = AP.persona.name || "";
  $("#pf-call").value = AP.persona.call_user || "";
  $("#pf-meow").checked = !!AP.persona.meow;
  $("#pf-keywords").value = (AP.persona.keywords || []).join("、");
  // 正在输入时不要覆盖用户写的内容
  if (document.activeElement !== $("#pf-custom")) {
    $("#pf-custom").value = AP.persona.custom_prompt || "";
  }

  refreshAllAvatars();
  renderList();
}

function refreshAllAvatars() {
  $$(".msg.other > .avatar, .msg.share > .avatar").forEach((el) => { el.outerHTML = avatarHTML("neko"); });
  $$(".msg.self > .avatar").forEach((el) => { el.outerHTML = avatarHTML("user"); });
}

async function refreshAppearance() {
  try {
    const d = await (await fetch("/api/appearance")).json();
    AP.urls = d.urls || {};
    AP.appearance = d.appearance || {};
    AP.persona = d.persona || {};
    AP.styles = d.styles || [];
    $("#pf-style").innerHTML = AP.styles
      .map((s) => `<option value="${esc(s)}"${s === AP.persona.style ? " selected" : ""}>${esc(s)}</option>`)
      .join("");
    applyAppearance();
  } catch (e) {
    setHint("读取外观设置失败：" + e.message);
  }
}

/* ---------------- 状态 ---------------- */

async function refreshStatus() {
  try {
    const d = await (await fetch("/api/status")).json();
    const st = d.status;
    $("#status-box").textContent = JSON.stringify(st, null, 2);
    const ico = $("#rail-status-ico");
    ico.className = "";
    if (st.llm_ready) { ico.classList.add("ok"); $("#rail-status-lbl").textContent = "在线"; }
    else { ico.classList.add("bad"); $("#rail-status-lbl").textContent = "缺Key"; }
    $("#notes-count").textContent = st.notes || 0;
    $("#notes-count").classList.toggle("zero", !st.notes);
    $("#memory-count").textContent = st.memories || 0;
    $("#memory-count").classList.toggle("zero", !st.memories);
    $("#list-foot-text").textContent =
      `${st.notes} 条笔记 · ${st.memories} 条记忆 · ${st.messages} 条对话`;
    fillLLM(st);
    // 朗读开关按后端存的值恢复 —— 以前 HTML 里写死 checked，
    // 于是每次加载都变回"开"，用户关了下次又自己开。
    const av = $("#auto-voice");
    if (av && typeof st.auto_read === "boolean") {
      av.checked = st.auto_read;
      av.closest(".switch")?.classList.toggle("on", st.auto_read);
    }
    // 声音总开关：关掉之后她只出文字，对话页的「朗读」键必须跟着收起来
    // （声音都没了还留着"朗读"可点，那是在骗用户）。
    const voiceOn = st.voice_enabled !== false;
    const von = $("#voice-on");
    if (von) {
      von.checked = voiceOn;
      von.closest(".switch")?.classList.toggle("on", voiceOn);
      von.closest(".fold-body")?.classList.toggle("muted", !voiceOn);
    }
    const vh = $("#voice-master-hint");
    if (vh) vh.textContent = voiceOn ? "已开启" : "已关闭（只出文字）";
    av?.closest(".switch")?.classList.toggle("hidden", !voiceOn);
    return st;
  } catch {
    $("#rail-status-ico").className = "bad";
    $("#rail-status-lbl").textContent = "离线";
    return null;
  }
}

/* 大脑配置区：显示是否已连接，回填接口地址与模型名（**不回填 Key**） */
function fillLLM(st) {
  if (!st) return;
  const baseEl = $("#llm-base");
  const modelEl = $("#llm-model");
  if (baseEl && !baseEl.value) baseEl.value = st.llm_base || "";
  if (modelEl && !modelEl.value) modelEl.value = st.llm_model || "";
  const state = $("#llm-state");
  if (state) {
    state.textContent = st.llm_ready
      ? `已连接（${st.llm_model}）`
      : "还没配置 —— 粘贴 Key 后点右边保存";
    state.style.color = st.llm_ready ? "var(--wx-green)" : "var(--text-dim)";
  }
}

/* ---------------- 消息（微信气泡） ---------------- */

/** 把一条回复拆成「独立消息」。
 *
 * 微信里表情包是**单独一条消息**（不套气泡、不跟文字挤在一起），
 * 所以这里把 [表情:x] / [图片:...] 抽出来各自成条，文字部分另成条。
 */
function splitParts(text) {
  const src = String(text || "");
  // 音乐卡也要单独成块：它是一张整卡，不能跟正文挤在一个气泡里
  const re = /\[(?:表情|图片):[^\]]+\]|\[音乐卡:[A-Za-z0-9+/=]+\]/g;
  const parts = [];
  let last = 0;
  let m;
  while ((m = re.exec(src)) !== null) {
    const before = src.slice(last, m.index).trim();
    if (before) parts.push({ kind: "text", content: before });
    const isCard = m[0].startsWith("[音乐卡:");
    parts.push(isCard ? { kind: "music", marker: m[0] } : { kind: "sticker", marker: m[0] });
    last = m.index + m[0].length;
  }
  const tail = src.slice(last).trim();
  if (tail) parts.push({ kind: "text", content: tail });
  return parts.length ? parts : [{ kind: "text", content: src }];
}

/** 解出音乐卡里的数据。解不出来返回 null（那就当普通文本，别炸整体渲染）。 */
function parseMusicCard(marker) {
  try {
    const b64 = marker.slice("[音乐卡:".length, -1);
    const bin = atob(b64);
    // base64 -> UTF-8：atob 给的是字节串，中文必须自己还原
    const bytes = Uint8Array.from(bin, (ch) => ch.charCodeAt(0));
    return JSON.parse(new TextDecoder("utf-8").decode(bytes));
  } catch {
    return null;
  }
}

/**
 * 画一张音乐卡片（微信那种）。
 *
 * 能播就内嵌播放器，不能播就跳官方页。**播放地址不在卡片里存死** ——
 * 网易云的直链带时间戳、过一会儿就失效，卡片却是要进历史记录的。
 * 所以点播时才去 `/api/music/url` 现取一次。
 */
function musicCardHTML(card) {
  const cover = card.cover
    ? `<img class="mc-cover" src="${esc(card.cover)}" alt="" onerror="this.style.visibility='hidden'" />`
    : `<div class="mc-cover mc-cover-empty">♪</div>`;
  const sub = [card.artist, card.album].filter(Boolean).map(esc).join(" · ");
  const badge = card.provider === "qq" ? "QQ音乐" : "网易云";
  const act = card.playable
    ? `<button class="mc-play" data-song="${esc(card.id)}" data-prov="${esc(card.provider || "")}"
         data-web="${esc(card.web)}" title="播放">▶</button>`
    : `<a class="mc-play mc-out" href="${esc(card.web)}" target="_blank" rel="noreferrer"
         title="在${badge}里打开">↗</a>`;
  const note = card.playable ? "点一下播放" : "这首没有直链，点开去官方听";
  return `<div class="music-card-msg">
    ${cover}
    <div class="mc-main">
      <div class="mc-title">${esc(card.title || "")}</div>
      <div class="mc-sub">${sub || badge}</div>
      <div class="mc-note">${esc(note)}</div>
    </div>
    ${act}
  </div>`;
}

/** 全局唯一的播放器：点新的卡片就换歌，不会同时响两首。 */
let MC_AUDIO = null;

function fmtTime(sec) {
  if (!Number.isFinite(sec) || sec < 0) return "0:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** 打开/更新悬浮播放器。 */
function showPlayer(card) {
  const box = $("#music-player");
  if (!box) return;
  const img = $("#pl-cover");
  if (card.cover) { img.src = card.cover; img.style.visibility = ""; } else { img.style.visibility = "hidden"; }
  $("#pl-title").textContent = card.title || "—";
  $("#pl-artist").textContent = [card.artist, card.album].filter(Boolean).join(" · ");
  $("#pl-fill").style.width = "0%";
  $("#pl-cur").textContent = "0:00";
  $("#pl-dur").textContent = "0:00";
  $("#pl-toggle").textContent = "⏸";
  box.hidden = false;
}

function syncPlayer() {
  const a = MC_AUDIO;
  if (!a) return;
  const dur = a.duration;
  if (Number.isFinite(dur) && dur > 0) {
    $("#pl-fill").style.width = ((a.currentTime / dur) * 100).toFixed(1) + "%";
    $("#pl-dur").textContent = fmtTime(dur);
  }
  $("#pl-cur").textContent = fmtTime(a.currentTime);
  $("#pl-toggle").textContent = a.paused ? "▶" : "⏸";
}

function closePlayer() {
  if (MC_AUDIO) { MC_AUDIO.pause(); MC_AUDIO = null; }
  document.querySelectorAll(".mc-play[data-song]").forEach((b) => { b.textContent = "▶"; });
  $("#music-player").hidden = true;
}

async function playMusicCard(btn) {
  const songId = btn.dataset.song;
  const prov = btn.dataset.prov || "";
  const web = btn.dataset.web || "";
  if (!songId) return;

  // 再点一次 = 暂停
  if (MC_AUDIO && MC_AUDIO.dataset.song === songId && !MC_AUDIO.paused) {
    MC_AUDIO.pause();
    btn.textContent = "▶";
    syncPlayer();
    return;
  }
  if (MC_AUDIO) MC_AUDIO.pause();
  document.querySelectorAll(".mc-play[data-song]").forEach((b) => { b.textContent = "▶"; });

  const old = btn.textContent;
  btn.textContent = "…";
  let url = "";
  let card = {};
  try {
    // 卡片自己带着歌名/封面；只有直链要现取
    const holder = btn.closest(".mc-wrap");
    card = {
      title: holder?.querySelector(".mc-title")?.textContent || "",
      artist: holder?.querySelector(".mc-sub")?.textContent || "",
      cover: holder?.querySelector(".mc-cover")?.getAttribute("src") || "",
    };
  } catch { /* 取不到就空着，只影响播放器上的标题 */ }
  try {
    const r = await (await fetch(
      `/api/music/url?id=${encodeURIComponent(songId)}&provider=${encodeURIComponent(prov)}`)).json();
    url = r.url || "";
  } catch { /* 网络问题往下走，会提示 */ }

  if (!url) {
    btn.textContent = old;
    setHint("这首现在拿不到直链（可能要会员），点右边的箭头去官方听");
    if (web) window.open(web, "_blank");
    return;
  }
  if (!MC_AUDIO || MC_AUDIO.dataset.song !== songId) {
    MC_AUDIO = new Audio(url);
    MC_AUDIO.dataset.song = songId;
    MC_AUDIO.addEventListener("timeupdate", syncPlayer);
    MC_AUDIO.addEventListener("loadedmetadata", syncPlayer);
    MC_AUDIO.addEventListener("ended", () => {
      btn.textContent = "▶";
      document.querySelectorAll(".mc-play[data-song]").forEach((b) => {
        if (b.dataset.song === songId) b.textContent = "▶";
      });
      syncPlayer();
    });
    MC_AUDIO.addEventListener("error", () => { btn.textContent = "✕"; syncPlayer(); });
  } else {
    MC_AUDIO.src = url;
  }
  showPlayer(card);
  try {
    await MC_AUDIO.play();
    btn.textContent = "⏸";
  } catch {
    btn.textContent = "▶";
    setHint("浏览器不让自动播放，再点一次试试");
  }
  syncPlayer();
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".mc-play[data-song]");
  if (btn) { e.preventDefault(); playMusicCard(btn); }
});

$("#pl-toggle")?.addEventListener("click", () => {
  if (!MC_AUDIO) return;
  if (MC_AUDIO.paused) MC_AUDIO.play().catch(() => {}); else MC_AUDIO.pause();
  setTimeout(syncPlayer, 60);
});
$("#pl-close")?.addEventListener("click", closePlayer);
$("#pl-bar")?.addEventListener("click", (e) => {
  if (!MC_AUDIO || !Number.isFinite(MC_AUDIO.duration)) return;
  const r = e.currentTarget.getBoundingClientRect();
  MC_AUDIO.currentTime = ((e.clientX - r.left) / r.width) * MC_AUDIO.duration;
  syncPlayer();
});

/** 悬浮播放器**可以拖**。位置记在 localStorage，下次还在原地。 */
(function initPlayerDrag() {
  const box = $("#music-player");
  if (!box) return;
  const KEY = "neko.player.pos";
  const apply = (left, top) => {
    const r = box.getBoundingClientRect();
    box.style.left = Math.min(Math.max(4, left), Math.max(4, innerWidth - r.width - 4)) + "px";
    box.style.top = Math.min(Math.max(4, top), Math.max(4, innerHeight - r.height - 4)) + "px";
    box.style.right = "auto";
    box.style.bottom = "auto";
  };
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || "null");
    if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) {
      apply(saved.left, saved.top);
    }
  } catch { /* 存坏了就回到默认位置 */ }

  let drag = null;
  box.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button")) return;          // 按钮不参与拖动
    const r = box.getBoundingClientRect();
    drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    try { box.setPointerCapture(e.pointerId); } catch { /* 老浏览器 */ }
    box.classList.add("dragging");
  });
  box.addEventListener("pointermove", (e) => {
    if (!drag) return;
    e.preventDefault();
    apply(e.clientX - drag.dx, e.clientY - drag.dy);
  });
  const end = () => {
    if (!drag) return;
    drag = null;
    box.classList.remove("dragging");
    try {
      localStorage.setItem(KEY, JSON.stringify({
        left: parseFloat(box.style.left), top: parseFloat(box.style.top),
      }));
    } catch { /* 隐私模式写不了，忽略 */ }
  };
  box.addEventListener("pointerup", end);
  box.addEventListener("pointercancel", end);
})();

function addMsg(role, text, opts = {}) {
  if (role === "sys") {
    const div = document.createElement("div");
    div.className = "msg sys";
    div.innerHTML = `<div class="bubble">${esc(text)}</div>`;
    messagesEl.appendChild(div);
    scrollDown();
    return div;
  }
  const isSelf = role === "self";
  const parts = splitParts(text);
  let lastEl = null;
  parts.forEach((p, i) => {
    // ---- 音乐卡：整张卡片，不套气泡 ----
    if (p.kind === "music") {
      const card = parseMusicCard(p.marker);
      const div = document.createElement("div");
      div.className = "msg music-msg " + (isSelf ? "self" : "other");
      if (opts.id && i === 0) div.dataset.id = String(opts.id);
      const pick = opts.id && i === 0 ? '<span class="msg-pick" aria-hidden="true"></span>' : "";
      div.innerHTML = pick + avatarHTML(isSelf ? "user" : "neko")
        + `<div class="mc-wrap">${card ? musicCardHTML(card) : ""}</div>`;
      messagesEl.appendChild(div);
      lastEl = div;
      return;
    }
    const isSticker = p.kind === "sticker";
    const div = document.createElement("div");
    div.className = "msg " + (isSelf ? "self" : "other")
      + (isSticker ? " sticker-msg" : "")
      + (opts.share && i === 0 ? " share" : "");
    // 数据库里的消息 id —— 按条删除要靠它。拆成多条时只有第一条挂 id
    // （它们本来就是同一条消息渲染出来的）。
    if (opts.id && i === 0) div.dataset.id = String(opts.id);
    const badge = (!isSticker && opts.badge)
      ? `<span class="item-badge${opts.badgeWarn ? " warn" : ""}">${esc(opts.badge)}</span>` : "";
    const pick = opts.id && i === 0 ? '<span class="msg-pick" aria-hidden="true"></span>' : "";
    div.innerHTML = pick + avatarHTML(isSelf ? "user" : "neko")
      + `<div class="bubble${isSticker ? " sticker-bubble" : ""}">`
      + renderText(isSticker ? p.marker : p.content) + badge + `</div>`;
    messagesEl.appendChild(div);
    lastEl = div;
  });
  scrollDown();
  if (!isSelf && text) lastMessagePreview = text;
  return lastEl;
}

/** 把 id 补到某条消息的 DOM 上（流式回复刚结束时用）。 */
function tagMsgId(el, id) {
  if (el && id) {
    el.dataset.id = String(id);
    if (!el.querySelector(".msg-pick")) {
      const dot = document.createElement("span");
      dot.className = "msg-pick";
      dot.setAttribute("aria-hidden", "true");
      el.prepend(dot);
    }
  }
}

function scrollDown() { messagesEl.scrollTop = messagesEl.scrollHeight; }

function playAudio(path, msgEl) {
  if (!path) return;
  const url = "/audio/" + encodeURIComponent(path.split(/[\\/]/).pop());
  const attach = () => {
    // 浏览器（含 Edge 应用窗口）会拦下"没有用户交互就自动播放"的音频。
    // 早先这里用 catch(() => {}) 把失败吞掉了，用户只会觉得"没声音"却不知道为什么。
    // 现在改成给这条消息挂一个可点的喇叭。
    if (!msgEl) return;
    const bubble = msgEl.querySelector(".bubble");
    if (!bubble || bubble.querySelector(".play-audio")) return;
    const btn = document.createElement("button");
    btn.className = "play-audio";
    btn.textContent = "🔊 点这里播放语音";
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      player.src = url;
      player.play().catch(() => {});
    });
    bubble.appendChild(btn);
  };
  // 元素可能不存在（老版本 DOM / 深链进入时还没渲染），别让 .checked 抛异常
  const autoVoice = $("#auto-voice");
  if (autoVoice && !autoVoice.checked) return;    // 用户关了朗读就不打扰
  player.src = url;
  const p = player.play();
  if (p && typeof p.catch === "function") p.catch(attach);
}

async function loadHistory() {
  try {
    const d = await (await fetch("/api/history?limit=60")).json();
    const rows = d.messages || [];
    // **先清空再渲染**。原来这里只往后追加，从不移除旧节点，于是：
    //   1. 清除聊天记录后数据库确实空了（验证过 11 -> 0），但界面上的旧气泡
    //      一直留着 —— 看起来就是"点了没清空"；
    //   2. 每次进来（启动 / 清除后 / 重连）空历史都会再追加一条问候语，
    //      反复几次就攒成"同行者，你来了"刷屏。
    messagesEl.innerHTML = "";
    if (!rows.length) {
      const g = await (await fetch("/api/greet")).json();
      const el = addMsg("other", g.text);
      if (g.audio) playAudio(g.audio, el);
      return;
    }
    rows.forEach((m) => {
      if (m.role === "user") addMsg("self", m.content, { id: m.id });
      else if (m.role === "assistant") {
        addMsg("other", m.content, {
          id: m.id,
          share: m.channel === "proactive",
          badge: m.channel === "proactive" ? "她自己学的" : "",
        });
      }
    });
  } catch {
    addMsg("sys", "后端没起来，请用「启动蕴-控制台.bat」看看日志");
  }
}

/* ---------------- 聊天 ---------------- */

function sendText(text) {
  if (busy || !text.trim()) return;
  busy = true;
  sendBtn.disabled = true;
  const selfEl = addMsg("self", text);   // id 等 done 事件回来再补（见下）

  currentRaw = "";
  currentNekoMsg = addMsg("other", "");
  currentNekoMsg.querySelector(".bubble").classList.add("typing");

  const es = new EventSource("/api/chat/stream?text=" + encodeURIComponent(text));
  let settled = false;

  // 收尾只能做一次，而且**无论如何都要做**。
  // 踩过的坑：原来 done 分支里 playAudio/renderText 任意一处抛异常，
  // 后面的 es.close() 和 finishTurn() 就永远执行不到，busy 卡在 true ——
  // 界面永远停在"正在输入"，用户看到的就是"这只助手卡住不动了"。
  const settle = () => {
    if (settled) return;
    settled = true;
    clearInterval(idleWatch);
    try { es.close(); } catch { /* 已经关了 */ }
    finishTurn();
  };

  // 卡死看门狗：**后端不发数据也不报错**的时候（连接还开着），
  // onerror 永远不会触发。所以自己数：超过 45 秒没有任何事件就收尾，
  // 并把这条回复标记为中断，而不是让它永远转圈。
  let lastEventAt = Date.now();
  const idleWatch = setInterval(() => {
    if (settled) return;
    if (Date.now() - lastEventAt < 45000) return;
    const bubble = currentNekoMsg ? currentNekoMsg.querySelector(".bubble") : null;
    if (bubble) {
      bubble.classList.remove("typing");
      bubble.innerHTML = renderText(
        (currentRaw || "") + "\n\n（这条回复中断了 —— 后端超过 45 秒没动静）");
    }
    reportClient("chat-stall", `超过 45 秒没有事件，已强制收尾；已收 ${currentRaw.length} 字`);
    settle();
  }, 5000);

  es.onmessage = (ev) => {
    lastEventAt = Date.now();
    if (ev.data === "[DONE]") { settle(); return; }
    let p;
    try { p = JSON.parse(ev.data); } catch { return; }
    if (p.type !== "delta" && p.type !== "done") return;
    try {
      const bubble = currentNekoMsg ? currentNekoMsg.querySelector(".bubble") : null;
      if (p.type === "delta") {
        currentRaw += p.text || "";
        if (bubble) {
          bubble.classList.remove("typing");
          bubble.innerHTML = renderText(currentRaw);
          scrollDown();
        }
      } else {
        currentRaw = p.text || currentRaw;
        const parts = splitParts(currentRaw);
        // 后端把这次问答两条消息的 id 带回来了 —— 挂到 DOM 上才能"按条删除"
        tagMsgId(selfEl, p.user_id);
        if (parts.length > 1 || parts[0].kind === "sticker") {
          // 要拆成多条独立消息就先撤掉流式那条临时气泡，再按最终内容重排
          const tmp = currentNekoMsg;
          const fresh = addMsg("other", currentRaw, { id: p.assistant_id });
          if (tmp) tmp.remove();
          if (!fresh) tagMsgId(messagesEl.lastElementChild, p.assistant_id);
        } else {
          tagMsgId(currentNekoMsg, p.assistant_id);
          if (bubble) {
            bubble.classList.toggle("sticker-bubble", false);
            bubble.innerHTML = renderText(currentRaw);
          }
        }
        lastMessagePreview = currentRaw;
        if (p.audio) playAudio(p.audio, messagesEl.lastElementChild);
        settle();
      }
    } catch (err) {
      // 渲染/放音出错不能连累收尾 —— 先把错误报上去，再确保 UI 解锁
      reportClient("chat-render", `${err && err.message ? err.message : err}`,
                   err && err.stack);
      settle();
    }
  };
  es.onerror = () => {
    const bubble = currentNekoMsg ? currentNekoMsg.querySelector(".bubble") : null;
    if (bubble) {
      bubble.classList.remove("typing");
      if (!currentRaw) bubble.textContent = "（连接后端失败了，检查一下服务还在不在）";
    }
    settle();
  };
}

/** 把前端异常报到后端日志 —— 出了事得能查到，不然只能靠猜。 */
function reportClient(kind, message, stack) {
  try {
    fetch("/api/clientlog", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: kind || "error", message: String(message || ""),
                             source: location.hash || "", stack: String(stack || "") }),
    }).catch(() => {});
  } catch { /* 上报本身失败就算了 */ }
}

// 全局兜底：任何未捕获的异常/未处理的 Promise 都记下来
window.addEventListener("error", (e) => {
  reportClient("js-error", `${e.message} @ ${e.filename}:${e.lineno}`,
               e.error && e.error.stack);
});
window.addEventListener("unhandledrejection", (e) => {
  const r = e.reason;
  reportClient("js-reject", r && r.message ? r.message : String(r), r && r.stack);
});

function finishTurn() {
  busy = false;
  sendBtn.disabled = false;
  inputEl.focus({ preventScroll: true });
  refreshStatus();
  renderList();
}

async function runAction(url, body, opts = {}) {
  if (busy) return null;
  busy = true;
  sendBtn.disabled = true;
  setHint(opts.hint || "她正在忙…");
  try {
    const r = await fetch(url, {
      method: opts.method || "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json();
    setHint("");
    if (data.text || data.message) {
      const el = addMsg("other", data.text || data.message, {
        share: data.kind === "study" || data.kind === "share",
        badge: data.level === "subtitle" ? "有字幕"
             : data.level === "asr" ? "语音识别"
             : data.level === "audience" ? "观众视角"
             : data.level === "desc" ? "仅简介"
             : data.level === "title" ? "仅标题" : "",
        badgeWarn: !!data.level && data.level !== "subtitle",
      });
      if (data.audio) playAudio(data.audio, el);
    } else if (data.ok === false) {
      addMsg("sys", data.reason || "没成功");
    }
    await refreshStatus();
    if (opts.after) await opts.after();
    return data;
  } catch (e) {
    setHint("请求失败：" + e.message);
    return null;
  } finally {
    busy = false;
    sendBtn.disabled = false;
  }
}

/* ---------------- 列表栏 ---------------- */

function renderList() {
  if (view === "chat") renderChatList();
  else if (view === "notes") renderNotesList();
  else if (view === "memory") renderMemoryList();
}

/** 列表预览用的纯文本：把标记换成占位文字，免得预览里露出 [表情:think] 这种原始标记。 */
function plainPreview(t) {
  return String(t || "")
    .replace(/\[表情:[^\]]+\]/g, "[表情]")
    .replace(/\[图片:[^\]]+\]/g, "[图片]")
    .replace(/\s+/g, " ")
    .trim();
}

function renderChatList() {
  const preview = lastMessagePreview
    ? esc(plainPreview(lastMessagePreview)).slice(0, 60)
    : "还没有聊过，跟她说点什么";
  listBody.innerHTML = `
    <div class="item active" data-open="chat">
      <div class="item-avatar">${
        AP.urls.neko_avatar ? `<img src="${esc(AP.urls.neko_avatar)}" alt="">` : esc(nekoName().slice(0, 1))
      }</div>
      <div class="item-main">
        <div class="item-top">
          <span class="item-title">${esc(nekoName())}</span>
          <span class="item-time">${fmtTime(Date.now() / 1000)}</span>
        </div>
        <div class="item-sub">${preview}</div>
      </div>
    </div>
    <div class="list-empty" style="padding:18px 14px;text-align:left">
      输入框上方有快捷指令，也可以直接打字：<br>
      「学点东西」「考考我」「你学过什么」「状态」
    </div>`;
}

/** 笔记的「内容来源等级」徽章。
 *
 * 用 level（不是 confidence）——它才是"这份笔记是从哪来的"。
 * 缺 level 的老笔记显示「来源未知」，**不能默认成「仅标题」**：
 * 那会把一份有六个知识点的笔记说得比实际更没依据，是在骗用户。
 */
function levelBadge(n) {
  const lv = (n && n.level) || "";
  if (lv === "subtitle") return { text: "有字幕", warn: false };
  if (lv === "asr") return { text: "语音识别", warn: false };
  if (lv === "audience") return { text: "观众视角", warn: false };
  if (lv === "desc") return { text: "仅简介", warn: true };
  if (lv === "title") return { text: "仅标题", warn: true };
  return { text: "来源未知", warn: true };
}

function renderNotesList() {
  if (!NOTES.length) {
    listBody.innerHTML = `<div class="list-empty">还没有笔记。<br>点聊天里的「📺 学点东西」，让她去看一个视频。</div>`;
    return;
  }
  listBody.innerHTML = NOTES.map((n) => {
    const b = levelBadge(n);
    return `<div class="item${n.bvid === activeNoteId ? " active" : ""}" data-note="${esc(n.bvid)}">
      <div class="item-avatar" style="background:linear-gradient(135deg,#4aa3ff,#56d0a0)">📄</div>
      <div class="item-main">
        <div class="item-top">
          <span class="item-title">${esc(n.title)}</span>
          <span class="item-time">${fmtTime(n.ts)}</span>
        </div>
        <div class="item-sub">
          <span class="item-badge${b.warn ? " warn" : ""}">${b.text}</span>
          ${esc(n.author || "未知UP")} · ${Math.round((n.duration || 0) / 60)} 分钟
        </div>
      </div>
      <button class="item-del" data-del-note="${esc(n.bvid)}" title="删掉这条笔记">×</button>
    </div>`;
  }).join("");
  // 悬停出现的小 ×：不点开也能删
  listBody.querySelectorAll("[data-del-note]").forEach((btn) => {
    btn.addEventListener("click", (ev) => deleteNoteQuick(btn.dataset.delNote, ev));
  });
}

function renderMemoryList() {
  if (!MEMORIES.length) {
    listBody.innerHTML = `<div class="list-empty">还没有长期记忆。<br>在右边写一句「我喜欢…」，她就记住了。</div>`;
    return;
  }
  const names = { preference: "偏好", fact: "事实", goal: "目标" };
  listBody.innerHTML = MEMORIES.map((m) => `
    <div class="item">
      <div class="item-avatar" style="background:linear-gradient(135deg,#ffb44a,#ff7a8a)">🧠</div>
      <div class="item-main">
        <div class="item-top">
          <span class="item-title">${esc(m.value)}</span>
          <span class="item-time">${esc(names[m.kind] || m.kind)}</span>
        </div>
        <div class="item-sub">${esc(m.key || "")}</div>
      </div>
    </div>`).join("");
}

/* ---------------- 笔记 ---------------- */

async function refreshNotes() {
  try {
    const d = await (await fetch("/api/notes?limit=100")).json();
    NOTES = d.notes || [];
    $("#notes-sub").textContent = `${NOTES.length} 条`;
    renderNotesList();
    if (activeNoteId) {
      // 刷新后保持选中项，但如果笔记被删了就清空详情
      if (NOTES.some((x) => x.bvid === activeNoteId)) showNote(activeNoteId);
      else { activeNoteId = null; $("#note-detail").innerHTML = `<div class="empty-lg">左边选一条笔记看看</div>`; }
    }
  } catch { /* 首次运行没数据，正常 */ }
}

function showNote(bvid) {
  const n = NOTES.find((x) => x.bvid === bvid);
  if (!n) return;
  activeNoteId = bvid;
  const b = levelBadge(n);
  const points = (n.points || []).map((p) => `<li>${esc(p)}</li>`).join("");
  const tags = (n.tags || []).map((t) => `<span class="item-badge">${esc(t)}</span>`).join(" ");
  $("#note-detail").innerHTML = `
    <div class="note-card">
      <h2>${esc(n.title)}</h2>
      <div class="sub">
        ${esc(n.author || "未知UP")} · ${Math.round((n.duration || 0) / 60)} 分钟 ·
        <span class="item-badge${b.warn ? " warn" : ""}">${b.text}</span>
        ${n.topic ? `<span class="item-badge">${esc(n.topic)}</span>` : ""} ${tags}
      </div>
      ${n.source ? `<div class="src-line">依据：${esc(n.source)}</div>` : ""}
      ${n.summary ? `<h4>她总结的</h4><div class="summary">${esc(n.summary)}</div>` : ""}
      ${points ? `<h4>知识点</h4><ol>${points}</ol>` : ""}
      ${n.url ? `<h4>原视频</h4><a href="${esc(n.url)}" target="_blank" rel="noreferrer">${esc(n.url)}</a>` : ""}
      <div class="note-actions">
        <button class="ghost-btn danger" id="btn-del-note">删除这条笔记</button>
        <span class="hint" id="del-note-hint">只删笔记，聊天记录和长期记忆都不动</span>
      </div>
    </div>`;
  wireDeleteNote(bvid);
  renderNotesList();
}

/* 单条笔记删除。用"再点一次确认"，和「数据清理」里的按钮保持一致的手感。 */
let pendingDeleteNote = null;

function wireDeleteNote(bvid) {
  const btn = $("#btn-del-note");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const hint = $("#del-note-hint");
    if (pendingDeleteNote !== bvid) {
      pendingDeleteNote = bvid;
      hint.textContent = "再点一次才会真的删除";
      hint.style.color = "#d03050";
      setTimeout(() => {
        if (pendingDeleteNote === bvid) {
          pendingDeleteNote = null;
          hint.textContent = "只删笔记，聊天记录和长期记忆都不动";
          hint.style.color = "";
        }
      }, 4000);
      return;
    }
    pendingDeleteNote = null;
    hint.textContent = "删除中…";
    try {
      const d = await (await fetch("/api/notes/" + encodeURIComponent(bvid), {
        method: "DELETE",
      })).json();
      if (!d.ok) throw new Error(d.detail || "删除失败");
      activeNoteId = null;
      await refreshNotes();
      $("#note-detail").innerHTML =
        `<div class="list-empty">这条笔记已经删掉了。<br>左边还有别的笔记。</div>`;
      renderList();
      await refreshStatus();
    } catch (err) {
      hint.textContent = "删除失败：" + err.message;
      hint.style.color = "#d03050";
    }
  });
}

/** 列表项上那个悬停出现的小 ×，不用先点开也能删。 */
async function deleteNoteQuick(bvid, ev) {
  if (ev) ev.stopPropagation();
  try {
    const d = await (await fetch("/api/notes/" + encodeURIComponent(bvid), {
      method: "DELETE",
    })).json();
    if (!d.ok) throw new Error(d.detail || "删除失败");
    if (activeNoteId === bvid) {
      activeNoteId = null;
      $("#note-detail").innerHTML = `<div class="list-empty">这条笔记已经删掉了。</div>`;
    }
    await refreshNotes();
    renderList();
    await refreshStatus();
  } catch (err) {
    $("#hint").textContent = "删除笔记失败：" + err.message;
    setTimeout(() => { $("#hint").textContent = ""; }, 3000);
  }
}

/* ---------------- 记忆 ---------------- */

async function refreshMemories() {
  try {
    const d = await (await fetch("/api/memories")).json();
    MEMORIES = d.memories || [];
    const names = { preference: "偏好", fact: "事实", goal: "目标" };
    $("#mem-list").innerHTML = MEMORIES.length
      ? MEMORIES.map((m) => `
        <div class="mem-item">
          <div>
            <span class="k">${esc(names[m.kind] || m.kind)}${m.key ? " · " + esc(m.key) : ""}</span>
            <div class="v">${esc(m.value)}</div>
          </div>
          <button class="ghost-btn" data-forget="${esc(m.value)}">删除</button>
        </div>`).join("")
      : `<div class="empty-lg">还没有长期记忆</div>`;
    $("#memory-count").textContent = MEMORIES.length;
    $("#memory-count").classList.toggle("zero", !MEMORIES.length);
    renderMemoryList();
  } catch { /* ignore */ }
}

/* ---------------- 可折叠小节（设置 / 音乐） ----------------
 * 用户要的是"点一下才展开"。做法：把每个 `h3.sec` 后面的兄弟节点
 * （直到下一个 `h3.sec`）整体搬进一个 `.fold-body`，标题本身当开关。
 *
 * 为什么是"搬节点"而不是重新生成 HTML：设置页里几十个元素都被 app.js
 * attach 过监听器（试听、保存 Key、清除数据…），搬动节点监听器跟着走，
 * 而 innerHTML 重写会把它们全部丢掉 —— 那是个不值得踩的坑。
 * 音色试听和 RVC 变声现在同属「声音」一个小节，所以天然折叠在一起。
 */
const FOLD_PAPERS = "#view-settings .paper, #view-music .paper";

function foldKey(head) {
  return "fold:" + (head.id || head.textContent.trim().slice(0, 16));
}

function setFold(head, body, open) {
  body.classList.toggle("open", open);
  head.classList.toggle("open", open);
  try { localStorage.setItem(foldKey(head), open ? "1" : "0"); } catch { /* 隐私模式写不了，忽略 */ }
}

function buildFolds() {
  $$(FOLD_PAPERS).forEach((paper) => {
    Array.from(paper.querySelectorAll("h3.sec")).forEach((head) => {
      if (head.dataset.fold) return;
      const body = document.createElement("div");
      body.className = "fold-body";
      let n = head.nextElementSibling;
      while (n && !(n.tagName === "H3" && n.classList.contains("sec"))) {
        const next = n.nextElementSibling;
        body.appendChild(n);              // 搬过去，不是克隆
        n = next;
      }
      if (!body.childNodes.length) return;   // 空小节不折叠，免得点了没反应
      head.after(body);
      head.dataset.fold = "1";
      head.classList.add("fold-head");
      if (!head.title) head.title = "点击展开 / 收起";
      let open = false;
      try { open = localStorage.getItem(foldKey(head)) === "1"; } catch { /* ignore */ }
      body.classList.toggle("open", open);
      head.classList.toggle("open", open);
      head.addEventListener("click", () => setFold(head, body, !body.classList.contains("open")));
    });
  });
}

/** 展开某个元素所在的小节。深链（`#settings-bili`）滚过去之前要先展开，
 *  否则滚到的是一个收起的标题，用户以为点错了。 */
function openFoldFor(el) {
  if (!el) return;
  if (el.classList?.contains("fold-head")) {
    const body = el.nextElementSibling;
    if (body?.classList.contains("fold-body")) { setFold(el, body, true); return; }
  }
  const body = el.closest?.(".fold-body");
  if (body && !body.classList.contains("open")) {
    const head = body.previousElementSibling;
    if (head?.classList.contains("fold-head")) setFold(head, body, true);
  }
}

/* ---------------- 视图切换 ---------------- */

function switchView(next) {
  if (!["chat", "notes", "memory", "music", "settings"].includes(next)) next = "chat";
  view = next;
  if (location.hash.slice(1) !== next) {
    try { history.replaceState(null, "", "#" + next); } catch { location.hash = next; }
  }
  $$(".rail-btn[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === next));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + next));
  $("#app").classList.toggle("no-list", next === "settings" || next === "music");
  if (next === "chat") { renderChatList(); $("#chat-dot").classList.remove("show"); }
  else if (next === "notes") {
    // 顺手把最新那条笔记显示出来：否则进来是一大片空白，还得先点一下
    refreshNotes().then(() => {
      if (!activeNoteId && NOTES.length) showNote(NOTES[0].bvid);
    });
  }
  else if (next === "memory") refreshMemories();
  else if (next === "music") refreshMusic();
  else if (next === "settings") { refreshAppearance(); refreshStatus(); refreshVoices(); refreshRvc(); refreshBili(); refreshSchedule(); refreshTopics(); refreshProviders(); }
}

/* ---------------- 上传头像 / 背景 ---------------- */

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(fr.result);
    fr.onerror = () => reject(new Error("读取文件失败"));
    fr.readAsDataURL(file);
  });
}

async function uploadImage(kind, file) {
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) { alert("图片太大了，请控制在 8MB 以内"); return; }
  setHint("正在上传…");
  try {
    const dataUrl = await readFileAsDataURL(file);
    const r = await fetch("/api/appearance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, data_url: dataUrl }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "上传失败");
    AP.urls = d.urls; AP.appearance = d.appearance; AP.persona = d.persona || AP.persona;
    applyAppearance();
    setHint("已更新");
    setTimeout(() => setHint(""), 1500);
  } catch (e) {
    setHint("上传失败：" + e.message);
  }
}

async function resetImage(kind) {
  setHint("正在恢复默认…");
  try {
    const d = await (await fetch("/api/appearance/reset", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind }),
    })).json();
    AP.urls = d.urls; AP.appearance = d.appearance;
    applyAppearance();
    setHint("已恢复默认");
    setTimeout(() => setHint(""), 1500);
  } catch (e) { setHint("失败：" + e.message); }
}

/* ---------------- 音色试听 ---------------- */

function voiceIsCurrent(p, cur) {
  const pEnabled = p.enabled === undefined ? true : p.enabled;
  return p.azure_voice === cur.azure_voice
    && Math.abs((p.pitch_semitones ?? 0) - (cur.pitch_semitones ?? 0)) < 0.01
    && pEnabled === (cur.enabled !== false);
}

async function refreshVoices() {
  try {
    const d = await (await fetch("/api/voice/presets")).json();
    const cur = d.current || {};
    $("#voice-list").innerHTML = (d.presets || []).map((p) => {
      const using = voiceIsCurrent(p, cur);
      return `<div class="voice-item${using ? " using" : ""}">
        <div class="vi-main">
          <b>${esc(p.label)}${using ? " · 正在使用" : ""}</b>
          <small>${esc(p.desc || "")}</small>
        </div>
        <div class="vi-ctl">
          <button class="ghost-btn" data-preview="${esc(p.key)}">试听</button>
          <button class="send-btn" data-apply="${esc(p.key)}" style="padding:5px 15px">用这个</button>
        </div>
      </div>`;
    }).join("");
  } catch { /* ignore */ }
}

$("#voice-list").addEventListener("click", async (e) => {
  const hint = $("#voice-hint");
  const pv = e.target.closest("[data-preview]");
  const ap = e.target.closest("[data-apply]");
  if (pv) {
    hint.textContent = "正在合成试听…";
    try {
      const d = await (await fetch("/api/voice/preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: pv.dataset.preview }),
      })).json();
      if (!d.ok) throw new Error(d.detail || "合成失败");
      player.src = "/audio/" + encodeURIComponent(d.audio.split(/[\\/]/).pop());
      await player.play().catch(() => {});
      hint.textContent = "试听：" + d.text;
    } catch (err) {
      hint.textContent = "试听失败：" + err.message;
    }
  }
  if (ap) {
    hint.textContent = "正在切换…";
    try {
      const d = await (await fetch("/api/voice/apply", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: ap.dataset.apply }),
      })).json();
      if (!d.ok) throw new Error(d.detail || "切换失败");
      hint.textContent = `已切换到「${d.label}」——之后所有回复都用这个声音`;
      await refreshVoices();
      await refreshStatus();
    } catch (err) {
      hint.textContent = "切换失败：" + err.message;
    }
  }
});

/* ---------------- RVC 变声 ---------------- */

/** 变声状态：整合包/模型在不在、引擎起没起、当前用的是哪个音色。 */
async function refreshRvc() {
  const list = $("#rvc-list");
  const state = $("#rvc-state");
  const on = $("#rvc-on");
  try {
    const d = await (await fetch("/api/rvc/status")).json();
    const r = d.rvc || {};
    on.checked = !!r.enabled;
    const cur = r.voice_name || "";
    if (!r.dir_ok) {
      state.textContent = "找不到 RVC 整合包 —— config.json 里 voice.rvc.dir 指的目录里要有一个 runtime\\python.exe";
    } else if (!(r.voices || []).length) {
      state.textContent = "还没有配置音色模型";
    } else {
      state.textContent = `${r.enabled ? "已开启" : "已关闭"} · 当前「${cur || "未选"}」`
        + (r.running ? " · 变声引擎已就绪" : " · 引擎空闲（第一次说话要多等十几秒）");
    }
    list.innerHTML = (r.voices || []).map((v) => {
      const using = !!r.enabled && v.name === cur;
      return `<div class="voice-item${using ? " using" : ""}">
        <div class="vi-main">
          <b>${esc(v.name)}${using ? " · 正在使用" : ""}</b>
          <small>${esc(v.ok ? v.model : "模型文件不存在：" + v.model)}</small>
        </div>
        <div class="vi-ctl">
          <button class="ghost-btn" data-rvc-preview="${esc(v.name)}"${v.ok ? "" : " disabled"}>试听</button>
          <button class="send-btn" data-rvc-apply="${esc(v.name)}" style="padding:5px 15px"${v.ok ? "" : " disabled"}>用这个</button>
        </div>
      </div>`;
    }).join("");
  } catch {
    state.textContent = "读不到变声状态（服务没起？）";
  }
}

$("#rvc-list").addEventListener("click", async (e) => {
  const hint = $("#rvc-hint");
  const pv = e.target.closest("[data-rvc-preview]");
  const ap = e.target.closest("[data-rvc-apply]");
  if (pv) {
    // 如实说明会等：第一次试听要把 RVC 引擎拉起来（十几秒），不说明会被当成卡死
    hint.textContent = `正在用「${pv.dataset.rvcPreview}」合成…第一次要等引擎起来，可能十几秒`;
    try {
      const d = await (await fetch("/api/rvc/preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: pv.dataset.rvcPreview }),
      })).json();
      if (!d.ok) throw new Error(d.detail || "合成失败");
      player.src = "/audio/" + encodeURIComponent(d.audio.split(/[\\/]/).pop());
      await player.play().catch(() => {});
      hint.textContent = "试听：" + d.text;
      await refreshRvc();
    } catch (err) {
      hint.textContent = "试听失败：" + err.message;
    }
  }
  if (ap) {
    hint.textContent = "正在切换…";
    try {
      const d = await (await fetch("/api/rvc/apply", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: ap.dataset.rvcApply }),
      })).json();
      if (!d.ok) throw new Error(d.detail || "切换失败");
      hint.textContent = `已切换到「${d.applied}」——之后所有回复都用这个音色`;
      await refreshRvc();
      await refreshStatus();
    } catch (err) {
      hint.textContent = "切换失败：" + err.message;
    }
  }
});

$("#rvc-on").addEventListener("change", async (e) => {
  const hint = $("#rvc-hint");
  const want = e.target.checked;
  hint.textContent = want ? "正在开启…" : "正在关闭…";
  try {
    const d = await (await fetch("/api/rvc/toggle", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on: want }),
    })).json();
    if (!d.ok) throw new Error(d.detail || "切换失败");
    hint.textContent = d.enabled
      ? "已开启变声——之后所有回复都用模型音色"
      : "已关闭变声——回到 edge-tts 的原声（每句话也快几秒）";
    await refreshRvc();
  } catch (err) {
    hint.textContent = "切换失败：" + err.message;
    e.target.checked = !want;      // 失败就把开关拨回去，别让界面撒谎
  }
});

/* ---------------- 表情面板 ---------------- */

$("#btn-emoji").addEventListener("click", () => {
  const panel = $("#emoji-panel");
  panel.classList.toggle("hidden");
  if (!panel.classList.contains("hidden")) {
    const active = panel.querySelector(".etab.active");
    renderEmojiPanel(active ? active.dataset.etab : "sticker");
  }
});

/** 收起表情面板。 */
function closeEmojiPanel() {
  const panel = $("#emoji-panel");
  if (panel) panel.classList.add("hidden");
}

// **点空白处就收起面板。**
// 之前只能再点一次「😀 表情」才收得回去，而那个按钮在面板另一头，
// 用户第一反应是点别处 —— 点了没反应就像"打不开关不掉"。Esc 也收。
//
// 但**点输入框那一块不收**：和微信一样，你可能是想边打字边挑表情。
document.addEventListener("click", (e) => {
  const panel = $("#emoji-panel");
  if (!panel || panel.classList.contains("hidden")) return;
  if (e.target.closest("#emoji-panel") || e.target.closest("#btn-emoji")) return;
  if (e.target.closest(".composer")) return;       // 输入框/发送键/快捷指令
  panel.classList.add("hidden");
}, true);          // 捕获阶段：别的处理器 stopPropagation 也不影响收起
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeEmojiPanel();
});

$$(".etab").forEach((t) => {
  t.addEventListener("click", () => {
    $$(".etab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    renderEmojiPanel(t.dataset.etab);
  });
});

/** 表情包直接作为一条独立消息发出去 —— 跟微信里点表情包一样，不塞进输入框。 */
function sendStickerNow(marker) {
  if (busy) {
    $("#hint").textContent = "等她回完这条再发";
    return;
  }
  $("#emoji-panel").classList.add("hidden");
  sendText(marker);
}

$("#emoji-body").addEventListener("click", async (e) => {
  const st = e.target.closest("[data-sticker]");
  if (st) { sendStickerNow(`[表情:${st.dataset.sticker}]`); return; }
  const net = e.target.closest("[data-net]");
  if (net) {
    // 网络图先下载到本地缓存，再作为独立消息发出去
    const out = $("#sticker-results");
    if (out) out.innerHTML = `<span class="hint">正在下载这张图…</span>`;
    try {
      const d = await (await fetch("/api/stickers/pick", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: net.dataset.net }),
      })).json();
      if (!d.ok) throw new Error(d.detail || "下载失败");
      sendStickerNow(d.marker);
    } catch (err) {
      if (out) out.innerHTML = `<span class="hint">下载失败：${esc(err.message)}</span>`;
    }
    return;
  }
  const ins = e.target.closest("[data-insert]");
  if (ins) insertIntoInput(ins.dataset.insert);   // 颜文字 / emoji 仍然插到光标处
});

/* ---------------- B 站扫码登录 ---------------- */

let biliTimer = null;

function stopBiliPoll() {
  if (biliTimer) { clearInterval(biliTimer); biliTimer = null; }
}

async function refreshBili() {
  try {
    const d = await (await fetch("/api/bili/status")).json();
    const state = $("#bili-state");
    const login = $("#btn-bili-login");
    const logout = $("#btn-bili-logout");
    if (d.logged_in) {
      state.textContent = `已登录：${d.uname}` + (d.level ? `（Lv${d.level}）` : "");
      state.style.color = "var(--wx-green)";
      login.classList.add("hidden");
      logout.classList.remove("hidden");
    } else if (d.configured) {
      state.textContent = "已保存凭据，但校验失败（可能过期了），建议重新扫码";
      state.style.color = "#d03050";
      login.classList.remove("hidden");
      logout.classList.remove("hidden");
    } else {
      state.textContent = "未登录 —— 字幕拿不到，她只能靠弹幕和评论反推内容";
      state.style.color = "var(--text-dim)";
      login.classList.remove("hidden");
      logout.classList.add("hidden");
    }
  } catch { /* ignore */ }
}

$("#btn-bili-login").addEventListener("click", async () => {
  const row = $("#bili-qr-row");
  const hint = $("#bili-hint");
  hint.textContent = "正在获取二维码…";
  try {
    const d = await (await fetch("/api/bili/qr", { method: "POST" })).json();
    if (!d.ok) throw new Error(d.detail || "获取失败");
    $("#bili-qr").innerHTML = d.svg;
    row.classList.remove("hidden");
    hint.textContent = "等待扫码…";
    stopBiliPoll();
    biliTimer = setInterval(async () => {
      try {
        const p = await (await fetch("/api/bili/qr/poll", { method: "POST" })).json();
        hint.textContent = p.message || "";
        if (p.state === "ok") {
          stopBiliPoll();
          row.classList.add("hidden");
          hint.textContent = "✔ 登录成功，字幕已解锁";
          await refreshBili();
          await refreshStatus();
        } else if (p.state === "expired" || p.state === "error") {
          stopBiliPoll();
        }
      } catch { /* 单次轮询失败不中断 */ }
    }, 2500);
  } catch (err) {
    hint.textContent = "获取二维码失败：" + err.message;
  }
});

$("#btn-bili-logout").addEventListener("click", async () => {
  stopBiliPoll();
  $("#bili-qr-row").classList.add("hidden");
  const hint = $("#bili-hint");
  hint.textContent = "已退出";
  try {
    await fetch("/api/bili/logout", { method: "POST" });
    await refreshBili();
    await refreshStatus();
  } catch { /* ignore */ }
  setTimeout(() => { hint.textContent = ""; }, 2500);
});

/* 聊天窗顶部的「自学」快捷开关 —— 设置页里那个埋得太深，日常开关放这里 */
async function refreshStudyToggle() {
  try {
    const d = await (await fetch("/api/schedule")).json();
    $("#auto-study").checked = !!(d.schedule || {}).auto_study;
  } catch { /* ignore */ }
}

// 「朗读」开关：**存进配置**，下次打开按存的值恢复。
// 以前它只是 HTML 里写死的 `checked`，没有任何持久化 —— 关了下一次加载又变回开，
// 用户看到的就是"朗读键总是莫名开启"。
$("#auto-voice")?.addEventListener("change", async (e) => {
  // **先把元素存进局部变量。** `e.currentTarget` 只在事件派发期间有效，
  // 一旦 await 过它就变成 null —— 直接在后面写 `e.currentTarget.checked`
  // 会抛 "Cannot set properties of null"。
  const cb = e.currentTarget;
  const on = cb.checked;
  const label = cb.closest(".switch");
  if (label) label.style.opacity = ".55";
  try {
    const d = await (await fetch("/api/voice/auto-read", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on }),
    })).json();
    if (!d.ok) throw new Error(d.detail || "切换失败");
    cb.checked = !!d.auto_read;
    label?.classList.toggle("on", !!d.auto_read);
    $("#hint").textContent = on ? "已开启自动朗读（这个设置会被记住）"
                                : "已关闭自动朗读（这个设置会被记住）";
    setTimeout(() => { $("#hint").textContent = ""; }, 3000);
  } catch (err) {
    cb.checked = !on;                       // 失败就回滚，别让开关骗人
    $("#hint").textContent = "切换失败：" + err.message;
  } finally {
    if (label) label.style.opacity = "";
  }
});

// 「声音」总开关：关掉之后她只出文字 —— 不合成、不朗读，
// 对话页顶部的「朗读」键也会一起收起来（由 refreshStatus 负责）。
$("#voice-on")?.addEventListener("change", async (e) => {
  const cb = e.currentTarget;            // await 之后 currentTarget 会变 null
  const on = cb.checked;
  const label = cb.closest(".switch");
  if (label) label.style.opacity = ".55";
  try {
    const d = await (await fetch("/api/voice/enabled", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on }),
    })).json();
    if (!d.ok) throw new Error(d.detail || "切换失败");
    await refreshStatus();               // 朗读键的显隐也在里面
    await refreshVoices();
    await refreshRvc();
    const hint = $("#voice-hint");
    if (hint) {
      hint.textContent = on ? "声音已开启——之后回复会合成语音"
                            : "声音已关闭——她只出文字";
    }
  } catch (err) {
    cb.checked = !on;                    // 失败就回滚，别让开关骗人
    const hint = $("#voice-hint");
    if (hint) hint.textContent = "切换失败：" + err.message;
  } finally {
    if (label) label.style.opacity = "";
  }
});

$("#auto-study").addEventListener("change", async () => {  const on = $("#auto-study").checked;
  const label = $("#auto-study").closest(".switch");
  label.style.opacity = ".55";
  try {
    const d = await (await fetch("/api/schedule", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_study: on }),
    })).json();
    if (!d.ok) throw new Error(d.detail || "切换失败");
    $("#hint").textContent = on ? "已开启自学，她会定时自己去学" : "已关闭自学，她只在你点「学点东西」时才学";
    setTimeout(() => { $("#hint").textContent = ""; }, 3000);
    await refreshSchedule();     // 设置页那份也同步
  } catch (err) {
    $("#auto-study").checked = !on;   // 失败就回滚，别让开关状态骗人
    $("#hint").textContent = "切换失败：" + err.message;
  } finally {
    label.style.opacity = "";
  }
});

/* ---------------- 自主学习计划 ---------------- */

async function refreshSchedule() {
  try {
    const d = await (await fetch("/api/schedule")).json();
    const s = d.schedule || {};
    $("#sch-auto").checked = !!s.auto_study;
    $("#auto-study").checked = !!s.auto_study;   // 顶部快捷开关同步
    $("#sch-times").value = (s.times || []).join(", ");
    $("#sch-every").value = (s.every_hours === undefined ? 0 : s.every_hours);
    $("#sch-catchup").checked = s.catch_up !== false;
    const st = $("#sch-state");
    if (!s.auto_study) {
      st.textContent = "已关闭 —— 她只在你点「学点东西」时才学，不会自己学";
      st.style.color = "#d03050";
    } else if (d.running) {
      const last = d.last_study
        ? "上次自学 " + new Date(d.last_study * 1000).toLocaleTimeString()
        : "本次启动后还没学过（到点或隔够时间会自动学）";
      st.textContent = "运行中 · " + last;
      st.style.color = "var(--wx-green)";
    } else {
      st.textContent = "已开启，但调度线程没在跑 —— 点一下「保存」会重新拉起";
      st.style.color = "#d03050";
    }
  } catch { /* ignore */ }
}

$("#btn-sch-save").addEventListener("click", async () => {
  const hint = $("#sch-hint");
  hint.textContent = "保存中…";
  const times = $("#sch-times").value.split(/[,，\s]+/).map((x) => x.trim()).filter(Boolean);
  const body = {
    auto_study: $("#sch-auto").checked,
    times: times,
    every_hours: Number($("#sch-every").value || 0),
    catch_up: $("#sch-catchup").checked,
  };
  try {
    const d = await (await fetch("/api/schedule", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })).json();
    if (!d.ok) throw new Error(d.detail || "保存失败");
    await refreshSchedule();
    hint.textContent = d.running ? "已保存，自学已启动" : "已保存（自学处于关闭状态）";
  } catch (err) {
    hint.textContent = "失败：" + err.message;
  }
  setTimeout(() => { hint.textContent = ""; }, 3500);
});

/* ---------------- 学习方向（轮换列表 + 指定内容立刻学） ---------------- */

let TOPICS = [];

function renderTopicChips() {
  const box = $("#topic-chips");
  box.innerHTML = TOPICS.length
    ? TOPICS.map((t, i) =>
        `<span class="chip">${esc(t)}<button data-del-topic="${i}" title="删掉">×</button></span>`).join("")
    : `<span class="hint">还没有方向，加一个吧（删光了自动轮换就没得学了）</span>`;
  box.querySelectorAll("[data-del-topic]").forEach((b) => {
    b.addEventListener("click", () => {
      TOPICS.splice(Number(b.dataset.delTopic), 1);
      renderTopicChips();
    });
  });
}

async function refreshTopics() {
  try {
    const d = await (await fetch("/api/topics")).json();
    TOPICS = d.topics || [];
    renderTopicChips();
  } catch { /* ignore */ }
}

function addTopicFromInput() {
  const el = $("#topic-new");
  const v = el.value.trim();
  if (!v) return;
  if (!TOPICS.includes(v)) TOPICS.push(v);
  el.value = "";
  renderTopicChips();
  el.focus({ preventScroll: true });
}

$("#btn-topic-add").addEventListener("click", addTopicFromInput);
$("#topic-new").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); addTopicFromInput(); }
});

$("#btn-topics-save").addEventListener("click", async () => {
  const hint = $("#topics-hint");
  hint.textContent = "保存中…";
  try {
    const d = await (await fetch("/api/topics", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topics: TOPICS }),
    })).json();
    if (!d.ok) throw new Error(d.detail || "保存失败");
    TOPICS = d.topics || [];
    renderTopicChips();
    hint.textContent = `已保存 ${TOPICS.length} 个方向`;
  } catch (err) {
    hint.textContent = "失败：" + err.message;
  }
  setTimeout(() => { hint.textContent = ""; }, 3500);
});

/* 「让她去学」：有 BV 号就学那个视频，否则当主题学 */
$("#btn-study-now").addEventListener("click", async () => {
  const btn = $("#btn-study-now");
  const hint = $("#study-now-hint");
  const raw = $("#study-now").value.trim();
  if (!raw) { hint.textContent = "先写点想让她学的东西"; return; }
  const bv = (raw.match(/BV[0-9A-Za-z]{10}/) || [])[0];
  const body = bv ? { bvid: bv } : { topic: raw };
  btn.disabled = true;
  hint.textContent = "她正在去 B 站看…（十几秒）";
  try {
    const d = await (await fetch("/api/study", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })).json();
    if (!d.ok) throw new Error(d.text || d.detail || "学习失败");
    const lv = { subtitle: "有字幕", asr: "语音识别", audience: "观众视角", desc: "仅简介", title: "仅标题" }[d.level] || d.level || "";
    hint.textContent = `学完了（${lv}）· ${(d.note || {}).title || ""}`.slice(0, 60);
    await refreshNotes();
    renderList();
  } catch (err) {
    hint.textContent = "失败：" + err.message;
  } finally {
    btn.disabled = false;
    setTimeout(() => { hint.textContent = ""; }, 6000);
  }
});

/* ---------------- 数据清理（两次点击确认，避免误删） ---------------- */

const CLEAR_LABEL = { chat: "对话", memory: "记忆", notes: "笔记" };
let pendingClear = null;
let clearTimer = null;

/** 把所有清除按钮恢复原状。 */
function resetClearButtons() {
  pendingClear = null;
  clearTimeout(clearTimer);
  $$("[data-clear]").forEach((b) => {
    if (b.dataset.label) b.textContent = b.dataset.label;
    b.classList.remove("confirming");
  });
}

$$("[data-clear]").forEach((btn) => {
  btn.dataset.label = btn.textContent.trim();
  btn.addEventListener("click", async () => {
    const what = btn.dataset.clear;
    const hint = $("#clear-hint");

    // 第一次点：**按钮自己**变成"再点一次确认"。
    // 原来反馈写在最后一行的 #clear-hint 上，而四个按钮分在四行 ——
    // 点第一行的人在下面第三行才看到提示，按钮本身毫无变化，
    // 于是以为"这个键没作用"。反馈必须出现在点击的地方。
    if (pendingClear !== what) {
      resetClearButtons();
      pendingClear = what;
      btn.textContent = "再点一次确认";
      btn.classList.add("confirming");
      clearTimer = setTimeout(resetClearButtons, 6000);
      // 「全部清除」代价最大（对话+记忆+笔记一起没），所以把**具体要删多少**
      // 摆出来。这一行的 hint 就在按钮旁边，看得见。
      if (what === "all") {
        try {
          const st = (await (await fetch("/api/status")).json()).status || {};
          hint.textContent = `将删除：对话 ${st.messages} 条、记忆 ${st.memories} 条、` +
            `笔记 ${st.notes} 条（删除前会自动存一份档，能读回来）`;
        } catch {
          hint.textContent = "将删除对话 + 记忆 + 笔记（删除前会自动存档）";
        }
      } else {
        hint.textContent = "";
      }
      return;
    }

    resetClearButtons();
    btn.textContent = "清除中…";
    try {
      const d = await (await fetch("/api/data/clear", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ what: what, confirm: "确认" }),
      })).json();
      if (!d.ok) throw new Error(d.detail || "删除失败");
      if (what === "chat" || what === "all") lastMessagePreview = "";
      const parts = Object.entries(d.cleared).map(([k, v]) => `${CLEAR_LABEL[k] || k} ${v} 条`);
      btn.textContent = "已清除";
      hint.textContent = "已清除：" + parts.join("，") +
        (d.backup ? `（清除前已自动存档：${d.backup}）`
                  : "（清除前的自动备份已关闭 —— 这次删除没有退路）");
      await refreshStatus();
      await refreshNotes();
      await refreshMemories();
      if (what === "chat" || what === "all") await loadHistory();
      renderList();
      await refreshSaves();
      setTimeout(() => { btn.textContent = btn.dataset.label; }, 2000);
    } catch (err) {
      btn.textContent = btn.dataset.label;
      hint.textContent = "失败：" + err.message;
    }
  });
});

/* ---------------- 事件绑定 ---------------- */

$$(".rail-btn[data-view]").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
$("#rail-status-btn").addEventListener("click", () => switchView("settings"));
$("#rail-avatar").addEventListener("click", () => {
  switchView("settings");
  const el = $('label.file-btn input[data-kind="neko_avatar"]');
  if (el) el.click();
});

listBody.addEventListener("click", (e) => {
  const noteEl = e.target.closest("[data-note]");
  if (noteEl) showNote(noteEl.dataset.note);
});

inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    const t = inputEl.value.trim();
    if (!t) return;
    inputEl.value = "";
    inputEl.style.height = "auto";
    sendText(t);
  }
});
inputEl.addEventListener("input", () => {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + "px";
});
sendBtn.addEventListener("click", () => {
  const t = inputEl.value.trim();
  if (!t) return;
  inputEl.value = "";
  inputEl.style.height = "auto";
  sendText(t);
});

$$(".composer-tools .tool[data-cmd]").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.cmd === "学点东西") {
      runAction("/api/study", { topic: null }, { hint: "她去 B 站了，可能要等十几秒…", after: refreshNotes });
    } else {
      sendText(btn.dataset.cmd);
    }
  });
});
$("#btn-proactive").addEventListener("click", () =>
  runAction("/api/proactive", {}, { hint: "她正在翻自己的笔记…", after: refreshNotes }));

$("#btn-clear").addEventListener("click", async () => {
  if (!confirm("清空全部对话历史？（长期记忆和笔记会保留）")) return;
  await fetch("/api/history", { method: "DELETE" });
  messagesEl.innerHTML = "";
  addMsg("sys", "对话已清空，但我的记忆还在");
  refreshStatus();
});

$("#btn-refresh-notes").addEventListener("click", refreshNotes);

$("#btn-add-mem").addEventListener("click", async () => {
  const v = $("#mem-value").value.trim();
  if (!v) return;
  await fetch("/api/memory", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: v, kind: "fact", key: "" }),
  });
  $("#mem-value").value = "";
  await refreshMemories();
  await refreshStatus();
});
$("#btn-clear-mem").addEventListener("click", async () => {
  if (!confirm("清空全部长期记忆？这一步不可撤销。")) return;
  await fetch("/api/memory", { method: "DELETE" });
  await refreshMemories();
  await refreshStatus();
});
$("#mem-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-forget]");
  if (!btn) return;
  await fetch("/api/memory/forget", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: btn.dataset.forget }),
  });
  await refreshMemories();
  await refreshStatus();
});

$$('input[type="file"][data-kind]').forEach((inp) => {
  inp.addEventListener("change", () => {
    uploadImage(inp.dataset.kind, inp.files && inp.files[0]);
    inp.value = "";
  });
});
$$("[data-reset]").forEach((btn) => btn.addEventListener("click", () => resetImage(btn.dataset.reset)));

// 背景浓度：拖动时即时预览，松手后保存
$("#bg-opacity").addEventListener("input", (e) => {
  const v = Number(e.target.value) / 100;
  $("#bg-opacity-val").textContent = `${e.target.value}%`;
  document.documentElement.style.setProperty("--chat-bg-opacity", String(v));
});
$("#bg-opacity").addEventListener("change", async (e) => {
  const opacity = Number(e.target.value) / 100;
  try {
    const d = await (await fetch("/api/appearance/opacity", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ opacity }),
    })).json();
    AP.appearance = d.appearance || AP.appearance;
  } catch { /* 预览已经生效，失败也不影响当前显示 */ }
});

// 主题切换
$$('input[name="theme"]').forEach((r) => {
  r.addEventListener("change", async () => {
    if (!r.checked) return;
    try {
      await fetch("/api/appearance/theme", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ theme: r.value }),
      });
      await refreshAppearance();
    } catch (e) { setHint("切换主题失败：" + e.message); }
  });
});

$("#btn-save-persona").addEventListener("click", async () => {
  const kws = $("#pf-keywords").value.split(/[、,，\s]+/).map((s) => s.trim()).filter(Boolean);
  const body = {
    name: $("#pf-name").value.trim(),
    call_user: $("#pf-call").value.trim(),
    style: $("#pf-style").value,
    meow: $("#pf-meow").checked,
    keywords: kws,
    custom_prompt: $("#pf-custom").value,
  };
  const r = await fetch("/api/persona", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (r.ok) {
    const d = await r.json();
    AP.persona = d.persona;
    $("#persona-hint").textContent = "已保存";
    setTimeout(() => { $("#persona-hint").textContent = ""; }, 1800);
    applyAppearance();
    refreshStatus();
  } else {
    $("#persona-hint").textContent = "保存失败";
  }
});

/* ---------------- 换 AI 模型 ---------------- */

let LLM_PROVIDERS = [];

/** 拉服务商预设，并按当前 base_url 选中对应项。 */
async function refreshProviders() {
  try {
    const d = await (await fetch("/api/llm/providers")).json();
    LLM_PROVIDERS = d.providers || [];
    const cur = ((d.current || {}).base_url || "").replace(/\/$/, "");
    const sel = $("#llm-provider");
    sel.innerHTML = LLM_PROVIDERS.map((p) =>
      `<option value="${esc(p.key)}">${esc(p.label)}</option>`).join("")
      + `<option value="__custom">自定义…</option>`;
    const hit = LLM_PROVIDERS.find((p) => p.base_url.replace(/\/$/, "") === cur);
    sel.value = hit ? hit.key : "__custom";
    $("#llm-provider-hint").textContent = hit ? (hit.hint || "") : "手动填接口地址";
  } catch { /* ignore */ }
}

$("#llm-provider").addEventListener("change", (e) => {
  const p = LLM_PROVIDERS.find((x) => x.key === e.target.value);
  if (!p) { $("#llm-provider-hint").textContent = "手动填接口地址"; return; }
  $("#llm-base").value = p.base_url;
  $("#llm-provider-hint").textContent = p.hint || "";
  $("#llm-models-hint").textContent = "换服务商后先点「保存」，再获取模型列表";
});

/** 向接口问它有哪些模型。 */
$("#btn-llm-models").addEventListener("click", async () => {
  const hint = $("#llm-models-hint");
  hint.textContent = "正在问接口…";
  try {
    const d = await (await fetch("/api/llm/models")).json();
    if (!d.ok) throw new Error(d.detail || "拿不到列表");
    const sel = $("#llm-model-pick");
    sel.innerHTML = `<option value="">（共 ${d.models.length} 个，选一个即切换）</option>`
      + d.models.map((m) =>
          `<option value="${esc(m)}"${m === d.current ? " selected" : ""}>${esc(m)}${m === d.current ? "（当前）" : ""}</option>`
        ).join("");
    hint.textContent = `拿到 ${d.models.length} 个模型`;
  } catch (err) {
    hint.textContent = "失败：" + err.message;
  }
});

/** 选一个模型 → 立刻切换并测通。 */
$("#llm-model-pick").addEventListener("change", async (e) => {
  const model = e.target.value;
  if (!model) return;
  const hint = $("#llm-models-hint");
  $("#llm-model").value = model;
  hint.textContent = `正在切到 ${model} …`;
  try {
    const d = await (await fetch("/api/llm", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: model }),
    })).json();
    if (!d.ok) throw new Error(d.detail || "切换失败");
    $("#llm-hint").textContent = `已切到 ${model}`;
    hint.textContent = "切换成功，正在测通…";
    await testLLM();
    hint.textContent = `已切到 ${model}`;
    await refreshStatus();
  } catch (err) {
    hint.textContent = "切换失败：" + err.message;
  }
});

/** 真发一句话过去，确认这个模型确实能用。 */
async function testLLM() {
  const hint = $("#llm-test-hint");
  hint.textContent = "测试中…";
  hint.style.color = "";
  try {
    const d = await (await fetch("/api/llm/test", { method: "POST" })).json();
    if (d.ok && d.warn) {
      hint.textContent = `⚠ ${d.model}：${d.warn}`;
      hint.style.color = "#d98014";
    } else if (d.ok) {
      hint.textContent = `✔ ${d.model} 可用（${d.ms}ms，回复「${d.reply}」）`;
      hint.style.color = "var(--wx-green)";
    } else {
      hint.textContent = `✘ ${d.model} 不可用：${d.error}`;
      hint.style.color = "#d03050";
    }
  } catch (err) {
    hint.textContent = "测试失败：" + err.message;
    hint.style.color = "#d03050";
  }
}

$("#btn-llm-test").addEventListener("click", testLLM);

// 在界面里填 API Key：写进 config.json 并立刻生效，不用去编辑器改 JSON
$("#btn-save-llm").addEventListener("click", async () => {
  const h = $("#llm-hint");
  const body = {};
  const key = $("#llm-key").value.trim();
  const base = $("#llm-base").value.trim();
  const model = $("#llm-model").value.trim();
  if (key) body.api_key = key;
  if (base) body.base_url = base;
  if (model) body.model = model;
  if (!body.api_key && !body.base_url && !body.model) {
    h.textContent = "请先粘贴 API Key";
    return;
  }
  h.textContent = "保存中…";
  try {
    const d = await (await fetch("/api/llm", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    })).json();
    if (!d.ok) throw new Error(d.detail || "保存失败");
    $("#llm-key").value = "";                 // 保存后清空输入框，避免 Key 留在页面上
    $("#status-box").textContent = JSON.stringify(d.status, null, 2);
    fillLLM(d.status);
    await refreshStatus();
    h.textContent = d.status.llm_ready ? "✔ 已保存，大脑已连接" : "已保存，但看起来 Key 还不对";
  } catch (e) {
    h.textContent = "失败：" + e.message;
  }
  setTimeout(() => { h.textContent = ""; }, 5000);
});

// 重新读取 config.json（改完 API Key 不用重启）
$("#btn-reload").addEventListener("click", async () => {
  const h = $("#reload-hint");
  h.textContent = "读取中…";
  try {
    const d = await (await fetch("/api/reload", { method: "POST" })).json();
    if (!d.ok) throw new Error(d.detail || "读取失败");
    $("#status-box").textContent = JSON.stringify(d.status, null, 2);
    AP.persona = d.persona || AP.persona;
    await refreshAppearance();
    await refreshStatus();
    h.textContent = d.status.llm_ready ? "✔ 已接上大脑" : "Key 还没填对";
  } catch (e) {
    h.textContent = "失败：" + e.message;
  }
  setTimeout(() => { h.textContent = ""; }, 3000);
});

// 搜索
$("#search-input").addEventListener("input", async (e) => {
  const q = e.target.value.trim();
  if (!q) { renderList(); return; }
  try {
    const d = await (await fetch("/api/search?q=" + encodeURIComponent(q))).json();
    const notes = d.notes || [], mems = d.memories || [];
    if (!notes.length && !mems.length) {
      listBody.innerHTML = `<div class="list-empty">没找到「${esc(q)}」相关的内容</div>`;
      return;
    }
    let html = "";
    if (notes.length) {
      html += `<div class="list-empty" style="padding:12px 14px 4px;text-align:left">笔记</div>`;
      html += notes.map((n) => `<div class="item" data-note="${esc(n.bvid)}">
        <div class="item-avatar" style="background:linear-gradient(135deg,#4aa3ff,#56d0a0)">📄</div>
        <div class="item-main"><div class="item-title">${esc(n.title)}</div></div></div>`).join("");
    }
    if (mems.length) {
      html += `<div class="list-empty" style="padding:12px 14px 4px;text-align:left">记忆</div>`;
      html += mems.map((m) => `<div class="item">
        <div class="item-avatar" style="background:linear-gradient(135deg,#ffb44a,#ff7a8a)">🧠</div>
        <div class="item-main"><div class="item-title">${esc(m.value)}</div></div></div>`).join("");
    }
    listBody.innerHTML = html;
  } catch { /* ignore */ }
});

/* ---------------- 实时事件 ---------------- */

function subscribeEvents() {
  let es;
  try { es = new EventSource("/api/events"); } catch { return; }
  es.onmessage = (ev) => {
    let d;
    try { d = JSON.parse(ev.data); } catch { return; }
    if (d.type === "hello") return;
    if (d.type === "share" && d.text) {
      const el = addMsg("other", d.text, { share: true, badge: "她自己学的" });
      lastMessagePreview = d.text;
      if (d.audio) playAudio(d.audio, el);
      if (view !== "chat") $("#chat-dot").classList.add("show");
      refreshStatus();
      refreshNotes();
    }
  };
  es.onerror = () => { es.close(); setTimeout(subscribeEvents, 8000); };
}

/* 上报一次启动后的关键元素状态。窗口没有控制台，
   "界面某块空白但又不报错"这种情况只能靠这个看到真相。 */
function reportBoot() {
  try {
    const q = (s) => document.querySelector(s);
    const lb = q("#list-body");
    const rail = q(".rail");
    const info = {
      rail_btns: document.querySelectorAll(".rail-btn").length,
      rail_display: rail ? getComputedStyle(rail).display : "NO-RAIL",
      rail_h: rail ? Math.round(rail.getBoundingClientRect().height) : -1,
      list_children: lb ? lb.children.length : -1,
      list_display: lb ? getComputedStyle(lb).display : "NO-BODY",
      list_h: lb ? Math.round(lb.getBoundingClientRect().height) : -1,
      list_html: lb ? lb.innerHTML.slice(0, 90) : "",
      head_display: q(".list-head") ? getComputedStyle(q(".list-head")).display : "NO-HEAD",
      search_display: q(".search-box") ? getComputedStyle(q(".search-box")).display : "NO-SEARCH",
      app_class: q("#app") ? q("#app").className : "NO-APP",
      view: view,
      hash: location.hash,
      ap_urls: JSON.stringify(AP.urls),
      persona: JSON.stringify(AP.persona).slice(0, 80),
      // 清除按钮是否完成了绑定（绑定时会写 data-label）；用来排查"点了没反应"
      clear_btns: Array.from(document.querySelectorAll("[data-clear]"))
        .map((b) => `${b.dataset.clear}:${b.dataset.label || "未绑定"}`).join(" ") || "无按钮",
      // —— 下面这些是"到底画在哪"的关键 ——
      win: window.innerWidth + "x" + window.innerHeight,
      dpr: window.devicePixelRatio,
      body_zoom: getComputedStyle(document.body).zoom,
      rect_rail: JSON.stringify(rectOf(rail)),
      rect_avatar: JSON.stringify(rectOf(q(".rail-avatar"))),
      rect_btn1: JSON.stringify(rectOf(q(".rail-btn"))),
      rect_status: JSON.stringify(rectOf(q("#rail-status-btn"))),
      rect_search: JSON.stringify(rectOf(q(".search-box"))),
      rect_item: JSON.stringify(rectOf(q("#list-body .item"))),
      cs_btn1: q(".rail-btn") ? styleOf(q(".rail-btn")) : "",
      cs_item: q("#list-body .item") ? styleOf(q("#list-body .item")) : "",
      cs_search: q(".search-box") ? styleOf(q(".search-box")) : "",
      cs_railavatar: q(".rail-avatar") ? styleOf(q(".rail-avatar")) : "",
    };
    fetch("/api/clientlog", {
      method: "POST", headers: { "Content-Type": "application/json" },
      // hash / view / 视图类名放进 message：stack 那条会被后端截断，
      // 关键信息被截掉就看不出问题了（这次就吃了这个亏）
      body: JSON.stringify({
        kind: "boot",
        message: `hash=${location.hash || "(空)"} view=${view} active=${(() => {
          const a = document.querySelector(".view.active");
          return a ? a.id : "无";
        })()}`,
        source: "",
        stack: JSON.stringify(info),
      }),
    });
  } catch (e) { /* 诊断用，失败无所谓 */ }
}

function rectOf(el) {
  if (!el) return "NO-EL";
  const r = el.getBoundingClientRect();
  return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
}

function styleOf(el) {
  const cs = getComputedStyle(el);
  return `display=${cs.display} vis=${cs.visibility} op=${cs.opacity} color=${cs.color} bg=${cs.backgroundColor} font=${cs.fontSize} overflow=${cs.overflow} transform=${cs.transform}`;
}

/* ---------------- 存档（类似游戏存档） ---------------- */

const SAVE_LABEL = { messages: "对话", memories: "记忆", notes: "笔记" };

function saveCountsText(counts) {
  return Object.entries(counts || {})
    .map(([k, v]) => `${SAVE_LABEL[k] || k} ${v}`).join(" · ");
}

async function refreshSaves() {
  const box = $("#saves-list");
  if (!box) return;
  try {
    const d = await (await fetch("/api/saves")).json();
    const saves = d.saves || [];
    if (d.dir) $("#saves-dir").textContent =
      "存在 " + d.dir + "（「跳转」会先把当前进度收进它自己的存档，再切过去，不会丢）";
    if (!saves.length) {
      box.innerHTML = '<div class="saves-empty">还没有存档。点上面的「存档」存一份。</div>';
      return;
    }
    box.innerHTML = "";
    saves.forEach((s) => {
      const row = document.createElement("div");
      row.className = "save-item" + (s.active ? " is-active" : "");
      const name = document.createElement("div");
      name.className = "save-meta";
      const tag = s.auto ? '<span class="save-tag auto">自动</span>' : "";
      const cur = s.active ? '<span class="save-tag current">当前</span>' : "";
      name.innerHTML = `<b>${esc(s.name)}</b>${cur}${tag}` +
        `<small>${esc(s.time)} · ${esc(saveCountsText(s.counts))} · ${(s.size / 1024).toFixed(0)} KB</small>`;

      // 跳转：不覆盖当前进度，而是把当前进度写回它自己的槽位再切过去。
      // 用户的原话是「不要覆盖存档，改为跳转存档」。
      const jump = document.createElement("button");
      jump.className = "ghost-btn";
      if (s.active) {
        jump.textContent = "在此存档中";
        jump.disabled = true;
      } else {
        jump.textContent = "跳转";
        jump.addEventListener("click", async () => {
          jump.textContent = "跳转中…";
          jump.disabled = true;
          try {
            const r = await (await fetch("/api/saves/switch", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ name: s.name }),
            })).json();
            if (!r.ok) throw new Error(r.detail || "跳转失败");
            const kept = r.kept ? `，当前进度已收进「${r.kept}」` : "";
            $("#saves-hint").textContent = `已跳到「${s.name}」` + kept;
            await refreshStatus();
            await refreshNotes();
            await refreshMemories();
            await loadHistory();
            renderList();
            await refreshSaves();
          } catch (err) {
            jump.disabled = false;
            jump.textContent = "跳转";
            $("#saves-hint").textContent = "失败：" + err.message;
          }
        });
      }

      const del = document.createElement("button");
      del.className = "ghost-btn danger";
      del.textContent = "删除";
      del.addEventListener("click", async () => {
        del.disabled = true;
        del.textContent = "删除中…";
        try {
          const r = await fetch(`/api/saves/${encodeURIComponent(s.name)}`, { method: "DELETE" });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          $("#saves-hint").textContent = `已删除存档「${s.name}」`;
        } catch (err) {
          if (del.isConnected) { del.disabled = false; del.textContent = "删除"; }
          $("#saves-hint").textContent = `删除失败（${err.message}）：存档可能正被占用，重开一次软件再试`;
        }
        await refreshSaves();
      });
      row.append(name, jump, del);
      box.appendChild(row);
    });
  } catch {
    box.innerHTML = '<div class="saves-empty">读不到存档列表（后端没起来？）</div>';
  }
}

$("#btn-save-create")?.addEventListener("click", async () => {
  const hint = $("#saves-hint");
  hint.textContent = "存档中…";
  try {
    const r = await (await fetch("/api/saves", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("#save-name").value.trim() }),
    })).json();
    if (!r.ok) throw new Error(r.detail || "存档失败");
    $("#save-name").value = "";
    hint.textContent = `已存档「${r.save.name}」：` + saveCountsText(r.save.counts);
    await refreshSaves();
  } catch (err) {
    hint.textContent = "失败：" + err.message;
  }
});

/* ---------------- 设备连接 ---------------- */

let DEV = { enabled: false, gateway: "", token: "", devices: [] };
let CONN = null;

function renderDevList() {
  const box = $("#dev-list");
  box.innerHTML = "";
  if (!DEV.devices.length) {
    box.innerHTML = '<div class="saves-empty">还没有登记任何设备。' +
      (DEV.enabled ? "下面加一个。" : "（先打开上面的开关）") + "</div>";
    return;
  }
  DEV.devices.forEach((d, i) => {
    const row = document.createElement("div");
    row.className = "save-item";
    const meta = document.createElement("div");
    meta.className = "save-meta";
    const warn = d.dangerous ? '<span class="save-tag warn">危险·需确认</span>' : "";
    meta.innerHTML = `<b>${esc(d.name)}</b>${warn}` +
      `<small class="dev-acts">${esc(d.id)} · 动作：${esc((d.actions || []).join("、")) || "无"}</small>`;
    const del = document.createElement("button");
    del.className = "ghost-btn danger";
    del.textContent = "删除";
    del.addEventListener("click", async () => {
      const next = DEV.devices.filter((_, j) => j !== i)
        .map((x) => ({ id: x.id, name: x.name, kind: x.kind,
                       dangerous: x.dangerous, actions: x.actions }));
      await saveDevices(next);
    });
    row.append(meta, del);
    box.appendChild(row);
  });
}

async function saveDevices(list) {
  const hint = $("#dev-status");
  hint.textContent = "保存中…";
  try {
    const r = await (await fetch("/api/devices/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: $("#dev-enabled").checked,
        gateway: $("#dev-gateway").value.trim(),
        token: $("#dev-token").value.trim(),
        devices: list.map((d) => ({
          id: d.id || d.name,
          name: d.name,
          kind: d.kind || "",
          dangerous: !!d.dangerous,
          // 动作统一成 {名: 名} 的字典形式（网关命令缺省就是动作名本身）
          actions: Object.fromEntries((d.actions || []).map((a) => [a, a])),
        })),
      }),
    })).json();
    if (!r.ok) throw new Error(r.detail || "保存失败");
    DEV = { enabled: r.enabled, gateway: r.gateway, devices: r.devices };
    $("#dev-gateway").value = r.gateway || "";
    renderDevList();
    hint.textContent = "已保存";
  } catch (err) {
    hint.textContent = "保存失败：" + err.message;
  }
}

async function refreshDevices() {
  try {
    const d = await (await fetch("/api/devices")).json();
    DEV = { enabled: d.enabled, gateway: d.gateway, devices: d.devices || [] };
    $("#dev-enabled").checked = !!d.enabled;
    $("#dev-gateway").value = d.gateway || "";
    renderDevList();
  } catch {
    $("#dev-list").innerHTML = '<div class="saves-empty">读不到设备列表</div>';
  }
}

async function refreshConnectInfo() {
  try {
    CONN = await (await fetch("/api/connect-info")).json();
    $("#conn-lan").textContent = CONN.base_lan;
    $("#conn-token").textContent = CONN.token || "（未生成）";
    $("#conn-note").textContent = CONN.lan_reachable
      ? "局域网已开放，硬件可以直接连上面的地址"
      : "⚠ 现在只监听本机（" + CONN.bind + "），硬件连不上 —— 打开上面「允许局域网接入」并重启";
    $("#conn-endpoints").innerHTML = CONN.endpoints
      .map((e) => `<code>${e.method} ${esc(e.path)}</code> — ${esc(e.note)}`)
      .join("<br>");
    $("#dev-lan").checked = !!CONN.lan_reachable;
  } catch {
    $("#conn-note").textContent = "读不到接入信息（后端没起来？）";
  }
}

$("#btn-dev-test")?.addEventListener("click", async () => {
  const hint = $("#dev-status");
  hint.textContent = "测试中…";
  try {
    const r = await (await fetch("/api/devices/test", { method: "POST" })).json();
    if (r.ok) {
      const st = r.state ? "：" + Object.entries(r.state).map(([k, v]) => `${k}=${v}`).join("，") : "";
      hint.textContent = `✅ 网关通了${st}`;
    } else {
      hint.textContent = `❌ ${r.reason}${r.hint ? "。" + r.hint : ""}`;
    }
  } catch (err) {
    hint.textContent = "❌ 测试失败：" + err.message;
  }
});

$("#btn-dev-save")?.addEventListener("click", () => {
  saveDevices(DEV.devices.map((d) => ({ ...d, actions: d.actions || [] })));
});

$("#dev-enabled")?.addEventListener("change", () => {
  saveDevices(DEV.devices.map((d) => ({ ...d, actions: d.actions || [] })));
});

$("#dev-lan")?.addEventListener("change", async () => {
  const want = $("#dev-lan").checked;
  const hint = $("#dev-status");
  hint.textContent = "切换中…";
  try {
    const r = await (await fetch("/api/connect/bind", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lan: want }),
    })).json();
    if (!r.ok) throw new Error(r.detail || "切换失败");
    hint.textContent = r.note;
    $("#conn-hint").textContent = want
      ? "局域网接入已打开，重启软件后生效。Token 已自动生成，硬件请求必须带上它。"
      : "局域网接入已关闭，重启软件后生效。";
    await refreshConnectInfo();
  } catch (err) {
    $("#dev-lan").checked = !want;
    hint.textContent = "切换失败：" + err.message;
  }
});

$("#btn-dev-add")?.addEventListener("click", async () => {
  const name = $("#dev-new-name").value.trim();
  if (!name) { $("#dev-status").textContent = "设备名不能为空"; return; }
  const raw = $("#dev-new-actions").value.trim() || "开、关";
  const actions = raw.split(/[、,，\s]+/).map((s) => s.trim()).filter(Boolean);
  const next = DEV.devices.map((d) => ({ ...d, actions: d.actions }))
    .concat([{ id: name, name, kind: "", dangerous: $("#dev-new-danger").checked, actions }]);
  await saveDevices(next);
  $("#dev-new-name").value = "";
  $("#dev-new-actions").value = "";
  $("#dev-new-danger").checked = false;
});

$("#btn-conn-token")?.addEventListener("click", async () => {
  const hint = $("#conn-hint");
  hint.textContent = "重新生成中…";
  try {
    const r = await (await fetch("/api/connect/token", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ regenerate: true }),
    })).json();
    hint.textContent = "新 Token 已生成，旧的对硬件立刻失效，记得同步改固件";
    await refreshConnectInfo();
  } catch (err) {
    hint.textContent = "生成失败：" + err.message;
  }
});

$("#btn-conn-copy")?.addEventListener("click", async () => {
  const hint = $("#conn-hint");
  if (!CONN) { hint.textContent = "信息还没读到"; return; }
  const lines = [
    "蕴 · 猫娘伴友 —— 机器人接入信息",
    "本机地址: " + CONN.base_local,
    "局域网地址: " + CONN.base_lan,
    "访问 Token: " + (CONN.token || "（未设置）"),
    "鉴权方式: Authorization: Bearer <token>（本机 127.0.0.1 不需要）",
    "",
    "可用接口:",
    ...CONN.endpoints.map((e) => `  ${e.method} ${e.path}  — ${e.note}`),
  ];
  const text = lines.join("\n");
  try {
    await navigator.clipboard.writeText(text);
    hint.textContent = "已复制到剪贴板";
  } catch {
    // 剪贴板没有权限时退化成显示出来，让用户手动复制
    hint.textContent = "复制失败，手动复制：" + text.slice(0, 60) + "…";
    console.log(text);
  }
});

/* ---------------- 余额 / 预算（一行小字） ---------------- */

let METER = null;

async function refreshMeter(force = false) {
  const el = $("#meter");
  if (!el) return;
  try {
    const d = await (await fetch("/api/meter" + (force ? "?refresh=true" : ""))).json();
    METER = d;

    // 显示开关：设置页里那个「显示消耗金额」。关掉就整行藏起来。
    const show = d.show !== false;
    el.style.display = show && d.enabled ? "" : "none";
    const cb = $("#budget-show");
    if (cb) cb.checked = !!show && !!d.enabled;
    const llmCb = $("#budget-llm");
    if (llmCb) llmCb.checked = d.llm_enabled !== false;
    const daily = $("#budget-daily");
    if (daily && document.activeElement !== daily) daily.value = d.limit ?? 0;
    const det = $("#budget-detail");
    if (det) {
      // 主口径是**蕴自己的消耗**（按 token 估算）。余额差会把同一个 key
      // 在别处（另一个会话、你自己的脚本）的消耗也算进来，只能当参考。
      const own = Number(d.spent_today || 0);
      const acct = Number(d.account_spent_today || 0);
      const tk = d.today || {};
      det.textContent = d.enabled
        ? `蕴自己今日 ¥${own.toFixed(4)} / 上限 ¥${Number(d.limit || 0).toFixed(2)}`
          + ` · 调用 ${tk.calls || 0} 次`
          + `（输入 ${tk.prompt_tokens || 0} / 输出 ${tk.completion_tokens || 0} tokens）`
          + ` · 账户整体今日 ¥${acct.toFixed(2)}（含别处）`
          + ` · 余额 ¥${d.balance == null ? "?" : Number(d.balance).toFixed(2)}`
          + ` · 拦下 ${(d.blocked || {}).count || 0} 次`
        : "预算功能已在配置里关闭";
    }

    if (!d.enabled) { el.textContent = "预算关"; el.title = "预算功能已在配置里关闭"; return; }
    // 主显示是**已消耗金额**（今日），不是余额 —— 用户要看的是"烧了多少"。
    // 余额挪到悬停详情里，一行小字保持不抢眼。
    if (d.spent_today === null || d.spent_today === undefined) {
      el.textContent = "¥?";
      el.className = "meter unknown";
      el.title = "余额查不到(" + (d.error || "未知") + ") —— 消耗金额算不出来，点一下重试";
      return;
    }
    const t = d.today || {};
    const bl = d.blocked || {};
    const fmt = (v) => (v === null || v === undefined ? "—" : Number(v).toFixed(2));
    // 主口径 = 蕴自己（按 token 估算）。账户口径单列，明确标注"含别处"。
    const detail =
      `蕴自己今日 ¥${Number(d.spent_today || 0).toFixed(4)} / 上限 ¥${fmt(d.limit)}（点一下刷新）\n` +
      `今日调用 ${t.calls || 0} 次 · 输入 ${t.prompt_tokens || 0}`
      + `（缓存命中 ${t.cache_hit || 0}） / 输出 ${t.completion_tokens || 0} tokens\n` +
      `── 以下为账户口径，含同一个 key 在别处的消耗 ──\n` +
      `账户今日 ¥${fmt(d.account_spent_today)} · 当前余额 ¥${fmt(d.balance)}\n` +
      `被拦下 ${bl.count || 0} 次` + (bl.last ? `（最近：${bl.last}）` : "");

    // 大模型总开关关着时，主显示换成「离线」—— 一眼就知道现在不会再花钱
    if (d.llm_enabled === false) {
      el.textContent = "离线";
      el.className = "meter off";
      el.title = "⚠ 大模型已关闭 —— 不会产生任何 API 消耗\n"
        + "（在「设置 → 用量与预算」里打开）\n\n" + detail;
      return;
    }

    // 主显示是**已消耗金额**（今日），不是余额 —— 用户要看的是"烧了多少"。
    // 余额挪到悬停详情里，一行小字保持不抢眼。
    if (d.spent_today === null || d.spent_today === undefined) {
      el.textContent = "¥?";
      el.className = "meter unknown";
      el.title = "余额查不到(" + (d.error || "未知") + ") —— 消耗金额算不出来，点一下重试";
      return;
    }
    const spent = Number(d.spent_today) || 0;
    const lim = Number(d.limit) || 0;
    const pct = lim > 0 ? spent / lim : 0;
    // 单次对话通常也就几厘钱，小数位给够才看得出在动
    el.textContent = "¥" + (spent < 0.01 ? spent.toFixed(4)
                          : spent < 0.1 ? spent.toFixed(3) : spent.toFixed(2));
    el.className = "meter" + (pct >= 0.8 ? " low" : "");
    el.title = detail;
  } catch {
    el.textContent = "¥?";
    el.className = "meter unknown";
    el.title = "读不到消耗数据（后端没起来？）";
  }
}

$("#meter")?.addEventListener("click", () => refreshMeter(true));

/** 保存预算设置。show 是"显不显示那行小字"的开关。 */
async function saveBudget(patch) {
  const hint = $("#budget-hint");
  if (hint) hint.textContent = "保存中…";
  try {
    const r = await (await fetch("/api/budget/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    })).json();
    if (!r.ok) throw new Error(r.detail || "保存失败");
    if (hint) hint.textContent = "已保存";
    await refreshMeter(true);
  } catch (err) {
    if (hint) hint.textContent = "保存失败：" + err.message;
  }
}

$("#budget-show")?.addEventListener("change", (e) => {
  saveBudget({ show: e.target.checked });
});
// 大模型总开关：关掉后一次 API 请求都不会发出去，界面上显示「离线」
$("#budget-llm")?.addEventListener("change", async (e) => {
  const want = e.target.checked;
  const hint = $("#budget-hint");
  if (hint) hint.textContent = want ? "正在接入大模型…" : "正在断开大模型…";
  await saveBudget({ llm_enabled: want });
  if (hint) hint.textContent = want
    ? "已接入大模型"
    : "已断开大模型 —— 不会再有 API 消耗。她仍能陪聊、看笔记、操作设备。";
});
$("#btn-budget-save")?.addEventListener("click", () => {
  const v = parseFloat($("#budget-daily").value);
  saveBudget({ daily_cny: Number.isFinite(v) && v >= 0 ? v : 0 });
});
$("#btn-budget-refresh")?.addEventListener("click", () => refreshMeter(true));

/* ---------------- 深链定位 ---------------- */

/**
 * 滚到某个设置小节，**并且验证有没有真的滚到位**。
 *
 * 原来是一句 `setTimeout(() => anchor.scrollIntoView(), 350)`，问题是：
 * 那一刻目标视图可能还是 `display:none`（切换视图和内容布局都需要时间），
 * 对隐藏元素调 scrollIntoView 是**空操作**，于是深链有时能定位、有时停在页首。
 * 现在改成"等它可见 → 滚 → 检查 → 不够就重试"，最多约 1.4 秒。
 */
async function scrollToSection(anchor) {
  const scroller = anchor.closest(".paper") || document.scrollingElement;
  for (let i = 0; i < 12; i++) {
    await new Promise((r) => setTimeout(r, 120));
    if (!anchor.offsetParent) continue;                 // 还不可见，再等
    anchor.scrollIntoView({ block: "start" });
    const top = anchor.getBoundingClientRect().top;
    const base = scroller === document.scrollingElement
      ? 0 : scroller.getBoundingClientRect().top;
    // 已经贴近容器顶部（或容器实在滚不动了）就算成功
    const maxed = scroller !== document.scrollingElement
      && scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 4;
    if (Math.abs(top - base) < 90 || maxed) return true;
  }
  return false;
}

/* ---------------- 按条删除聊天记录 ---------------- */

//: 选择模式下的选中集合（存消息 id）
const picked = new Set();

function pickModeOn() {
  return messagesEl.classList.contains("selecting");
}

function setPickMode(on) {
  messagesEl.classList.toggle("selecting", on);
  $("#pick-bar").classList.toggle("show", on);
  if (!on) picked.clear();
  refreshPickUI();
}

function refreshPickUI() {
  $("#pick-count").textContent = `已选 ${picked.size} 条`;
  $("#btn-pick-del").disabled = picked.size === 0;
  messagesEl.querySelectorAll(".msg").forEach((el) => {
    const id = el.dataset.id;
    el.classList.toggle("selected", !!id && picked.has(id));
  });
}

/** 删除给定的消息 id（一条或多条）。 */
async function deleteMessages(ids) {
  if (!ids.length) return;
  const hint = pickModeOn() ? $("#pick-hint") : null;
  if (hint) hint.textContent = "删除中…";
  try {
    const r = await (await fetch("/api/messages/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    })).json();
    if (!r.ok) throw new Error(r.detail || "删除失败");
    if (hint) hint.textContent = `已删除 ${r.deleted} 条`;
    picked.clear();
    await loadHistory();
    await refreshStatus();
    renderList();
    if (pickModeOn()) { setPickMode(false); }
    if (hint) hint.textContent = `已删除 ${r.deleted} 条`;
  } catch (err) {
    const msg = "删除失败：" + err.message;
    if (hint) hint.textContent = msg;
    else setHint(msg);
  }
}

/* ---------- 消息右键 / 长按菜单（仿微信） ---------- */

//: 当前右键菜单指向的消息 id
let menuTargetId = null;
let menuEl = null;
let pressTimer = null;
let pressStart = null;

function closeMsgMenu() {
  if (menuEl) { menuEl.remove(); menuEl = null; }
  menuTargetId = null;
}

/** 在光标附近弹出菜单。超出视口就翻到另一侧。 */
function openMsgMenu(x, y, id) {
  closeMsgMenu();
  menuTargetId = id;
  menuEl = document.createElement("div");
  menuEl.className = "msg-menu";
  menuEl.innerHTML = `
    <button type="button" data-act="copy">复制</button>
    <button type="button" data-act="multi">多选</button>
    <div class="sep"></div>
    <button type="button" data-act="delete" class="danger">删除</button>`;
  document.body.appendChild(menuEl);
  const r = menuEl.getBoundingClientRect();
  menuEl.style.left = Math.max(8, Math.min(x, innerWidth - r.width - 8)) + "px";
  menuEl.style.top = Math.max(8, Math.min(y, innerHeight - r.height - 8)) + "px";
}

function msgTextOf(el) {
  const b = el && el.querySelector(".bubble");
  return b ? (b.innerText || "").trim() : "";
}

// 右键
messagesEl.addEventListener("contextmenu", (e) => {
  if (pickModeOn()) return;                 // 多选模式下不弹菜单
  const el = e.target.closest(".msg");
  if (!el || !el.dataset.id) return;
  e.preventDefault();
  openMsgMenu(e.clientX, e.clientY, el.dataset.id);
});

// 长按（触摸 / 按住鼠标不动）—— 微信在手机上是长按，桌面上两个都支持更顺手
messagesEl.addEventListener("pointerdown", (e) => {
  if (pickModeOn()) return;
  if (e.pointerType === "mouse" && e.button !== 0) return;
  const el = e.target.closest(".msg");
  if (!el || !el.dataset.id) return;
  pressStart = { x: e.clientX, y: e.clientY, el };
  el.classList.add("pressing");
  pressTimer = setTimeout(() => {
    pressTimer = null;
    el.classList.remove("pressing");
    openMsgMenu(e.clientX, e.clientY, el.dataset.id);
    // 菜单出来后别再触发选择/点击
    self.getSelection?.()?.removeAllRanges?.();
  }, 550);
});
const endPress = (e) => {
  if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
  if (pressStart) { pressStart.el.classList.remove("pressing"); pressStart = null; }
};
messagesEl.addEventListener("pointerup", endPress);
messagesEl.addEventListener("pointercancel", endPress);
messagesEl.addEventListener("pointermove", (e) => {
  // 手指/鼠标挪动超过阈值就当成滑动，取消长按
  if (pressStart && Math.hypot(e.clientX - pressStart.x, e.clientY - pressStart.y) > 10) {
    endPress(e);
  }
});

// 菜单动作
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".msg-menu button");
  if (!btn) { closeMsgMenu(); return; }
  const act = btn.dataset.act;
  const id = menuTargetId;
  const el = id ? messagesEl.querySelector(`.msg[data-id="${id}"]`) : null;
  if (act === "copy") {
    try { await navigator.clipboard.writeText(msgTextOf(el)); setHint("已复制"); }
    catch { setHint("复制失败（浏览器不给剪贴板权限）"); }
    closeMsgMenu();
  } else if (act === "multi") {
    closeMsgMenu();
    if (id) picked.add(id);
    setPickMode(true);
    setHint("点消息可多选，底部操作条删除");
  } else if (act === "delete") {
    // 两击确认：第一次点变红问一次，再点才真删
    if (!btn.classList.contains("armed")) {
      btn.classList.add("armed");
      btn.textContent = "确认删除";
      setTimeout(() => {
        if (btn.isConnected) { btn.classList.remove("armed"); btn.textContent = "删除"; }
      }, 3000);
      return;
    }
    closeMsgMenu();
    await deleteMessages([Number(id)]);
  }
});

// 点别处 / Esc / 滚动都关掉菜单
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMsgMenu(); });
messagesEl.addEventListener("scroll", closeMsgMenu, true);
window.addEventListener("blur", closeMsgMenu);

// 多选模式下点整条消息切换选中
messagesEl.addEventListener("click", (e) => {
  if (!pickModeOn()) return;
  const el = e.target.closest(".msg");
  const id = el && el.dataset.id;
  if (!id) return;
  if (picked.has(id)) picked.delete(id); else picked.add(id);
  refreshPickUI();
});

$("#btn-pick-cancel")?.addEventListener("click", () => { setPickMode(false); setHint(""); });
$("#btn-pick-all")?.addEventListener("click", () => {
  const all = Array.from(messagesEl.querySelectorAll(".msg[data-id]")).map((el) => el.dataset.id);
  if (picked.size === all.length) picked.clear();
  else all.forEach((id) => picked.add(id));
  refreshPickUI();
});
$("#btn-pick-del")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  if (!btn.classList.contains("armed")) {
    btn.classList.add("armed");
    btn.textContent = `确认删除 ${picked.size} 条？`;
    setTimeout(() => {
      if (btn.isConnected) { btn.classList.remove("armed"); btn.textContent = "删除"; }
    }, 4000);
    return;
  }
  btn.classList.remove("armed");
  btn.textContent = "删除";
  await deleteMessages(Array.from(picked).map(Number));
});

/* ---------------- 一起听音乐 ---------------- */

let MUSIC_QR_TIMER = null;

function stopQrPoll() {
  if (MUSIC_QR_TIMER) { clearInterval(MUSIC_QR_TIMER); MUSIC_QR_TIMER = null; }
}

function songRowHTML(s, { comment = "", when = "", id = "", songId = "", provider = "",
                          artist = "", title = "" } = {}) {
  const cover = s.cover
    ? `<img src="${esc(s.cover)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'" />`
    : `<img alt="" />`;
  const sub = esc(s.artist || "") + (s.album ? " · " + esc(s.album) : "");
  // 「喜欢 / 不喜欢」直接当成喂给听歌倾向的信号
  const vote = id
    ? `<button class="ghost-btn small" data-like="1" data-artist="${esc(artist || s.artist || "")}"
         data-title="${esc(title || s.title || "")}" title="喜欢（会把她/他记进偏好）">👍</button>
       <button class="ghost-btn small" data-like="0" data-artist="${esc(artist || s.artist || "")}"
         data-title="${esc(title || s.title || "")}" title="不喜欢（以后避开）">👎</button>`
    : "";
  return `<div class="song-row"${id ? ` data-session="${id}"` : ""}>
    ${cover}
    <div class="s-main">
      <div class="s-title">${esc(s.title || "")}</div>
      <div class="s-sub">${sub}</div>
      ${comment ? `<div class="s-comment">${esc(comment)}</div>` : ""}
    </div>
    ${when ? `<span class="s-when">${esc(when)}</span>` : ""}
    ${vote}
    ${id ? `<button class="ghost-btn danger small" data-del-session="${id}">删</button>` : ""}
  </div>`;
}

function renderMusicNow(song, comment) {
  const box = $("#music-now");
  if (!box) return;
  if (!song) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="music-card">
    ${song.cover ? `<img class="cover" src="${esc(song.cover)}" alt="" />` : `<div class="cover"></div>`}
    <div class="meta">
      <div class="song-title">${esc(song.title)}</div>
      <div class="song-artist">${esc(song.artist)}${song.album ? " · " + esc(song.album) : ""}</div>
      <div class="song-comment">${esc(comment || "")}</div>
    </div>
  </div>`;
}

async function refreshMusic() {
  try {
    const d = await (await fetch("/api/music/state")).json();
    const cur = d.provider || "netease";
    const mine = d[cur] || {};
    const logged = !!mine.logged_in;
    const label = cur === "qq" ? "QQ音乐" : "网易云";
    $("#mp-netease").checked = cur === "netease";
    $("#mp-qq").checked = cur === "qq";
    $("#music-who").textContent = logged
      ? `已登录 ${label}：${mine.nickname || "用户"}`
      : `还没登录 ${label}（搜歌不需要登录）`;
    $("#btn-music-logout").style.display = logged ? "" : "none";
    $("#music-sub").textContent = logged
      ? `她看得见你在 ${label} 听什么，也能陪你一起听`
      : "登录后她能看见你在听什么（不登录也能一起听）";

    // Cookie 兜底的步骤要跟着服务商变 —— 两家要抄的字段完全不同
    const steps = $("#music-cookie-steps");
    if (steps) {
      steps.innerHTML = cur === "qq"
        ? '步骤：① 浏览器打开 <code>y.qq.com</code> 并登录 → '
          + '② 按 <code>F12</code> → <code>Application</code>（应用）→ '
          + '<code>Cookies</code> → <code>https://y.qq.com</code> → '
          + '③ 找到 <code>qm_keyst</code>（可能叫 <code>qqmusic_key</code>）'
          + '和 <code>uin</code> 那几行，<b>整段复制</b>粘到下面。'
        : '步骤：① 浏览器打开 <code>music.163.com</code> 并登录 → '
          + '② 按 <code>F12</code> → <code>Application</code>（应用）→ '
          + '<code>Cookies</code> → <code>https://music.163.com</code> → '
          + '③ 找到 <code>MUSIC_U</code> 那一行，<b>整段复制</b>'
          + '（或直接复制 <code>document.cookie</code>）粘到下面。';
      $("#music-cookie-text").placeholder = cur === "qq"
        ? "qm_keyst=xxxxx; uin=xxxxx; ...（整段粘贴就行）"
        : "MUSIC_U=xxxxx; __csrf=xxxxx; ...（整段粘贴就行）";
    }

    // 听歌倾向
    try {
      const t = await (await fetch("/api/music/taste")).json();
      $("#taste-likes").value = (t.likes || []).join("、");
      $("#taste-dislikes").value = (t.dislikes || []).join("、");
      $("#taste-history").checked = t.prefer_history !== false;
      $("#taste-hint").textContent = (t.likes || []).length || (t.dislikes || []).length
        ? `喜欢 ${(t.likes || []).length} · 避开 ${(t.dislikes || []).length}` : "";
    } catch { /* 读不到就留空 */ }

    // 最近在听
    const rBox = $("#music-recent");
    if (logged) {
      const r = await (await fetch("/api/music/recent?limit=15")).json();
      const songs = r.songs || [];
      $("#music-recent-hint").textContent = songs.length ? `${songs.length} 首` : "";
      rBox.innerHTML = songs.length
        ? songs.map((s) => songRowHTML(s, { when: s.play ? `${s.play} 次` : "" })).join("")
        : `<div class="empty">最近没有听歌记录（或这家没开放这个接口）</div>`;
    } else {
      $("#music-recent-hint").textContent = "";
      rBox.innerHTML = `<div class="empty">登录后这里会显示你最近在听什么</div>`;
    }

    // 一起听过
    const s = await (await fetch("/api/music/sessions")).json();
    const list = s.sessions || [];
    $("#music-count").textContent = list.length;
    $("#music-count").classList.toggle("zero", !list.length);
    $("#music-stats").textContent = list.length
      ? `${list.length} 次` + ((s.stats?.top_artists || []).length
        ? " · 最常听 " + s.stats.top_artists.map(([a, n]) => `${a}×${n}`).join("、") : "")
      : "";
    $("#music-sessions").innerHTML = list.length
      ? list.map((x) => {
        // 标一下来源：是她主动分享的，还是在音乐页点的「一起听」
        const tag = x.source === "share" ? "她分享的" : "一起听";
        const when = new Date((x.ts || 0) * 1000)
          .toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
        return songRowHTML(
          { title: x.title, artist: x.artist, cover: x.cover },
          { comment: x.comment, id: x.id, when: `${tag} · ${when}`,
            artist: x.artist, title: x.title },
        );
      }).join("")
      : `<div class="empty">还没有一起听过 · 她会推荐，或在「一起听一首」里点一首</div>`;
  } catch {
    setHint("读不到音乐状态（后端没起来？）");
  }
}

async function musicLogin(provider) {
  stopQrPoll();
  const hint = $("#music-login-hint");
  const p = provider || (($("#mp-qq") || {}).checked ? "qq" : "netease");
  hint.textContent = "正在生成二维码…";
  try {
    const d = await (await fetch("/api/music/qr?provider=" + p, { method: "POST" })).json();
    if (!d.ok) { hint.textContent = "生成失败：" + (d.reason || "未知"); return; }
    const box = $("#music-qr");
    // 网易云给的是 SVG，QQ 给的是 PNG data URI —— 两种都塞进去就行
    if (d.svg) box.innerHTML = d.svg;
    else if (d.image) box.innerHTML = `<img src="${d.image}" alt="登录二维码" style="width:100%" />`;
    else { hint.textContent = "没拿到二维码图"; return; }
    box.style.display = "flex";
    // **必须说清用哪个 App 扫。** QQ 的码是 QQ互联登录码，
    // 只有手机QQ App 内置扫码器认；用微信/QQ音乐/系统相机扫会当成普通网址，
    // 打开的就是 QQ 下载页 —— 用户反馈过这个。
    hint.textContent = p === "qq"
      ? "⚠ 必须用【手机QQ】App 里的扫码（不是QQ音乐、不是微信、不是相机）—— " +
        "用别的扫会跳到 QQ 下载页。约 4 分钟内有效"
      : "用手机【网易云音乐】App 扫码（约 4 分钟内有效）";
    let alive = 0;
    MUSIC_QR_TIMER = setInterval(async () => {
      alive += 1;
      if (alive > (d.ttl || 240) / 3) {
        stopQrPoll(); box.style.display = "none";
        hint.textContent = "二维码过期了，重新点一次「扫码登录」";
        return;
      }
      try {
        const u = `/api/music/qr/poll?key=${encodeURIComponent(d.key)}&provider=${p}`;
        const r = await (await fetch(u)).json();
        if (r.done) {
          stopQrPoll(); box.style.display = "none";
          hint.textContent = `登录成功：${r.nickname || ""}`;
          await refreshMusic();
        } else if (r.message) {
          hint.textContent = r.message;
        }
      } catch { /* 单次失败不打断轮询 */ }
    }, 3000);
  } catch (e) {
    hint.textContent = "生成失败：" + e.message;
  }
}

async function musicSetProvider(p) {
  await fetch("/api/music/provider", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider: p }),
  });
  stopQrPoll();
  $("#music-qr").style.display = "none";
  $("#music-login-hint").textContent = "点右上角「扫码登录」用手机扫一下，不登录也能一起听。";
  await refreshMusic();
}

async function musicLogout() {
  stopQrPoll();
  $("#music-qr").style.display = "none";
  await fetch("/api/music/logout", { method: "POST" });
  $("#music-login-hint").textContent = "已退出登录。随时可以再扫码登回来。";
  await refreshMusic();
}

async function musicTogether() {
  const btn = $("#btn-music-together");
  const kw = ($("#music-keyword").value || "").trim();
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "在挑歌…";
  // 这一步要调模型让她说邀请语，实测约十秒。不给提示的话用户会以为没反应、
  // 再点一次，于是同一个歌记两条（已加后端去重，但前端也不该误导）。
  setHint("正在挑歌，并让她说点什么…（约十秒）");
  try {
    const d = await (await fetch("/api/music/together", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keyword: kw }),
    })).json();
    if (!d.ok) {
      setHint("一起听失败：" + (d.reason || "未知"));
      if (d.need_login) $("#music-login-hint").textContent = d.reason;
      return;
    }
    renderMusicNow(d.song, d.comment);
    if (d.audio) playAudio(d.audio, null);
    setHint(`一起听了《${d.song.title}》 · 回「讲讲」我就展开说`);
    await refreshMusic();
  } catch (e) {
    setHint("一起听失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

$("#btn-music-login")?.addEventListener("click", () => musicLogin());
$("#btn-music-logout")?.addEventListener("click", musicLogout);
$("#btn-music-cookie-toggle")?.addEventListener("click", (e) => {
  const box = $("#music-cookie-box");
  const open = box.style.display !== "none";
  box.style.display = open ? "none" : "block";
  e.currentTarget.textContent = open ? "展开" : "收起";
});
$("#btn-music-cookie-save")?.addEventListener("click", async () => {
  const hint = $("#music-cookie-hint");
  const text = $("#music-cookie-text").value || "";
  const prov = ($("#mp-qq") || {}).checked ? "qq" : "netease";
  hint.textContent = "导入中…";
  try {
    const r = await (await fetch("/api/music/cookie?provider=" + prov, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    })).json();
    if (!r.ok) { hint.textContent = r.reason || "导入失败"; return; }
    hint.textContent = `导入成功：${r.nickname || "已登录"}（拿到 ${(r.fields || []).length} 个字段）`;
    $("#music-cookie-text").value = "";
    await refreshMusic();
  } catch (e) {
    hint.textContent = "导入失败：" + e.message;
  }
});
$("#mp-netease")?.addEventListener("change", () => musicSetProvider("netease"));
$("#mp-qq")?.addEventListener("change", () => musicSetProvider("qq"));
$("#btn-music-refresh")?.addEventListener("click", refreshMusic);
$("#btn-music-together")?.addEventListener("click", musicTogether);
$("#music-keyword")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); musicTogether(); }
});
$("#music-sessions")?.addEventListener("click", async (e) => {
  // 表态：喜欢 / 不喜欢 -> 学进听歌倾向
  const vote = e.target.closest("[data-like]");
  if (vote) {
    const like = vote.dataset.like === "1";
    vote.disabled = true;
    try {
      const r = await (await fetch("/api/music/feedback", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ artist: vote.dataset.artist || "",
                               title: vote.dataset.title || "", like }),
      })).json();
      if (r.ok) {
        setHint(`${like ? "已记下喜欢" : "已记下不喜欢"}：${r.artist}`);
        await refreshMusic();
      } else {
        setHint(r.reason || "记不下这首歌的歌手");
      }
    } catch (err) {
      setHint("操作失败：" + err.message);
    } finally {
      vote.disabled = false;
    }
    return;
  }
  const btn = e.target.closest("[data-del-session]");
  if (!btn) return;
  const id = btn.dataset.delSession;
  if (!btn.classList.contains("armed")) {
    btn.classList.add("armed");
    btn.textContent = "确认";
    setTimeout(() => {
      if (btn.isConnected) { btn.classList.remove("armed"); btn.textContent = "删"; }
    }, 3500);
    return;
  }
  await fetch("/api/music/sessions/delete", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids: [Number(id)] }),
  });
  await refreshMusic();
});

/** 把输入框里的「、,，」分隔列表存进听歌倾向。 */
async function saveTaste() {
  const split = (v) => String(v || "").split(/[、,，;；\s]+/).map((s) => s.trim()).filter(Boolean);
  const body = {
    likes: split($("#taste-likes").value),
    dislikes: split($("#taste-dislikes").value),
    prefer_history: $("#taste-history").checked,
    preview: ($("#music-keyword").value || "").trim(),
  };
  const box = $("#taste-preview-box");
  try {
    const r = await (await fetch("/api/music/taste", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })).json();
    setHint(`听歌倾向已保存（喜欢 ${(r.likes || []).length} · 避开 ${(r.dislikes || []).length}）`);
    const pv = r.preview;
    if (box && pv && pv.song && pv.song.title) {
      box.innerHTML = `<div class="row"><div class="row-label">
        <b>按这个倾向，她会挑：</b>
        <small>《${esc(pv.song.title)}》— ${esc(pv.song.artist || "")}
          ${pv.why ? "（" + esc(pv.why) + "）" : ""}</small>
        <small>候选打分：${(pv.candidates || []).map((c) =>
          `${esc(c.title || "")} ${c.score}`).join(" · ")}</small>
        </div></div>`;
    } else if (box) {
      box.innerHTML = "";
    }
    await refreshMusic();
  } catch (e) {
    setHint("保存失败：" + e.message);
  }
}

$("#btn-taste-save")?.addEventListener("click", saveTaste);
$("#btn-taste-preview")?.addEventListener("click", saveTaste);

/**
 * 在**聊天页**点「一起听歌」。
 *
 * 之前只有音乐页有这个入口，聊天聊到一半想一起听还得先切过去 ——
 * 用户反馈"一起听功能无法使用"，其实是找不到入口。
 * 这里让她把卡片直接发到对话里（消息本身已经落库，刷新后也还在）。
 */
async function togetherInChat() {
  const btn = $("#btn-together");
  const kw = (inputEl.value || "").trim();     // 输入框里写了歌名就用它
  if (btn) { btn.disabled = true; }
  const old = btn ? btn.textContent : "";
  if (btn) btn.textContent = "🎧 在挑歌…";
  setHint(kw ? `正在找《${kw}》并让她说点什么…（约十秒）`
             : "正在从你的「最近在听」里挑一首…（约十秒）");
  try {
    const d = await (await fetch("/api/music/together", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keyword: kw }),
    })).json();
    if (!d.ok) {
      setHint("一起听失败：" + (d.reason || "未知"));
      return;
    }
    inputEl.value = "";
    addMsg("other", d.text || "", { id: d.message_id || undefined });
    if (d.audio) playAudio(d.audio, messagesEl.lastElementChild);
    renderChatList();
    setHint(`一起听了《${d.song.title}》 · 回「讲讲」我就展开说`);
  } catch (e) {
    setHint("一起听失败：" + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old; }
  }
}

$("#btn-together")?.addEventListener("click", togetherInChat);

/* ---------------- 启动 ---------------- */

(async function boot() {
  buildFolds();                // 设置 / 音乐的小节先折起来（用户要求"点一下才展开"）
  await refreshAppearance();
  await refreshStatus();
  await refreshNotes();
  await refreshMemories();
  await refreshVoices();
  await refreshRvc();
  await refreshStickers();     // 必须在渲染历史消息之前，否则 [表情:x] 显示不出来
  await refreshStudyToggle();  // 顶部的「自学」开关要反映真实状态
  await loadHistory();
  await refreshSaves();        // 存档列表
  await refreshDevices();      // 设备白名单
  await refreshConnectInfo();  // 机器人接入信息
  await refreshMeter();        // 余额（一行小字）
  renderList();
  subscribeEvents();
  // 支持 #settings / #notes 这类深链；带后缀（如 #settings-budget）时再滚到对应小节
  const h = location.hash.slice(1);
  if (h) {
    const target = h.split("-")[0];
    if (target !== "chat") switchView(target);
    const anchor = h.includes("-") ? $(`#sec-${h.split("-").slice(1).join("-")}`) : null;
    if (anchor) {
      openFoldFor(anchor);       // 先展开再滚，否则滚到的是一个收起的标题
      scrollToSection(anchor);
    }
  }
  reportBoot();
  inputEl.focus({ preventScroll: true });
})();
