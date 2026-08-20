const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

function argValue(name, fallback = '') {
  const index = process.argv.indexOf(name);
  if (index >= 0 && process.argv[index + 1]) return process.argv[index + 1];
  return fallback;
}

const baseUrl = argValue('--base-url', 'http://127.0.0.1:3000').replace(/\/$/, '');
const outDir = path.resolve(argValue('--out', path.join(process.cwd(), 'docs', 'audit_ui')));
const adminPassword = argValue('--admin-password', process.env.LOGISTICA_TEST_ADMIN_PASSWORD || 'admin123');
const debugMode = process.argv.includes('--debug');
const adminPasswordCandidates = Array.from(
  new Set(
    [
      process.env.LOGISTICA_TEST_ADMIN_PASSWORD || '',
      adminPassword || '',
      'admin123',
      'AdminAudit123',
      'CasaCampo@2026!',
    ]
      .map((v) => String(v || '').trim())
      .filter(Boolean),
  ),
);
const forcePasswordValue = argValue('--force-password', 'AdminAudit123');

const pages = [
  ['/dashboard', 'dashboard'],
  ['/orders', 'orders'],
  ['/orders/new', 'orders_new'],
  ['/clients', 'clients'],
  ['/drivers', 'drivers'],
  ['/vehicles', 'vehicles'],
  ['/route-cities', 'route_cities'],
  ['/routes', 'routes'],
  ['/routes/new', 'routes_new'],
  ['/load-settlement', 'load_settlement'],
  ['/relatorios', 'relatorios'],
  ['/settings?section=profile', 'settings_profile'],
  ['/settings?section=users', 'settings_users'],
  ['/settings?section=permissions&perm_role=Gestor', 'settings_permissions'],
  ['/backup', 'backup'],
];

const viewports = [
  { name: 'desktop', width: 1600, height: 980 },
  { name: 'notebook', width: 1366, height: 768 },
  { name: 'small', width: 1024, height: 700 },
];

const zoomLevels = [0.9, 1.0, 1.25];

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

async function collectMetrics(page) {
  return await page.evaluate(() => {
    const root = document.documentElement;
    const body = document.body;
    const hasHOverflow = (root.scrollWidth > root.clientWidth + 6) || (body.scrollWidth > body.clientWidth + 6);
    const hasVOverflow = (root.scrollHeight > root.clientHeight + 1) || (body.scrollHeight > body.clientHeight + 1);
    const brokenTables = Array.from(document.querySelectorAll('table')).filter((t) => {
      if (t.closest('.table-wrap,.table-container,.table-scroll')) return false;
      return t.scrollWidth > t.clientWidth + 8;
    }).length;
    const clippedInputs = Array.from(document.querySelectorAll('input,select,textarea,button'))
      .filter((el) => {
        if (String(el.type || '').toLowerCase() === 'hidden') return false;
        const st = window.getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return false;
        if (r.right <= 0 || r.left >= window.innerWidth || r.bottom <= 0 || r.top >= window.innerHeight) return false;
        return r.right > window.innerWidth + 1 || r.left < -1;
      }).length;
    return {
      hasHOverflow,
      hasVOverflow,
      brokenTables,
      clippedInputs,
      title: (document.querySelector('h1')?.textContent || '').trim(),
    };
  });
}

async function submitForm(page) {
  const navPromise = page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 5000 }).catch(() => null);
  await page.$eval('form', (form) => {
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.submit();
  });
  await navPromise;
  await page.waitForTimeout(150);
}

async function login(page) {
  for (const pwd of adminPasswordCandidates) {
    await page.goto(`${baseUrl}/login`, { waitUntil: 'domcontentloaded' });
    if (debugMode) console.log(`LOGIN_TRY pwd=${pwd}`);
    await page.fill('input[name="username"]', 'admin');
    await page.fill('input[name="password"]', pwd);
    await submitForm(page);
    if (debugMode) console.log(`LOGIN_URL after_submit=${page.url()}`);
    if (page.url().includes('/force-password')) {
      await page.fill('input[name="new_password"]', forcePasswordValue);
      await page.fill('input[name="confirm_password"]', forcePasswordValue);
      await submitForm(page);
      if (debugMode) console.log(`LOGIN_URL after_force=${page.url()}`);
      if (page.url().includes('/dashboard')) return true;
    } else if (page.url().includes('/dashboard')) {
      return true;
    }
  }
  throw new Error('Falha no login de auditoria visual: nenhuma senha de administrador funcionou.');
}

(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const report = [];
  const errors = [];
  const browser = await launchBrowser();
  const page = await browser.newPage();
  page.on('pageerror', (err) => errors.push(`PAGEERROR ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(`CONSOLE ${msg.text()}`);
  });

  try {
    await login(page);
    for (const vp of viewports) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      for (const zoom of zoomLevels) {
        for (const [url, key] of pages) {
          await page.goto(`${baseUrl}${url}`, { waitUntil: 'domcontentloaded' });
          await page.evaluate((z) => {
            document.body.style.zoom = String(z);
            window.scrollTo(0, 0);
          }, zoom);
          await page.waitForTimeout(250);

          const metrics = await collectMetrics(page);
          const tag = `${vp.name}_z${String(zoom).replace('.', '_')}`;
          const file = path.join(outDir, `${tag}_${key}.png`);
          await page.screenshot({ path: file, fullPage: false });
          report.push({ viewport: vp.name, zoom, page: url, key, file, ...metrics });
        }
      }
    }
  } finally {
    await browser.close();
  }

  const reportPath = path.join(outDir, 'ui_layout_report.json');
  fs.writeFileSync(reportPath, JSON.stringify({ generatedAt: new Date().toISOString(), report, errors }, null, 2), 'utf-8');

  const warn = report.filter((r) => r.hasHOverflow || r.brokenTables > 0 || r.clippedInputs > 0);
  console.log(`REPORT=${reportPath}`);
  console.log(`TOTAL=${report.length}`);
  console.log(`WARNINGS=${warn.length}`);
  if (errors.length) {
    console.log(`CONSOLE_ERRORS=${errors.length}`);
    for (const e of errors.slice(0, 20)) console.log(e);
  } else {
    console.log('CONSOLE_ERRORS=0');
  }
})();
