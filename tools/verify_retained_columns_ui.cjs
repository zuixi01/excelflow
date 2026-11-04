// Browser regression using the real-model run's saved task as an isolated API fixture.
// Does not write to the running application's business API.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const directory = path.resolve(process.argv[2]);
  const fixture = JSON.parse(fs.readFileSync(path.join(directory, 'ui-fixture.json'), 'utf8'));
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let task = structuredClone(fixture.task), rows = structuredClone(fixture.rows), saved;
  const endpoint = `/api/v1/workflow-tasks/${task.id}`;
  try {
    await page.route('**/api/**', async route => {
      const request = route.request(), url = new URL(request.url());
      const json = value => route.fulfill({status:200, contentType:'application/json', body:JSON.stringify(value)});
      if (url.pathname === '/api/v1/auth/csrf') return json({csrf_token:'isolated-ui-fixture'});
      if (url.pathname === endpoint) return json(task);
      if (url.pathname === endpoint + '/maintenance') {
        if (request.method() === 'PATCH') {
          saved = request.postDataJSON();
          for (const row of rows.items) Object.assign(row.values_json, saved.changes[row.id] || {});
          task.revision++;
          rows.revision = task.revision;
          return json(task);
        }
        return json(rows);
      }
      if (url.pathname === endpoint + '/mapping') {
        saved = request.postDataJSON();
        task = structuredClone(fixture.task);
        task.status = 'ready';
        return json(task);
      }
      if (request.method() !== 'GET') throw new Error('Unexpected business API write: ' + url.pathname);
      return route.continue();
    });
    const root = process.env.RETAINED_UI_WEB || 'http://127.0.0.1:5173';
    await page.goto(root + '/#task/' + task.id);
    await page.getByRole('button', {name:'维护数据', exact:true}).click();
    await page.locator('.editor-table tbody textarea').first().waitFor();
    const titles = await page.locator('.editor-table thead th[scope="col"]').allTextContents();
    const compact = ['compact', 'grouped'].includes(task.payload.config.source_header_layout);
    const expectedTitles = task.payload.config.fields.filter(f=>!f.hidden).map(f => f.source_column ? (compact ? f.source_column.header : `原表保留列/${f.source_column.sheet} · 第${f.source_column.column}列/${f.source_column.header}`) : f.rule.title);
    assert.deepEqual(titles.map(t=>t.trim()), expectedTitles);
    assert.ok(!titles.includes('序号'));
    const input = page.locator('.editor-table tbody textarea').first();
    assert.equal(await input.inputValue(), '在线维护修改\n保留第二行');
    await input.fill('浏览器维护\n完整第二行');
    await page.getByRole('button', {name:'保存并校验', exact:true}).click();
    await page.getByRole('status').filter({hasText:'已保存并完成校验'}).waitFor();
    const retainedId = task.payload.config.fields.find(field => field.source_column).rule.name;
    assert.equal(saved.changes[rows.items[0].id][retainedId], '浏览器维护\n完整第二行');
    await page.locator('.editor-scroll').evaluate(el => {el.scrollLeft = el.scrollWidth;});
    await page.screenshot({path:path.join(directory, '维护页面.png'), fullPage:true});
    // Exercise field confirmation, where retained columns must not become target fields.
    task = structuredClone(fixture.task);
    task.status = 'mapping';
    task.payload.config.fields = task.payload.config.fields.filter(field => !field.source_column);
    delete task.payload.mapping;
    await page.reload();
    await page.getByText('原表保留列 4 列（追加在动态字段后）', {exact:true}).waitFor();
    assert.equal(await page.locator('select[aria-label="维护字段"]').first().locator('option').count(), 6);
    await page.locator('button[aria-label="移除映射"]').first().click();
    await page.getByText('原表保留列 5 列（追加在动态字段后）', {exact:true}).waitFor();
    await page.locator('.unmatched-targets').last().locator('summary').click();
    await page.screenshot({path:path.join(directory, '字段确认页面.png'), fullPage:true});
    assert.deepEqual(errors, []);
    const result = {status:'passed', backend:'isolated fixture from real-model task',
      checks:['fixed-dynamic-retained order', 'all retained columns visible', 'multiline editing and save payload',
              'unmatched source preview', 'removed binding moves to retained preview', 'no source extras offered as targets'],
      business_api_writes:0, browser_errors:errors};
    fs.writeFileSync(path.join(directory, 'ui-result.json'), JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  } catch (error) {
    await page.screenshot({path:path.join(directory, 'ui-failure.png'), fullPage:true});
    console.error(JSON.stringify({saved, errors, alerts:await page.getByRole('alert').allTextContents()}));
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
