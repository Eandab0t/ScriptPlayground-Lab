"use strict";
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("scriptplaygroundDesktop", {
  desktop: true,
  platform: process.platform,
  // Native OS notification; the promise resolves once it has been shown
  // ("shown") or after the user activates it ("clicked").
  notify(title, body) {
    return ipcRenderer.invoke("scriptplayground:notify", { title, body });
  },
  // Tray mute toggle state; cb runs on every change (including from the tray).
  onTrayMute(cb) {
    ipcRenderer.on("scriptplayground:tray-mute", (_event, muted) => cb(muted));
  },
  // Tell the main process how the web client's sound toggle is configured so
  // the tray checkbox always matches the in-app one.
  sendSoundState(enabled) {
    ipcRenderer.send("scriptplayground:sound-state", Boolean(enabled));
  },
  // Shell diagnostics: tray presence, mute state, AUMID (tests/walks only).
  debugState() {
    return ipcRenderer.invoke("scriptplayground:debug-state");
  },
});
