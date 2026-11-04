const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const apiRoot = process.env.STANDARDIZATION_API || 'http://127.0.0.1:8002';
  const webRoot = process.env.STANDARDIZATION_WEB || 'http://127.0.0.1:5174';
  const output = path.resolve('var/acceptance/standardization');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const errors = [];
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(webRoot+'/#fields');
    await page.getByRole('heading', { name: '字段配置', exact: true }).waitFor();
    await page.getByRole('textbox', { name: '字段名称 1', exact: true }).first().waitFor();
    await page.getByRole('button', { name: '添加字段', exact: true }).first().click();
    await page.locator('.config-section').first().getByRole('textbox', { name: /字段名称/ }).last().fill(`运营补充备注${Date.now()}`);
    if(!await page.getByRole('textbox', { name: '字段组名称', exact: true }).count()) {
      await page.getByRole('button', { name: '添加字段组', exact: true }).click();
      await page.getByRole('textbox', { name: '字段组名称', exact: true }).fill('模拟运营平台');
    }
    const attributeSection = page.locator('.config-section').filter({ has: page.getByRole('heading', { name: '动态字段', exact: true }) });
    if(!await attributeSection.getByRole('textbox', { name: '字段名称 1', exact: true }).count()) {
      await attributeSection.getByRole('button', { name: '添加字段', exact: true }).first().click();
      await attributeSection.getByRole('textbox', { name: '字段名称 1', exact: true }).fill('系列');
    }
    await page.getByRole('button', { name: '保存草稿', exact: true }).click();
    await page.getByRole('status').filter({ hasText: '字段草稿已保存' }).waitFor();
    await page.getByRole('button', { name: '预览结构', exact: true }).click();
    await page.getByRole('heading', { name: /输出结构预览/ }).waitFor();
    await page.getByRole('button', { name: '关闭结构预览', exact: true }).click();
    await page.getByRole('button', { name: '发布配置', exact: true }).click();
    await page.getByRole('status').filter({ hasText: /已发布/ }).waitFor();
    await page.screenshot({ path: path.join(output, 'fields-desktop.png'), fullPage: true });
    for (const viewport of [{ width: 375, height: 812 }, { width: 812, height: 375 }]) {
      await page.setViewportSize(viewport);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, 'field configuration overflow');
      await page.screenshot({ path: path.join(output, `fields-${viewport.width}.png`), fullPage: true });
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole('button', { name: '返回工作台', exact: true }).click();
    await page.getByRole('button', { name: '新建任务', exact: true }).first().click();
    await page.getByLabel('任务名称', { exact: true }).fill('供应商样表验收');
    await page.getByLabel('选择原始Excel', { exact: true }).setInputFiles('C:/Users/Administrator/Documents/xwechat_files/wxid_0ih8lozninqz22_9cfa/msg/file/2026-10/表格.xlsx');
    await page.getByRole('button', { name: '上传并分析', exact: true }).click();
    await page.waitForURL('**/#task/*');
    await page.getByRole('button', { name: '确认并生成维护表 →', exact: true }).waitFor({ timeout: 30000 });
    assert.equal(await page.locator('.mapping-area article').count(), 3);
    assert.equal(await page.getByRole('combobox', { name: '动态字段类目', exact: true }).count(), 0);
    assert.equal(await page.locator('.mapping-coverage').count(), 3);
    await page.screenshot({ path: path.join(output, 'mapping-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 375, height: 812 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, 'mapping overflow');
    await page.screenshot({ path: path.join(output, 'mapping-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole('button', { name: '确认并生成维护表 →', exact: true }).click();
    await page.getByRole('button', { name: '最终表与下载', exact: true }).waitFor();
    const taskId = page.url().split('#task/')[1];
    let task = await (await page.request.get(`${apiRoot}/api/v1/workflow-tasks/${taskId}`)).json();
    assert.equal(task.payload.record_count, 77);
    const rows = await (await page.request.get(`${apiRoot}/api/v1/workflow-tasks/${taskId}/maintenance?limit=100`)).json();
    const corrections = {};
    for (const row of rows.items) {
      for (const issue of row.issues) {
        assert.equal(issue.field, 'warranty_days', 'unexpected issue in sample');
        corrections[row.id] = { warranty_days: row.values_json.warranty_days.replace(/天$/, '') };
      }
    }
    const fixed = await page.request.patch(`${apiRoot}/api/v1/workflow-tasks/${taskId}/maintenance`, { data: { base_revision: task.revision, changes: corrections } });
    assert.equal(fixed.status(), 200);
    await page.reload();
    await page.getByRole('button', { name: '最终表与下载', exact: true }).click();
    assert.equal(await page.getByRole('combobox', { name: '选择最终输出模板', exact: true }).count(), 0);
    await page.getByRole('button', { name: '生成最终表', exact: true }).click();
    await page.getByRole('button', { name: '预览 标准商品表.xlsx', exact: true }).waitFor({ timeout: 30000 });
    await page.getByRole('button', { name: '预览 标准商品表.xlsx', exact: true }).click();
    await page.getByRole('dialog', { name: '最终表预览', exact: true }).waitFor();
    await page.screenshot({ path: path.join(output, 'output-preview-desktop.png'), fullPage: true });
    await page.getByRole('button', { name: '关闭最终表预览', exact: true }).click();
    await page.screenshot({ path: path.join(output, 'output-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 375, height: 812 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, 'output overflow');
    await page.screenshot({ path: path.join(output, 'output-mobile.png'), fullPage: true });
    task = await (await page.request.get(`${apiRoot}/api/v1/workflow-tasks/${taskId}`)).json();
    const run = task.exports[0];
    const result = await page.request.get(`${apiRoot}/api/v1/workflow-tasks/${taskId}/exports/${run.id}/files/${encodeURIComponent('标准商品表.xlsx')}`);
    assert.equal(result.status(), 200);
    fs.writeFileSync(path.join(output, 'standardization-verified.xlsx'), await result.body());
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ taskId, records: task.payload.record_count, status: task.status, browserErrors: errors, screenshots: output }));
  } catch(error) {
    if(page){console.error((await page.locator('body').innerText()).slice(0,3000));await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});}
    throw error;
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
