const { defineConfig, devices } = require('@playwright/test');

const port = Number(process.env.SCRIPTPLAYGROUND_TEST_PORT || 8741);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error('SCRIPTPLAYGROUND_TEST_PORT must be a valid TCP port');
}
const baseURL = `http://127.0.0.1:${port}`;

module.exports = defineConfig({
  testDir: './tests/browser',
  fullyParallel: true,
  reporter: 'list',
  use: {
    ...devices['Desktop Chrome'],
    baseURL,
    browserName: 'chromium',
  },
  webServer: {
    command: `python -X utf8 main.py --no-browser --port ${port}`,
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
