const fs = require('fs');
const path = require('path');
const Module = require('module');

Module._initPaths();

const { chromium } = require('playwright');

function argValue(name, fallback = '') {
  const index = process.argv.indexOf(name);
  if (index >= 0 && process.argv[index + 1]) return process.argv[index + 1];
  return fallback;
}

const baseUrl = argValue('--base-url', 'http://127.0.0.1:3000').replace(/\/$/, '');
const outDir = path.resolve(argValue('--out', path.join(process.cwd(), 'docs', 'manual_assets', 'screenshots')));
const adminPassword = argValue('--admin-password', process.env.LOGISTICA_TEST_ADMIN_PASSWORD || 'admin123');

fs.mkdirSync(outDir, { recursive: true });

async function launchBrowser() {
  try {
    return await chromium.launch({ headless: true });
  } catch (error) {
    const edgeCandidates = [
      'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
      'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    ];
    const edgePath = edgeCandidates.find((candidate) => fs.existsSync(candidate));
    if (!edgePath) throw error;
    return await chromium.launch({ headless: true, executablePath: edgePath });
  }
}

async function capture(page, name, url, scrollY = 0) {
  await page.goto(`${baseUrl}${url}`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(450);
  await page.evaluate((y) => window.scrollTo(0, y), scrollY);
  await page.waitForTimeout(250);
  await page.screenshot({
    path: path.join(outDir, `${name}.png`),
    fullPage: false,
  });
}

(async () => {
  const browser = await launchBrowser();
  const page = await browser.newPage({
    viewport: { width: 1365, height: 900 },
    deviceScaleFactor: 1,
  });

  await capture(page, '01_login', '/login');

  await page.fill('input[name="username"]', 'admin');
  await page.fill('input[name="password"]', adminPassword);
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
    page.click('button'),
  ]);
  await page.waitForTimeout(500);

  const pages = [
    ['02_dashboard', '/dashboard'],
    ['03_orders', '/orders'],
    ['04_order_new', '/orders/new'],
    ['05_billing', '/faturamento'],
    ['06_routes', '/routes'],
    ['07_route_detail', '/routes/1'],
    ['08_catalog', '/route-cities'],
    ['09_settings', '/settings?perm_role=Faturamento', 720],
    ['10_backup', '/backup'],
  ];

  for (const [name, url, scrollY = 0] of pages) {
    await capture(page, name, url, scrollY);
  }

  await browser.close();
})();
