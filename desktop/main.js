"use strict";
// WildCat-3 desktop shell. The window only ever loads the loopback control panel
// started by the Python launcher; this process owns the window chrome, full screen,
// external links and the close prompt. It never reads or stores credentials.
const { app, BrowserWindow, Menu, dialog, ipcMain, screen, session, shell } = require("electron");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const ROOT = process.env.WILDCAT_ROOT || process.cwd();
const SESSION_FILE = process.env.WILDCAT_SESSION || path.join(ROOT, "data", "web-session.json");
const WINDOW_STATE = path.join(ROOT, "data", "desktop-window.json");
const MINIMUM = { width: 900, height: 620 };
const DEFAULT = { width: 1440, height: 900 };

let panel = null;
let panelWindow = null;
let closable = false;

function readPanel() {
  const saved = JSON.parse(fs.readFileSync(SESSION_FILE, "utf8"));
  const port = Number(saved.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("The session file has no local port.");
  if (typeof saved.token !== "string" || !saved.token) throw new Error("The session file has no session token.");
  return { port, token: saved.token, origin: `http://127.0.0.1:${port}` };
}

// Small typed helper for the two control-panel calls this process makes itself.
// node:http keeps full header control, which the panel requires for POST requests.
function request(method, endpoint) {
  return new Promise((resolve, reject) => {
    const headers = { "X-Wildcat-Token": panel.token, Accept: "application/json" };
    const body = method === "POST" ? "{}" : "";
    if (body) {
      headers["Content-Type"] = "application/json";
      headers.Origin = panel.origin;
      headers["Content-Length"] = String(Buffer.byteLength(body));
    }
    const call = http.request({ host: "127.0.0.1", port: panel.port, path: endpoint, method, headers }, response => {
      let text = "";
      response.setEncoding("utf8");
      response.on("data", chunk => { text += chunk; });
      response.on("end", () => {
        if (response.statusCode !== 200) return reject(new Error(`The control panel returned ${response.statusCode}.`));
        try { resolve(JSON.parse(text)); } catch (error) { reject(error); }
      });
    });
    call.setTimeout(5000, () => call.destroy(new Error("The control panel did not respond.")));
    call.on("error", reject);
    call.end(body);
  });
}

function savedBounds() {
  try {
    const saved = JSON.parse(fs.readFileSync(WINDOW_STATE, "utf8"));
    const bounds = { x: saved.x, y: saved.y, width: saved.width, height: saved.height };
    if (!Object.values(bounds).every(Number.isFinite)) return null;
    if (bounds.width < MINIMUM.width || bounds.height < MINIMUM.height) return null;
    // Ignore a position left behind by a display that is no longer connected.
    const visible = screen.getAllDisplays().some(display => {
      const area = display.workArea;
      return bounds.x < area.x + area.width && bounds.x + bounds.width > area.x
        && bounds.y < area.y + area.height && bounds.y + bounds.height > area.y;
    });
    return visible ? bounds : null;
  } catch {
    return null;
  }
}

function rememberBounds() {
  if (!panelWindow || panelWindow.isDestroyed() || panelWindow.isFullScreen() || panelWindow.isMinimized()) return;
  try {
    fs.writeFileSync(WINDOW_STATE, JSON.stringify(panelWindow.getNormalBounds()), { mode: 0o600 });
  } catch {
    // A saved window position is a convenience; never block a close on it.
  }
}

function announceFullScreen() {
  if (!panelWindow || panelWindow.isDestroyed()) return;
  panelWindow.webContents.send("wildcat:full-screen", panelWindow.isFullScreen());
}

function toggleFullScreen() {
  if (!panelWindow || panelWindow.isDestroyed()) return false;
  // macOS animates into full screen, so report the requested state; the panel's own
  // class and label follow the enter/leave events instead.
  const target = !panelWindow.isFullScreen();
  panelWindow.setFullScreen(target);
  return target;
}

// Closing the window is a frontend action: it never cancels orders and never stops
// the engine implicitly. Ask first whenever trading is actually in progress.
async function confirmClose() {
  let trading = false;
  try {
    const state = await request("GET", "/api/state");
    trading = Boolean(state.busy) && state.operation === "run";
  } catch {
    trading = false;
  }
  if (!trading) return true;
  const { response } = await dialog.showMessageBox(panelWindow, {
    type: "warning",
    buttons: ["Keep trading", "Stop the bot safely", "Cancel"],
    defaultId: 0,
    cancelId: 2,
    noLink: true,
    message: "The bot is still running.",
    detail: "Closing this window does not stop trading and does not cancel anything on Kraken. "
      + "The launcher's Terminal window keeps the engine alive; open the launcher again to come back. "
      + "Choose Stop the bot safely to request graceful reconciliation and watch it finish here first.",
  });
  if (response === 1) {
    try {
      await request("POST", "/api/stop");
    } catch (error) {
      dialog.showMessageBox(panelWindow, { type: "error", message: "Could not request a safe stop.", detail: error.message });
    }
    return false;
  }
  return response === 0;
}

function buildMenu() {
  const mac = process.platform === "darwin";
  const template = [
    ...(mac ? [{ role: "appMenu" }] : []),
    { role: "editMenu" },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { role: "toggleDevTools" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    { role: "windowMenu" },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function hardenSession() {
  const store = session.defaultSession;
  store.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  store.setPermissionCheckHandler(() => false);
  // The panel declares `default-src 'self'`; this is the same rule at the network layer.
  store.webRequest.onBeforeRequest({ urls: ["http://*/*", "https://*/*"] }, (details, callback) => {
    callback({ cancel: !details.url.startsWith(`${panel.origin}/`) });
  });
}

function createWindow() {
  const bounds = savedBounds();
  panelWindow = new BrowserWindow({
    ...DEFAULT,
    ...(bounds || {}),
    minWidth: MINIMUM.width,
    minHeight: MINIMUM.height,
    show: false,
    backgroundColor: "#000000",
    title: "WildCat-3",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    trafficLightPosition: { x: 14, y: 11 },
    fullscreenable: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webviewTag: false,
      spellcheck: false,
      devTools: true,
    },
  });

  panelWindow.once("ready-to-show", () => panelWindow.show());
  panelWindow.on("enter-full-screen", announceFullScreen);
  panelWindow.on("leave-full-screen", announceFullScreen);
  panelWindow.on("resize", rememberBounds);
  panelWindow.on("move", rememberBounds);
  panelWindow.on("close", event => {
    if (closable) return;
    event.preventDefault();
    confirmClose().then(agreed => {
      if (!agreed) return;
      closable = true;
      rememberBounds();
      panelWindow.close();
    });
  });
  panelWindow.on("closed", () => { panelWindow = null; });

  const contents = panelWindow.webContents;
  contents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://")) shell.openExternal(url);
    return { action: "deny" };
  });
  contents.on("will-navigate", (event, url) => {
    if (url.startsWith(`${panel.origin}/`)) return;
    event.preventDefault();
    if (url.startsWith("https://")) shell.openExternal(url);
  });
  contents.on("will-attach-webview", event => event.preventDefault());
  contents.on("before-input-event", (event, input) => {
    // F11 is the habitual full-screen key everywhere; macOS keeps Control-Command-F too.
    if (input.type === "keyDown" && input.key === "F11" && !input.isAutoRepeat) {
      event.preventDefault();
      toggleFullScreen();
    }
  });
  contents.on("render-process-gone", () => contents.reload());
  contents.on("did-fail-load", (_event, code, description, url, isMainFrame) => {
    if (!isMainFrame || code === -3) return;
    dialog.showMessageBox(panelWindow, {
      type: "error",
      buttons: ["Retry", "Quit"],
      defaultId: 0,
      message: "Could not reach the WildCat-3 control panel.",
      detail: `${description} (${url})\n\nKeep the launcher's Terminal window open, then retry.`,
    }).then(({ response }) => (response === 0 ? panelWindow.loadURL(`${panel.origin}/`) : app.quit()));
  });

  panelWindow.loadURL(`${panel.origin}/`);
}

app.setName("WildCat-3");
// Per-project Chromium state keeps the cache inside the gitignored data directory and
// scopes the single-instance lock to this project folder.
app.setPath("userData", path.join(ROOT, "data", "desktop"));
app.commandLine.appendSwitch("disable-http-cache");

if (!app.requestSingleInstanceLock()) {
  app.exit(0);
} else {
  app.on("second-instance", () => {
    if (!panelWindow || panelWindow.isDestroyed()) return;
    if (panelWindow.isMinimized()) panelWindow.restore();
    panelWindow.focus();
  });
  app.on("window-all-closed", () => app.quit());
  app.on("web-contents-created", (_event, contents) => {
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
  });
  app.on("certificate-error", (event, _contents, _url, _error, _certificate, callback) => {
    event.preventDefault();
    callback(false);
  });

  ipcMain.on("wildcat:session", event => {
    event.returnValue = {
      token: panel ? panel.token : "",
      platform: process.platform,
      fullScreen: Boolean(panelWindow && !panelWindow.isDestroyed() && panelWindow.isFullScreen()),
    };
  });
  ipcMain.handle("wildcat:toggle-full-screen", () => toggleFullScreen());

  app.whenReady().then(() => {
    try {
      panel = readPanel();
    } catch (error) {
      dialog.showErrorBox("WildCat-3", `Could not read the local session.\n\n${error.message}\n\n`
        + "Start the panel with the launcher, or with: uv run wildcat3 web");
      app.exit(1);
      return;
    }
    hardenSession();
    buildMenu();
    createWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
}
