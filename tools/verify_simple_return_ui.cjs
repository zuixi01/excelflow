const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const out=path.resolve('outputs/simple-return-20261103');
 const fixture=JSON.parse(fs.readFileSync(path.join(out,'fixture.json'),'utf8'));
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[];
 page.on('pageerror',error=>errors.push(error.message));page.setDefaultTimeout(20000);
 let confirmBody,failSave=true,exportRequests=0;
 try{
  await page.route('**/api/**',async route=>{
   if(route.request().url().endsWith('/auth/csrf')){await route.fulfill({json:{csrf_token:'isolated-qa'}});return;}
   if(route.request().url().endsWith('/maintenance/returns/apply')){
    confirmBody=route.request().postDataJSON();
    if(failSave){await route.fulfill({status:500,json:{detail:'测试保存失败，请重试'}});return;}
   }
   if(route.request().method()==='POST'&&route.request().url().endsWith('/exports'))exportRequests++;
   await route.continue();
  });
  await page.route('**/simple-return-test',route=>route.fulfill({contentType:'text/html',body:`<html><meta charset="utf-8"><body class="sop" style="display:block;padding:24px"><div id="test"></div><script type="module">
   import {createApp} from '/node_modules/.vite/deps/vue.js';
   import Component from '/src/workflow/TaskDetail.vue';
   import '/src/workflow/style.css';
   createApp(Component,{id:${JSON.stringify(fixture.task_id)}}).mount('#test');
  </script></body></html>`}));
  await page.goto('http://127.0.0.1:5178/simple-return-test');
  await page.getByLabel('回传维护表',{exact:true}).setInputFiles(fixture.uncertain_file);
  const unresolved=page.getByRole('dialog',{name:'维护表回传预览'});
  await unresolved.getByRole('heading',{name:'以下行需要核对对应关系'}).waitFor();
  await unresolved.getByLabel('商品行处理',{exact:true}).waitFor();
  assert.equal(await unresolved.getByLabel('商品行处理',{exact:true}).count(),1);
  assert.equal(await unresolved.getByLabel('回传列处理',{exact:true}).count(),0);
  await unresolved.getByRole('button',{name:'取消',exact:true}).click();
  await page.getByLabel('回传维护表',{exact:true}).setInputFiles(fixture.file);
  const preview=page.getByRole('dialog',{name:'维护表回传预览'});
  await preview.waitFor();
  assert.equal(await page.getByRole('dialog',{name:'确认回传字段结构'}).count(),0);
  await preview.getByRole('table',{name:'字段修改对照'}).getByText('厂牌',{exact:true}).waitFor();
  await preview.getByText('C 列 · C5',{exact:true}).waitFor();
  assert.equal(await preview.locator('.diff-before').first().innerText(),'10');
  assert.equal(await preview.locator('.diff-after').first().innerText(),'21');
  const changed=await page.request.get('http://127.0.0.1:8017/api/v1/workflow-tasks/'+fixture.task_id);
  assert.equal((await changed.json()).revision,1,'preview must not mutate task');
  await page.screenshot({path:path.join(out,'上传后直接展示改动.png')});
  await preview.getByRole('heading',{name:'新增行（1 行）',exact:true}).waitFor();
  await preview.getByRole('heading',{name:'文件中删除的行（1 行）',exact:true}).waitFor();
  assert.equal(await preview.getByText('第 6 行',{exact:true}).count(),2);
  await preview.getByText('确认后将删除 1 条缺少记录',{exact:true}).waitFor();
  assert.equal(await preview.getByLabel('同时删除回传文件中缺少的记录（默认保留）').count(),0);
  await preview.getByRole('button',{name:'确认保存这些改动',exact:true}).click();
  await preview.getByText('测试保存失败，请重试',{exact:true}).waitFor();
  assert(await preview.isVisible(),'failed save must keep preview open');
  assert.equal(await page.getByRole('status',{name:'回传保存成功'}).count(),0);
  assert.equal(exportRequests,0);
  failSave=false;
  await preview.getByRole('button',{name:'确认保存这些改动',exact:true}).click();
  await preview.waitFor({state:'hidden'});
  await page.getByRole('status',{name:'回传保存成功'}).waitFor();
  await page.getByRole('heading',{name:'本次输出方案',exact:true}).waitFor();
  assert.match(await page.getByRole('button',{name:'最终表与下载',exact:true}).getAttribute('class'),/active/);
  assert.equal(exportRequests,0,'saving must not generate exports');
  assert.equal(confirmBody.confirm_structure,true);
  const saved=await page.request.get('http://127.0.0.1:8017/api/v1/workflow-tasks/'+fixture.task_id);
  const task=await saved.json();assert.equal(task.revision,2);assert.equal(task.payload.record_count,3);
  assert.equal(task.exports.length,0,'no automatic export exists after save');
  const records=await (await page.request.get('http://127.0.0.1:8017/api/v1/workflow-tasks/'+fixture.task_id+'/maintenance')).json();
  const added=records.items.find(r=>!['row_0','row_1','row_2'].includes(r.id));
  assert(added && added.values_json.sku==='');
  await page.screenshot({path:path.join(out,'回传保存成功自动进入最终表.png')});
  await page.getByRole('button',{name:'维护数据',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('input[aria-label="回传维护表"]')?.disabled===false);
  await page.getByLabel('回传维护表',{exact:true}).setInputFiles(fixture.file);
  await page.getByRole('status',{name:'回传保存成功'}).getByText(/这份文件的修改已保存到 R2/).waitFor();
  assert.equal(await preview.count(),0,'repeat must skip diff/save dialog');
  assert.equal(exportRequests,0,'repeat must not export');
  const repeated=await (await page.request.get('http://127.0.0.1:8017/api/v1/workflow-tasks/'+fixture.task_id)).json();
  assert.equal(repeated.revision,2,'repeat must not create a revision');
  await page.screenshot({path:path.join(out,'重复回传已保存提示.png')});
  const generated=page.waitForResponse(response=>response.request().method()==='POST'&&response.url().endsWith('/exports'));
  await page.getByRole('button',{name:'生成最终表',exact:true}).click();
  assert((await generated).ok(),'manual generation must succeed');
  assert.equal(exportRequests,1,'only the manual click may generate');
  let exported;
  for(let attempt=0;attempt<100;attempt++){
   exported=await (await page.request.get('http://127.0.0.1:8017/api/v1/workflow-tasks/'+fixture.task_id)).json();
   if(exported.exports.length)break;
   await new Promise(resolve=>setTimeout(resolve,100));
  }
  assert.equal(exported.exports.length,1);
  await page.waitForFunction(()=>Array.from(document.querySelectorAll('.section-heading button')).find(button=>button.textContent.trim()==='生成最终表')?.disabled===false,undefined,{timeout:60000});
  // A saved workbook may still contain validation errors. Keep its save
  // confirmation, but block generation and offer a direct route to the issues.
  const invalidFile=path.join(out,'测试回传_清空必填品牌.xlsx');
  const workbook=await page.request.get('http://127.0.0.1:8017/api/v1/workflow-tasks/'+fixture.task_id+'/maintenance/download');
  assert(workbook.ok());fs.writeFileSync(invalidFile,await workbook.body());
  require('node:child_process').execFileSync(path.resolve('.venv/Scripts/python.exe'),['-c',
   'import sys; from openpyxl import load_workbook; p=sys.argv[1]; w=load_workbook(p); w["维护表"].cell(4,4).value=None; w.save(p); w.close()',invalidFile]);
  await page.getByRole('button',{name:'维护数据',exact:true}).click();
  await page.getByLabel('回传维护表',{exact:true}).setInputFiles(invalidFile);
  await preview.getByRole('button',{name:'确认保存这些改动',exact:true}).click();
  await preview.waitFor({state:'hidden'});
  await page.getByRole('status',{name:'回传保存成功'}).getByText(/还有 .* 个数据问题/).waitFor();
  assert(await page.getByRole('button',{name:'生成最终表',exact:true}).isDisabled());
  assert.equal(exportRequests,1,'invalid return must not trigger another export');
  await page.getByRole('button',{name:'返回处理问题',exact:true}).click();
  assert(await page.getByRole('checkbox',{name:'只看问题',exact:true}).isChecked());
  require('node:child_process').execFileSync(path.resolve('.venv/Scripts/python.exe'),['-c',
   'import sys; from openpyxl import load_workbook; p=sys.argv[1]; w=load_workbook(p); w["维护表"].cell(4,3).value="100"; w.save(p); w.close()',invalidFile]);
  await page.waitForFunction(()=>document.querySelector('input[aria-label="回传维护表"]')?.disabled===false);
  await page.getByLabel('回传维护表',{exact:true}).setInputFiles(invalidFile);
  await preview.getByText('这是旧版 R2，当前数据为 R3',{exact:true}).waitFor();
  assert(await preview.getByRole('button',{name:'确认保存这些改动',exact:true}).isDisabled());
  await preview.getByText('C 列 · C4',{exact:true}).waitFor();
  await preview.getByRole('checkbox',{name:'我已核对与当前版本的差异，确认按本次回传覆盖修改并同步删除列出的行列',exact:true}).check();
  await page.screenshot({path:path.join(out,'旧版回传差异确认.png')});
  await preview.getByRole('button',{name:'确认保存这些改动',exact:true}).click();
  await preview.waitFor({state:'hidden'});
  await page.getByRole('status',{name:'回传保存成功'}).getByText('维护表回传已保存成功（R4）',{exact:true}).waitFor();
  assert.equal(confirmBody.confirm_stale,true);
  assert.equal(exportRequests,1,'confirmed stale return must still wait for manual export');
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(out,'浏览器验证.json'),JSON.stringify({passed:true,automatic_preview:true,correct_cell:'维护表!C5',before:'10',after:'21',rename:'品牌 → 厂牌',added_row:6,deleted_original_row:6,saved_revision:task.revision,failed_save_keeps_preview:true,save_notice_and_output_navigation:true,no_export_before_click:true,manual_export_requests:exportRequests,errors},null,2));
  console.log('PASS: save/failure/manual export/validation; repeated return skips saving; changed old workbook reviews current differences and requires explicit confirmation');
 }catch(error){await page.screenshot({path:path.join(out,'验证失败.png')});throw error;}finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1;});
