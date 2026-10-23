// Execute com Node + Playwright, informando o JSON de fixtures gerado em banco isolado.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');

(async () => {
  const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage({viewport: {width: 1600, height: 1000}});
    const errors = [], requests = [], writes = [];
    const delays = {};
    let pageRefreshes = 0;
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (request.method() === 'POST') writes.push(request.url()); });
    await page.addInitScript(() => {
      window.tripTestPolls = [];
      const originalSetInterval = window.setInterval.bind(window);
      window.setInterval = (callback, delay, ...args) => {
        if (delay === 30000) window.tripTestPolls.push(() => callback(...args));
        return originalSetInterval(callback, delay, ...args);
      };
    });
    await page.route('**/static/app.js', route => route.fulfill({status: 200, contentType: 'text/javascript', body: fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8')}));
    await page.route('**/registro-viagens', route => {
      if (route.request().resourceType() !== 'document') pageRefreshes++;
      return route.fulfill({status: 200, contentType: 'text/html', body: fixture.html});
    });
    await page.route('**/api/trips/search-item?*', async route => {
      const url = new URL(route.request().url());
      const query = url.searchParams.get('q');
      const field = url.searchParams.get('field');
      requests.push({query, field});
      if (query === 'NF-AMBIGUA') {
        return route.fulfill({status: 409, json: {ok: false, message: 'Esta NF tem mais de um pedido. Informe o número do pedido.'}});
      }
      if (delays[query]) await new Promise(resolve => setTimeout(resolve, delays[query]));
      const item = field === 'invoice_number'
        ? Object.values(fixture.items).find(item => item.invoice_number === query)
        : fixture.items[query];
      await route.fulfill({status: 200, json: {ok: true, item: item || null}}).catch(() => {});
    });
    await page.goto('http://127.0.0.1:3000/registro-viagens');
    const field = (name, row=1) => page.locator(`#row_${row} [name="${name}"]`);
    async function waitValue(name, expected, row=1) {
      await page.waitForFunction(({name, expected, row}) => document.querySelector(`#row_${row} [name="${name}"]`)?.value === expected, {name, expected, row}, {timeout: 5000});
    }
    await field('delivery_date').fill('2026-10-06');
    await field('order_number').fill('705335'); // Sem sair do campo.
    await waitValue('invoice_number', '311929');
    assert.equal(await field('payment_type').inputValue(), 'Pix');
    assert.equal(await field('delivery_date').inputValue(), '2026-10-06');
    assert.equal(await field('weight_kg').inputValue(), '825');
    assert.match(await field('notes').inputValue(), /1.5 UN Produto fracionado/);
    assert.equal(requests.at(-1).field, 'order_number');
    const firstCount = requests.length;
    await field('order_number').blur();
    await page.waitForTimeout(100);
    assert.equal(requests.length, firstCount, 'Blur repetiu a consulta já concluída');

    // O poll global de 30s não pode substituir a tabela editável por HTML vazio.
    assert.ok(await page.evaluate(() => window.tripTestPolls.length > 0), 'Teste não carregou o atualizador global');
    await page.locator('#destination').focus(); // Fora da tabela de pedidos.
    await page.evaluate(() => window.tripTestPolls.forEach(callback => callback()));
    await page.waitForTimeout(150);
    assert.equal(await field('order_number').inputValue(), '705335', 'Atualização global apagou o pedido digitado');
    assert.equal(await field('invoice_number').inputValue(), '311929');
    assert.equal(pageRefreshes, 0, 'Tela de digitação iniciou uma atualização de listagem');

    await field('order_number').fill('697045');
    await waitValue('invoice_number', '310343');
    assert.equal(await field('client_name').inputValue(), fixture.items['697045'].client_name);
    assert.equal(await field('weight_kg').inputValue(), '3250');
    assert.equal(await field('payment_type').inputValue(), 'Cartão');
    assert.equal(await field('delivery_date').inputValue(), '2026-10-06');

    await field('notes').fill('Observação digitada pelo operador');
    await field('order_number').fill('705335');
    await waitValue('invoice_number', '311929');
    assert.equal(await field('notes').inputValue(), 'Observação digitada pelo operador');

    await field('order_number').fill('NAO-EXISTE');
    await page.waitForFunction(() => document.querySelector('#badge_baixa_1').textContent.includes('Manual'));
    assert.equal(await field('invoice_number').inputValue(), '');
    assert.equal(await field('client_name').inputValue(), '');

    await field('order_number').fill('705335');
    await waitValue('invoice_number', '311929');
    await page.getByRole('button', {name: '+ Adicionar Linha', exact: true}).click();
    delays['705335'] = 1100;
    await field('order_number', 2).fill('705335');
    await page.waitForTimeout(550); // A primeira requisição já iniciou.
    await page.getByRole('button', {name: '+ Adicionar Linha', exact: true}).focus();
    await page.evaluate(() => window.tripTestPolls.forEach(callback => callback()));
    await page.waitForTimeout(150);
    assert.equal(await page.locator('#ordersTableBody tr').count(), 2, 'Poll removeu uma linha durante o autocomplete');
    assert.equal(await field('invoice_number').inputValue(), '311929', 'Consulta da segunda linha apagou a primeira');
    await field('order_number', 2).fill('697045');
    await waitValue('invoice_number', '310343', 2);
    await page.waitForTimeout(800);
    assert.equal(await field('invoice_number', 2).inputValue(), '310343', 'Resposta antiga sobrescreveu o pedido atual');
    assert.equal(await field('client_name', 2).inputValue(), fixture.items['697045'].client_name);
    await page.getByRole('button', {name: '+ Adicionar Linha', exact: true}).focus();
    await page.evaluate(() => window.tripTestPolls.forEach(callback => callback()));
    await page.waitForTimeout(150);
    assert.equal(await page.locator('#ordersTableBody tr').count(), 2, 'Atualização global removeu linhas adicionais');
    assert.equal(await field('order_number', 2).inputValue(), '697045');
    assert.equal(await field('notes').inputValue(), 'Observação digitada pelo operador');

    await page.getByRole('button', {name: '+ Adicionar Linha', exact: true}).click();
    await field('invoice_number', 3).fill('311929');
    await waitValue('order_number', '705335', 3);
    assert.equal(requests.at(-1).field, 'invoice_number');
    await page.locator('#destination').fill('Destino digitado');
    await page.locator('#expLunchQty').fill('2');
    await page.locator('#expLunchVal').fill('30');
    await page.locator('#expOvernightQty').fill('2');
    await page.locator('#expOvernightVal').fill('60');
    assert.match(await page.locator('#expOvernightTotal').textContent(), /R\$\s*120,00/);
    assert.match(await page.locator('#expLunchTotal').textContent(), /R\$\s*60,00/);
    assert.match(await page.locator('#displayTotalExpensesBadge').textContent(), /Total: R\$\s*180,00/);
    assert.match(await page.locator('#displayTotalExpenses').textContent(), /R\$\s*180,00/);
    await page.locator('#expOvernightVal').blur();
    assert.match(await page.locator('#expOvernightVal').inputValue(), /R\$\s*60,00/);
    assert.deepEqual(await page.evaluate(() => getTripFormData().expenses.overnight), {qty: 2, unit_val: 60});
    await page.locator('#expFuelVal').fill('1.234,56');
    await page.locator('#expFuelVal').blur();
    assert.match(await page.locator('#expFuelVal').inputValue(), /R\$\s*1\.234,56/);
    await page.locator('#expTollVal').fill('12,34');
    await page.locator('#expOtherVal').fill('0,10');
    await page.locator('#expOtherVal').blur();
    assert.match(await page.locator('#displayTotalExpenses').textContent(), /R\$\s*1\.427,00/);
    assert.equal(await page.evaluate(() => getTripFormData().expenses.fuel.total_val), 1234.56);
    await page.locator('#expOtherVal').fill('inválido');
    assert.equal(await page.locator('#expOtherVal').evaluate(field => field.checkValidity()), false);
    await page.locator('#expOtherVal').fill('0,10');
    await page.locator('#expOtherVal').blur();
    const beforePolls = await page.evaluate(() => getTripFormData());
    await page.locator('#destination').focus();
    await page.evaluate(() => {
      for (let cycle = 0; cycle < 3; cycle++) window.tripTestPolls.forEach(callback => callback());
    });
    await page.waitForTimeout(150);
    assert.deepEqual(await page.evaluate(() => getTripFormData()), beforePolls, 'Poll alterou o formulário com três pedidos');

    // A mensagem específica da API precisa aparecer, sem apagar as outras linhas.
    await field('invoice_number', 3).fill('NF-AMBIGUA');
    await page.waitForFunction(() => document.querySelector('#badge_baixa_3').textContent.includes('Informe o número do pedido'));
    assert.equal(await field('invoice_number', 3).inputValue(), 'NF-AMBIGUA');
    assert.equal(await field('order_number', 2).inputValue(), '697045');
    await field('invoice_number', 3).fill('311929');
    await waitValue('order_number', '705335', 3);

    // Listagens continuam atualizando; a proteção não desliga o polling do sistema.
    const listHtml = text => `<!doctype html><html><head><script src="/static/app.js" defer></script></head><body>
      <div id="realtimeIndicator"><span class="realtime-dot"></span><span id="realtimeText"></span></div>
      <div class="table-wrap"><table><tbody><tr><td>${text}</td></tr></tbody></table></div></body></html>`;
    let listRefreshes = 0, holdListResponse = false, releaseListResponse;
    await page.route('**/orders', async route => {
      if (route.request().resourceType() === 'document') {
        return route.fulfill({status: 200, contentType: 'text/html', body: listHtml('Lista original')});
      }
      listRefreshes++;
      if (holdListResponse) await new Promise(resolve => { releaseListResponse = resolve; });
      return route.fulfill({status: 200, contentType: 'text/html', body: listHtml('Lista atualizada')});
    });
    await page.goto('http://127.0.0.1:3000/orders');
    await page.evaluate(() => window.tripTestPolls.forEach(callback => callback()));
    await page.waitForFunction(() => document.querySelector('.table-wrap td')?.textContent === 'Lista atualizada');
    assert.equal(listRefreshes, 1);

    holdListResponse = true;
    const pendingPoll = page.waitForRequest(request => request.url().endsWith('/orders') && request.resourceType() !== 'document');
    await page.evaluate(() => window.tripTestPolls.forEach(callback => callback()));
    await pendingPoll;
    await page.evaluate(() => {
      const form = document.createElement('form');
      form.dataset.realtimeRefresh = 'off';
      document.body.appendChild(form);
      document.querySelector('.table-wrap td').textContent = 'Edição iniciada durante a consulta';
    });
    const pendingResponse = page.waitForResponse(response => response.url().endsWith('/orders'));
    releaseListResponse();
    await pendingResponse;
    await page.waitForTimeout(150);
    assert.equal(await page.locator('.table-wrap td').textContent(), 'Edição iniciada durante a consulta');
    const countBeforeGuard = listRefreshes;
    await page.evaluate(() => window.tripTestPolls.forEach(callback => callback()));
    await page.waitForTimeout(100);
    assert.equal(listRefreshes, countBeforeGuard, 'Marcador do formulário não bloqueou a atualização');
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, [], 'Autocomplete enviou uma operação de gravação');
    console.log('OK: autocomplete, três linhas e despesas preservadas durante polls, proteção de respostas em trânsito e listagens atualizando normalmente; sem gravações.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
