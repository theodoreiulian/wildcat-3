"use strict";
const $ = id => document.getElementById(id);
let token = location.hash.slice(1) || sessionStorage.getItem("wildcat-session") || "";
if (location.hash) { sessionStorage.setItem("wildcat-session", token); history.replaceState(null, "", location.pathname); }
let current = null, pending = false, disconnected = true, selected = "TOTAL";

function workspaceView(view) {
  const activity = view === "activity";
  $("portfolio-view").hidden = activity;
  $("activity-view").hidden = !activity;
  $("view-portfolio").setAttribute("aria-pressed", String(!activity));
  $("view-activity").setAttribute("aria-pressed", String(activity));
  sessionStorage.setItem("wildcat-view", activity ? "activity" : "portfolio");
}

const STATE_CLASS = {HALTED:"down", UNCERTAIN:"down", DEGRADED:"down", COOLDOWN:"warn", "DAILY ENTRY LIMIT":"warn", PROTECTED:"up", SCANNING:"up"};
const LEVEL_CLASS = {CRITICAL:"down", ERROR:"down", WARNING:"warn", WARN:"warn", FILL:"up", ORDER:"up"};
const numeric = value => value === null || value === undefined || value === "" || !Number.isFinite(Number(value)) ? null : Number(value);
const amount = value => Math.abs(value).toLocaleString("en-IE", {minimumFractionDigits:2, maximumFractionDigits:2});
const money = value => { const n = numeric(value); return n === null ? "—" : `${n < 0 ? "-" : ""}€${amount(n)}`; };
const signedMoney = value => { const n = numeric(value); return n === null ? "—" : `${n > 0 ? "+" : n < 0 ? "-" : ""}€${amount(n)}`; };
const exact = value => value === null || value === undefined || value === "" ? "—" : String(value).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
const quote = value => {
  const n = numeric(value); if (n === null) return "—";
  const digits = Math.abs(n) >= 100 ? 2 : Math.abs(n) >= 1 ? 4 : 6;
  return `€${Math.abs(n).toLocaleString("en-IE", {minimumFractionDigits:digits, maximumFractionDigits:digits})}`;
};
const percentage = value => { const n = numeric(value); return n === null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(2)}%`; };
const signClass = value => numeric(value) > 0 ? "up" : numeric(value) < 0 ? "down" : "";
const localTime = stamp => new Date(stamp * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
const shortDate = stamp => new Date(stamp * 1000).toLocaleDateString([], {month:"short", day:"numeric"});

function duration(seconds) {
  if (seconds == null) return "—";
  const value = Math.max(0, Math.round(seconds));
  if (value >= 3600) return `${Math.floor(value / 3600)}h ${String(Math.floor(value % 3600 / 60)).padStart(2, "0")}m`;
  if (value >= 60) return `${Math.floor(value / 60)}m ${String(value % 60).padStart(2, "0")}s`;
  return `${value}s`;
}
function put(id, text, cls = "") {
  const el = $(id); el.textContent = text; el.classList.remove("up", "down", "warn", "off", "idle");
  if (cls) el.classList.add(cls);
}
function engineState(label, cls) {
  const el = $("result"), lamp = document.createElement("i"), text = document.createElement("span");
  lamp.setAttribute("aria-hidden", "true"); text.textContent = label;
  el.className = `engine-state ${cls}`; el.replaceChildren(lamp, text);
}
function notice(message, error = false) {
  $("feedback").hidden = false; $("feedback").textContent = message; $("feedback").classList.toggle("error", error);
}
async function api(path, body) {
  const response = await fetch(path, {method:body ? "POST" : "GET", headers:{"X-Wildcat-Token":token,
    ...(body ? {"Content-Type":"application/json"} : {})}, ...(body ? {body:JSON.stringify(body)} : {}), cache:"no-store"});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed.");
  return result;
}

function controls() {
  const locked = pending || disconnected || !!current?.busy;
  document.querySelectorAll("form input, form button").forEach(el => { el.disabled = locked; });
  $("mode").disabled = locked; $("confirm-live").disabled = locked;
  const live = $("mode").value === "live";
  $("live-confirmation").hidden = !live;
  $("start").textContent = live ? "Start live" : "Start paper";
  $("start").disabled = locked || (live && (!current?.live_ready || !$("confirm-live").checked));
  $("check-account").disabled = locked || !current?.configured.KRAKEN_API_KEY;
  $("remove-kraken").disabled = locked || !current?.configured.KRAKEN_API_KEY;
  $("remove-telegram").disabled = locked || !current?.configured.TELEGRAM_BOT_TOKEN;
  $("doctor").disabled = locked;
  $("stop").disabled = pending || disconnected || !current?.busy || current?.stopping;
  $("stop").textContent = current?.stopping ? "Stopping…" : "Stop safely";
}

function portfolioItems(view) {
  const cash = numeric(view.cash ?? view.available);
  const items = [];
  if (cash !== null) items.push({symbol:"EUR", name:"Euro", quantity:String(cash), price:"1", value:String(cash), change_24h:null});
  return items.concat(view.positions || []);
}
function selectAsset(symbol) {
  selected = symbol;
  renderPortfolio(current.view);
  renderChart(current.view);
}
function renderPortfolio(view) {
  const body = $("portfolio-rows"), items = portfolioItems(view);
  body.replaceChildren();
  put("pos-count", `${items.length} asset${items.length === 1 ? "" : "s"}`);
  if (!items.length) {
    const row = document.createElement("tr"), cell = document.createElement("td");
    cell.colSpan = 5; cell.className = "empty-row"; cell.textContent = "Start the engine to load your portfolio."; row.append(cell); body.append(row); return;
  }
  if (selected !== "TOTAL" && !items.some(item => item.symbol === selected)) selected = "TOTAL";
  for (const item of items) {
    const row = document.createElement("tr"), cell = document.createElement("td"), button = document.createElement("button");
    cell.colSpan = 5; button.type = "button"; button.className = "asset-button" + (selected === item.symbol ? " selected" : "");
    button.setAttribute("aria-pressed", String(selected === item.symbol));
    const code = item.symbol.split("/")[0], pnl = item.change_24h;
    const specs = [["asset", `<i>${code.slice(0, 3)}</i><span>${code}<small>${item.symbol === "EUR" ? "CASH" : item.symbol}</small></span>`],
      ["", quote(item.price)], [signClass(pnl), percentage(pnl)], ["", exact(item.quantity)], ["", money(item.value)]];
    for (const [cls, value] of specs) {
      const span = document.createElement("span"); span.className = cls;
      if (cls === "asset") {
        const icon = document.createElement("i"), words = document.createElement("span"), small = document.createElement("small");
        icon.textContent = code.slice(0, 3); words.textContent = code; small.textContent = item.symbol === "EUR" ? "CASH" : item.symbol; words.append(small); span.append(icon, words);
      } else span.textContent = value;
      button.append(span);
    }
    button.onclick = () => selectAsset(item.symbol); cell.append(button); row.append(cell); body.append(row);
  }
}

function drawSeries(series) {
  const clean = (series || []).map(point => [numeric(point[0]), numeric(point[1])]).filter(point => point[0] !== null && point[1] !== null);
  const empty = clean.length < 2; $("chart-empty").hidden = !empty; $("price-chart").hidden = empty;
  if (empty) { $("chart-line").setAttribute("d", ""); $("chart-area").setAttribute("d", ""); return null; }
  const times = clean.map(point => point[0]), values = clean.map(point => point[1]);
  const minTime = Math.min(...times), maxTime = Math.max(...times), low = Math.min(...values), high = Math.max(...values);
  const spread = high - low || Math.max(Math.abs(high) * .01, 1);
  const points = clean.map(([time, value]) => [((time - minTime) / (maxTime - minTime || 1)) * 900, 342 - ((value - low) / spread) * 324]);
  const path = points.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
  $("chart-line").setAttribute("d", path); $("chart-area").setAttribute("d", `${path} L900,360 L0,360 Z`);
  put("chart-start", shortDate(minTime)); put("chart-end", shortDate(maxTime)); put("chart-low", `LOW ${quote(low)}`); put("chart-high", `HIGH ${quote(high)}`);
  return {first:values[0], last:values.at(-1)};
}
function renderChart(view) {
  const positions = view.positions || [];
  let series, title, caption, currentValue, change;
  if (selected === "TOTAL") {
    series = view.equity_history || []; title = "Equity history"; caption = "TOTAL PORTFOLIO"; currentValue = view.equity;
  } else if (selected === "EUR") {
    const now = numeric(view.updated) || Date.now() / 1000; series = [[now - 86400, 1], [now, 1]];
    title = "EUR CASH"; caption = "BASE CURRENCY / CASH POSITION"; currentValue = 1; change = 0;
  } else {
    const position = positions.find(item => item.symbol === selected);
    series = (view.price_history || {})[selected] || []; title = `${selected.split("/")[0]} price history`; caption = `${selected} · 1H`; currentValue = position?.price; change = position?.change_24h;
  }
  put("chart-title", title); put("chart-caption", caption); put("chart-price", selected === "TOTAL" ? money(currentValue) : quote(currentValue));
  const range = drawSeries(series);
  if (change == null && range?.first) change = (range.last / range.first - 1) * 100;
  put("chart-change", percentage(change), signClass(change));
  $("price-chart").setAttribute("aria-label", `${title}, ${percentage(change)} over displayed period`);
}

function renderOrders(orders) {
  const list = $("orders"); list.replaceChildren(); put("ord-count", `${orders.length} order${orders.length === 1 ? "" : "s"}`);
  if (!orders.length) { const item = document.createElement("li"); item.className = "none"; item.textContent = "No working protection orders."; list.append(item); return; }
  for (const order of orders) {
    const item = document.createElement("li"), name = document.createElement("b"), detail = document.createElement("span"), state = document.createElement("em");
    name.textContent = order.symbol; detail.textContent = `${String(order.type).toUpperCase()} · ${exact(order.quantity)} · ${order.price}`;
    state.textContent = order.protected ? "VERIFIED" : "CHECK"; if (!order.protected) state.className = "warn";
    item.title = order.id || ""; item.append(name, detail, state); list.append(item);
  }
}
function renderEvents(events) {
  const list = $("events"); list.replaceChildren();
  if (!events.length) { const item = document.createElement("li"); item.className = "none"; item.textContent = "No activity yet."; list.append(item); return; }
  for (const event of events) {
    const item = document.createElement("li"), time = document.createElement("time"), level = document.createElement("b"), message = document.createElement("span");
    time.textContent = localTime(event.time); level.textContent = event.level; level.className = LEVEL_CLASS[event.level] || ""; message.textContent = event.message;
    item.append(time, level, message); list.append(item);
  }
}
function showConsole(text) {
  const pre = $("logs"), atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
  pre.textContent = text || "No operation output yet."; if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function render(state) {
  current = state; disconnected = false;
  if (state.busy && state.operation === "run") $("mode").value = state.mode;
  const view = state.view || {}, running = state.busy && state.operation === "run", status = String(view.status || (state.busy ? "STARTING" : "OFFLINE"));
  put("connection", running ? "MARKET DATA LIVE" : "BACKEND ONLINE"); $("connection").className = "connection" + (running ? " live" : "");
  put("mode-tag", state.mode.toUpperCase());
  put("equity", money(view.equity)); put("available", money(view.available)); put("unrealized", signedMoney(view.unrealized), signClass(view.unrealized)); put("realized", signedMoney(view.realized), signClass(view.realized));
  const age = view.updated ? Math.max(0, state.now - view.updated) : null;
  put("snapshot-label", age === null ? "NO SNAPSHOT" : `${duration(age)} AGO${!running || age > 60 ? " · STALE" : ""}`, age !== null && (!running || age > 60) ? "warn" : "");
  put("runtime-status", state.busy ? (state.stopping ? "STOPPING" : state.operation.toUpperCase()) : "STOPPED", state.busy ? "up" : "off");
  put("uptime", state.busy ? duration(view.uptime) : "—"); put("recon", view.last_reconciled ? `${duration(state.now - view.last_reconciled)} AGO` : "NEVER");
  put("dd-val", view.drawdown == null ? "—" : `${Number(view.drawdown).toFixed(2)}%`, Number(view.drawdown) >= 8 ? "down" : Number(view.drawdown) >= 6 ? "warn" : "");
  renderPortfolio(view); renderChart(view); renderOrders(view.orders || []); renderEvents(state.events || []);
  const failed = String(state.result).startsWith("Operation failed"), engineError = failed || STATE_CLASS[status] === "down";
  engineState(engineError ? "ERROR" : running ? "ACTIVE" : "STOPPED", engineError ? "down" : running ? "up" : "idle");
  showConsole((state.logs || []).join("\n"));
  put("kraken-saved", state.live_ready ? "CHECKED" : state.configured.KRAKEN_API_KEY ? "SAVED" : "NO KEYS", state.live_ready ? "up" : "");
  put("telegram-saved", state.configured.TELEGRAM_BOT_TOKEN ? "ON" : "OFF", state.configured.TELEGRAM_BOT_TOKEN ? "up" : "");
  put("setup-summary", `${state.configured.KRAKEN_API_KEY ? "KRAKEN SAVED" : "KRAKEN NOT SET"} · ${state.configured.TELEGRAM_BOT_TOKEN ? "ALERTS ON" : "ALERTS OFF"}`);
  $("api-key").placeholder = state.configured.KRAKEN_API_KEY ? "Saved · enter replacement" : "Kraken API key";
  $("api-secret").placeholder = state.configured.KRAKEN_API_SECRET ? "Saved · enter replacement" : "Kraken private key";
  controls();
}
async function refresh() {
  try { render(await api("/api/state")); }
  catch (error) { disconnected = true; $("connection").className = "connection bad"; $("connection").textContent = "BACKEND OFFLINE"; engineState("ERROR", "down"); notice(`${error.message} Keep the launcher open and reconnect before operating the engine.`, true); controls(); }
}
async function act(path, body, success) {
  if (pending) return; pending = true; controls();
  try { await api(path, body); if (success) notice(success); await refresh(); }
  catch (error) { notice(error.message, true); }
  finally { pending = false; controls(); }
}

$("kraken-form").addEventListener("submit", async event => {
  event.preventDefault(); const key = $("api-key").value.trim(), secret = $("api-secret").value.trim();
  if (!key || !secret) return notice("Enter both Kraken keys.", true);
  await act("/api/settings", {KRAKEN_API_KEY:key, KRAKEN_API_SECRET:secret}, "Kraken keys saved. Check the account before live mode.");
  $("api-key").value = ""; $("api-secret").value = ""; $("confirm-live").checked = false; controls();
});
$("telegram-form").addEventListener("submit", async event => {
  event.preventDefault(); const bot = $("telegram-token").value.trim(), chat = $("telegram-chat").value.trim();
  if (!bot || !chat) return notice("Enter both a bot token and chat ID.", true);
  await act("/api/settings", {TELEGRAM_BOT_TOKEN:bot, TELEGRAM_CHAT_ID:chat}, "Telegram alerts saved.");
  $("telegram-token").value = ""; $("telegram-chat").value = "";
});
$("remove-kraken").onclick = () => act("/api/settings", {KRAKEN_API_KEY:"", KRAKEN_API_SECRET:""}, "Kraken keys removed locally.");
$("remove-telegram").onclick = () => act("/api/settings", {TELEGRAM_BOT_TOKEN:"", TELEGRAM_CHAT_ID:""}, "Telegram alerts removed.");
$("find-chat").onclick = async () => {
  if (pending) return; pending = true; controls();
  try {
    const result = await api("/api/telegram-chats", {token:$("telegram-token").value.trim()});
    if (!result.chats.length) throw new Error("No private chat found. Send /start to your bot, then try again.");
    $("chat-choice").replaceChildren(...result.chats.map(chat => { const option = document.createElement("option"); option.value = chat.id; option.textContent = `${chat.name} · ${chat.id}`; return option; }));
    $("chat-picker").hidden = false; $("telegram-chat").value = result.chats[0].id; notice("Chat found. Save alerts to finish.");
  } catch (error) { notice(error.message, true); } finally { pending = false; controls(); }
};
$("chat-choice").onchange = () => { $("telegram-chat").value = $("chat-choice").value; };
$("check-account").onclick = () => act("/api/start", {action:"check"}, "Checking the dedicated account. No orders will be placed.");
$("doctor").onclick = () => act("/api/start", {action:"doctor"}, "Testing Kraken public market access.");
$("start").onclick = () => act("/api/start", {action:"run", mode:$("mode").value, confirmed:$("confirm-live").checked}, "Engine start requested.");
$("stop").onclick = () => act("/api/stop", {}, "Safe stop requested.");
$("mode").onchange = () => { $("confirm-live").checked = false; controls(); };
$("confirm-live").onchange = controls;

$("view-portfolio").onclick = () => workspaceView("portfolio");
$("view-activity").onclick = () => workspaceView("activity");

function settings(open) {
  $("settings-panel").hidden = !open;
  $("settings-button").setAttribute("aria-expanded", String(open));
  $("settings-button").setAttribute("aria-label", open ? "Close connections and alerts" : "Open connections and alerts");
  if (open) $("settings-close").focus(); else $("settings-button").focus();
}
$("settings-button").onclick = () => settings($("settings-panel").hidden);
$("settings-close").onclick = () => settings(false);
document.addEventListener("keydown", event => { if (event.key === "Escape" && !$("settings-panel").hidden) settings(false); });

function tick() { $("clock-time").textContent = `${new Date().toISOString().slice(11, 19)} UTC`; }
workspaceView(sessionStorage.getItem("wildcat-view") === "activity" ? "activity" : "portfolio");
tick(); setInterval(tick, 1000);
async function poll() { await refresh(); setTimeout(poll, 2500); }
poll();
