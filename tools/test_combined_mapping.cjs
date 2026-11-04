const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
let browser;
before(async () => { browser = await chromium.launch({ headless: true, channel: 'msedge' }); });
after(async () => { await browser?.close(); });
function fixture(state) {
  const field = (name, title) => ({ rule: { name, title, type: 'string', required: name === 'product_name', aliases: [], enum_values: [] }, editable: true, hidden: false, category_id: name.startsWith('local.') && state.legacy ? 'group' : '' });
  const binding = (field, column) => ({ field, columns: [column], operation: 'copy', argument: '', part: 0, method: 'exact', reason: '同名字段' });
  return { id: 'task_combined_fixture', status: 'mapping', revision: state.revision || 0, payload: {
    name: '联合字段验收', filename: '供应商.xlsx', workflow: 'standardization', template_version: 2, stage: 'mapping', record_count: 0, error_count: 0,
    field_snapshot: { version: 2, matching_scope: state.legacy ? undefined : 'all_fields', dynamic_source: 'local_preview', profile: { name: '联合字段', fixed_fields: [], local_categories: [{ id: 'group', title: '新类目', attributes: [], specifications: [] }] } },
    config: { name: '联合字段', category_field: state.legacy ? 'source_category' : '', fields: [field('product_name', '固定字段/商品名称'), field('brand', '固定字段/品牌'), field('local.group.attribute.color', '动态字段/商品属性/颜色'), field('local.group.specification.power', '动态字段/商品规格/功率')], outputs: [], import_rules: [], unique_key: [], cross_checks: [] },
    sheets: [{ sheet: '供应商任意表名', header_row: 1, header_depth: 1, row_count: 1,
      columns: [{ column: 1, header: '商品名称', field: 'product_name', candidates: [], example: '筒灯' }, { column: 2, header: '颜色', field: '', candidates: [], example: '白色' }],
      preview: [['商品名称', '颜色'], ['筒灯', '白色']], category_id: 'unclassified',
      suggested_bindings: [binding('product_name', 1), ...(!state.legacy ? [binding('local.group.attribute.color', 2)] : [])],
      mapping_notes: state.legacy ? ['尚未选择动态字段类目，本工作表仅使用固定字段。'] : [],
      mapping_summary: { total: 4, matched: 2, unmatched: 2, model_status: 'not_configured' },
    }] }, exports: [], history: [] };
}
async function setup(t, state = {}) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(async () => { await page.close(); assert.deepEqual(errors, []); });
  await page.route('**/api/v1/**', route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.endsWith('/workflow-settings')) return route.fulfill({ json: { can_manage_templates: true, ai: { configured: false } } });
    if (url.pathname.endsWith('/mapping/analyze')) {
      assert.deepEqual(request.postDataJSON(), { base_revision: state.revision || 0 });
      state.legacy = false; state.revision = (state.revision || 0) + 1;
      return route.fulfill({ json: fixture(state) });
    }
    if (url.pathname.endsWith('/task_combined_fixture')) return route.fulfill({ json: fixture(state) });
    throw new Error('Unexpected API request: ' + url.pathname);
  });
  await page.goto((process.env.COMBINED_TEST_WEB || 'http://127.0.0.1:5173') + '/#task/task_combined_fixture');
  await page.getByRole('button', { name: '确认并生成维护表 →', exact: true }).waitFor();
  return page;
}
test('all target fields are available without selecting a dynamic category and unmatched fields are visible', async t => {
  const page = await setup(t);
  assert.equal(await page.getByRole('combobox', { name: '动态字段类目' }).count(), 0);
  const options = await page.getByRole('combobox', { name: '维护字段' }).first().locator('option').allTextContents();
  assert.ok(options.includes('动态字段/商品属性/颜色'));
  assert.ok(options.includes('动态字段/商品规格/功率'));
  assert.match(await page.locator('.mapping-coverage').innerText(), /目标 4/);
  assert.match(await page.locator('.mapping-coverage').innerText(), /已匹配 2/);
  await page.locator('.unmatched-targets summary').click();
  assert.match(await page.locator('.unmatched-targets').innerText(), /动态字段\/商品规格\/功率/);
  assert.equal(await page.getByRole('button', { name: '确认并生成维护表 →', exact: true }).isDisabled(), false);
  await page.getByRole('button', { name: '重新分析全部字段', exact: true }).click();
  await page.getByRole('status').filter({ hasText: /全部.*字段/ }).waitFor();
  const output = path.resolve('var/acceptance/combined-mapping');
  fs.mkdirSync(output, { recursive: true });
  for (const viewport of [{ width: 1440, height: 950 }, { width: 375, height: 812 }]) {
    await page.setViewportSize(viewport);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    await page.screenshot({ path: path.join(output, `mapping-${viewport.width}.png`), fullPage: true });
  }
});
test('old scoped tasks require explicit all-field reanalysis rather than a category choice', async t => {
  const page = await setup(t, { legacy: true });
  assert.equal(await page.getByRole('combobox', { name: '动态字段类目' }).count(), 0);
  assert.equal(await page.getByRole('button', { name: '确认并生成维护表 →', exact: true }).isDisabled(), true);
  await page.getByRole('button', { name: '重新分析全部字段', exact: true }).click();
  await page.getByRole('status').filter({ hasText: /全部.*字段/ }).waitFor();
  assert.equal(await page.getByRole('button', { name: '确认并生成维护表 →', exact: true }).isDisabled(), false);
  assert.ok((await page.getByRole('combobox', { name: '维护字段' }).first().locator('option').allTextContents()).includes('动态字段/商品规格/功率'));
});
