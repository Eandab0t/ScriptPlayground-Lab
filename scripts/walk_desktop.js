"use strict";
/* Walk the real Electron desktop app end to end: connect a bot folder, run it,
 * submit a slash command, click the bot's button — and prove no browser window
 * ever opens (sweeps Windows process command lines for the app URL).
 *
 * Usage: node scripts/walk_desktop.js [--packaged]
 *
 * Default walks the dev checkout via desktop/main.js. --packaged launches the
 * built distributable (release/win-unpacked/ScriptPlayground.exe) instead, so
 * the walk proves what actually shipped in the package.
 */
const { _electron: electron } = require("playwright");
const { execSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const PACKAGED = process.argv.includes("--packaged");
const PACKAGED_EXE = path.join(ROOT, "release", "win-unpacked", "ScriptPlayground.exe");
const DATA_DIR = path.join(ROOT, ".test-artifacts", PACKAGED ? "desktop-walk-packaged" : "desktop-walk");
const ELECTRON_PROFILE = path.join(DATA_DIR, "electron-user-data");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Any running process whose command line mentions the app URL in a browser
// process would show up here (wmic is gone on current Windows builds, so this
// uses Get-CimInstance).
function browserWindowProcesses(port) {
  try {
    const output = execSync(
      `powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object CommandLine | Select-Object Name,CommandLine | ConvertTo-Csv -NoTypeInformation"`,
      { encoding: "utf8", windowsHide: true, maxBuffer: 32 * 1024 * 1024 },
    ).toLowerCase();
    const leaks = [];
    for (const line of output.split("\n")) {
      if (!line.includes(`127.0.0.1:${port}`)) continue;
      const name = (line.match(/(msedge|chrome|firefox|browser_broker)\.exe/) || [])[1];
      if (name && !leaks.includes(name)) leaks.push(name);
    }
    return leaks;
  } catch (error) {
    throw new Error(`browser sweep failed (cannot verify): ${error.message.split("\n")[0]}`);
  }
}

(async () => {
  const port = PACKAGED ? 8798 : 8797;
  process.env.SCRIPTPLAYGROUND_DATA_DIR = DATA_DIR;
  process.env.SCRIPTPLAYGROUND_PORT = String(port);

  if (PACKAGED && !fs.existsSync(PACKAGED_EXE)) {
    throw new Error(`packaged exe missing: ${PACKAGED_EXE} — run npm run dist first`);
  }
  // With a data-dir override the server reads workspaces from DATA_DIR/bots,
  // so seed a disposable data dir with the showcase bot in both modes.
  fs.rmSync(DATA_DIR, { recursive: true, force: true });
  fs.mkdirSync(path.join(DATA_DIR, "bots"), { recursive: true });
  fs.cpSync(path.join(ROOT, "bots", "showcase_bot"), path.join(DATA_DIR, "bots", "showcase_bot"), { recursive: true });

  console.log("== browser sweep BEFORE ==", JSON.stringify(browserWindowProcesses(port)));
  const launchedBrowser = browserWindowProcesses(port);
  if (launchedBrowser.length) throw new Error("browser already running before launch");

  console.log("launching Electron app…", PACKAGED ? `(packaged: ${PACKAGED_EXE})` : "(dev)" );
  const app = await electron.launch(
    PACKAGED
      ? { executablePath: PACKAGED_EXE, args: [`--user-data-dir=${ELECTRON_PROFILE}`], env: { ...process.env } }
      : { args: [`--user-data-dir=${ELECTRON_PROFILE}`, path.join(ROOT)], env: { ...process.env } },
  );
  const win = await app.firstWindow();
  // Pin the window to the app's own default (desktop/main.js). Headless CI
  // displays are small, and at narrow widths the message hover toolbar sits
  // under the sidebar, so its clicks are intercepted instead of hitting the
  // button. Walking the real 1600x980 layout keeps this test about the app.
  await win.waitForLoadState('domcontentloaded');
  const { width, height } = win.viewportSize() ?? { width: 1600, height: 980 };
  if (width < 1600 || height < 980) {
    await win.setViewportSize({ width: 1600, height: 980 });
  }
  console.log("window:", await win.title());

  const connected = win.locator("#me-name");
  await connected.waitFor({ timeout: 30000 });
  console.log("UI live, acting as:", await connected.textContent());

  // Wait for app boot to finish loading /api/workspaces; the selector remains
  // disabled until its options are ready, intentionally independent of Run.
  const workspaceSelect = win.locator("#workspace-select");
  await win.locator("#workspace-select:not(:disabled)").waitFor({ timeout: 30000 });
  if (await workspaceSelect.locator('option[value="showcase_bot"]').count() !== 1) {
    throw new Error("showcase_bot was not present after workspace discovery");
  }

  // 1) connect the bot folder
  await workspaceSelect.selectOption("showcase_bot");
  await win.locator("#btn-connect").click();
  await win.locator("#run-stats").filter({ hasText: "connected showcase_bot" }).waitFor({ timeout: 15000 });
  console.log("1. connected bots/showcase_bot/bot.py");

  // 2) run it (boots the hosted discord.py bot)
  await win.getByRole("button", { name: "▶ Run", exact: true }).click();
  await win.locator("#run-stats").filter({ hasText: "slash commands" }).waitFor({ timeout: 90000 });
  console.log("2. bot booted:", (await win.locator("#run-stats").textContent()).trim());

  // 3) slash command /panel via the palette
  const composer = win.locator("#composer-input");
  await composer.click();
  await composer.pressSequentially("/panel");
  const item = win.locator("#palette .palette-item").first();
  await item.waitFor({ timeout: 5000 });
  await item.click();
  await win.locator("#cmd-send").click();
  const panelMsg = win.locator(".msg").filter({ hasText: "Control panel ready:" }).last();
  await panelMsg.waitFor({ timeout: 15000 });
  console.log("3. /panel replied from the bot");

  // 4) click the bot's button
  await panelMsg.locator(".btn", { hasText: "Wave" }).click();
  await win.locator(".msg").filter({ hasText: "waved from the desktop app!" }).last()
    .waitFor({ timeout: 15000 });
  console.log("4. clicked Wave — bot replied (ephemeral)");

  // 5) reactions: hover the panel message, add 🔥, expect a highlighted pill
  await panelMsg.hover();
  await panelMsg.locator(`button[title="Add reaction"]`).click();
  await win.locator("#emoji-picker.open").waitFor({ timeout: 5000 });
  await win.locator("#emoji-picker.open .emoji-cell", { hasText: "🔥" }).first().click();
  await panelMsg.locator(".reaction-bar .reaction.me", { hasText: "🔥" }).waitFor({ timeout: 10000 });
  console.log("5. reacted 🔥 from the hover toolbar — pill highlighted as me");

  // 6) user settings overlay: panes switch, sound switch toggles, Escape closes
  await win.locator("#open-user-settings").click();
  await win.locator("#user-settings-overlay.open").waitFor({ timeout: 5000 });
  await win.locator(`.settings-nav-item[data-pane="appearance"]`).click();
  await win.locator(`.settings-pane[data-pane="appearance"].active`).waitFor({ timeout: 5000 });
  await win.locator(`.settings-nav-item[data-pane="sound"]`).click();
  const soundSwitch = win.locator("#us-sound-switch");
  const before = await soundSwitch.getAttribute("aria-checked");
  await soundSwitch.click();
  if ((await soundSwitch.getAttribute("aria-checked")) === before) throw new Error("sound switch did not toggle");
  await soundSwitch.click();
  await win.keyboard.press("Escape");
  await win.locator("#user-settings-overlay").waitFor({ state: "hidden", timeout: 5000 });
  console.log("6. user settings overlay: appearance + sound panes, switch toggled, Escape closed");

  // 7) the no-browser-window proof DURING the session
  const during = browserWindowProcesses(port);
  console.log("== browser sweep DURING ==", JSON.stringify(during));
  if (during.length) throw new Error(`browser windows opened: ${during.join(", ")}`);

  const serverLogs = [];
  app.process().stdout.on("data", (d) => serverLogs.push(String(d)));
  console.log("closing app…");
  await app.close();

  await sleep(3000);
  const after = browserWindowProcesses(port);
  console.log("== browser sweep AFTER ==", JSON.stringify(after));
  if (after.length) throw new Error(`browser windows lingered: ${after.join(", ")}`);

  console.log(`WALK-PASS (${PACKAGED ? "packaged" : "dev"}): full flow complete, zero browser windows opened`);
})().catch((error) => {
  console.error("WALK-FAIL:", error);
  process.exit(1);
});
