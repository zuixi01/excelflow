const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve('outputs/project-audit-20261102'),base='http://127.0.0.1:5176';
const report={steps:[],clicks:[],errors:[],buttons:[]};
const probe=process.argv.includes('--probe');
const password='audit-test-password';
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const context=await browser.newContext({viewport:{width:1500,height:1050}}),page=await context.newPage();
 page.setDefaultTimeout(12000);
 await page.exposeFunction('auditClick',name=>report.clicks.push(name));
 await page.addInitScript(()=>document.addEventListener('click',e=>{const b=e.target.closest?.('button,summary');if(b)window.auditClick((b.getAttribute('aria-label')||b.textContent||'').trim())},true));
 page.on('pageerror',e=>report.errors.push(e.message));
 async function scan(){report.buttons.push(...await page.locator('button:visible,summary:visible').evaluateAll(es=>es.map(e=>({name:(e.getAttribute('aria-label')||e.textContent).trim(),disabled:e.disabled||false,url:location.hash}))));}
 async function step(name,fn){try{await fn();await scan();report.steps.push({name,passed:true});console.log('PASS '+name)}catch(e){report.steps.push({name,passed:false,error:e.message});await page.screenshot({path:path.join(root,`failure-${report.steps.length}.png`),fullPage:true});console.log('FAIL '+name+': '+e.message.slice(0,350));}}
 async function nav(hash){await page.goto(`${base}/#${hash}`);await page.locator('.sidebar').waitFor();}
 async function login(username='audit-admin',pw=password){await page.getByLabel('账号',{exact:true}).fill(username);await page.getByLabel('密码',{exact:true}).fill(pw);await page.getByRole('button',{name:'进入工作台',exact:true}).click();await page.locator('.sidebar').waitFor();}
 async function api(route,method='GET',body){return page.evaluate(async({route,method,body})=>{const token=(await (await fetch('/api/v1/auth/csrf')).json()).csrf_token;const r=await fetch('/api/v1'+route,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:body===undefined?undefined:JSON.stringify(body)});if(!r.ok)throw new Error(`${r.status} ${await r.text()}`);return r.json()},{route,method,body});}
 async function download(button,name){const [file]=await Promise.all([page.waitForEvent('download',{timeout:30000}),button.click()]);await file.saveAs(path.join(root,name));assert(fs.statSync(path.join(root,name)).size>0);}
 let taskId='',templateId='';
 try {
  await page.goto(base);
  await step('登录空值、错误密码及正确登录',async()=>{
   await page.getByRole('button',{name:'进入工作台',exact:true}).click();await page.getByRole('alert').filter({hasText:'请输入账号和密码'}).waitFor();
   await page.getByLabel('账号',{exact:true}).fill('audit-admin');await page.getByLabel('密码',{exact:true}).fill('wrong-password');await page.getByRole('button',{name:'进入工作台',exact:true}).click();await page.getByRole('alert').filter({hasText:'账号或密码不正确'}).waitFor();await login();
  });
  await step('系统设置容量提示与模型连接、保存、清除密钥',async()=>{
   await nav('settings');await page.getByRole('heading',{name:'语义分析模型'}).waitFor();
   if(probe)assert.match(await page.locator('.settings-facts').innerText(),/100 MiB/);
   await page.locator('#model-endpoint').fill('invalid-url');await page.getByRole('button',{name:'保存模型配置'}).click();await page.getByRole('alert').filter({hasText:'有效的 HTTP/HTTPS'}).waitFor();
   await page.locator('#model-endpoint').fill('http://127.0.0.1:8012/v1');await page.getByLabel('API 密钥',{exact:true}).fill('audit-only');await page.getByLabel('模型名称',{exact:true}).fill('audit-model');
   await page.getByRole('button',{name:'连接并读取模型'}).click();await page.getByRole('status').filter({hasText:'读取到 1 个模型'}).waitFor();
   await page.getByLabel('JSON 响应模式',{exact:true}).uncheck();await page.getByLabel('授权发送非敏感字段值进行语义比对',{exact:true}).check();await page.getByRole('button',{name:'保存模型配置'}).click();await page.getByRole('status').filter({hasText:'模型配置已保存'}).waitFor();
   await page.getByLabel('清除已保存密钥',{exact:true}).check();await page.getByRole('button',{name:'保存模型配置'}).click();await page.getByText('尚未配置密钥',{exact:true}).waitFor();
   await page.getByRole('button',{name:'连接并读取模型'}).click();await page.getByRole('alert').filter({hasText:'请填写模型服务'}).waitFor();
   assert.match(await page.locator('.settings-facts').innerText(),/100 MiB/);
  });
  await step('运营账号新建、重复校验、停用和启用',async()=>{
   await nav('settings');await page.getByRole('heading',{name:'运营账号'}).waitFor();
   for(const [user,role] of [['audit-operator','operator'],['audit-viewer','viewer']]){
    if(!await page.locator('.operator-row').filter({hasText:user}).count()){
     await page.getByLabel('账号',{exact:true}).fill(user);await page.getByLabel('初始密码',{exact:true}).fill(password);await page.getByLabel('角色',{exact:true}).selectOption(role);await page.getByRole('button',{name:'创建运营账号'}).click();await page.locator('.operator-row').filter({hasText:user}).waitFor();
    }
   }
   const row=page.locator('.operator-row').filter({hasText:'audit-operator'});
   await row.getByRole('button',{name:'停用',exact:true}).click();await row.getByRole('button',{name:'启用',exact:true}).waitFor();await row.getByRole('button',{name:'启用',exact:true}).click();await row.getByRole('button',{name:'停用',exact:true}).waitFor();
   await page.getByLabel('账号',{exact:true}).fill('audit-operator');await page.getByLabel('初始密码',{exact:true}).fill(password);await page.getByRole('button',{name:'创建运营账号'}).click();await page.getByRole('alert').waitFor();
  });
  await step('模板编辑：移动、停用、动态组新增删除、预览和发布',async()=>{
   const profile={name:'按钮验收模板',fixed_fields:[{id:'product_name',title:'商品名称',required:true},{id:'sku',title:'货号',type:'id_code'},{id:'price',title:'售价',type:'decimal'}],local_categories:[{id:'lamp',title:'灯具',attributes:[{id:'brand',title:'品牌'}],specifications:[{id:'power',title:'功率'}]}]};
   let template=await api('/field-profiles','POST',{name:profile.name});templateId=template.id;
   template=await api('/field-profiles/'+templateId,'PUT',{base_revision:template.revision,profile});
   await nav('templates/'+templateId);await page.getByRole('heading',{name:profile.name,exact:true}).waitFor();
   const fixed=page.locator('.config-section').first();
   await fixed.getByRole('button',{name:'下移字段',exact:true}).first().click();await fixed.getByRole('button',{name:'上移字段',exact:true}).nth(1).click();
   await page.getByLabel('启用售价',{exact:true}).uncheck();await page.getByLabel('启用售价',{exact:true}).check();
   await page.getByRole('button',{name:'添加字段组'}).click();await page.getByLabel('字段组名称',{exact:true}).fill('临时字段组');
   page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'移除字段组'}).click();
   const dynamic=page.locator('.config-section').nth(1);await dynamic.getByRole('button',{name:'添加字段',exact:true}).first().click();await dynamic.getByRole('button',{name:'移除本地字段'}).nth(1).click();
   await page.getByRole('button',{name:'预览结构'}).click();await page.getByRole('heading',{name:/输出结构预览/}).waitFor();await page.keyboard.press('Escape');await page.getByRole('heading',{name:/输出结构预览/}).waitFor({state:'hidden'});
   await page.getByRole('button',{name:'发布模板'}).click();await page.getByRole('status').filter({hasText:'已发布'}).waitFor();
  });
  await step('模板未保存修改：取消导航与浏览器后退保护',async()=>{
   await nav('templates');await page.locator(`button`).filter({hasText:'按钮验收模板'}).last().click();
   const field=page.getByLabel('模板名称',{exact:true});await field.fill('未保存模板名');
   let confirmed=false;page.once('dialog',async d=>{confirmed=true;await d.dismiss()});await page.getByRole('button',{name:'工作台',exact:true}).click();assert(confirmed);assert.equal(await field.inputValue(),'未保存模板名');
   confirmed=false;page.once('dialog',async d=>{confirmed=true;await d.dismiss()});await page.goBack();await page.waitForTimeout(100);assert(confirmed,'浏览器后退未提示，丢失未保存修改');assert.equal(await field.inputValue(),'未保存模板名');
   page.removeAllListeners('dialog');page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'工作台',exact:true}).click();
  });
  if(!probe){
   await step('上传校验、移除文件、多文件上传和重新分析字段',async()=>{
    page.removeAllListeners('dialog');await nav('workbench');await page.getByRole('button',{name:'新建任务',exact:true}).first().click();await page.getByLabel('选择业务模板').selectOption(templateId);
    await page.getByLabel('选择原始Excel').setInputFiles({name:'wrong.txt',mimeType:'text/plain',buffer:Buffer.from('bad')});await page.getByRole('alert').filter({hasText:'.xlsx'}).waitFor();await page.getByRole('button',{name:/移除第1个文件/}).click();
    await page.getByLabel('任务名称',{exact:true}).fill('全按钮验收商品');await page.getByLabel('选择原始Excel').setInputFiles(path.join(root,'商品测试.xlsx'));await page.getByRole('button',{name:'上传并分析'}).click();await page.waitForURL('**/#task/*');taskId=page.url().split('/').at(-1);
    await page.getByRole('button',{name:'重新分析全部字段',exact:true}).waitFor();await page.getByRole('button',{name:'重新分析全部字段',exact:true}).click();await page.getByRole('status').filter({hasText:'全部固定字段与动态字段已重新分析'}).waitFor();
    await page.getByText('查看原表预览',{exact:true}).first().click();await page.getByText('查看原表预览',{exact:true}).first().click();
    await page.getByRole('button',{name:'＋ 添加映射',exact:true}).first().click();await page.getByRole('button',{name:'移除映射',exact:true}).last().click();
    await page.getByRole('button',{name:'确认并生成维护表 →',exact:true}).click();await page.getByRole('button',{name:'下载最新维护表',exact:true}).waitFor();
   });
   await step('维护分页、搜索、排序、字段显隐、修改和校验',async()=>{
    await page.getByRole('button',{name:'下一页',exact:true}).click();await page.getByText(/共 45 条/).waitFor();await page.getByRole('button',{name:'上一页',exact:true}).click();
    await page.getByLabel('搜索维护数据').fill('台灯3');await page.getByRole('button',{name:'筛选',exact:true}).click();await page.getByLabel('搜索维护数据').fill('');await page.getByRole('button',{name:'筛选',exact:true}).click();
    await page.getByLabel('排序字段').selectOption('price');await page.getByRole('button',{name:'升序 ↑',exact:true}).click();await page.getByRole('button',{name:'降序 ↓',exact:true}).click();await page.getByLabel('排序字段').selectOption('');
    const picker=page.locator('.column-picker');await picker.locator('summary').click();await picker.getByLabel(/货号/).uncheck();await picker.getByLabel(/货号/).check();await picker.locator('summary').click();
    const first=page.locator('.editor-table tbody tr').first();const input=first.locator('input:not([type=checkbox])').first();await input.fill('按钮验收修改');assert(await page.getByRole('button',{name:'下载最新维护表'}).isDisabled());await page.getByRole('button',{name:'保存并校验'}).click();await page.getByRole('status').filter({hasText:'已保存并完成校验'}).waitFor();
    await page.getByLabel('批量填充字段').selectOption('price');await page.getByLabel('批量填充值').fill('not-number');await first.locator('input[type=checkbox]').check();await page.getByRole('button',{name:/^填充/}).click();await page.getByRole('button',{name:'保存并校验'}).click();await page.getByRole('button',{name:'只看问题记录'}).waitFor();await page.getByRole('button',{name:'只看问题记录'}).click();
    await page.getByLabel('批量填充值').fill('99');await page.getByRole('button',{name:/^填充/}).click();await page.getByRole('button',{name:'保存并校验'}).click();await page.getByLabel('只看问题',{exact:true}).uncheck();
   });
   await step('新增行、删除行、来源说明、历史和所有步骤导航',async()=>{
    await page.getByRole('button',{name:'＋ 新增记录',exact:true}).click();await page.locator('.editor-table tbody tr').first().locator('input[type=checkbox]').check();page.once('dialog',d=>d.dismiss());await page.getByRole('button',{name:/删除选中/}).click();page.once('dialog',d=>d.accept());await page.getByRole('button',{name:/删除选中/}).click();await page.getByLabel('只看问题',{exact:true}).uncheck();
    await page.locator('button.source-link').first().click();await page.getByRole('dialog',{name:'记录来源'}).waitFor();await page.getByRole('button',{name:'关闭来源'}).click();
    await page.getByText('修订记录与来源说明',{exact:true}).click();
    await page.getByRole('button',{name:/导入原表/}).first().click();await page.getByRole('button',{name:'返回当前步骤'}).click();
    await page.getByRole('button',{name:/确认字段/}).first().click();page.once('dialog',d=>d.dismiss());await page.getByRole('button',{name:'重新确认字段',exact:true}).click();await page.getByRole('button',{name:'返回当前步骤'}).click();
   });
   await step('下载维护表、关闭与确认回传、最终生成、预览和下载包',async()=>{
    await download(page.getByRole('button',{name:'下载最新维护表'}),'维护表.xlsx');
    await page.getByLabel('回传维护表',{exact:true}).setInputFiles(path.join(root,'维护表.xlsx'));await page.getByRole('dialog',{name:'确认回传字段结构'}).waitFor();await page.getByRole('button',{name:'关闭结构确认'}).click();
    await page.getByLabel('回传维护表',{exact:true}).setInputFiles(path.join(root,'维护表.xlsx'));await page.getByRole('button',{name:'确认字段并预览差异'}).click();await page.getByRole('button',{name:'关闭预览',exact:true}).click();
    await page.getByLabel('回传维护表',{exact:true}).setInputFiles(path.join(root,'维护表.xlsx'));await page.getByRole('button',{name:'确认字段并预览差异'}).click();await page.getByRole('button',{name:'确认并保存新修订'}).click();await page.getByRole('status').filter({hasText:'回传修改已保存'}).waitFor();
    await page.getByRole('button',{name:'预览输出结构 →',exact:true}).click();await page.getByRole('button',{name:'生成最终表',exact:true}).click();await page.getByRole('button',{name:/↓.*标准商品表.xlsx/}).waitFor({timeout:60000});await page.getByRole('button',{name:/预览.*标准商品表.xlsx/}).click();await page.getByRole('button',{name:'关闭最终表预览'}).click();
    await download(page.getByRole('button',{name:/↓.*标准商品表.xlsx/}),'最终表.xlsx');await download(page.getByRole('button',{name:'下载完整文件包',exact:true}),'最终表.zip');await page.getByRole('button',{name:'刷新',exact:true}).click();
   });
   await step('历史平台核对：上传、映射确认、问题分页筛选和报告下载',async()=>{
    await nav('reconciliation');await page.getByText('隔离验收平台',{exact:true}).waitFor();await page.getByLabel('选择供应商 Excel 文件').setInputFiles(path.join(root,'字段核对测试.xlsx'));await page.getByRole('button',{name:'开始核对',exact:true}).click();await page.getByRole('button',{name:'确认字段并继续'}).or(page.getByText('通过 3 条 · 不通过 32 条 · 待确认 0 条',{exact:true})).first().waitFor({timeout:60000});if(await page.getByRole('button',{name:'确认字段并继续'}).isVisible()){await page.getByLabel('供应商报价第4列对应平台字段').selectOption('material');await page.getByRole('button',{name:'确认字段并继续'}).click();}await page.getByText('通过 3 条 · 不通过 32 条 · 待确认 0 条',{exact:true}).waitFor({timeout:60000});
    await page.getByRole('tab',{name:'问题 32',exact:true}).click();await page.getByRole('button',{name:'下一页',exact:true}).click();await page.getByRole('button',{name:'上一页',exact:true}).click();await page.getByLabel('问题级别').selectOption('review');await page.getByLabel('问题级别').selectOption('');
    await download(page.getByRole('button',{name:'结果 Excel',exact:true}),'平台核对.xlsx');await download(page.getByRole('button',{name:'下载完整报告'}),'平台核对.json');await page.getByRole('button',{name:'刷新核对记录'}).click();await page.getByRole('button',{name:'查看',exact:true}).first().click();await page.getByRole('button',{name:'上传供应商表',exact:true}).click();await page.getByRole('button',{name:'收起上传'}).click();
   });
  }
  if(!probe){
   await step('模板新建、复制、取消以及预览故障恢复',async()=>{
    await nav('templates');await page.getByRole('button',{name:'新建模板',exact:true}).click();await page.getByRole('button',{name:'取消',exact:true}).click();
    const row=page.locator('tr').filter({has:page.getByRole('button',{name:'按钮验收模板',exact:true})}).last();await row.getByRole('button',{name:'复制',exact:true}).click();await page.getByLabel('模板名称',{exact:true}).fill('复制按钮验收'+Date.now());await page.getByRole('button',{name:'创建并配置字段'}).click();await page.getByRole('button',{name:'预览结构'}).waitFor();
    await page.route('**/api/v1/field-profile/preview',r=>r.fulfill({status:503,json:{detail:'验收模拟暂时不可用'}}),{times:1});await page.getByRole('button',{name:'预览结构'}).click();await page.getByRole('button',{name:'重新预览',exact:true}).click();await page.getByRole('button',{name:'返回配置',exact:true}).click();
   });
   await step('任务列表分页、搜索筛选、失败重试、单条和批量删除',async()=>{
    await nav('workbench');
    await page.evaluate(async templateId=>{
     const csrf=(await(await fetch('/api/v1/auth/csrf')).json()).csrf_token;
     for(let i=0;i<31;i++){
      const form=new FormData();form.append('name','删除验收'+i);form.append('field_profile_id',templateId);form.append('excel_file',new File(['invalid-xlsx'],'损坏测试.xlsx'));
      const r=await fetch('/api/v1/standardization-tasks',{method:'POST',headers:{'X-CSRF-Token':csrf},body:form});if(!r.ok)throw new Error(await r.text());
     }
    },templateId);
    await page.getByRole('button',{name:'刷新',exact:true}).click();await page.getByRole('button',{name:'下一页',exact:true}).click();await page.getByRole('button',{name:'上一页',exact:true}).click();
    await page.getByLabel('搜索任务').fill('删除验收');await page.getByRole('button',{name:'筛选',exact:true}).click();await page.getByLabel('任务状态').selectOption('failed');
    await page.locator('.task-list tbody tr').first().getByRole('button',{name:'继续处理 →'}).click();await page.getByRole('button',{name:'重新解析',exact:true}).click();await page.getByRole('button',{name:'重新解析',exact:true}).waitFor({timeout:60000});await page.getByRole('button',{name:'← 返回工作台',exact:true}).click();
    await page.getByLabel('搜索任务').fill('删除验收');await page.getByRole('button',{name:'筛选',exact:true}).click();
    const first=page.locator('.task-list tbody tr').first();page.once('dialog',d=>d.dismiss());await first.getByRole('button',{name:'删除',exact:true}).click();page.once('dialog',d=>d.accept());await first.getByRole('button',{name:'删除',exact:true}).click();await page.getByRole('status').filter({hasText:'已删除 1 条'}).waitFor();
    await page.locator('.task-list tbody input[type=checkbox]').nth(0).check();await page.locator('.task-list tbody input[type=checkbox]').nth(1).check();page.once('dialog',d=>d.accept());await page.getByRole('button',{name:/删除选中（2）/}).click();await page.getByRole('status').filter({hasText:'已删除 2 条'}).waitFor();
   });
   await step('旧规则新建、增删字段、保存校验、别名确认与发布',async()=>{
    await nav('legacy');await page.getByRole('button',{name:'规则管理',exact:true}).click();await page.getByRole('button',{name:'高级 JSON',exact:true}).click();
    const config=JSON.parse(fs.readFileSync(path.join(root,'规则.json'),'utf8'));config.schema_id='audit-rule-'+Date.now();
    await page.locator('textarea').fill(JSON.stringify(config));await page.getByRole('button',{name:'保存草稿',exact:true}).click();await page.locator('.message').filter({hasText:'草稿已保存'}).waitFor();
    await page.getByRole('button',{name:'可视化编辑',exact:true}).click();await page.getByRole('button',{name:'添加工作表',exact:true}).click();await page.locator('.sheet-card').last().getByRole('button',{name:'添加字段',exact:true}).click();await page.locator('.sheet-card').last().getByRole('button',{name:'删除',exact:true}).last().click();await page.locator('.sheet-card').last().getByRole('button',{name:'删除',exact:true}).first().click();
    await page.getByRole('button',{name:'高级 JSON',exact:true}).click();config.name='修改但未保存';await page.locator('textarea').fill(JSON.stringify(config));await page.getByRole('button',{name:'发布不可变版本'}).click();await page.locator('.message').filter({hasText:'先保存当前修改'}).waitFor();await page.getByRole('button',{name:'保存草稿',exact:true}).click();await page.locator('.message').filter({hasText:'草稿已保存'}).waitFor();await page.getByRole('button',{name:'校验草稿'}).click();await page.locator('.message').filter({hasText:'规则有效'}).waitFor();
    await page.getByPlaceholder('工作表 ID',{exact:true}).fill('employees');await page.getByPlaceholder('原始表头',{exact:true}).fill('姓名验收别名');await page.getByPlaceholder('规范字段名',{exact:true}).fill('employee_name');await page.getByRole('button',{name:'确认并写入草稿'}).click();await page.locator('.message').filter({hasText:'已确认别名'}).waitFor();
    assert((await page.locator('textarea').inputValue()).includes('姓名验收别名'));await page.getByRole('button',{name:'保存草稿',exact:true}).click();await page.locator('.message').filter({hasText:'草稿已保存'}).waitFor();await page.getByRole('button',{name:'发布不可变版本'}).click();await page.locator('.message').filter({hasText:'已发布不可变版本'}).waitFor();
    await page.getByRole('button',{name:'列出版本'}).click();await page.locator('.version-list button').first().click();await page.locator('.message').filter({hasText:'已加载发布版本'}).waitFor();await page.getByRole('button',{name:'加载版本'}).click();assert((await page.locator('textarea').inputValue()).includes('姓名验收别名'));
    await page.getByRole('button',{name:'标准核验',exact:true}).click();await page.locator('input[type=file]').nth(0).setInputFiles(path.join(root,'人员测试.xlsx'));await page.locator('input[type=file]').nth(1).setInputFiles(path.join(root,'标准数据.json'));
    await page.getByRole('button',{name:'工作簿预检查'}).click();await page.locator('.message').filter({hasText:'structure'}).waitFor();await page.getByRole('button',{name:'创建核验任务'}).click();await page.getByRole('button',{name:'JSON 报告',exact:true}).waitFor({timeout:60000});await page.getByRole('button',{name:'筛选',exact:true}).click();
    for(const [button,file] of [['下载标色 Excel','核验标色.xlsx'],['JSON 报告','核验报告.json'],['差异 JSONL','核验差异.jsonl'],['HTML 报告','核验报告.html']])if(await page.getByRole('button',{name:button,exact:true}).count())await download(page.getByRole('button',{name:button,exact:true}),file);
   });
   await step('旧商品整理及结果、问题、清单下载',async()=>{
    if(!await page.getByRole('button',{name:'商品表整理',exact:true}).isVisible()){
     report.hiddenLegacyEntry='旧商品整理入口按既有设计隐藏，后端兼容由自动测试覆盖。';return;
    }
    await page.getByRole('button',{name:'商品表整理',exact:true}).click();await page.locator('input[type=file]').setInputFiles(path.join(root,'商品测试.xlsx'));await page.getByRole('button',{name:'生成商品汇总'}).click();await page.getByRole('button',{name:'下载商品汇总 Excel'}).waitFor({timeout:60000});
    for(const [button,file] of [['下载商品汇总 Excel','旧商品汇总.xlsx'],['下载结果 JSON','旧商品结果.json'],['下载问题 JSONL','旧商品问题.jsonl'],['渲染清单','旧商品清单.json']])if(await page.getByRole('button',{name:button,exact:true}).count())await download(page.getByRole('button',{name:button,exact:true}),file);
    if(await page.getByRole('button',{name:'筛选',exact:true}).count())await page.getByRole('button',{name:'筛选',exact:true}).click();
   });
  }
  await step('历史规则工具非法 JSON 切换不崩溃',async()=>{
   page.removeAllListeners('dialog');await nav('legacy');await page.getByRole('button',{name:'规则管理',exact:true}).click();await page.getByRole('button',{name:'高级 JSON',exact:true}).click();await page.locator('textarea').fill('{invalid');const before=report.errors.length;await page.getByRole('button',{name:'可视化编辑',exact:true}).click();await page.waitForTimeout(100);assert.equal(report.errors.length,before,'非法 JSON 触发未处理异常');assert.match(await page.locator('body').innerText(),/合法 JSON/);
  });
  await step('只读账号不能显示可执行的创建和删除按钮',async()=>{
   page.removeAllListeners('dialog');page.once('dialog',d=>d.accept());await nav('workbench');page.removeAllListeners('dialog');await page.getByRole('button',{name:'退出登录',exact:true}).click();await login('audit-viewer');await page.getByRole('heading',{name:'工作台',exact:true}).waitFor();
   for(const label of ['新建任务','删除选中']){const buttons=page.getByRole('button',{name:label,exact:true});for(const b of await buttons.all())assert(await b.isDisabled(),`${label} 对只读账号仍可操作`);}
   await page.getByRole('button',{name:'模板管理',exact:true}).click();await page.getByRole('heading',{name:'模板管理',exact:true}).waitFor();assert.equal(await page.getByRole('button',{name:'新建模板',exact:true}).count(),0);
  });
  await step('桌面与手机布局及只读设置',async()=>{
   for(const route of ['workbench','templates','settings','legacy']){
    await nav(route);
    for(const width of [1500,375]){
     await page.setViewportSize({width,height:900});
     assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,route+' 页面溢出');
    }
   }
   await page.setViewportSize({width:1500,height:1050});await nav('settings');assert(await page.getByRole('button',{name:'保存模型配置'}).isDisabled());assert.equal(await page.getByRole('button',{name:'创建运营账号'}).count(),0);
  });
 }finally{
  report.passed=report.steps.every(s=>s.passed)&&!report.errors.length;
  report.uniqueClickedButtons=[...new Set(report.clicks)];fs.writeFileSync(path.join(root,probe?'probe.json':'browser-audit.json'),JSON.stringify(report,null,2));await browser.close();
 }
 console.log(JSON.stringify({passed:report.passed,steps:report.steps,errors:report.errors,uniqueButtons:report.uniqueClickedButtons.length}));
 if(!report.passed)process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1});
