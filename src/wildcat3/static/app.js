"use strict";
const $ = id => document.getElementById(id);
let token = location.hash.slice(1) || sessionStorage.getItem("wildcat-session") || "";
if (location.hash) { sessionStorage.setItem("wildcat-session", token); history.replaceState(null, "", location.pathname); }
let current = null, pending = false, disconnected = true;
const money = value => value == null ? "—" : new Intl.NumberFormat("en-IE", {style:"currency",currency:"EUR"}).format(Number(value));
function notice(message, error=false) { $("feedback").hidden=false; $("feedback").textContent=message; $("feedback").classList.toggle("error",error); }
async function api(path, body) {
  const response = await fetch(path, {method:body ? "POST":"GET",headers:{"X-Wildcat-Token":token,...(body ? {"Content-Type":"application/json"}:{})},...(body ? {body:JSON.stringify(body)}:{}),cache:"no-store"});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed.");
  return result;
}
function controls() {
  const locked = pending || disconnected || !!current?.busy;
  document.querySelectorAll("form input, form button").forEach(el => el.disabled=locked);
  $("mode").disabled=locked;
  $("confirm-live").disabled=locked;
  const live = $("mode").value === "live";
  $("live-confirmation").hidden=!live;
  $("mode-help").textContent=live ? "Uses the EUR balance and protective orders on your Kraken account." : "Uses real market prices with simulated money. No Kraken keys needed.";
  $("start").textContent=live ? "Start live trading" : "Start paper trading";
  $("start").disabled=locked || (live && (!current?.live_ready || !$("confirm-live").checked));
  $("check-account").disabled=locked || !current?.configured.KRAKEN_API_KEY;
  $("remove-kraken").disabled=locked || !current?.configured.KRAKEN_API_KEY;
  $("remove-telegram").disabled=locked || !current?.configured.TELEGRAM_BOT_TOKEN;
  $("doctor").disabled=locked;
  $("stop").disabled=pending || disconnected || !current?.busy || current?.stopping;
  $("stop").textContent=current?.stopping ? "Stopping safely…" : "Stop bot safely";
}
function rows(id, items, empty) {
  const body=$(id); body.replaceChildren();
  for(const cells of items.length ? items : [[empty]]) {
    const row=document.createElement("tr");
    for(const text of cells) {const td=document.createElement("td"); td.textContent=String(text); if(cells.length===1)td.colSpan=3; row.append(td);}
    body.append(row);
  }
}
function list(id, items, empty) { $(id).replaceChildren(...(items.length ? items : [empty]).map(text=>{const li=document.createElement("li");li.textContent=text;return li;})); }
function render(state) {
  current=state; disconnected=false;
  if (state.busy && state.operation === "run") $("mode").value=state.mode;
  $("connection").textContent="Local connection";
  $("kraken-saved").textContent=state.live_ready ? "Account checked" : state.configured.KRAKEN_API_KEY ? "Keys saved · not checked" : "No keys saved";
  $("telegram-saved").textContent=state.configured.TELEGRAM_BOT_TOKEN ? "Telegram settings saved. Values remain hidden." : "Notifications are not configured.";
  $("api-key").placeholder=state.configured.KRAKEN_API_KEY ? "Saved · enter a new key to replace" : "Paste your Kraken API key";
  $("api-secret").placeholder=state.configured.KRAKEN_API_SECRET ? "Saved · enter a new private key to replace" : "Paste your Kraken private key";
  $("result").textContent=state.result;
  $("runtime-status").textContent=state.busy ? `${state.mode.toUpperCase()} · ${state.stopping ? "STOPPING" : state.operation.toUpperCase()}` : "Stopped";
  const view=state.view;
  $("equity").textContent=money(view.equity); $("available").textContent=money(view.available);
  $("pnl").textContent=`${money(view.realized)} / ${money(view.unrealized)}`;
  $("wins").textContent=view.wins == null ? "—" : `${view.wins} / ${view.losses}`;
  const age=view.updated ? Math.max(0,Math.round(state.now-view.updated)) : null;
  $("snapshot-label").textContent=`${state.mode.toUpperCase()} · ${age === null ? "No snapshot yet" : `Saved ${age}s ago${!state.busy || age>60 ? " · not live" : ""}`}`;
  $("health").textContent=view.health || "Start paper mode to see your first account snapshot.";
  rows("positions",(view.positions||[]).map(p=>[p.symbol,p.quantity,money(p.pnl)]),"No open positions");
  rows("orders",(view.orders||[]).map(o=>[o.symbol,`${o.side} · ${o.type}`,o.protected ? "Verified" : "Pending / unverified"]),"No working orders");
  list("reasons",Object.entries(view.reasons||{}).map(([pair,reason])=>`${pair} — ${reason}`),"Candidate decisions appear after a market scan.");
  list("events",state.events.map(e=>`${new Date(e.time*1000).toLocaleTimeString()} · ${e.level} · ${e.message}`),"No recorded events yet.");
  $("logs").textContent=state.logs.join("\n") || "No operation output yet.";
  if (state.result.startsWith("Operation failed")) $("operation-details").open=true;
  controls();
}
async function refresh() {
  try { render(await api("/api/state")); }
  catch(error) {disconnected=true; $("connection").textContent="Disconnected"; notice(`${error.message} Keep the launcher open; reconnect before operating the bot.`,true); controls();}
}
async function act(path, body, success) {
  if(pending)return; pending=true; controls();
  try {await api(path,body); if(success)notice(success); await refresh();}
  catch(error){notice(error.message,true);}
  finally{pending=false;controls();}
}
$("kraken-form").addEventListener("submit",async event=>{event.preventDefault();const key=$("api-key").value.trim(),secret=$("api-secret").value.trim();if(!key||!secret){notice("Enter both Kraken keys to save or replace them.",true);return;}await act("/api/settings",{KRAKEN_API_KEY:key,KRAKEN_API_SECRET:secret},"Kraken keys saved locally. Next, check your account.");$("api-key").value="";$("api-secret").value="";$("confirm-live").checked=false;controls();});
$("telegram-form").addEventListener("submit",async event=>{event.preventDefault();const bot=$("telegram-token").value.trim(),chat=$("telegram-chat").value.trim();if(!bot||!chat){notice("Enter both a bot token and chat ID.",true);return;}await act("/api/settings",{TELEGRAM_BOT_TOKEN:bot,TELEGRAM_CHAT_ID:chat},"Telegram settings saved. Alerts begin when the bot runs.");$("telegram-token").value="";$("telegram-chat").value="";});
$("remove-kraken").onclick=()=>act("/api/settings",{KRAKEN_API_KEY:"",KRAKEN_API_SECRET:""},"Keys removed locally. Revoke them on Kraken if you no longer need them.");
$("remove-telegram").onclick=()=>act("/api/settings",{TELEGRAM_BOT_TOKEN:"",TELEGRAM_CHAT_ID:""},"Telegram settings removed.");
$("find-chat").onclick=async()=>{
  if(pending)return; pending=true;controls();
  try {
    const result=await api("/api/telegram-chats",{token:$("telegram-token").value.trim()});
    if(!result.chats.length)throw new Error("No private chat found. Send /start to your new bot in Telegram, then try again.");
    $("chat-choice").replaceChildren(...result.chats.map(chat=>{const option=document.createElement("option");option.value=chat.id;option.textContent=`${chat.name} · ${chat.id}`;return option;}));
    $("chat-picker").hidden=false;$("telegram-chat").value=result.chats[0].id;
    notice("Found Telegram chats. Choose yours and save the settings.");
  }catch(error){notice(error.message,true);}finally{pending=false;controls();}
};
$("chat-choice").onchange=()=>{$("telegram-chat").value=$("chat-choice").value;};
$("check-account").onclick=()=>act("/api/start",{action:"check"},"Checking account access and reconciliation. This can take a minute; no orders will be placed.");
$("doctor").onclick=()=>act("/api/start",{action:"doctor"},"Checking Kraken’s public markets. No orders will be placed.");
$("start").onclick=()=>act("/api/start",{action:"run",mode:$("mode").value,confirmed:$("confirm-live").checked},"Startup requested. Watch the runtime and account status below.");
$("stop").onclick=()=>act("/api/stop",{},"Safe stop requested. Waiting for the current operation to finish.");
$("mode").onchange=()=>{$("confirm-live").checked=false;controls();};
$("confirm-live").onchange=controls;
async function poll(){await refresh();setTimeout(poll,2500);}poll();
