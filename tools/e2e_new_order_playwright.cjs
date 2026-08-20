const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");
const { spawn, execFileSync } = require("child_process");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..");
const SOURCE_DB = path.join(ROOT, "data", "logistica_casa_do_campo.sqlite3");
const PY = process.env.PYTHON || "python";
const LOGIN_PASSWORDS = [
  process.env.LOGISTICA_TEST_ADMIN_PASSWORD || "",
  "CasaCampo@2026!",
  "admin123",
  "AdminAudit123",
].map((x) => String(x || "").trim()).filter(Boolean);

function freePort() {
  return new Promise((resolve, reject) => {
    const server = http.createServer();
    server.listen(0, "127.0.0.1", () => {
      const p = server.address().port;
      server.close(() => resolve(p));
    });
    server.on("error", reject);
  });
}

function waitHealth(baseUrl, timeoutMs = 25000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get(`${baseUrl}/healthz`, (res) => {
        res.resume();
        if (res.statusCode === 200) return resolve(true);
        if (Date.now() - start > timeoutMs) return reject(new Error("healthz timeout"));
        setTimeout(tick, 250);
      });
      req.on("error", () => {
        if (Date.now() - start > timeoutMs) return reject(new Error("healthz timeout"));
        setTimeout(tick, 250);
      });
    };
    tick();
  });
}

function ensureSeedData(dbPath) {
  const pyCode = `
import sqlite3, hashlib, time, sys
db_path = sys.argv[1]
pwd = "admin123"
hashed = hashlib.sha256(("casa_do_campo_local_v3:" + pwd).encode("utf-8")).hexdigest()
now_ts = time.strftime("%Y-%m-%d %H:%M:%S")
with sqlite3.connect(db_path) as db:
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT id FROM users WHERE LOWER(username)=LOWER('admin') LIMIT 1").fetchone()
    if row:
        db.execute("UPDATE users SET password_hash=?, active=1, must_change_password=0 WHERE id=?", (hashed, int(row["id"])))
    else:
        db.execute("INSERT INTO users(name,username,password_hash,role,active,created_at,must_change_password) VALUES(?,?,?,?,?,?,0)", ("Administrador", "admin", hashed, "GOD", 1, now_ts))
    c = db.execute("SELECT id FROM clients WHERE active=1 LIMIT 1").fetchone()
    if not c:
        db.execute(
            "INSERT INTO clients(customer_code,name,phone,whatsapp,city,farm_name,address,route_name,active,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,1,?,?,1)",
            ("1001", "Cliente E2E", "(33) 98888-0000", "(33) 98888-0000", "Cidade E2E", "Fazenda E2E", "Endereco E2E", "Rota E2E", now_ts, now_ts),
        )
    rc = db.execute("SELECT id FROM route_cities WHERE active=1 LIMIT 1").fetchone()
    if not rc:
        db.execute(
            "INSERT INTO route_cities(route_name,city,uf,delivery_order,active,notes,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,1)",
            ("Rota E2E", "Cidade E2E", "MG", 1, 1, "Seed E2E", now_ts, now_ts),
        )
    db.commit()
`;
  execFileSync(PY, ["-c", pyCode, dbPath], { stdio: "ignore" });
}

async function loginAsAdmin(page, baseUrl) {
  for (const pwd of LOGIN_PASSWORDS) {
    await page.goto(`${baseUrl}/login`, { waitUntil: "domcontentloaded" });
    await page.fill('input[name="username"]', "admin");
    await page.fill('input[name="password"]', pwd);
    await Promise.all([
      page.waitForLoadState("domcontentloaded"),
      page.click('button[type="submit"], button:has-text("Entrar")'),
    ]);
    if (page.url().includes("/force-password")) {
      await page.fill('input[name="new_password"]', "AdminAudit123");
      await page.fill('input[name="confirm_password"]', "AdminAudit123");
      await Promise.all([
        page.waitForLoadState("domcontentloaded"),
        page.click('button[type="submit"], button:has-text("Salvar"), button:has-text("Atualizar")'),
      ]);
    }
    if (page.url().includes("/dashboard")) return true;
  }
  return false;
}

async function ensureOrderFormDefaults(page) {
  const paymentExists = await page.$('select[name="payment_method"]');
  if (paymentExists) {
    const options = await page.$$eval('select[name="payment_method"] option', opts => opts.map(o => o.value).filter(Boolean));
    if (options.includes("Pix")) {
      await page.selectOption('select[name="payment_method"]', "Pix");
    } else if (options.length) {
      await page.selectOption('select[name="payment_method"]', options[0]);
    }
  }

  const cityValue = await page.$eval('#cityInput', el => el.value || "");
  if (!cityValue) {
    const cityOption = await page.$$eval('#cityInput option', opts => opts.map(o => o.value).find(v => v));
    if (cityOption) await page.selectOption("#cityInput", cityOption);
  }

  const routeValue = await page.$eval('#routeInput', el => el.value || "");
  if (!routeValue) {
    const routeOption = await page.$$eval('#routeInput option', opts => opts.map(o => o.value).find(v => v));
    if (routeOption) await page.selectOption("#routeInput", routeOption);
  }
}

async function run() {
  if (!fs.existsSync(SOURCE_DB)) throw new Error(`Banco fonte ausente: ${SOURCE_DB}`);
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "e2e_new_order_"));
  const dbPath = path.join(tempDir, "e2e.sqlite3");
  fs.copyFileSync(SOURCE_DB, dbPath);
  ensureSeedData(dbPath);

  const port = await freePort();
  const baseUrl = `http://127.0.0.1:${port}`;
  const env = {
    ...process.env,
    APP_RUNTIME: "legacy",
    APP_HOST: "127.0.0.1",
    APP_PORT: String(port),
    DATABASE_URL: `sqlite:///${dbPath.replace(/\\/g, "/")}`,
    LOGISTICA_DB_PATH: dbPath,
    LOGISTICA_HOST: "127.0.0.1",
    LOGISTICA_PORT: String(port),
  };
  const server = spawn(PY, ["app.py"], {
    cwd: ROOT,
    env,
    stdio: "ignore",
    windowsHide: true,
  });

  let browser;
  try {
    await waitHealth(baseUrl, 30000);
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();

    const logged = await loginAsAdmin(page, baseUrl);
    if (!logged) throw new Error("Login admin falhou no E2E");

    await page.goto(`${baseUrl}/orders/new`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#clientSearchInput", { timeout: 10000 });

    // Abre lista ao focar/clicar.
    await page.click("#clientSearchInput");
    await page.waitForSelector("#clientSearchResults .client-result-item", { timeout: 10000 });

    // Seleciona um cliente existente na lista e confirma preenchimento.
    const existing = await page.$$eval("#clientSearchResults .client-result-item", rows => {
      const target = rows.find(r => (r.getAttribute("data-client-value") || "").trim());
      return target
        ? {
            value: target.getAttribute("data-client-value") || "",
            text: (target.textContent || "").trim(),
          }
        : null;
    });
    if (!existing || !existing.value) throw new Error("Nenhum cliente existente encontrado para o teste E2E.");

    const token = (existing.text.match(/[A-Za-zÀ-ÿ0-9]{3,}/) || [existing.text.slice(0, 6)])[0];
    const searchSeed = String(token || "").trim() || "a";
    await page.fill("#clientSearchInput", searchSeed);
    await page.waitForTimeout(120);
    let pickIndex = await page.$$eval("#clientSearchResults .client-result-item", rows => {
      return rows.findIndex(r => (r.getAttribute("data-client-value") || "").trim());
    });
    if (pickIndex < 0) {
      await page.fill("#clientSearchInput", "");
      await page.waitForTimeout(120);
      pickIndex = await page.$$eval("#clientSearchResults .client-result-item", rows => {
        return rows.findIndex(r => (r.getAttribute("data-client-value") || "").trim());
      });
    }
    if (pickIndex < 0) throw new Error("Busca de cliente não retornou item selecionável.");
    await page.click(`#clientSearchResults .client-result-item:nth-of-type(${pickIndex + 1})`);
    await page.waitForTimeout(120);

    const selectedClient = await page.$eval("#clientSelect", el => el.value || "");
    const selectedCode = await page.$eval("#clientCodeInput", el => el.value || "");
    if (!selectedClient) throw new Error("Falha ao selecionar cliente pelo novo campo de busca.");
    if (!selectedCode) throw new Error("Código do cliente não foi preenchido após seleção.");

    await ensureOrderFormDefaults(page);

    const orderNo = `E2E-NOVO-${Date.now()}`;
    const today = new Date().toISOString().slice(0, 10);
    await page.fill('input[name="order_number"]', orderNo);
    await page.fill('#saleDate', today);
    await page.fill('#weightKgInput', "123,45");
    await page.fill('#addressInput', "Endereco E2E");

    const missingRequired = await page.$$eval("form.professional-form [required]", fields =>
      fields
        .filter(f => {
          const tag = (f.tagName || "").toLowerCase();
          if (tag === "select") return !f.value;
          return !(f.value || "").trim();
        })
        .map(f => f.name || f.id || f.tagName)
    );
    if (missingRequired.length) {
      throw new Error(`Campos obrigatórios sem valor antes do submit: ${missingRequired.join(", ")}`);
    }

    const csrfToken = await page.evaluate(() => {
      const key = "csrf_token=";
      for (const part of String(document.cookie || "").split(";")) {
        const item = part.trim();
        if (item.startsWith(key)) return decodeURIComponent(item.slice(key.length));
      }
      return "";
    });
    const waitPost = page.waitForResponse(
      r => r.request().method() === "POST" && r.url().includes("/orders/new"),
      { timeout: 15000 }
    );
    await page.$eval("form.professional-form", (form, token) => {
      let csrf = form.querySelector('input[name="_csrf"]');
      if (!csrf) {
        csrf = document.createElement("input");
        csrf.type = "hidden";
        csrf.name = "_csrf";
        form.prepend(csrf);
      }
      csrf.value = token || "";
      form.submit();
    }, csrfToken);
    const postResp = await waitPost;
    if (!postResp) throw new Error("Submit não disparou POST /orders/new.");
    await page.waitForFunction(
      () => /\/orders\/\d+/.test(window.location.pathname) || !!document.querySelector(".alert.danger"),
      { timeout: 12000 }
    );

    if (!/\/orders\/\d+/.test(page.url())) {
      const errMsg = await page.$eval(".alert.danger", el => (el.textContent || "").trim()).catch(() => "");
      throw new Error(`Fluxo de novo pedido não redirecionou. ${errMsg || `URL atual: ${page.url()}`}`);
    }
    const html = await page.content();
    if (!html.includes(orderNo)) {
      throw new Error("Pedido criado não exibiu o número esperado na tela de detalhe.");
    }

    console.log("PASS e2e_new_order_playwright");
  } finally {
    if (browser) await browser.close().catch(() => {});
    if (server && !server.killed) {
      server.kill();
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    try {
      fs.rmSync(tempDir, { recursive: true, force: true });
    } catch (_) {
      try {
        await new Promise((resolve) => setTimeout(resolve, 350));
        fs.rmSync(tempDir, { recursive: true, force: true });
      } catch (_) {
        // lock transitório em Windows não deve invalidar o resultado funcional do teste.
      }
    }
  }
}

run().catch((err) => {
  console.error("FAIL e2e_new_order_playwright", err && err.message ? err.message : err);
  process.exit(1);
});
