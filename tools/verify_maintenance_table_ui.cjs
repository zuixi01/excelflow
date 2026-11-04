// Browser-only HTTP fixtures; never writes to the user's API or business data.
const { chromium } = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const path = require('node:path');
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const output = path.resolve('var/acceptance/maintenance-table');
  fs.mkdirSync(output, { recursive: true });
  try {
    for (const risk of [false, true]) {
      const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
      const errors = [], requests = [];
      let revision = 1;
      page.on('pageerror', error => errors.push(error.message));
      const fields = ['product_name', 'sku', 'sale_price'].map((id, i) => ({ rule: {
        name: id, title: ['商品名称', '货号', '售价'][i], type: 'string', required: false, aliases: [], enum_values: []
      }, editable: true, default: null }));
      const task = () => ({ id: 'table-ui', status: 'ready', revision, exports: [], history: [],
        payload: { name: '新增删除与排序验收', filename: '供应商.xlsx', workflow: 'standardization',
          record_count: 2, error_count: 0, sheets: [],
          config: { fields, outputs: [], preserve_extras: true },
          field_snapshot: { version: 1, profile: { local_categories: [] } } } });
      await page.route('**/api/v1/**', async route => {
        const request = route.request(), pathname = new URL(request.url()).pathname;
        let data;
        if (pathname.endsWith('/auth/session')) data = { authenticated: true, role: 'owner', username: '测试管理员' };
        else if (pathname.endsWith('/auth/csrf')) data = { csrf_token: 'test' };
        else if (pathname.endsWith('/workflow-settings')) data = { can_manage_templates: true };
        else if (pathname.endsWith('/workflow-tasks/table-ui')) data = task();
        else if (pathname.endsWith('/maintenance')) data = { revision, total: 0, items: [] };
        else if (pathname.endsWith('/maintenance/returns')) data = { id: 'return-ui', payload: {
          counts: { changed: 1, added: 1, missing: 1 },
          samples: [{ id: 'row-2', before: { sale_price: '10' }, after: { sale_price: '12' } }],
          added_samples: [{ values: { product_name: '新增灯具', sku: '0003' } }],
          missing_samples: [{ id: 'row-1', values: { product_name: '缺少的灯具', sku: '0001' } }],
          identity_warnings: risk ? [{ code: 'POSSIBLE_ID_LOSS', count: 1, message: '新增行与缺少记录的内容或货号一致，可能是误删了隐藏 ID。请核对新增和缺少明细。' }] : []
        } };
        else if (pathname.endsWith('/maintenance/returns/apply')) {
          requests.push(request.postDataJSON()); revision++; data = task();
        } else throw new Error(`Unexpected API ${pathname}`);
        await route.fulfill({ json: data });
      });
      await page.goto((process.env.STANDARDIZATION_WEB || 'http://127.0.0.1:5173') + '/#task/table-ui');
      await page.getByLabel('回传维护表', { exact: true }).setInputFiles({ name: '维护表.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('fixture') });
      const dialog = page.getByRole('dialog', { name: '维护表回传预览' });
      await dialog.waitFor();
      await dialog.getByRole('heading', { name: '新增商品（最多显示 20 条）' }).waitFor();
      await dialog.getByText('商品名称：缺少的灯具；货号：0001', { exact: true }).waitFor();
      const confirm = dialog.getByRole('button', { name: '确认并保存新修订' });
      assert.equal(await confirm.isDisabled(), risk);
      assert.equal(await dialog.getByLabel('同时删除回传文件中缺少的记录（默认保留）', { exact: true }).isChecked(), false);
      await page.screenshot({ path: path.join(output, risk ? 'identity-warning-desktop.png' : 'differences-desktop.png'), fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({ path: path.join(output, risk ? 'identity-warning-mobile.png' : 'differences-mobile.png'), fullPage: true });
      if (risk) {
        await dialog.getByLabel('我已核对 ID 风险提示及差异，确认这些新增和修改符合预期', { exact: true }).check();
        await dialog.getByLabel('同时删除回传文件中缺少的记录（默认保留）', { exact: true }).check();
      }
      assert.equal(await confirm.isDisabled(), false);
      await confirm.click();
      await dialog.waitFor({ state: 'hidden' });
      assert.deepEqual(requests, [{ preview_id: 'return-ui', delete_missing: risk, acknowledge_identity_warnings: risk }]);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log(JSON.stringify({ passed: true, scenarios: 2, browserErrors: 0, screenshots: output }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
