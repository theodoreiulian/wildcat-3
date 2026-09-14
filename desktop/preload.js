"use strict";
// The only bridge between the control panel and Electron. It exposes the launch
// session token and window chrome controls, and nothing that can touch the disk,
// the shell or the trading process.
const { contextBridge, ipcRenderer } = require("electron");

const session = ipcRenderer.sendSync("wildcat:session");
const listeners = new Set();
let fullScreen = Boolean(session.fullScreen);

ipcRenderer.on("wildcat:full-screen", (_event, value) => {
  fullScreen = Boolean(value);
  for (const listener of listeners) listener(fullScreen);
});

contextBridge.exposeInMainWorld("wildcat", {
  platform: String(session.platform || ""),
  token: String(session.token || ""),
  isFullScreen: () => fullScreen,
  toggleFullScreen: () => ipcRenderer.invoke("wildcat:toggle-full-screen"),
  onFullScreenChange: listener => {
    if (typeof listener !== "function") return;
    listeners.add(listener);
    listener(fullScreen);
  },
});
