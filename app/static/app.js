/* Discord Parser — клиент панели */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  me: null,
  connected: false,
  guildId: localStorage.getItem("dp.guild") || "",
  guilds: [],
  channels: [],
  scan: null,
  ws: null,
  wsRetry: 0,
  view: "overview",
  selected: new Set(),
  messages: { offset: 0, limit: 50, total: 0 },
  authors: { offset: 0, limit: 50, total: 0 },
};

/* ------------------------------------------------------------------ утилиты */
function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

const nf = new Intl.NumberFormat("ru-RU");

function fmtNum(n) {
  return nf.format(Number(n || 0));
}

function fmtDate(ms, withTime = true) {
  if (!ms) return "—";
  const d = new Date(Number(ms));
  if (Number.isNaN(d.getTime())) return "—";
  const opts = withTime
    ? { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }
    : { day: "2-digit", month: "short", year: "numeric" };
  return d.toLocaleString("ru-RU", opts);
}

function fmtAgo(ms) {
  if (!ms) return "—";
  const diff = Date.now() - Number(ms);
  const min = Math.floor(diff / 60000);
  if (min < 1) return "только что";
  if (min < 60) return `${min} мин назад`;
  const hours = Math.floor(min / 60);
  if (hours < 24) return `${hours} ч назад`;
  const days = Math.floor(hours / 24);
  if (days < 31) return `${days} дн назад`;
  return fmtDate(ms, false);
}

function avatarUrl(user, size = 64) {
  if (!user || !user.avatar) return null;
  const ext = String(user.avatar).startsWith("a_") ? "gif" : "png";
  return `https://cdn.discordapp.com/avatars/${user.id}/${user.avatar}.${ext}?size=${size}`;
}

function initials(name) {
  return String(name || "?").trim().slice(0, 2).toUpperCase();
}

function avatarHtml(user, name, cls = "avatar-sm") {
  const url = avatarUrl(user);
  const inner = url
    ? `<img src="${esc(url)}" alt="" loading="lazy" onerror="this.remove()">`
    : esc(initials(name));
  return `<span class="${cls}">${inner}</span>`;
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 300);
  }, kind === "error" ? 7000 : 4000);
}

function setStatus(text) {
  $("#status-line").textContent = text;
}

async function api(path, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (typeof data.detail === "string") detail = data.detail;
      else if (Array.isArray(data.detail)) detail = data.detail.map((d) => d.msg).join(", ");
    } catch (_) {
      /* ignore */
    }
    const error = new Error(detail);
    error.status = res.status;
    throw error;
  }
  if (res.status === 204) return null;
  return res.json();
}

/* ------------------------------------------------------------------ сессия */
async function init() {
  bindEvents();
  connectWs();
  try {
    const session = await api("/api/session");
    state.me = session.me;
    state.connected = session.connected;
    renderAccount();
    await loadGuilds(false);
    if (!state.connected && !state.guilds.length) {
      openConnect();
    } else if (state.guildId) {
      await selectGuild(state.guildId);
    }
    if (!state.connected) {
      // без токена панель работает с уже собранными данными из локальной базы
      setStatus("Нет подключения к Discord — показаны данные из локальной базы");
    }
  } catch (err) {
    toast(`Не удалось загрузить состояние: ${err.message}`, "error");
  }
  updateExports();
}

function renderAccount() {
  const name = state.me
    ? state.me.global_name || state.me.username
    : "Не подключено";
  $("#account-name").textContent = name;
  $("#account-mode").textContent = state.me
    ? state.me.bot ? "бот-аккаунт" : "пользовательский токен"
    : "нажмите 🛰️";
  const av = $("#account-avatar");
  const url = avatarUrl(state.me, 64);
  av.innerHTML = url
    ? `<img src="${esc(url)}" alt="" onerror="this.remove()">`
    : esc(initials(name));
  $("#logout").disabled = !state.connected;
}

function openConnect() {
  $("#connect-modal").hidden = false;
  setTimeout(() => $("#token-input").focus(), 60);
}

async function submitToken() {
  const token = $("#token-input").value.trim();
  const remember = $("#token-remember").checked;
  const errEl = $("#token-error");
  errEl.textContent = "";
  if (token.length < 5) {
    errEl.textContent = "Введите токен.";
    return;
  }
  const btn = $("#token-submit");
  btn.disabled = true;
  btn.textContent = "Подключаюсь…";
  try {
    const data = await api("/api/token", { method: "POST", body: { token, remember } });
    state.connected = true;
    state.me = data.me;
    renderAccount();
    $("#token-input").value = "";
    $("#connect-modal").hidden = true;
    toast(`Подключено: ${data.me.global_name || data.me.username}`, "ok");
    await loadGuilds(true);
    if (state.guilds.length) {
      await selectGuild(state.guilds[0].id);
    } else {
      toast("У аккаунта нет доступных серверов", "error");
    }
  } catch (err) {
    errEl.textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Подключиться";
  }
}

async function logout() {
  try {
    await api("/api/token", { method: "DELETE" });
  } catch (_) {
    /* ignore */
  }
  state.connected = false;
  state.me = null;
  renderAccount();
  openConnect();
}

/* ------------------------------------------------------------------ серверы */
async function loadGuilds(refresh = false) {
  const data = await api(`/api/guilds${refresh ? "?refresh=true" : ""}`);
  state.guilds = data.guilds || [];
  const select = $("#guild-select");
  if (!state.guilds.length) {
    select.innerHTML = `<option value="">— нет данных —</option>`;
    return;
  }
  select.innerHTML = state.guilds
    .map(
      (g) =>
        `<option value="${esc(g.id)}">${esc(g.name || g.id)} · ${fmtNum(g.message_count)} сообщ.</option>`
    )
    .join("");
  if (!state.guildId || !state.guilds.some((g) => g.id === state.guildId)) {
    state.guildId = state.guilds[0].id;
  }
  select.value = state.guildId;
}

async function selectGuild(guildId) {
  state.guildId = guildId;
  localStorage.setItem("dp.guild", guildId);
  $("#guild-select").value = guildId;
  await Promise.all([loadChannels(), loadOverview()]);
  if (state.view === "messages") await loadMessages(true);
  if (state.view === "authors") await loadAuthors(true);
  updateExports();
}

async function syncGuild() {
  if (!state.guildId) return toast("Сначала выберите сервер", "error");
  const btn = $("#sync-guild");
  btn.disabled = true;
  btn.textContent = "⟳ …";
  try {
    const data = await api(`/api/guilds/${state.guildId}/sync`, { method: "POST" });
    state.channels = data.channels || [];
    renderChannels();
    await loadGuilds(false);
    toast(`Каналы обновлены: ${state.channels.length}`, "ok");
  } catch (err) {
    toast(`Синхронизация: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "⟳ Синхр.";
  }
}

/* ------------------------------------------------------------------ каналы */
async function loadChannels() {
  if (!state.guildId) {
    state.channels = [];
    renderChannels();
    return;
  }
  try {
    const data = await api(`/api/guilds/${state.guildId}/channels`);
    state.channels = data.channels || [];
  } catch (err) {
    state.channels = [];
    toast(`Каналы: ${err.message}`, "error");
  }
  renderChannels();
}

const CHANNEL_TYPES = {
  0: "текст",
  2: "голос",
  4: "категория",
  5: "анонсы",
  10: "ветка",
  11: "ветка",
  12: "ветка",
  13: "stage",
  15: "форум",
};

function isTextChannel(ch) {
  return [0, 5, 10, 11, 12, 15].includes(Number(ch.type));
}

function renderChannels() {
  const list = $("#channel-list");
  const textChannels = state.channels.filter(isTextChannel);
  $("#channel-count").textContent = `${textChannels.length}`;

  if (!state.channels.length) {
    list.innerHTML = `<p class="muted small">Нет каналов. Нажмите «Синхр.» в шапке.</p>`;
    updateChannelSelects();
    return;
  }

  list.innerHTML = state.channels
    .map((ch) => {
      const text = isTextChannel(ch);
      const checked = state.selected.size ? state.selected.has(ch.id) : text && !ch.is_thread;
      const status = ch.complete ? "done" : ch.scan_status === "error" ? "error" : "";
      const meta = ch.complete
        ? "✓"
        : ch.message_count
          ? fmtNum(ch.message_count)
          : CHANNEL_TYPES[ch.type] || ch.type;
      return `<label class="channel-item ${status} ${ch.is_thread ? "thread" : ""}" title="${esc(ch.topic || "")}">
        <input type="checkbox" data-channel="${esc(ch.id)}" ${checked && text ? "checked" : ""} ${text ? "" : "disabled"}>
        <span>${ch.is_thread ? "↳ " : ""}#${esc(ch.name || ch.id)}</span>
        <span class="meta">${esc(meta)}</span>
      </label>`;
    })
    .join("");

  $$("#channel-list input[data-channel]").forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) state.selected.add(input.dataset.channel);
      else state.selected.delete(input.dataset.channel);
    });
  });

  updateChannelSelects();
}

function updateChannelSelects() {
  const select = $("#msg-channel");
  const current = select.value;
  select.innerHTML =
    `<option value="">Все каналы</option>` +
    state.channels
      .filter((ch) => ch.message_count > 0)
      .map(
        (ch) =>
          `<option value="${esc(ch.id)}">#${esc(ch.name || ch.id)} (${fmtNum(ch.message_count)})</option>`
      )
      .join("");
  if (state.channels.some((ch) => ch.id === current)) select.value = current;

  $("#export-msg-csv").href = exportUrl("messages", "csv");
  $("#export-msg-json").href = exportUrl("messages", "json");
  $("#export-auth-csv").href = exportUrl("authors", "csv");
  $("#export-auth-json").href = exportUrl("authors", "json");
}

function selectedChannelIds() {
  const boxes = $$("#channel-list input[data-channel]:checked");
  return boxes.map((b) => b.dataset.channel);
}

function setChannelSelection(mode) {
  $$("#channel-list input[data-channel]").forEach((input) => {
    const ch = state.channels.find((c) => c.id === input.dataset.channel);
    if (!ch || !isTextChannel(ch)) {
      input.checked = false;
      return;
    }
    if (mode === "all") input.checked = true;
    else if (mode === "none") input.checked = false;
    else if (mode === "text") input.checked = !ch.is_thread;
  });
  state.selected.clear();
  selectedChannelIds().forEach((id) => state.selected.add(id));
}

/* ------------------------------------------------------------------ сканирование */
async function startScan() {
  if (!state.guildId) return toast("Сначала выберите сервер", "error");
  if (!state.connected) return openConnect();

  const channelIds = selectedChannelIds();
  if (!channelIds.length) return toast("Выберите хотя бы один канал", "error");
  const mode = $("#scan-mode").value;
  const isFull = mode !== "auto";
  if (isFull && !confirm("Полное сканирование может занять время. Продолжить?")) return;

  try {
    const data = await api("/api/scan", {
      method: "POST",
      body: {
        guild_id: state.guildId,
        channel_ids: channelIds,
        mode,
        include_threads: $("#scan-threads").checked,
        fetch_members: $("#scan-members").checked,
      },
    });
    handleScan(data);
    toast("Сканирование запущено", "ok");
  } catch (err) {
    toast(`Запуск: ${err.message}`, "error");
  }
}

async function stopScan() {
  try {
    handleScan(await api("/api/scan/stop", { method: "POST" }));
  } catch (err) {
    toast(`Стоп: ${err.message}`, "error");
  }
}

function handleScan(scan) {
  if (!scan) return;
  const prev = state.scan;
  state.scan = scan;

  const running = scan.status === "running";
  const pill = $("#scan-pill");
  pill.textContent = {
    running: "идёт",
    done: "готово",
    stopped: "остановлено",
    error: "ошибка",
    idle: "готово",
  }[scan.status] || scan.status;
  pill.className = `pill ${scan.status === "running" ? "running" : scan.status === "error" ? "error" : scan.status === "done" ? "done" : ""}`;

  $("#scan-start").disabled = running;
  $("#scan-stop").disabled = !running;
  $("#progress-wrap").hidden = scan.status === "idle" || scan.status === undefined;

  const total = scan.channels_total || 0;
  const done = scan.channels_done || 0;
  const percent = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  $("#progress-bar").style.width = `${percent}%`;
  $("#progress-percent").textContent = `${percent}%`;
  $("#progress-text").textContent = scan.message || (running ? "Собираю историю…" : "Готово");
  $("#ps-fetched").textContent = fmtNum(scan.messages_fetched || 0);
  $("#ps-new").textContent = fmtNum(scan.messages_new || 0);
  $("#ps-channels").textContent = `${done}/${total}`;

  const errors = (scan.errors || []).slice(-6).reverse();
  $("#scan-errors").innerHTML = errors
    .map((e) => `<li>${esc(e.channel || "сервер")}: ${esc(e.error)}</li>`)
    .join("");

  if (running) setStatus(`Сканирование: ${scan.message || ""}`);

  const finishedNow =
    prev && prev.status === "running" && ["done", "stopped", "error"].includes(scan.status);
  if (finishedNow) {
    toast(
      scan.status === "done"
        ? `Готово: собрано ${fmtNum(scan.messages_fetched)} сообщений, новых ${fmtNum(scan.messages_new)}`
        : `Сканирование ${scan.status === "stopped" ? "остановлено" : "завершилось с ошибкой"}`,
      scan.status === "done" ? "ok" : "error"
    );
    state.selected.clear();
    loadChannels();
    loadGuilds(false);
    loadOverview();
    if (state.view === "messages") loadMessages(true);
    if (state.view === "authors") loadAuthors(true);
    setStatus("Готов");
  }

  if (running) {
    // прогресс по каналам в сайдбаре
    $$("#channel-list input[data-channel]").forEach((input) => {
      /* статусы обновим после завершения */
    });
  }
}

async function refreshScanStatus() {
  try {
    const data = await api("/api/scan/status");
    handleScan(data.scanner);
    renderScanTable(data.database.channels || []);
  } catch (_) {
    /* ignore */
  }
}

function renderScanTable(rows) {
  const tbody = $("#scan-table tbody");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty">Пока нет данных. Запустите сканирование.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows
    .slice(0, 100)
    .map((r) => {
      const cls = r.complete ? "done" : r.status || "pending";
      const label = r.complete ? "готово" : r.status || "ожидает";
      return `<tr>
        <td>#${esc(r.name || r.channel_id)}</td>
        <td><span class="tag ${esc(cls)}">${esc(label)}</span></td>
        <td class="num">${fmtNum(r.fetched)}</td>
        <td class="nowrap muted">${r.last_run_at ? fmtAgo(r.last_run_at * 1000) : "—"}</td>
      </tr>`;
    })
    .join("");
}

/* ------------------------------------------------------------------ обзор */
async function loadOverview() {
  if (!state.guildId) return;
  try {
    const [stats, scan] = await Promise.all([
      api(`/api/stats?guild_id=${encodeURIComponent(state.guildId)}`),
      api("/api/scan/status"),
    ]);
    renderOverview(stats);
    handleScan(scan.scanner);
    renderScanTable(stats.scan_channels || []);
  } catch (err) {
    toast(`Статистика: ${err.message}`, "error");
  }
}

function renderOverview(stats) {
  $("#kpi-messages").textContent = fmtNum(stats.messages);
  $("#kpi-authors").textContent = fmtNum(stats.authors);
  $("#kpi-channels").textContent = fmtNum(stats.channels);
  $("#kpi-attachments").textContent = fmtNum(stats.attachments);
  $("#kpi-text").textContent = `текстовых: ${fmtNum(stats.text_messages)}`;
  $("#kpi-bots").textContent = `сообщений от ботов: ${fmtNum(stats.bot_messages)}`;
  $("#kpi-scan").textContent = `каналов просканировано ${stats.scan.complete}/${stats.scan.total}`;

  $("#kpi-range").textContent =
    stats.first_message_at && stats.last_message_at
      ? `${fmtDate(stats.first_message_at, false)} → ${fmtDate(stats.last_message_at, false)}`
      : "нет сообщений";
  $("#chart-range").textContent = stats.per_day.length
    ? `${stats.per_day.length} дн.`
    : "";

  renderBars($("#chart-days"), stats.per_day, (d) => d.day, (d) => d.count, "сообщ.");
  renderBars(
    $("#chart-hours"),
    Array.from({ length: 24 }, (_, h) => {
      const found = stats.per_hour.find((x) => x.hour === h);
      return { hour: h, count: found ? found.count : 0 };
    }),
    (d) => `${String(d.hour).padStart(2, "0")}:00`,
    (d) => d.count,
    "сообщ."
  );

  renderRank(
    $("#top-authors"),
    stats.top_authors.map((a) => ({
      id: a.id,
      name: a.global_name || a.username || a.id,
      value: a.count,
      bot: a.bot,
      author: a,
    })),
    (item) => openAuthor(item.id)
  );

  renderRank(
    $("#top-channels"),
    stats.per_channel.map((c) => ({ id: c.channel_id, name: `#${c.name}`, value: c, bot: false })),
    null,
    (v) => fmtNum(v.c)
  );
}

function renderBars(container, rows, labelFn, valueFn, unit) {
  if (!rows.length) {
    container.innerHTML = `<div class="chart-empty">Нет данных</div>`;
    return;
  }
  const max = Math.max(...rows.map(valueFn), 1);
  const dense = rows.length > 90;
  container.innerHTML = rows
    .map((row) => {
      const value = valueFn(row);
      const height = Math.max(2, (value / max) * 100);
      const label = `${labelFn(row)} · ${fmtNum(value)} ${unit}`;
      const title = dense ? "" : `<span class="sr">${esc(label)}</span>`;
      return `<div class="bar" style="height:${height}%" data-label="${esc(label)}" title="${esc(label)}"></div>`;
    })
    .join("");
}

function renderRank(container, items, onClick, valueFormatter) {
  if (!items.length) {
    container.innerHTML = `<p class="muted small">Нет данных</p>`;
    return;
  }
  const max = Math.max(...items.map((i) => (typeof i.value === "number" ? i.value : i.value.c)), 1);
  container.innerHTML = items
    .map((item, index) => {
      const value = typeof item.value === "number" ? item.value : item.value.c;
      const width = Math.max(4, (value / max) * 100);
      const display = valueFormatter ? valueFormatter(item.value) : fmtNum(value);
      return `<div class="rank-row ${item.bot ? "bot" : ""}" data-id="${esc(item.id)}">
        <span class="idx">${index + 1}</span>
        <span class="name">${esc(item.name)}</span>
        <span class="value">${esc(display)}</span>
        <span class="barline" style="width:${width}%"></span>
      </div>`;
    })
    .join("");

  if (onClick) {
    $$(".rank-row", container).forEach((row) =>
      row.addEventListener("click", () => onClick({ id: row.dataset.id }))
    );
  }
}

/* ------------------------------------------------------------------ сообщения */
async function loadMessages(reset = false) {
  if (!state.guildId) return;
  if (reset) state.messages.offset = 0;
  const params = new URLSearchParams({
    guild_id: state.guildId,
    limit: state.messages.limit,
    offset: state.messages.offset,
    sort: $("#msg-sort").value,
  });
  const q = $("#msg-search").value.trim();
  if (q) params.set("q", q);
  const channel = $("#msg-channel").value;
  if (channel) params.set("channel_id", channel);

  const tbody = $("#msg-table tbody");
  tbody.innerHTML = `<tr><td colspan="4" class="empty">Загрузка…</td></tr>`;
  try {
    const data = await api(`/api/messages?${params}`);
    state.messages.total = data.total;
    if (!data.items.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty">Сообщений не найдено. Запустите сканирование.</td></tr>`;
    } else {
      tbody.innerHTML = data.items
        .map((m) => {
          const name = m.author_name || m.username || m.author_id;
          const preview = (m.content || "").replace(/\s+/g, " ").slice(0, 260) || "(без текста)";
          const extras = [];
          if (m.attachments && m.attachments.length) extras.push(`📎${m.attachments.length}`);
          if (m.reactions && m.reactions.length) extras.push(`😀${m.reactions.length}`);
          if (m.pinned) extras.push("📌");
          return `<tr class="clickable" data-message="${esc(m.id)}">
            <td class="nowrap muted">${fmtDate(m.timestamp)}</td>
            <td class="nowrap">
              <div class="author-cell">
                ${avatarHtml({ id: m.author_id, avatar: m.avatar }, name)}
                <span>${esc(name)}</span>
                ${m.bot ? '<span class="tag">BOT</span>' : ""}
              </div>
            </td>
            <td class="nowrap muted">#${esc(m.channel_name)}</td>
            <td><span class="content-cell">${esc(preview)}</span> ${extras.join(" ")}</td>
          </tr>`;
        })
        .join("");
      $$("#msg-table tbody tr[data-message]").forEach((tr) =>
        tr.addEventListener("click", () => openMessage(tr.dataset.message))
      );
    }
    const page = Math.floor(state.messages.offset / state.messages.limit) + 1;
    const pages = Math.max(1, Math.ceil(data.total / state.messages.limit));
    $("#msg-page").textContent = `${page} / ${pages}`;
    $("#msg-total").textContent = `${fmtNum(data.total)} сообщений`;
    $("#msg-prev").disabled = state.messages.offset <= 0;
    $("#msg-next").disabled = page >= pages;
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty">Ошибка: ${esc(err.message)}</td></tr>`;
  }
}

async function openMessage(id) {
  try {
    const m = await api(`/api/messages/${id}`);
    const name = m.author_name || m.username || m.author_id;
    const attachments = (m.attachments || [])
      .map(
        (a) =>
          `<div class="attachment">📎 <a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.filename || a.url)}</a> <span class="muted small">${fmtNum(a.size || 0)} байт</span></div>`
      )
      .join("");
    const reactions = (m.reactions || [])
      .map((r) => `<span class="reaction">${esc(r.emoji?.name || "?")} ${fmtNum(r.count)}</span>`)
      .join("");
    const embeds = (m.embeds || []).length
      ? `<p class="muted small">Вложенных embed-блоков: ${m.embeds.length}</p>`
      : "";

    showDetail(`
      <div class="detail-head">
        ${avatarHtml({ id: m.author_id, avatar: m.avatar }, name, "avatar-lg")}
        <div>
          <h3>${esc(name)}</h3>
          <span class="muted small">#${esc(m.channel_name)} · ${fmtDate(m.timestamp)}${
            m.edited_timestamp ? " · изменено" : ""
          }</span>
        </div>
      </div>
      <div class="detail-list" style="margin-top:16px">
        <div class="msg"><p>${esc(m.content || "(без текста)")}</p></div>
      </div>
      ${attachments ? `<p class="section-title">Вложения</p>${attachments}` : ""}
      ${reactions ? `<div class="reactions">${reactions}</div>` : ""}
      ${embeds}
      <p class="section-title">Идентификаторы</p>
      <p class="muted small">message: <span class="code">${esc(m.id)}</span><br>
      author: <a href="#" data-author="${esc(m.author_id)}" class="code">${esc(m.author_id)}</a></p>
    `);
    $("#detail-body")
      .querySelector("[data-author]")
      ?.addEventListener("click", (e) => {
        e.preventDefault();
        openAuthor(e.target.dataset.author);
      });
  } catch (err) {
    toast(`Сообщение: ${err.message}`, "error");
  }
}

/* ------------------------------------------------------------------ авторы */
async function loadAuthors(reset = false) {
  if (!state.guildId) return;
  if (reset) state.authors.offset = 0;
  const params = new URLSearchParams({
    guild_id: state.guildId,
    limit: state.authors.limit,
    offset: state.authors.offset,
    sort: $("#author-sort").value,
  });
  const q = $("#author-search").value.trim();
  if (q) params.set("q", q);

  const tbody = $("#author-table tbody");
  tbody.innerHTML = `<tr><td colspan="5" class="empty">Загрузка…</td></tr>`;
  try {
    const data = await api(`/api/authors?${params}`);
    state.authors.total = data.total;
    if (!data.items.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty">Авторов не найдено.</td></tr>`;
    } else {
      tbody.innerHTML = data.items
        .map(
          (a) => `<tr class="clickable" data-author="${esc(a.id)}">
            <td>
              <div class="author-cell">
                ${avatarHtml(a, a.display_name)}
                <span>${esc(a.display_name)}</span>
                ${a.bot ? '<span class="tag">BOT</span>' : ""}
              </div>
            </td>
            <td class="num">${fmtNum(a.message_count)}</td>
            <td class="nowrap muted">${fmtDate(a.first_message_at, false)}</td>
            <td class="nowrap muted">${fmtAgo(a.last_message_at)}</td>
            <td class="nowrap muted">${fmtDate(a.account_created_at, false)}</td>
          </tr>`
        )
        .join("");
      $$("#author-table tbody tr[data-author]").forEach((tr) =>
        tr.addEventListener("click", () => openAuthor(tr.dataset.author))
      );
    }
    const page = Math.floor(state.authors.offset / state.authors.limit) + 1;
    const pages = Math.max(1, Math.ceil(data.total / state.authors.limit));
    $("#author-page").textContent = `${page} / ${pages}`;
    $("#author-total").textContent = `${fmtNum(data.total)} авторов`;
    $("#author-prev").disabled = state.authors.offset <= 0;
    $("#author-next").disabled = page >= pages;
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty">Ошибка: ${esc(err.message)}</td></tr>`;
  }
}

async function openAuthor(id) {
  try {
    const a = await api(`/api/authors/${encodeURIComponent(id)}?guild_id=${encodeURIComponent(state.guildId)}`);
    const name = a.global_name || a.username || a.id;
    const channels = (a.channels || [])
      .map(
        (c) =>
          `<div class="rank-row"><span class="idx"></span><span class="name">#${esc(c.name)}</span><span class="value">${fmtNum(c.count)}</span></div>`
      )
      .join("");
    const recent = (a.recent || [])
      .map(
        (m) => `<div class="msg"><time>${fmtDate(m.timestamp)}</time><p>${esc(
          (m.content || "(без текста)").slice(0, 400)
        )}</p></div>`
      )
      .join("");

    showDetail(`
      <div class="detail-head">
        ${avatarHtml(a, name, "avatar-lg")}
        <div>
          <h3>${esc(name)} ${a.bot ? '<span class="tag">BOT</span>' : ""}</h3>
          <span class="muted small">@${esc(a.username || "")}#${esc(a.discriminator || "0")} · ID ${esc(a.id)}</span>
        </div>
      </div>
      <div class="detail-grid">
        <div class="box"><span>Сообщений</span><b>${fmtNum(a.message_count)}</b></div>
        <div class="box"><span>Каналов</span><b>${fmtNum((a.channels || []).length)}</b></div>
        <div class="box"><span>Первое сообщение</span><b class="small">${fmtDate(a.first_message_at, false)}</b></div>
        <div class="box"><span>Последнее</span><b class="small">${fmtAgo(a.last_message_at)}</b></div>
        <div class="box"><span>Аккаунт создан</span><b class="small">${fmtDate(a.account_created_at, false)}</b></div>
        <div class="box"><span>На сервере с</span><b class="small">${a.joined_iso ? fmtDate(a.joined_at, false) : "—"}</b></div>
      </div>
      <p class="section-title">Куда пишет</p>
      <div class="rank-list">${channels || '<p class="muted small">Нет данных</p>'}</div>
      <p class="section-title">Последние сообщения</p>
      <div class="detail-list">${recent || '<p class="muted small">Нет данных</p>'}</div>
    `);
  } catch (err) {
    toast(`Автор: ${err.message}`, "error");
  }
}

function showDetail(html) {
  $("#detail-body").innerHTML = html;
  $("#detail-modal").hidden = false;
}

/* ------------------------------------------------------------------ экспорт */
function exportUrl(kind, fmt) {
  const params = new URLSearchParams({ fmt });
  if (state.guildId) params.set("guild_id", state.guildId);
  return `/api/export/${kind}?${params}`;
}

function updateExports() {
  updateChannelSelects();
}

async function clearData() {
  if (!state.guildId) return;
  if (!confirm("Удалить все собранные сообщения и прогресс этого сервера?")) return;
  try {
    await api(`/api/guilds/${state.guildId}/data`, { method: "DELETE" });
    toast("Данные сервера удалены", "ok");
    state.selected.clear();
    await loadChannels();
    await loadOverview();
    await loadGuilds(false);
  } catch (err) {
    toast(`Очистка: ${err.message}`, "error");
  }
}

/* ------------------------------------------------------------------ websocket */
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;

  ws.addEventListener("open", () => {
    state.wsRetry = 0;
    $("#ws-dot").classList.add("on");
    setStatus("Онлайн-соединение установлено");
    const timer = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send("ping");
      else clearInterval(timer);
    }, 25000);
  });

  ws.addEventListener("message", (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (payload.type === "scan") handleScan(payload.data);
    if (payload.type === "hello" && payload.data?.scanner) handleScan(payload.data.scanner);
    if (payload.type === "stats_stale") {
      loadGuilds(false);
    }
  });

  ws.addEventListener("close", () => {
    $("#ws-dot").classList.remove("on");
    const delay = Math.min(15000, 1000 * 2 ** state.wsRetry++);
    setTimeout(connectWs, delay);
  });
}

/* ------------------------------------------------------------------ события */
let searchTimer = null;

function switchView(view) {
  state.view = view;
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  if (view === "messages") loadMessages(true);
  if (view === "authors") loadAuthors(true);
  if (view === "overview") loadOverview();
  if (view === "export") updateExports();
}

function bindEvents() {
  $$(".nav-item").forEach((btn) =>
    btn.addEventListener("click", () => switchView(btn.dataset.view))
  );

  $("#guild-select").addEventListener("change", (e) => {
    if (e.target.value) selectGuild(e.target.value);
  });
  $("#sync-guild").addEventListener("click", syncGuild);

  $("#token-submit").addEventListener("click", submitToken);
  $("#token-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitToken();
  });
  $("#logout").addEventListener("click", logout);
  $("#connect-close").addEventListener("click", () => {
    $("#connect-modal").hidden = true;
  });
  $("#connect-modal").addEventListener("click", (e) => {
    if (e.target.id === "connect-modal") $("#connect-modal").hidden = true;
  });
  $("#account").addEventListener("click", (e) => {
    if (!state.connected && e.target.id !== "logout") openConnect();
  });

  $("#scan-start").addEventListener("click", startScan);
  $("#scan-stop").addEventListener("click", stopScan);
  $("#ch-select-all").addEventListener("click", () => setChannelSelection("all"));
  $("#ch-select-none").addEventListener("click", () => setChannelSelection("none"));
  $("#ch-select-text").addEventListener("click", () => setChannelSelection("text"));

  $("#msg-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadMessages(true), 350);
  });
  $("#msg-channel").addEventListener("change", () => loadMessages(true));
  $("#msg-sort").addEventListener("change", () => loadMessages(true));
  $("#msg-reset").addEventListener("click", () => {
    $("#msg-search").value = "";
    $("#msg-channel").value = "";
    $("#msg-sort").value = "newest";
    loadMessages(true);
  });
  $("#msg-prev").addEventListener("click", () => {
    state.messages.offset = Math.max(0, state.messages.offset - state.messages.limit);
    loadMessages();
  });
  $("#msg-next").addEventListener("click", () => {
    state.messages.offset += state.messages.limit;
    loadMessages();
  });

  $("#author-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadAuthors(true), 350);
  });
  $("#author-sort").addEventListener("change", () => loadAuthors(true));
  $("#author-prev").addEventListener("click", () => {
    state.authors.offset = Math.max(0, state.authors.offset - state.authors.limit);
    loadAuthors();
  });
  $("#author-next").addEventListener("click", () => {
    state.authors.offset += state.authors.limit;
    loadAuthors();
  });

  $("#clear-data").addEventListener("click", clearData);

  $("#detail-close").addEventListener("click", () => {
    $("#detail-modal").hidden = true;
  });
  $$("#detail-modal").forEach((modal) =>
    modal.addEventListener("click", (e) => {
      if (e.target === modal) modal.hidden = true;
    })
  );
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $("#detail-modal").hidden = true;
    }
  });

  // если сервер не подключён — по клику на панель предлагаем подключиться
  $("#scan-start").addEventListener("click", (e) => {
    if (!state.connected) {
      e.stopImmediatePropagation();
      openConnect();
    }
  }, true);
}

document.addEventListener("DOMContentLoaded", init);
