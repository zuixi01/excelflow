const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const path=require('node:path'),fs=require('node:fs'),assert=require('node:assert/strict');
const out=path.resolve('outputs/name-confirmation-return-20261102');
const partial=process.argv.includes('--partial');
const complete=process.argv.includes('--complete');
const id=partial||complete?JSON.parse(fs.readFileSync(path.join(out,'partial-baseline.json'),'utf8')).id:'task_91d9a3c4444f4eae96f0ca20ff6c981f';
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1500,height:1000}}),errors=[];
 page.setDefaultTimeout(20000);page.on('pageerror',e=>errors.push(e.message));
 try {
  await page.goto(`http://127.0.0.1:5177/#task/${id}`);
  await page.getByLabel('账号',{exact:true}).fill('audit');
  await page.getByLabel('密码',{exact:true}).fill('audit-test-password');
  await page.getByRole('button',{name:'进入工作台',exact:true}).click();
  const input=page.getByLabel('回传名称确认清单',{exact:true});await input.waitFor();
  if(complete){
   await input.setInputFiles(path.join(out,'仅验收_剩余106条.xlsx'));
   const dialog=page.getByRole('dialog',{name:'确认商品名称更新'});await dialog.waitFor();
   assert((await dialog.innerText()).includes('更新 106 条；空白跳过 3 条'));
   await dialog.getByRole('button',{name:'确认更新商品名称',exact:true}).click();
   await page.getByText('已按记录ID更新 106 条商品名称。校验通过，可进入“生成最终表”。',{exact:true}).waitFor();
   await page.getByRole('button',{name:'进入生成最终表',exact:true}).click();
   await page.getByRole('button',{name:'生成最终表',exact:true}).click();
   console.log('PASS remaining 106 updated; existing 3 retained. Generating final.');
   await page.getByRole('button',{name:'预览 标准商品表.xlsx',exact:true}).first().waitFor({timeout:240000});
   await page.getByRole('button',{name:'预览 标准商品表.xlsx',exact:true}).first().click();
   await page.getByRole('dialog').filter({hasText:'标准商品表.xlsx'}).waitFor();
   await page.screenshot({path:path.join(out,'106条补齐后最终表页面预览.png'),fullPage:false});
   assert.equal(errors.length,0,JSON.stringify(errors));
   console.log('PASS completed final export and preview.');
   return;
  }
  if(partial){
   await input.setInputFiles(path.join(out,'仅验收_填写3条.xlsx'));
   const dialog=page.getByRole('dialog',{name:'确认商品名称更新'});await dialog.waitFor();
   assert((await dialog.innerText()).includes('更新 3 条；空白跳过 106 条'));
   await page.screenshot({path:path.join(out,'3条名称回传预览.png'),fullPage:false});
   await dialog.getByRole('button',{name:'确认更新商品名称',exact:true}).click();
   await page.getByText('已按记录ID更新 3 条商品名称。仍有 106 个校验问题，请处理后生成。',{exact:true}).waitFor();
   assert.equal(errors.length,0,JSON.stringify(errors));
   await page.screenshot({path:path.join(out,'3条名称回传完成.png'),fullPage:false});
   console.log('PASS uploaded 3 names through browser, confirmed 3 updates and 106 blank skips');
   return;
  }
  await input.setInputFiles(path.join(out,'仅验收_重复ID.xlsx'));
  await page.getByRole('alert').filter({hasText:'记录ID重复'}).waitFor();
  console.log('PASS duplicate ID rejected');
  await input.setInputFiles(path.resolve('outputs/power-name-confirmation-20261102/109条商品名称待确认.xlsx'));
  let dialog=page.getByRole('dialog',{name:'确认商品名称更新'});await dialog.waitFor();
  assert((await dialog.innerText()).includes('空白跳过 109 条'));
  assert(await dialog.getByRole('button',{name:'确认更新商品名称',exact:true}).isDisabled());
  await dialog.getByRole('button',{name:'取消名称更新',exact:true}).click();
  console.log('PASS blank confirmations leave task unchanged; cancel works');
  await input.setInputFiles(path.join(out,'仅验收_109条已填写.xlsx'));await dialog.waitFor();
  assert((await dialog.innerText()).includes('更新 109 条'));
  await dialog.getByRole('button',{name:'下一页名称'}).click();
  await dialog.getByRole('button',{name:'上一页名称'}).click();
  await page.screenshot({path:path.join(out,'名称差异预览.png'),fullPage:false});
  await dialog.getByRole('button',{name:'确认更新商品名称',exact:true}).click();
  await page.getByText('已按记录ID更新 109 条商品名称。校验通过，可进入“生成最终表”。',{exact:true}).waitFor();
  await page.getByRole('button',{name:'进入生成最终表',exact:true}).click();
  await page.getByRole('button',{name:'生成最终表',exact:true}).click();
  console.log('PASS 109 names applied by ID; final generation started');
  await page.getByRole('button',{name:'预览 标准商品表.xlsx',exact:true}).first().waitFor({timeout:180000});
  await page.getByRole('button',{name:'预览 标准商品表.xlsx',exact:true}).first().click();
  await page.getByRole('dialog').filter({hasText:'标准商品表.xlsx'}).waitFor();
  assert.equal(errors.length,0,JSON.stringify(errors));
  await page.screenshot({path:path.join(out,'最终表预览.png'),fullPage:false});
  fs.writeFileSync(path.join(out,'browser.json'),JSON.stringify({passed:true,checked:['duplicate IDs','blank skip','cancel','pagination','109 reversed records','apply names','final export','preview'],pageErrors:errors},null,2));
  console.log('PASS final generated and previewed; no page errors');
 } catch(e){await page.screenshot({path:path.join(out,'failure.png'),fullPage:false});throw e;}
 finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
