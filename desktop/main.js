"use strict";
/* ScriptPlayground desktop shell.
 *
 * Lifecycle: this shell OWNS the Python server. It starts `python main.py
 * --no-browser` headlessly, opens one Electron window pointing at it, and
 * kills the server when the window closes. If a server is already listening
 * on the port (a previous launch, or `python main.py` in a terminal), it
 * attaches to it instead of spawning a second one — so clicking Run in the
 * UI never opens a browser window, and relaunching never piles up windows.
 */
const { app, BrowserWindow, Tray, Menu, Notification, dialog, shell, ipcMain, nativeImage } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const net = require("net");
const path = require("path");

const HOST = "127.0.0.1";
const BASE_PORT = Number(process.env.SCRIPTPLAYGROUND_PORT || 8741);
const START_URL = `http://${HOST}:${BASE_PORT}/`;
const SERVER_READY_TIMEOUT_MS = 30_000;

let serverProcess = null;
let serverPort = BASE_PORT;
let mainWindow = null;
let shuttingDown = false;
let spawnFailed = false;
let tray = null;
let soundMuted = false;      // mirrors the web client's sound toggle

const log = (...parts) => console.log("[desktop]", ...parts);

/* ---- notifications + tray ------------------------------------------------ */

ipcMain.handle("scriptplayground:notify", (_event, { title, body }) => new Promise((resolve) => {
  if (!Notification.isSupported()) return resolve("unsupported");
  const notification = new Notification({
    title: String(title || "ScriptPlayground").slice(0, 120),
    body: String(body || "").slice(0, 240),
    icon: path.join(__dirname, "..", "assets", "icon.png"),
    silent: true, // the web client already played its own sound cue
  });
  notification.on("click", () => {
    showWindow();
    resolve("clicked");
  });
  notification.on("failed", (_event, error) => {
    log("notification failed", error);
    resolve("failed");
  });
  notification.on("close", () => resolve("shown"));
  notification.show();
}));

// The web client reports every sound-toggle change so the tray checkbox and
// the in-app control can never disagree. Changes made from the tray are
// applied by the renderer without echoing back, so there is no loop.
ipcMain.on("scriptplayground:sound-state", (_event, enabled) => {
  const muted = !enabled;
  if (muted === soundMuted) return;
  soundMuted = muted;
  updateTray();
});

// Diagnostics for the desktop walk and tests.
ipcMain.handle("scriptplayground:debug-state", () => ({
  soundMuted,
  tray: Boolean(tray),
}));

function setSoundMuted(muted) {
  soundMuted = muted;
  updateTray();
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("scriptplayground:tray-mute", muted);
  }
}

function trayIcon() {
  // Tray needs a small square image; the PNG icon scales down cleanly.
  return nativeImage.createFromPath(path.join(__dirname, "..", "assets", "icon.png"));
}

function updateTray() {
  if (!tray) return;
  tray.setToolTip(`ScriptPlayground — sounds ${soundMuted ? "muted" : "on"}`);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Show ScriptPlayground", click: () => showWindow() },
    { type: "separator" },
    { label: "Sound effects", type: "checkbox", checked: !soundMuted,
      click: (item) => setSoundMuted(!item.checked) },
    { type: "separator" },
    { label: "Quit", click: () => app.quit() },
  ]));
}

function createTray() {
  if (tray || process.platform !== "win32") return; // tray is a Windows affordance here
  tray = new Tray(trayIcon());
  updateTray();
  tray.on("click", () => showWindow());
}

function showWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function ensureStartMenuShortcut() {
  // Toasts on Windows look the AUMID up in Start Menu shortcuts. Installers
  // normally create one; a portable/unpacked build has to make its own, or
  // every Notification fails with HRESULT -2143420140.
  if (process.platform !== "win32") return;
  try {
    const appData = process.env.APPDATA;
    if (!appData) return;
    const target = path.join(appData, "Microsoft", "Windows", "Start Menu", "Programs", "ScriptPlayground.lnk");
    if (!fs.existsSync(target)) {
      shell.writeShortcutLink(target, "create", {
        target: process.execPath,
        cwd: app.isPackaged ? path.dirname(process.execPath) : path.join(__dirname, ".."),
        args: app.isPackaged ? "" : ".",
        description: "ScriptPlayground — local Discord bot UI playground",
        icon: path.join(__dirname, "..", "assets", "icon.ico"),
        iconIndex: 0,
        appUserModelId: "com.ean.scriptplayground",
      });
      log("created Start Menu shortcut for toast support");
    }
    // Newer Windows also wants the AUMID registered per-user in the registry
    // (HKCU — no admin needed) or toasts fail with HRESULT -2143420140 even
    // with the shortcut in place.
    const { execFile } = require("child_process");
    execFile("reg", ["add", "HKCU\\SOFTWARE\\Classes\\AppUserModelId\\com.ean.scriptplayground",
      "/ve", "/d", "ScriptPlayground", "/f"], { windowsHide: true },
      (error) => { if (error) log("AUMID registry registration skipped:", error.message); });
  } catch (error) {
    log("shortcut creation skipped:", error.message);
  }
}

function freePort(start) {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", (error) => {
      if (error.code === "EADDRINUSE") resolve(null);
      else reject(error);
    });
    probe.once("listening", () => probe.close(() => resolve(start)));
    probe.listen(start, HOST);
  });
}

async function portReachable(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: HOST, port, timeout: 800 });
    socket.once("connect", () => { socket.destroy(); resolve(true); });
    socket.once("timeout", () => { socket.destroy(); resolve(false); });
    socket.once("error", () => resolve(false));
  });
}

async function pickPort() {
  for (let candidate = BASE_PORT; candidate < BASE_PORT + 20; candidate += 1) {
    const free = await freePort(candidate);
    if (free !== null) return free;
  }
  return 0; // let the OS choose is not supported by main.py; last resort keeps BASE_PORT
}

function serverCommand(port) {
  if (process.env.SCRIPTPLAYGROUND_NO_SERVER === "1") return null; // attach-only dev mode
  if (app.isPackaged) {
    const exe = path.join(process.resourcesPath, "server", "ScriptPlayground-server.exe");
    if (!fs.existsSync(exe)) return { missing: exe };
    // --auto-shutdown stops the server 30s after the last UI disconnects, so a
    // crashed shell can never leave an orphaned server behind.
    return {
      command: exe,
      args: ["--port", String(port), "--no-browser", "--auto-shutdown"],
      cwd: path.dirname(exe),
    };
  }
  const python = process.env.SCRIPTPLAYGROUND_PYTHON || "python";
  return {
    command: python,
    args: ["-X", "utf8", path.join(__dirname, "..", "main.py"), "--no-browser", "--port", String(port)],
    cwd: path.join(__dirname, ".."),
  };
}

function startServer(port) {
  const spec = serverCommand(port);
  if (!spec) return null;
  if (spec.missing) {
    dialog.showMessageBoxSync({
      type: "error",
      title: "ScriptPlayground",
      message: "The server component is missing from this build.",
      detail: `Expected ${spec.missing}. Rebuild with the "dist" npm script.`,
    });
    app.quit();
    return null;
  }
  log("spawning", spec.command, spec.args.join(" "));
  const child = spawn(spec.command, spec.args, {
    cwd: spec.cwd,
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.on("error", (error) => {
    serverProcess = null;
    spawnFailed = true;
    log("server spawn failed", error);
    dialog.showMessageBoxSync({
      type: "error",
      title: "ScriptPlayground",
      message: "Could not start the ScriptPlayground server.",
      detail: String(error),
    });
    app.quit();
  });
  const relay = (stream, streamName) => {
    let buffer = "";
    stream.setEncoding("utf8");
    stream.on("data", (chunk) => {
      buffer += chunk;
      let index;
      while ((index = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (line) log(`server/${streamName}:`, line);
      }
    });
  };
  relay(child.stdout, "out");
  relay(child.stderr, "err");
  child.on("exit", (code, signal) => {
    serverProcess = null;
    if (shuttingDown) return;
    log("server exited early", { code, signal });
    if (mainWindow && !mainWindow.isDestroyed()) {
      dialog.showMessageBoxSync({
        type: "error",
        title: "ScriptPlayground",
        message: "The ScriptPlayground server stopped unexpectedly.",
        detail: `Exit code ${code}${signal ? ` (${signal})` : ""}. Check the terminal output for details.`,
      });
    }
    app.quit();
  });
  return child;
}

async function waitForServer(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await portReachable(port)) return true;
    if (spawnFailed || (serverProcess === null && process.env.SCRIPTPLAYGROUND_NO_SERVER !== "1")) return false;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

function stopServer() {
  shuttingDown = true;
  if (!serverProcess) return;
  const child = serverProcess;
  serverProcess = null;
  if (process.platform === "win32") {
    // Tree-kill so Node bot subprocesses go down with the server.
    spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], { windowsHide: true });
  } else {
    child.kill("SIGTERM");
  }
}

function createWindow(url) {
  const win = new BrowserWindow({
    width: 1600,
    height: 980,
    minWidth: 940,
    minHeight: 600,
    backgroundColor: "#1e1f22",
    title: "ScriptPlayground",
    icon: path.join(__dirname, "..", "assets", "icon.ico"),
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });
  win.once("ready-to-show", () => win.show());
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    if (target.startsWith("http://") || target.startsWith("https://")) {
      const local = target.startsWith(`${url}/`) || target === url;
      if (local) {
        // Same-origin popups (e.g. the Embeder builder) stay in the app window.
        win.loadURL(target);
      } else {
        // Real external links (Discord OAuth, docs) open in the system browser.
        shell.openExternal(target);
      }
    }
    return { action: "deny" };
  });
  win.on("closed", () => { mainWindow = null; });
  win.loadURL(url);
  return win;
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => showWindow());

  // Windows refuses toast notifications without an App User Model ID;
  // use the packaged appId so dev and release behave identically.
  app.setAppUserModelId("com.ean.scriptplayground");

  app.whenReady().then(async () => {
    ensureStartMenuShortcut();
    let attached = await portReachable(BASE_PORT);
    if (attached) {
      log("attaching to a server already listening on", BASE_PORT);
    } else {
      const port = await pickPort();
      if (port !== BASE_PORT) log("port", BASE_PORT, "busy/unreachable — using", port);
      serverPort = port;
      serverProcess = startServer(port);
      const ready = await waitForServer(port, SERVER_READY_TIMEOUT_MS);
      if (!ready) {
        if (!spawnFailed) dialog.showMessageBoxSync({
          type: "error",
          title: "ScriptPlayground",
          message: "Could not start the ScriptPlayground server.",
          detail: "Ensure Python 3.12 with aiohttp is installed, or set SCRIPTPLAYGROUND_PYTHON to the interpreter path.",
        });
        app.quit();
        return;
      }
    }
    const url = attached ? START_URL : `http://${HOST}:${serverPort}/`;
    mainWindow = createWindow(url);
    createTray();
  });

  app.on("window-all-closed", () => {
    if (tray) { tray.destroy(); tray = null; }
    stopServer();
    app.quit();
  });

  app.on("before-quit", () => {
    if (tray) { tray.destroy(); tray = null; }
    stopServer();
  });
}
