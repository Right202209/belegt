// 无头截图：先 `npm run build && npm run preview`，再 `node scripts/screenshot.mjs`。
// 需要 playwright（不是本包依赖）：可设 PLAYWRIGHT_MODULE=/abs/path/node_modules/playwright/index.mjs
const mod = process.env.PLAYWRIGHT_MODULE ?? 'playwright';
const { chromium } = await import(mod).catch(() => {
  console.error('playwright not available – skipping screenshot');
  process.exit(0);
});
const url = process.env.URL ?? 'http://localhost:4173/#/inbox';
const out = process.env.OUT ?? 'docs/screenshot.png';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
await page.goto(url, { waitUntil: 'networkidle' });
await page.waitForSelector('.group');
// 展开 L1 组并选中 L3 第一条，展示证据侧栏
const l1 = page.locator('#grp-L1');
if (await l1.count()) await l1.click();
const first = page.locator('.group-L3 .row-main').first();
if (await first.count()) await first.click();
await page.waitForSelector('.panel .lineage, .panel .node', { timeout: 5000 }).catch(() => {});
await page.screenshot({ path: out, fullPage: false });
await browser.close();
console.log(`saved ${out}`);
