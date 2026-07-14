import { defineConfig, devices } from "@playwright/test";

const localNoProxy = new Set(
  [process.env.NO_PROXY, process.env.no_proxy, "127.0.0.1", "localhost"]
    .filter(Boolean)
    .flatMap((value) => value!.split(","))
    .map((value) => value.trim())
    .filter(Boolean),
);
process.env.NO_PROXY = [...localNoProxy].join(",");
process.env.no_proxy = process.env.NO_PROXY;

export default defineConfig({
  testDir: "./tests",
  outputDir: "../.local/design-qa/pricing-ui/test-results",
  reporter: [["list"], ["json", { outputFile: "../.local/design-qa/pricing-ui/axe-results.json" }]],
  use: { baseURL: "http://127.0.0.1:5174", screenshot: "only-on-failure", trace: "retain-on-failure" },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5174",
    url: "http://127.0.0.1:5174",
    reuseExistingServer: true,
    env: { NO_PROXY: "127.0.0.1,localhost", no_proxy: "127.0.0.1,localhost" },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
