import { defineConfig, devices } from "@playwright/test";

const env = process.env as Record<string, string | undefined>;
const isCi = Boolean(env["CI"]);
const parsePort = (value: string | undefined, fallback: number) => {
  if (!value) {
    return fallback;
  }

  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`PLAYWRIGHT_PORT must be an integer from 1 to 65535, got "${value}".`);
  }

  return parsed;
};
const port = parsePort(env["PLAYWRIGHT_PORT"], 4321);
const host = "127.0.0.1";
const baseURL = `http://${host}:${port}`;
const testSiteDir = ".context/e2e-site";
const prepareSiteCommand = `mkdir -p .context && rm -rf ${testSiteDir} && cp -R site.example ${testSiteDir}`;
const siteEnv = `FAVORITE_PLACES_SITE_DIR=${testSiteDir} PUBLIC_MAP_PROVIDER=leaflet PUBLIC_CHAT_ENABLED=true`;

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: isCi,
  retries: isCi ? 2 : 0,
  workers: isCi ? 1 : undefined,
  reporter: isCi ? "github" : "list",
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  webServer: {
    command: `${prepareSiteCommand} && ${siteEnv} bun run build:data && ${siteEnv} bun run build && ${siteEnv} bun run preview -- --host ${host} --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
