import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:4174', trace: 'retain-on-failure' },
  webServer: {
    // index.html is the artefact render_html.py produces; run that first.
    command: 'python3 -m http.server 4174',
    url: 'http://127.0.0.1:4174/index.html',
    reuseExistingServer: true,
    timeout: 20_000,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
