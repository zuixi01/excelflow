const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const output = path.resolve('.codex-artifacts/review-fixes-ui');
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  const errors = [];
  try {
    for (const role of ['owner', 'operator']) {
      const context = await browser.newContext();
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/api/v1/**', async route => {
        const url = new URL(route.request().url());
        const job = {job_id:'job_test', revision:0, status:'completed', progress:100, input_file_name:'supplier.xlsx',
          created_at:'2026-10-16T10:00:00Z', created_by_id:'admin', created_by_username:'admin',
          summary:{source_records:100,passed_records:100}, issue_count:0};
        let body;
        switch (url.pathname) {
          case '/api/v1/auth/session': body={authenticated:true,auth_required:true,username:role==='owner'?'admin':'alice',role}; break;
          case '/api/v1/workflow-settings': body={can_manage_templates:role==='owner'}; break;
          case '/api/v1/field-reconciliations/connections': body={items:[]}; break;
          case '/api/v1/field-reconciliations': body={items:[job],total:1}; break;
          case '/api/v1/field-reconciliations/job_test': body=job; break;
          case '/api/v1/field-reconciliations/job_test/mappings': body={items:[],fields:[]}; break;
          case '/api/v1/field-reconciliations/job_test/issues': body={items:[],total:0}; break;
          default: throw Error(url.pathname);
        }
        await route.fulfill({json:body});
      });
      await page.goto('http://127.0.0.1:5173/#workbench?mode=reconciliation');
      await page.getByRole('button',{name:'查看',exact:true}).click();
      await page.getByRole('heading',{name:'核对结果',exact:true}).waitFor();
      assert.equal(await page.getByRole('columnheader',{name:'创建账号',exact:true}).count(),role==='owner'?1:0);
      if(role==='owner') await page.getByText('创建账号：admin',{exact:true}).waitFor();
      for (const [label,width,height] of [['desktop',1440,1000],['mobile',375,812]]) {
        await page.setViewportSize({width,height});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
        await page.screenshot({path:path.join(output,`${role}-${label}.png`),fullPage:true});
      }
      await context.close();
    }
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({roles:['owner','operator'],browserErrors:errors,screenshots:output}));
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
