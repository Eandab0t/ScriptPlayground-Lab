/* Typing in the browser: the indicator is rendered from the simulated world's
 * own typing state (the server prunes expired entries), not from a client-side
 * timer, so an expired indicator disappears on the next poll.
 *
 * These drive the session the page actually booted with, captured from its own
 * POST /api/session response -- the app has no ?sid= binding.
 */
const { test, expect } = require('@playwright/test');

// Same default as playwright.config.js; CI overrides it via the env var.
const PORT = Number(process.env.SCRIPTPLAYGROUND_TEST_PORT || 8741);
const BASE = `http://127.0.0.1:${PORT}`;

let sid;

test.beforeEach(async ({ page }) => {
  const created = page.waitForResponse(
    (r) => r.url().endsWith('/api/session') && r.request().method() === 'POST');
  await page.goto('/');
  await expect(page.locator('#composer-input')).toBeVisible();
  sid = (await (await created).json()).sid;
});

async function startTyping(body = {}) {
  const response = await fetch(`${BASE}/api/session/${sid}/typing`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  expect(response.status).toBe(200);
  return (await response.json()).state;
}

test('a simulated user typing shows a transient indicator in the composer', async ({ page }) => {
  const row = page.locator('#typing-row');
  await expect(row).toBeHidden();

  const state = await startTyping();
  expect(state.typing).toHaveLength(1);
  const who = state.typing[0].name;

  await expect(row).toBeVisible();
  await expect(page.locator('#typing-name')).toHaveText(who);

  // Typing is transient state, not a timeline entry: the indicator never
  // becomes a message.
  const live = await (await fetch(`${BASE}/api/session/${sid}/state`)).json();
  expect(live.messages.filter((m) => (m.content || '').includes('typing'))).toHaveLength(0);
});

test('typing in another channel does not show in this one', async ({ page }) => {
  const row = page.locator('#typing-row');
  await startTyping();
  await expect(row).toBeVisible();

  // A second channel is created through the world, then the user types there.
  const created = await fetch(`${BASE}/api/session/${sid}/channels`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: 'elsewhere' }),
  });
  expect(created.status).toBe(200);
  const otherId = (await created.json()).channel.id;

  await row.evaluate(() => {});  // let the indicator settle before switching
  await page.locator('#channel-list').getByText('elsewhere', { exact: true }).click();
  await expect(page.locator('#typing-row')).toBeHidden();

  // ...and typing there shows there.
  await startTyping({ channel_id: otherId });
  await expect(row).toBeVisible();
});