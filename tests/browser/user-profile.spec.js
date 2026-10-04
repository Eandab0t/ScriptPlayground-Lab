const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');

let workspacePath;
async function holdWorkspaceList(page) {
  let requestStarted;
  let releaseRequest;
  const requested = new Promise((resolve) => { requestStarted = resolve; });
  const released = new Promise((resolve) => { releaseRequest = resolve; });
  await page.route('**/api/workspaces', async (route) => {
    requestStarted();
    await released;
    await route.continue();
  });
  return { requested, release: releaseRequest };
}

async function holdInitialScriptList(page) {
  let requestStarted;
  let releaseRequest;
  const requested = new Promise((resolve) => { requestStarted = resolve; });
  const released = new Promise((resolve) => { releaseRequest = resolve; });
  let firstList = true;
  await page.route('**/api/scripts', async (route) => {
    if (route.request().method() !== 'GET' || !firstList) return route.continue();
    firstList = false;
    const response = await route.fetch();
    const body = await response.text();
    requestStarted();
    await released;
    await route.fulfill({ response, body });
  });
  return { requested, release: releaseRequest };
}

async function openPageWithStorage(page, storage, delayWorkspaceList = false, delayScriptList = false) {
  const browserContext = page.context();
  await page.close();
  const appPage = await browserContext.newPage();
  const workspaceGate = delayWorkspaceList ? await holdWorkspaceList(appPage) : null;
  const scriptListGate = delayScriptList ? await holdInitialScriptList(appPage) : null;

  await appPage.addInitScript((values) => {
    if (sessionStorage.getItem('test-storage-seeded')) return;
    localStorage.clear();
    for (const [key, value] of Object.entries(values)) localStorage.setItem(key, value);
    sessionStorage.setItem('test-storage-seeded', 'true');
  }, storage);
  await appPage.goto('/');
  await expect(appPage.locator('#act-as option')).toHaveCount(4);
  return { page: appPage, workspaceGate, scriptListGate };
}

async function openLegacyDraft(page, draft, codeContext) {
  const storage = { 'pg-code': draft };
  if (codeContext) storage['pg-code-context'] = codeContext;
  return (await openPageWithStorage(page, storage)).page;
}

test.beforeEach(async ({ page }) => {
  const workspacesLoaded = page.waitForResponse((response) => response.url().endsWith('/api/workspaces'));
  await page.goto('/');
  await expect(page.locator('#act-as option')).toHaveCount(4);
  const { workspaces } = await (await workspacesLoaded).json();
  await expect(page.locator('#workspace-select option')).toHaveCount(workspaces.length + 1);
});

test.afterEach(() => {
  if (workspacePath) fs.rmSync(workspacePath, { recursive: true, force: true });
  workspacePath = null;
});

test('connected workspace opens nested files, runs the buffer, boots saved bot, and saves explicitly', async ({ page }) => {
  const name = `run-file-ui-${process.pid}-${Date.now()}`;
  workspacePath = fs.mkdtempSync(path.join(__dirname, '..', '..', 'bots', `${name}-`));
  const workspaceName = path.basename(workspacePath);
  fs.mkdirSync(path.join(workspacePath, 'cogs'));
  fs.writeFileSync(path.join(workspacePath, 'bot.py'), [
    'import discord',
    'from discord.ext import commands',
    'bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())',
    '@bot.event',
    'async def on_ready():',
    '    await bot.get_channel(900000000000000002).send("SAVED_ENTRY_BOOTED")',
    'async def main():',
    '    async with bot:',
    '        await bot.start("offline-simulated-token")',
    '',
  ].join(String.fromCharCode(10)));
  const workerSource = ['async def main():', '    await send("SAVED_NESTED_FILE")', ''].join(String.fromCharCode(10));
  fs.writeFileSync(path.join(workspacePath, 'cogs', 'worker.py'), workerSource);

  await page.evaluate(() => loadWorkspaces());
  const workspaces = await page.request.get('/api/workspaces').then((response) => response.json());
  const workspace = workspaces.workspaces.find((item) => item.name === workspaceName);
  expect(workspace.files).toEqual(['bot.py', 'cogs/worker.py']);
  await page.locator('#workspace-select').selectOption(workspaceName);
  await page.locator('#btn-connect').click();
  await expect(page.locator('#editor-filename')).toHaveText('bot.py');
  await expect(page.locator('#btn-run')).toHaveText('▶ Run');
  await expect(page.locator('#btn-run')).toHaveAttribute('title', /saved on disk/);
  await expect(page.locator('#btn-run-file')).toBeVisible();

  await page.locator('#workspace-file-select').selectOption('cogs/worker.py');
  await page.locator('#btn-open-file').click();
  await expect(page.locator('#editor-filename')).toHaveText('cogs/worker.py');
  await page.locator('#code').fill(['async def main():', '    await send("UNSAVED_BUFFER_RAN")', ''].join(String.fromCharCode(10)));
  await expect(page.locator('#workspace-dirty')).toBeVisible();
  const unsavedBuffer = ['async def main():', '    await send("UNSAVED_BUFFER_RAN")', ''].join(String.fromCharCode(10));
  expect(await page.evaluate(() => ({
    state: JSON.parse(localStorage.getItem('pg-workspace-state')),
    buffer: localStorage.getItem('pg-code'),
  }))).toEqual({
    state: { version: 1, workspace: workspaceName, filename: 'cogs/worker.py', baseline: workerSource },
    buffer: unsavedBuffer,
  });
  await page.reload();
  await expect(page.locator('#act-as option')).toHaveCount(4);
  await expect(page.locator('#workspace-select')).toHaveValue(workspaceName);
  await expect(page.locator('#workspace-file-select')).toHaveValue('cogs/worker.py');
  await expect(page.locator('#editor-filename')).toHaveText('cogs/worker.py');
  await expect(page.locator('#code')).toHaveValue(unsavedBuffer);
  await expect(page.locator('#workspace-dirty')).toBeVisible();
  await expect(page.locator('#run-stats')).toContainText('restored unsaved');

  await page.locator('#btn-run-file').click();
  await expect(page.locator('#timeline .msg').filter({ hasText: 'UNSAVED_BUFFER_RAN' }).last()).toBeVisible();
  await expect(page.locator('#run-stats')).toContainText('unsaved; disk unchanged');
  expect(fs.readFileSync(path.join(workspacePath, 'cogs', 'worker.py'), 'utf8')).toBe(workerSource);

  await page.locator('#btn-run').click();
  await expect(page.locator('#run-stats')).toContainText('saved bot booted', { timeout: 90000 });
  await expect(page.locator('#timeline .msg').filter({ hasText: 'SAVED_ENTRY_BOOTED' })).toBeVisible();
  await expect(page.locator('#timeline .msg').filter({ hasText: 'UNSAVED_BUFFER_RAN' })).toHaveCount(0);
  expect(fs.readFileSync(path.join(workspacePath, 'cogs', 'worker.py'), 'utf8')).toBe(workerSource);

  await page.locator('#btn-save').click();
  await expect(page.locator('#workspace-dirty')).toBeHidden();
  expect(fs.readFileSync(path.join(workspacePath, 'cogs', 'worker.py'), 'utf8'))
    .toContain('UNSAVED_BUFFER_RAN');
});

test('hosted Python exceptions retain workspace traceback details and open the failing line', async ({ page }) => {
  const workspaceName = `hosted-error-${process.pid}-${Date.now()}`;
  workspacePath = path.join(__dirname, '..', '..', 'bots', workspaceName);
  fs.mkdirSync(workspacePath, { recursive: true });
  const entrySource = [
    'import discord',
    'from discord.ext import commands',
    'bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())',
    '@bot.event',
    'async def on_ready():',
    '    from cogs.worker import fail_from_workspace',
    '    fail_from_workspace()',
    'async def main():',
    '    async with bot:',
    '        await bot.start("offline-simulated-token")',
    '',
  ].join(String.fromCharCode(10));
  const workerSource = [
    'def fail_from_workspace():',
    '    raise LookupError("nested worker exploded")',
    '',
  ].join(String.fromCharCode(10));
  fs.mkdirSync(path.join(workspacePath, 'cogs'));
  fs.writeFileSync(path.join(workspacePath, 'bot.py'), entrySource);
  fs.writeFileSync(path.join(workspacePath, 'cogs', 'worker.py'), workerSource);

  await page.evaluate(() => loadWorkspaces());
  await page.locator('#workspace-select').selectOption(workspaceName);
  await page.locator('#btn-connect').click();
  await expect(page.locator('#editor-filename')).toHaveText('bot.py');
  const projectRun = page.waitForResponse((response) =>
    response.url().includes('/api/session/') && response.url().endsWith('/project')
      && response.request().method() === 'POST');
  await page.locator('#btn-run').click();
  const runData = await (await projectRun).json();
  expect(runData.ok).toBe(true);
  expect(runData.last_run).toMatchObject({
    ok: false,
    exception: {
      type: 'LookupError', message: 'nested worker exploded',
      file: 'cogs/worker.py', line: 2, workspace: workspaceName,
    },
  });
  await expect(page.locator('#run-stats')).toContainText('hosted bot error');
  expect(runData.last_run.exception.traceback).toContain(`File "${workspaceName}/cogs/worker.py", line 2`);
  const stateData = await page.evaluate(async () =>
    (await fetch(`/api/session/${SID}/state`)).json());
  expect(stateData.last_run).toEqual(runData.last_run);

  const problems = page.locator('#problems');
  await expect(problems).toContainText('Python · 1 problem');
  await expect(problems).toContainText('LookupError: nested worker exploded');
  await expect(problems).toContainText(`${workspaceName}/cogs/worker.py:2`);
  const problem = problems.locator('.problem-entry');
  await expect(problems.locator('.problem-trace')).toBeVisible();
  await problems.locator('.problem-trace summary').click();
  await expect(problems.locator('.problem-trace pre')).toContainText('nested worker exploded');

  await problem.click();
  await expect(page.locator('#editor-filename')).toHaveText('cogs/worker.py');
  await expect(page.locator('#code')).toHaveValue(workerSource);
  await expect(page.locator('#workspace-file-select')).toHaveValue('cogs/worker.py');
  await expect.poll(() => page.locator('#code').evaluate((editor) => editor.selectionStart))
    .toBe(workerSource.split(String.fromCharCode(10))[0].length + 1);
  expect(fs.readFileSync(path.join(workspacePath, 'cogs', 'worker.py'), 'utf8')).toBe(workerSource);
});

test('late hosted Python main errors synchronize into the open browser', async ({ page }) => {
  const workspaceName = `hosted-late-error-${process.pid}-${Date.now()}`;
  workspacePath = path.join(__dirname, '..', '..', 'bots', workspaceName);
  fs.mkdirSync(workspacePath, { recursive: true });
  fs.writeFileSync(path.join(workspacePath, 'main.py'), [
    'import asyncio',
    'async def main():',
    '    await asyncio.sleep(1.0)',
    '    raise RuntimeError("late main exploded")',
    '',
  ].join(String.fromCharCode(10)));

  await page.evaluate(() => loadWorkspaces());
  await page.locator('#workspace-select').selectOption(workspaceName);
  await page.locator('#btn-connect').click();
  const projectRun = page.waitForResponse((response) =>
    response.url().includes('/api/session/') && response.url().endsWith('/project')
      && response.request().method() === 'POST');
  await page.locator('#btn-run').click();
  const runData = await (await projectRun).json();
  expect(runData.ok).toBe(true);
  expect(runData.last_run).toBeNull();

  await expect.poll(() => page.evaluate(() => lastState?.last_run?.exception?.message), {
    timeout: 4000,
  }).toBe('late main exploded');
  await page.locator('[data-inspector="problems"]').click();
  await expect(page.locator('#problems')).toContainText('RuntimeError: late main exploded');
  await expect(page.locator('#problems')).toContainText(`${workspaceName}/main.py:4`);
});

test('hosted Python import failures preserve HTTP 500 diagnostics in the UI', async ({ page }) => {
  const workspaceName = `hosted-import-error-${process.pid}-${Date.now()}`;
  workspacePath = path.join(__dirname, '..', '..', 'bots', workspaceName);
  fs.mkdirSync(workspacePath, { recursive: true });
  const entrySource = [
    'import hosted_missing_dependency_for_debug_regression',
    '',
  ].join(String.fromCharCode(10));
  fs.writeFileSync(path.join(workspacePath, 'bot.py'), entrySource);

  await page.evaluate(() => loadWorkspaces());
  await page.locator('#workspace-select').selectOption(workspaceName);
  await page.locator('#btn-connect').click();
  await expect(page.locator('#editor-filename')).toHaveText('bot.py');
  const projectRun = page.waitForResponse((response) =>
    response.url().includes('/api/session/') && response.url().endsWith('/project')
      && response.request().method() === 'POST');
  await page.locator('#btn-run').click();
  const response = await projectRun;
  const runData = await response.json();
  // Report the payload on failure: the mode and error explain why a run that
  // should have been a 500 was not.
  expect(response.status(), `status ${response.status()} body ${JSON.stringify(runData)}`).toBe(500);
  expect(runData.ok).toBe(false);
  expect(runData.error).toContain('ModuleNotFoundError');
  expect(runData.last_run).toMatchObject({
    ok: false,
    exception: {
      type: 'ModuleNotFoundError',
      message: "No module named 'hosted_missing_dependency_for_debug_regression'",
      file: 'bot.py', line: 1, workspace: workspaceName,
    },
  });
  expect(runData.last_run.exception.traceback).toContain(
    `File "${workspaceName}/bot.py", line 1`);
  const stateData = await page.evaluate(async () =>
    (await fetch(`/api/session/${SID}/state`)).json());
  expect(stateData.last_run).toEqual(runData.last_run);

  await expect(page.locator('#run-stats')).toContainText('hosted bot startup failed');
  const problems = page.locator('#problems');
  const problem = problems.locator('.problem-entry');
  await expect(problems).toContainText('Python · 1 problem');
  await expect(problems).toContainText(
    "ModuleNotFoundError: No module named 'hosted_missing_dependency_for_debug_regression'");
  await expect(problems).toContainText(`${workspaceName}/bot.py:1`);
  await expect(problems.locator('.problem-trace')).toBeVisible();
  await problems.locator('.problem-trace summary').click();
  await expect(problems.locator('.problem-trace pre')).toContainText(
    'hosted_missing_dependency_for_debug_regression');
});

test('legacy drafts are quarantined until the user chooses standalone migration or discard', async ({ page }) => {
  const legacyDraft = ['async def main():', '    await send("LEGACY_WORKSPACE_EDIT")', ''].join(String.fromCharCode(10));
  const migrationPage = await openLegacyDraft(page, legacyDraft);
  const notice = migrationPage.locator('#legacy-draft-notice');
  await expect(notice).toBeVisible();
  await expect(migrationPage.locator('#legacy-draft-message')).toContainText('origin is unknown');
  await expect(migrationPage.locator('#code')).toHaveValue(legacyDraft);
  await expect(migrationPage.locator('#code')).toHaveJSProperty('readOnly', true);
  await expect(migrationPage.locator('#btn-run')).toBeDisabled();
  await expect(migrationPage.locator('#btn-save')).toBeDisabled();
  await expect(migrationPage.locator('#btn-connect')).toBeDisabled();
  expect(await migrationPage.evaluate(() => ({
    code: localStorage.getItem('pg-code'),
    context: localStorage.getItem('pg-code-context'),
    workspace: localStorage.getItem('pg-workspace-state'),
  }))).toEqual({ code: legacyDraft, context: null, workspace: null });

  await migrationPage.locator('#legacy-draft-standalone').click();
  await expect(notice).toBeHidden();
  await expect(migrationPage.locator('#code')).toHaveValue(legacyDraft);
  await expect(migrationPage.locator('#code')).toHaveJSProperty('readOnly', false);
  await expect(migrationPage.locator('#btn-run')).toBeEnabled();
  await expect(migrationPage.locator('#btn-save')).toBeEnabled();
  expect(await migrationPage.evaluate(() => ({
    code: localStorage.getItem('pg-code'),
    context: localStorage.getItem('pg-code-context'),
    workspace: localStorage.getItem('pg-workspace-state'),
  }))).toEqual({ code: legacyDraft, context: 'standalone', workspace: null });

  await migrationPage.reload();
  await expect(migrationPage.locator('#legacy-draft-notice')).toBeHidden();
  await expect(migrationPage.locator('#code')).toHaveValue(legacyDraft);
  await expect(migrationPage.locator('#btn-run')).toBeEnabled();
  await expect(migrationPage.locator('#btn-run')).toHaveAttribute('title', 'Run the editor buffer as a playground script');
  await migrationPage.locator('#btn-run').click();
  await expect(migrationPage.locator('#timeline .msg').filter({ hasText: 'LEGACY_WORKSPACE_EDIT' }).last()).toBeVisible();
});

test('ordinary standalone scripts remain usable while the workspace list is delayed', async ({ page }) => {
  const source = ['async def main():', '    await send("NORMAL_LAUNCH_DURING_DISCOVERY")', ''].join(String.fromCharCode(10));
  const editedSource = ['async def main():', '    await send("EDITED_BUFFER_DURING_DISCOVERY")', ''].join(String.fromCharCode(10));
  const scriptName = `delayed-startup-${process.pid}-${Date.now()}`;
  const scriptUrl = `/api/scripts/${encodeURIComponent(scriptName)}`;
  const { page: appPage, workspaceGate } = await openPageWithStorage(page, {
    'pg-code': source,
    'pg-code-context': 'standalone',
  }, true);
  try {
    await workspaceGate.requested;
    await expect(appPage.locator('#workspace-select')).toBeDisabled();
    await expect(appPage.locator('#btn-connect')).toBeDisabled();
    await expect(appPage.locator('#code')).toHaveJSProperty('readOnly', false);
    await expect(appPage.locator('#btn-run')).toBeEnabled();
    await expect(appPage.locator('#btn-save')).toBeEnabled();

    await appPage.locator('#code').fill('');
    await appPage.locator('#btn-run').click();
    await expect(appPage.locator('#run-stats')).toContainText('Nothing to run');
    await expect(appPage.locator('#btn-run')).toBeEnabled();

    await appPage.locator('#code').fill(editedSource);
    await expect(appPage.locator('#code')).toHaveValue(editedSource);
    await expect(appPage.locator('#btn-run')).toBeEnabled();
    await expect(appPage.locator('#btn-save')).toBeEnabled();
    await appPage.locator('#btn-run').click();
    await expect(appPage.locator('#timeline .msg').filter({ hasText: 'EDITED_BUFFER_DURING_DISCOVERY' }).last()).toBeVisible();

    let promptDetails;
    let promptAccepted;
    appPage.once('dialog', (dialog) => {
      promptDetails = { type: dialog.type(), message: dialog.message() };
      promptAccepted = dialog.accept(scriptName);
    });
    const saveResponse = appPage.waitForResponse((response) =>
      response.url().endsWith('/api/scripts') && response.request().method() === 'POST');
    await appPage.locator('#btn-save').click();
    await promptAccepted;
    expect(promptDetails).toEqual({ type: 'prompt', message: 'Save script as:' });
    expect((await saveResponse).ok()).toBe(true);
    await expect(appPage.locator('#run-stats')).toContainText(`saved ${scriptName}`);
    const saved = await appPage.request.get(scriptUrl).then((response) => response.json());
    expect(saved.code).toBe(editedSource);

    await expect(appPage.locator('#code')).toHaveJSProperty('readOnly', false);
    await expect(appPage.locator('#btn-run')).toBeEnabled();
    await expect(appPage.locator('#btn-save')).toBeEnabled();
    await expect(appPage.locator('#workspace-select')).toBeDisabled();
    await expect(appPage.locator('#btn-connect')).toBeDisabled();
  } finally {
    workspaceGate.release();
    const deleted = await appPage.request.delete(scriptUrl);
    expect(deleted.ok()).toBe(true);
    expect((await appPage.request.get(scriptUrl)).status()).toBe(404);
  }
  await expect(appPage.locator('#workspace-select')).toBeEnabled();
  await expect(appPage.locator('#btn-connect')).toBeEnabled();
});

test('a transient workspace-list failure preserves saved workspace recovery state', async ({ page }) => {
  const name = `retry-restore-${process.pid}-${Date.now()}`;
  workspacePath = fs.mkdtempSync(path.join(__dirname, '..', '..', 'bots', `${name}-`));
  const workspaceName = path.basename(workspacePath);
  const diskSource = ['async def main():', '    await send("SAVED_AFTER_RETRY")', ''].join(String.fromCharCode(10));
  const unsavedBuffer = ['async def main():', '    await send("BUFFER_SURVIVES_RETRY")', ''].join(String.fromCharCode(10));
  fs.writeFileSync(path.join(workspacePath, 'bot.py'), diskSource);
  const savedState = {
    version: 1,
    workspace: workspaceName,
    filename: 'bot.py',
    baseline: diskSource,
  };
  const { page: appPage } = await openPageWithStorage(page, {
    'pg-code': unsavedBuffer,
    'pg-code-context': 'workspace',
    'pg-workspace-state': JSON.stringify(savedState),
  });
  await appPage.route('**/api/workspaces', (route) =>
    route.fulfill({ status: 503, body: 'temporarily unavailable' }));
  await appPage.reload();
  await expect(appPage.locator('#run-stats')).toContainText('workspace recovery failed; saved buffer preserved');
  await expect(appPage.locator('#code')).toHaveValue(unsavedBuffer);
  await expect(appPage.locator('#code')).toHaveJSProperty('readOnly', true);
  await expect(appPage.locator('#btn-run')).toBeDisabled();
  await expect(appPage.locator('#btn-save')).toBeDisabled();
  const recovered = await appPage.evaluate(() => ({
    code: localStorage.getItem('pg-code'),
    context: localStorage.getItem('pg-code-context'),
    workspace: JSON.parse(localStorage.getItem('pg-workspace-state')),
  }));
  expect(recovered).toEqual({ code: unsavedBuffer, context: 'workspace', workspace: savedState });

  await appPage.unroute('**/api/workspaces');
  await appPage.reload();
  await expect(appPage.locator('#workspace-select')).toHaveValue(workspaceName);
  await expect(appPage.locator('#code')).toHaveValue(unsavedBuffer);
  await expect(appPage.locator('#workspace-dirty')).toBeVisible();
  await expect(appPage.locator('#btn-run')).toBeEnabled();
});

test('a transient workspace-file failure preserves saved recovery state for retry', async ({ page }) => {
  const diskSource = ['async def main():', '    await send("SAVED_WORKSPACE_DISK_SOURCE")', ''].join(String.fromCharCode(10));
  const unsavedBuffer = ['async def main():', '    await send("UNSAVED_WORKSPACE_BUFFER")', ''].join(String.fromCharCode(10));
  const savedState = { version: 1, workspace: 'showcase_bot', filename: 'bot.py', baseline: diskSource };
  const { page: appPage } = await openPageWithStorage(page, {
    'pg-code': unsavedBuffer,
    'pg-code-context': 'workspace',
    'pg-workspace-state': JSON.stringify(savedState),
  });
  let failFileRead = true;
  await appPage.route('**/api/workspaces/showcase_bot/files/bot.py', (route) => {
    if (failFileRead) return route.fulfill({ status: 503, body: 'temporarily unavailable' });
    return route.continue();
  });

  await appPage.reload();
  await expect(appPage.locator('#run-stats')).toContainText('workspace recovery failed; saved buffer preserved');
  await expect(appPage.locator('#code')).toHaveValue(unsavedBuffer);
  await expect(appPage.locator('#code')).toHaveJSProperty('readOnly', true);
  await expect(appPage.locator('#btn-run')).toBeDisabled();
  await expect(appPage.locator('#btn-save')).toBeDisabled();
  const recovered = await appPage.evaluate(() => ({
    code: localStorage.getItem('pg-code'),
    context: localStorage.getItem('pg-code-context'),
    workspace: JSON.parse(localStorage.getItem('pg-workspace-state')),
  }));
  expect(recovered).toEqual({ code: unsavedBuffer, context: 'workspace', workspace: savedState });

  failFileRead = false;
  await appPage.reload();
  await expect(appPage.locator('#workspace-select')).toHaveValue('showcase_bot');
  await expect(appPage.locator('#code')).toHaveValue(unsavedBuffer);
  await expect(appPage.locator('#workspace-dirty')).toBeVisible();
  await expect(appPage.locator('#btn-run')).toBeEnabled();
});

test('workspace discovery does not re-enable an in-flight standalone run', async ({ page }) => {
  const source = ['async def main():', '    await send("RUN_FINISHES_AFTER_WORKSPACE_DISCOVERY")', ''].join(String.fromCharCode(10));
  const { page: appPage, workspaceGate } = await openPageWithStorage(page, {
    'pg-code': source,
    'pg-code-context': 'standalone',
  }, true);
  let runStarted;
  let releaseRun;
  const runRequestStarted = new Promise((resolve) => { runStarted = resolve; });
  const runGate = new Promise((resolve) => { releaseRun = resolve; });
  await appPage.route('**/api/session/*/run', async (route) => {
    runStarted();
    await runGate;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, ms: 1 }),
    });
  });
  try {
    await workspaceGate.requested;
    const pendingRun = appPage.locator('#btn-run').click();
    await runRequestStarted;
    await expect(appPage.locator('#btn-run')).toBeDisabled();

    workspaceGate.release();
    await expect(appPage.locator('#workspace-select')).toBeEnabled();
    await expect(appPage.locator('#btn-run')).toBeDisabled();
    await expect(appPage.locator('#code')).toHaveJSProperty('readOnly', false);

    releaseRun();
    await pendingRun;
    await expect(appPage.locator('#btn-run')).toBeEnabled();
  } finally {
    workspaceGate.release();
    releaseRun();
  }
});

test('a failed workspace-list request leaves standalone scripts runnable', async ({ page }) => {
  const source = ['async def main():', '    await send("RUN_AFTER_WORKSPACE_DISCOVERY_FAILURE")', ''].join(String.fromCharCode(10));
  const { page: appPage } = await openPageWithStorage(page, {
    'pg-code': source,
    'pg-code-context': 'standalone',
  });
  await appPage.evaluate((code) => {
    localStorage.setItem('pg-code', code);
    localStorage.setItem('pg-code-context', 'standalone');
  }, source);
  let workspaceRequestSeen = false;
  await appPage.route('**/api/workspaces', async (route) => {
    workspaceRequestSeen = true;
    await route.fulfill({ status: 503, body: 'temporarily unavailable' });
  });
  await appPage.reload();
  await expect.poll(() => workspaceRequestSeen).toBe(true);
  await expect(appPage.locator('#run-stats')).toContainText('Could not load workspaces.');
  await expect(appPage.locator('#workspace-select')).toBeDisabled();
  await expect(appPage.locator('#btn-connect')).toBeDisabled();
  await expect(appPage.locator('#code')).toHaveJSProperty('readOnly', false);
  await expect(appPage.locator('#btn-run')).toBeEnabled();
  await expect(appPage.locator('#btn-save')).toBeEnabled();

  await appPage.locator('#btn-run').click();
  await expect(appPage.locator('#timeline .msg').filter({ hasText: 'RUN_AFTER_WORKSPACE_DISCOVERY_FAILURE' }).last()).toBeVisible();
});

test('an older boot script-list response cannot undo a save during workspace discovery', async ({ page }) => {
  const source = ['async def main():', '    await send("SAVED_BEFORE_WORKSPACE_DISCOVERY")', ''].join(String.fromCharCode(10));
  const scriptName = `boot-race-${process.pid}-${Date.now()}`;
  const scriptUrl = `/api/scripts/${encodeURIComponent(scriptName)}`;
  const { page: appPage, workspaceGate, scriptListGate } = await openPageWithStorage(page, {
    'pg-code': source,
    'pg-code-context': 'standalone',
  }, true, true);
  try {
    await Promise.all([workspaceGate.requested, scriptListGate.requested]);
    await expect(appPage.locator('#code')).toHaveJSProperty('readOnly', false);
    await expect(appPage.locator('#btn-save')).toBeEnabled();
    await expect(appPage.locator('#workspace-select')).toBeDisabled();

    await appPage.locator('#code').fill(source);
    let promptAccepted;
    appPage.once('dialog', (dialog) => { promptAccepted = dialog.accept(scriptName); });
    const saveResponse = appPage.waitForResponse((response) =>
      response.url().endsWith('/api/scripts') && response.request().method() === 'POST');
    await appPage.locator('#btn-save').click();
    await promptAccepted;
    expect((await saveResponse).ok()).toBe(true);
    await expect(appPage.locator('#run-stats')).toContainText(`saved ${scriptName}`);
    await expect(appPage.locator('#script-select')).toHaveValue(scriptName);

    const staleListResponse = appPage.waitForResponse((response) =>
      response.url().endsWith('/api/scripts') && response.request().method() === 'GET');
    scriptListGate.release();
    await staleListResponse;
    await expect(appPage.locator('#script-select')).toHaveValue(scriptName);
    await expect(appPage.locator(`#script-select option[value="${scriptName}"]`)).toHaveCount(1);
  } finally {
    workspaceGate.release();
    scriptListGate.release();
    const deleted = await appPage.request.delete(scriptUrl);
    expect(deleted.ok()).toBe(true);
    expect((await appPage.request.get(scriptUrl)).status()).toBe(404);
  }
});

test('ambiguous migration stays quarantined until workspace discovery finishes', async ({ page }) => {

  const draft = 'await send("AMBIGUOUS_DRAFT")';
  const { page: appPage, workspaceGate } = await openPageWithStorage(page, { 'pg-code': draft }, true);
  try {
    await workspaceGate.requested;
    await expect(appPage.locator('#legacy-draft-notice')).toBeHidden();
    await expect(appPage.locator('#code')).toHaveValue(draft);
    await expect(appPage.locator('#code')).toHaveJSProperty('readOnly', true);
    await expect(appPage.locator('#btn-run')).toBeDisabled();
    await expect(appPage.locator('#btn-save')).toBeDisabled();
    await expect(appPage.locator('#btn-connect')).toBeDisabled();
  } finally {
    workspaceGate.release();
  }
  await expect(appPage.locator('#legacy-draft-notice')).toBeVisible();
  await expect(appPage.locator('#btn-run')).toBeDisabled();
  await appPage.locator('#legacy-draft-standalone').click();
  await expect(appPage.locator('#code')).toHaveJSProperty('readOnly', false);
  await expect(appPage.locator('#btn-run')).toBeEnabled();
  await expect(appPage.locator('#btn-run')).toHaveAttribute('title', 'Run the editor buffer as a playground script');
});

test('workspace-marked orphan draft stays quarantined until workspace discovery finishes', async ({ page }) => {
  const draft = 'await send("ORPHANED_WORKSPACE_DRAFT")';
  const { page: appPage, workspaceGate } = await openPageWithStorage(page, {
    'pg-code': draft,
    'pg-code-context': 'workspace',
  }, true);
  try {
    await workspaceGate.requested;
    await expect(appPage.locator('#workspace-select')).toBeDisabled();
    await expect(appPage.locator('#btn-connect')).toBeDisabled();
    await expect(appPage.locator('#legacy-draft-notice')).toBeHidden();
    await expect(appPage.locator('#code')).toHaveValue(draft);
    await expect(appPage.locator('#code')).toHaveJSProperty('readOnly', true);
    await expect(appPage.locator('#btn-run')).toBeDisabled();
    await expect(appPage.locator('#btn-save')).toBeDisabled();
    expect(await appPage.evaluate(() => ({
      code: localStorage.getItem('pg-code'),
      context: localStorage.getItem('pg-code-context'),
      workspace: localStorage.getItem('pg-workspace-state'),
    }))).toEqual({ code: draft, context: 'workspace', workspace: null });
  } finally {
    workspaceGate.release();
  }
  await expect(appPage.locator('#legacy-draft-notice')).toBeVisible();
  await expect(appPage.locator('#legacy-draft-message')).toContainText('last saved while a bot folder was connected');
  await appPage.locator('#legacy-draft-standalone').click();
  await expect(appPage.locator('#code')).toHaveJSProperty('readOnly', false);
  await expect(appPage.locator('#btn-run')).toBeEnabled();
});

test('saved workspace recovery stays locked until its file context is restored', async ({ page }) => {
  const name = `delayed-restore-${process.pid}-${Date.now()}`;
  workspacePath = fs.mkdtempSync(path.join(__dirname, '..', '..', 'bots', `${name}-`));
  const workspaceName = path.basename(workspacePath);
  const diskSource = ['async def main():', '    await send("SAVED_WORKSPACE_SOURCE")', ''].join(String.fromCharCode(10));
  const unsavedBuffer = ['async def main():', '    await send("RESTORED_WORKSPACE_BUFFER")', ''].join(String.fromCharCode(10));
  fs.writeFileSync(path.join(workspacePath, 'bot.py'), diskSource);
  const { page: appPage, workspaceGate } = await openPageWithStorage(page, {
    'pg-code': unsavedBuffer,
    'pg-code-context': 'workspace',
    'pg-workspace-state': JSON.stringify({ version: 1, workspace: workspaceName, filename: 'bot.py', baseline: diskSource }),
  }, true);
  let projectRunSeen = false;
  await appPage.route('**/api/session/*/project', async (route) => {
    projectRunSeen = true;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, mode: 'project', status: { bot: workspaceName, cogs: [], commands: [] } }),
    });
  });
  try {
    await workspaceGate.requested;
    await expect(appPage.locator('#code')).toHaveValue(unsavedBuffer);
    await expect(appPage.locator('#code')).toHaveJSProperty('readOnly', true);
    await expect(appPage.locator('#btn-run')).toBeDisabled();
    await expect(appPage.locator('#btn-save')).toBeDisabled();
    await expect(appPage.locator('#btn-connect')).toBeDisabled();
  } finally {
    workspaceGate.release();
  }

  await expect(appPage.locator('#workspace-select')).toHaveValue(workspaceName);
  await expect(appPage.locator('#editor-filename')).toHaveText('bot.py');
  await expect(appPage.locator('#code')).toHaveValue(unsavedBuffer);
  await expect(appPage.locator('#workspace-dirty')).toBeVisible();
  await expect(appPage.locator('#btn-run')).toBeEnabled();
  await expect(appPage.locator('#btn-run')).toHaveAttribute('title', /saved on disk/);
  await expect(appPage.locator('#btn-save')).toHaveText('Save file');
  await appPage.locator('#btn-run').click();
  await expect.poll(() => projectRunSeen).toBe(true);
});

test('migrated legacy draft saves to the library and restores its exact source', async ({ page }) => {
  const source = ['async def main():', '    await send("MIGRATED_LIBRARY_SOURCE")', ''].join(String.fromCharCode(10));
  const scriptName = `legacy-migration-${process.pid}-${Date.now()}`;
  const migrationPage = await openLegacyDraft(page, source);
  try {
    await expect(migrationPage.locator('#legacy-draft-notice')).toBeVisible();
    await migrationPage.locator('#legacy-draft-standalone').click();
    await expect(migrationPage.locator('#btn-save')).toBeEnabled();
    migrationPage.once('dialog', (dialog) => dialog.accept(scriptName));
    await migrationPage.locator('#btn-save').click();
    await expect(migrationPage.locator('#run-stats')).toContainText(`saved ${scriptName}`);

    await migrationPage.reload();
    await expect(migrationPage.locator('#act-as option')).toHaveCount(4);
    await migrationPage.locator('#script-select').selectOption(scriptName);
    await expect(migrationPage.locator('#code')).toHaveValue(source);
    const saved = await migrationPage.request.get(`/api/scripts/${encodeURIComponent(scriptName)}`).then((response) => response.json());
    expect(saved.code).toBe(source);
  } finally {
    await migrationPage.request.delete(`/api/scripts/${encodeURIComponent(scriptName)}`);
  }
});

test('workspace-marked legacy draft requires a choice and can be safely discarded', async ({ page }) => {
  const legacyDraft = 'await send("OLD_CONNECTED_WORKSPACE_EDIT")';
  const migrationPage = await openLegacyDraft(page, legacyDraft, 'workspace');
  await expect(migrationPage.locator('#legacy-draft-notice')).toBeVisible();
  await expect(migrationPage.locator('#legacy-draft-message')).toContainText('last saved while a bot folder was connected');
  await expect(migrationPage.locator('#btn-run')).toBeDisabled();
  await migrationPage.locator('#legacy-draft-discard').click();

  await expect(migrationPage.locator('#legacy-draft-notice')).toBeHidden();
  await expect(migrationPage.locator('#code')).not.toHaveValue(legacyDraft);
  await expect(migrationPage.locator('#btn-run')).toBeEnabled();
  const discardedEditorValue = await migrationPage.locator('#code').inputValue();
  expect(await migrationPage.evaluate(() => ({
    context: localStorage.getItem('pg-code-context'),
    workspace: localStorage.getItem('pg-workspace-state'),
    code: localStorage.getItem('pg-code'),
  }))).toEqual({ code: discardedEditorValue, context: 'standalone', workspace: null });

  await migrationPage.reload();
  await expect(migrationPage.locator('#legacy-draft-notice')).toBeHidden();
  await expect(migrationPage.locator('#code')).not.toHaveValue(legacyDraft);
});

test('known standalone drafts keep their existing standalone behavior', async ({ page }) => {
  const standaloneDraft = 'await send("KNOWN_STANDALONE_DRAFT")';
  const migrationPage = await openLegacyDraft(page, standaloneDraft, 'standalone');
  await expect(migrationPage.locator('#legacy-draft-notice')).toBeHidden();
  await expect(migrationPage.locator('#code')).toHaveValue(standaloneDraft);
  await expect(migrationPage.locator('#btn-run')).toBeEnabled();
  await expect(migrationPage.locator('#btn-save')).toBeEnabled();
});

test('switching simulated users updates the active actor UI', async ({ page }) => {
  const actor = page.locator('#act-as');

  await actor.selectOption('111111111111111111');
  await expect(page.locator('#me-name')).toHaveText('Alice');
  await expect(page.locator('#sim-context')).toHaveText('LOCAL · ACTING AS ALICE');
  await expect(page.locator('.member-row.active .member-name')).toHaveText('Alice');

  await actor.selectOption('222222222222222222');
  await expect(page.locator('#me-name')).toHaveText('Bob');
  await expect(page.locator('#sim-context')).toHaveText('LOCAL · ACTING AS BOB');
  await expect(page.locator('.member-row.active .member-name')).toHaveText('Bob');
});

test("the active custom user's profile can be edited and persists across actor switches", async ({ page }) => {
  await page.getByRole('button', { name: '＋ Add user' }).click();
  await page.locator('#settings-form [name="username"]').fill('Jamie');
  await page.locator('#settings-form [name="display_name"]').fill('Jamie Original');
  await page.locator('#settings-form [name="bio"]').fill('Before edit');
  await page.getByRole('button', { name: 'Add user', exact: true }).click();

  await expect(page.locator('#me-name')).toHaveText('Jamie Original');
  await expect(page.locator('#act-as option')).toHaveCount(5);
  await expect(page.locator('#edit-profile')).toBeEnabled();

  await page.getByRole('button', { name: '✎ Edit profile' }).click();
  await expect(page.locator('#profile-settings')).toBeVisible();
  await expect(page.locator('#settings-form [name="username"]')).toHaveValue('Jamie');
  await expect(page.locator('#settings-form [name="username"]')).toHaveJSProperty('readOnly', true);
  await page.locator('#settings-form [name="display_name"]').fill('Jamie Revised');
  await page.locator('#settings-form [name="bio"]').fill('Updated in the browser');
  await page.locator('#settings-form [name="status"]').selectOption('idle');
  await page.getByRole('button', { name: 'Save profile' }).click();

  await expect(page.locator('#me-name')).toHaveText('Jamie Revised');
  await expect(page.locator('#settings-overlay')).not.toHaveClass(/open/);

  await page.locator('#act-as').selectOption('111111111111111111');
  await expect(page.locator('#me-name')).toHaveText('Alice');
  await page.locator('#act-as').selectOption({ label: 'Jamie Revised' });
  await expect(page.locator('#me-name')).toHaveText('Jamie Revised');

  await page.locator('.member-row').filter({ hasText: 'Jamie Revised' }).click();
  await expect(page.locator('#profile-name')).toHaveText('Jamie Revised');
  await expect(page.locator('#profile-user')).toHaveText('@Jamie');
  await expect(page.locator('#profile-status')).toHaveText('idle');
  await expect(page.locator('#profile-bio')).toHaveText('Updated in the browser');
});

test('messages can be reacted to with the emoji picker and toggled off', async ({ page }) => {
  await page.getByRole('button', { name: '▶ Run' }).click();
  await page.locator('#timeline .msg').first().waitFor();
  await page.locator('#composer-input').fill('reaction bait');
  await page.keyboard.press('Enter');
  const target = page.locator('.msg').filter({ hasText: 'reaction bait' }).last();
  await expect(target).toBeVisible();

  await target.locator('button[title="Add reaction"]').click();
  await expect(page.locator('#emoji-picker')).toBeVisible();
  await page.locator('#emoji-picker .emoji-cell', { hasText: '🔥' }).first().click();

  const pill = target.locator('.reaction').filter({ hasText: '🔥' });
  await expect(pill).toHaveText(/1/);
  await expect(pill).toHaveClass(/me/);

  await pill.click();
  await expect(target.locator('.reaction')).toHaveCount(0);
});

test('the user settings overlay manages sounds and appearance', async ({ page }) => {
  await page.locator('#open-user-settings').click();
  await expect(page.locator('#user-settings-overlay')).toHaveClass(/open/);

  await expect(page.locator('#us-display')).toHaveText('You');
  await expect(page.locator('#us-username')).toHaveText('@You');
  await expect(page.locator('#us-act-as option')).toHaveCount(4);

  await page.locator('.settings-nav-item[data-pane="appearance"]').click();
  await page.locator('#us-message-display').selectOption('compact');
  await expect(page.locator('#message-display')).toHaveValue('compact');
  await page.locator('#us-density').selectOption('compact');
  await expect(page.locator('#density-select')).toHaveValue('compact');

  await page.locator('.settings-nav-item[data-pane="sound"]').click();
  await expect(page.locator('#us-sound-switch')).toHaveAttribute('aria-checked', 'true');
  await page.locator('#us-sound-switch').click();
  await expect(page.locator('#us-sound-switch')).toHaveAttribute('aria-checked', 'false');
  await expect(page.locator('#sound-toggle')).toHaveText('🔇');

  await page.keyboard.press('Escape');
  await expect(page.locator('#user-settings-overlay')).not.toHaveClass(/open/);
});

test('simulated voice connects, mutes, and leaves from the sidebar panel', async ({ page }) => {
  await page.locator('#voice-panel').waitFor();
  await page.locator('#voice-join-row').click();
  await expect(page.locator('#voice-controls')).toBeVisible();
  await expect(page.locator('#voice-members .voice-member')).toContainText('You');
  await expect(page.locator('#voice-channel-label')).toHaveText('playground');

  await page.locator('#voice-deafen').click();
  await expect(page.locator('#voice-deafen')).toHaveClass(/lit/);
  await page.locator('#voice-mute').click(); // undeafen -> unmute + speaking ring
  await expect(page.locator('#voice-mute')).not.toHaveClass(/lit/);
  await expect(page.locator('#voice-members .voice-member.speaking')).toBeVisible();

  await page.locator('#voice-leave').click();
  await expect(page.locator('#voice-controls')).toBeHidden();
  await expect(page.locator('#voice-members .voice-member')).toHaveCount(0);
});

test('uploads stage in the tray, reach the bot, and render as attachments', async ({ page }) => {
  await page.getByRole('button', { name: '▶ Run' }).click();
  await page.locator('#timeline .msg').first().waitFor();

  await page.locator('#file-input').setInputFiles({
    name: 'pic.png', mimeType: 'image/png', buffer: Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==', 'base64'),
  });
  await expect(page.locator('.upload-chip')).toHaveText(/pic\.png/);

  await page.locator('#composer-input').fill('here is my upload');
  await page.keyboard.press('Enter');
  const sent = page.locator('.msg').filter({ hasText: 'here is my upload' }).last();
  await expect(sent.locator('.attach-thumb')).toBeVisible();
  await expect(sent.locator('.attach-name')).toHaveText('pic.png');
  await expect(page.locator('.upload-chip')).toHaveCount(0);
});

test('Ctrl+K quick switcher jumps between channels and users', async ({ page }) => {
  await page.keyboard.press('Control+k');
  await expect(page.locator('#quick-switcher')).toHaveClass(/open/);
  await page.locator('#qs-input').fill('play');
  await page.keyboard.press('Enter');
  await expect(page.locator('#quick-switcher')).not.toHaveClass(/open/);

  await page.keyboard.press('Control+k');
  await page.locator('#qs-input').fill('@alice');
  await page.keyboard.press('Enter');
  await expect(page.locator('#me-name')).toHaveText('Alice');
});

test('channel creation and member moderation menus work against the mock', async ({ page }) => {
  await page.locator('.new-channel-btn').click();
  await page.locator('#nc-name').fill('mod lounge');
  await page.locator('#nc-topic').fill('mods only');
  await page.locator('#nc-create').click();
  await expect(page.locator('#chat-head-name')).toHaveText('# mod-lounge');
  await expect(page.locator('.channel-row.active')).toContainText('mod-lounge');

  await page.locator('.member-row').filter({ hasText: 'Carol' }).click({ button: 'right' });
  await expect(page.locator('#ctx-menu')).toBeVisible();
  await page.locator('.ctx-item', { hasText: 'Kick' }).click();
  await expect(page.locator('.member-row', { hasText: 'Carol' })).toHaveCount(0);

  await page.locator('.new-channel-btn').click();
  await page.keyboard.press('Escape'); // dialog closes without creating
  await expect(page.locator('#new-channel-overlay')).not.toHaveClass(/open/);
});
