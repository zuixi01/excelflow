import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';

const dir = path.dirname(fileURLToPath(import.meta.url));
const runtime = process.env.CODEX_DOC_NODE_MODULES || 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
const require = createRequire(path.join(runtime, '_resolve.cjs'));
const {marked} = await import(pathToFileURL(require.resolve('marked')).href);
const esc = s => String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const colors = {ink:'#173246', muted:'#526773', line:'#758994', fill:'#edf5f6', strong:'#d9eeeb', alt:'#fff0d8', paper:'#ffffff'};
function diagram(title,w,h,body){
 return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(title)}"><title>${esc(title)}</title><defs><marker id="arrow" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto"><path d="M0 0L9 4.5L0 9Z" fill="${colors.line}"/></marker></defs><style>text{font-family:'Microsoft YaHei','Noto Sans CJK SC',sans-serif;fill:${colors.ink};font-size:17px} .small{font-size:14px;fill:${colors.muted}} .title{font-size:25px;font-weight:600}</style><rect width="100%" height="100%" fill="${colors.paper}"/><text x="32" y="40" class="title">${esc(title)}</text>${body}</svg>`;
}
function box(x,y,w,h,lines,tone='fill'){
 const first = y+h/2-((lines.length-1)*24)/2+6;
 return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="10" fill="${colors[tone]}" stroke="#d3dfe3"/><text text-anchor="middle">${lines.map((l,i)=>`<tspan x="${x+w/2}" y="${first+i*24}"${i?' class="small"':''}>${esc(l)}</tspan>`).join('')}</text>`;
}
function line(d,label='',x=0,y=0){return `<path d="${d}" fill="none" stroke="${colors.line}" stroke-width="1.8" marker-end="url(#arrow)"/>${label?`<text x="${x}" y="${y}" class="small">${esc(label)}</text>`:''}`;}
let a='';
a+=box(375,80,300,74,['Vue 工作台','上传 · 确认 · 维护 · 下载'],'strong');
a+=box(375,210,300,74,['FastAPI 接口','身份 / 权限检查 · 请求校验']);
a+=box(375,340,300,82,['后台执行与任务恢复','开发：本地执行；配置队列：RQ']);
a+=box(375,485,300,82,['Python 标准化业务服务','解析 · 映射 · 校验 · 版本']);
a+=box(375,650,300,80,['openpyxl 新表导出','标准商品表 + 生成说明 + ZIP'],'strong');
a+=box(32,270,270,110,['SQL 数据库','配置 / 任务 / 维护记录','修订 / 执行租约']);
a+=box(32,490,270,95,['本地或共享文件目录','原表 / 解析数据 / 维护表 / 结果']);
a+=box(748,485,290,82,['可选 AI 模型','未匹配字段的语义建议'],'alt');
a+=box(748,155,290,120,['通用核验的独立路径','比对引擎 → 修复 Manifest','→ .NET / OpenXML']);
a+=box(748,320,290,82,['可选 MinIO / S3','通用审计快照与产物']);
a+=line('M525 154V210','HTTP',535,187)+line('M525 284V340')+line('M525 422V485')+line('M525 567V650');
a+=line('M375 246H334V325H302')+line('M375 526H302')+line('M375 526H330V365H302');
a+=line('M675 526H748','表头与字段信息',686,468)+line('M675 246H748','另一路径',682,226)+line('M893 275V320');
a+='<text x="32" y="786" class="small">主流程生成新标准表；.NET 与 MinIO 不属于每一次标准化导出的必经环节。</text>';
fs.writeFileSync(path.join(dir,'architecture.svg'),diagram('ExcelFlow 整体架构',1080,815,a));

let f='';
const nodes=[
 [80,['1 发布字段配置','固定字段 + 所有启用动态字段']],
 [205,['2 上传供应商 Excel','保存原表与字段快照']],
 [330,['3 识别工作表和表头','多层表头 · 来源位置 · 合并范围']],
 [455,['4 确认映射','规则 / AI 建议 → 人工确认']],
 [580,['5 维护记录并校验','类型 · 必填 · 范围等已配置规则']],
 [755,['6 创建独立导出任务','冻结记录版本 · 生成前再校验']],
 [880,['7 生成和下载','标准商品表 · 生成说明 · ZIP']],
];
for(const [y,labels] of nodes) f+=box(230,y,400,76,labels,(y===80||y===880)?'strong':'fill');
for(let i=0;i<4;i++)f+=line(`M430 ${nodes[i][0]+76}V${nodes[i+1][0]}`);
f+=line('M430 656V755','无阻断错误 ready',443,709)+line('M430 831V880');
f+=box(720,580,260,92,['修改或 Excel 回传','错误仍在就继续维护'],'alt');
f+=line('M630 610H720','有错误',650,596)+line('M850 672V710H570V656','保存后重新校验',666,735);
f+=box(720,330,260,92,['保留来源结构','未匹配列 / 纵向合并关系']);
f+=line('M630 368H720');
f+='<text x="230" y="1005" class="small">task：processing → mapping → maintenance / ready → completed</text><text x="230" y="1033" class="small">export 有独立 processing / completed 状态；失败或取消另走恢复流程。</text>';
fs.writeFileSync(path.join(dir,'workflow.svg'),diagram('商品标准化主流程',1030,1065,f));

let m='';
m+=box(40,95,280,90,['适用历史确认映射？','结构与配置等签名相同才复用']);
m+=box(395,95,280,90,['名称与别名匹配','唯一来源 → 规则建议']);
m+=box(750,95,280,90,['剩余目标字段','配置模型时进入语义分析']);
m+=box(750,280,280,100,['AI 整本工作簿请求','各表未匹配目标合并一次请求','仅传字段元数据与源表头'],'alt');
m+=box(395,280,280,100,['服务端严格检查','字段 / 列号 / 跨表 / 类型兼容']);
m+=box(40,280,280,100,['唯一且置信度 ≥ 0.85？','满足：建议；否则：人工选择']);
m+=box(40,485,280,90,['人工确认或调整','包括规则建议和模型建议'],'strong');
m+=box(395,485,280,90,['确定性转换与校验','取值规则 + 字段类型规则'],'strong');
m+=box(750,485,280,90,['无模型 / 失败 / 低置信度','列出原因与未匹配字段','仍可人工处理'],'alt');
m+=line('M320 140H395','不适用',334,122)+line('M675 140H750','未匹配',685,122)+line('M890 185V280');
m+=line('M750 330H675')+line('M395 330H320')+line('M180 380V485','建议 / 手选',193,434)+line('M320 530H395');
m+=line('M40 140H20V530H40','可复用',40,230);
m+=line('M535 185V220H360V450H180V485','规则建议',415,247);
m+=line('M1030 140H1055V530H1030')+line('M890 380V485','请求失败',905,437);
m+=line('M750 551H705V625H180V575','人工补充与确认',462,650);
m+='<text x="40" y="700" class="small">0.85 是建议使用门槛，不代表实测准确率；最终绑定需要人工确认。</text>';
fs.writeFileSync(path.join(dir,'mapping.svg'),diagram('字段映射的决策过程',1080,730,m));

let md=fs.readFileSync(path.join(dir,'README.md'),'utf8');
const headings=[];
let index=0;
const renderer=new marked.Renderer();
renderer.heading=function(token){const id=`section-${++index}`; if(token.depth===2) headings.push({id,text:token.text}); return `<h${token.depth} id="${id}">${this.parser.parseInline(token.tokens)}</h${token.depth}>`;};
let html=marked.parse(md,{renderer});
for(const name of ['architecture','workflow','mapping']) {
 html=html.replace(new RegExp(`<img[^>]+src="${name}\\.svg"[^>]*>`),`<figure>${fs.readFileSync(path.join(dir,`${name}.svg`),'utf8')}</figure>`);
}
const css=`:root{color-scheme:light}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f4f6f5;color:#203746;font-family:'Microsoft YaHei','Segoe UI',sans-serif;font-size:16px;line-height:1.85}header{background:#153d42;color:#fff;padding:32px max(24px,calc((100% - 1100px)/2));}header p{margin:0;color:#c7e2df}header strong{font-size:27px}nav{max-width:1100px;margin:24px auto;padding:0 24px;display:flex;gap:10px 20px;flex-wrap:wrap}nav a{color:#235958}main{max-width:1100px;background:white;margin:24px auto 70px;padding:32px 52px}h1{font-size:32px;line-height:1.4}h2{font-size:25px;border-top:1px solid #d4dedf;padding-top:32px;margin-top:42px;scroll-margin-top:24px}h3{font-size:19px;margin-top:28px}a{color:#176c79;text-underline-offset:3px}p{margin:16px 0}table{width:100%;border-collapse:collapse;font-size:14px;line-height:1.7;margin:22px 0;overflow-wrap:anywhere}th,td{padding:12px 14px;text-align:left;border-bottom:1px solid #dce4e5;vertical-align:top}th{background:#edf5f6;font-weight:600}blockquote{margin:24px 0;padding:6px 22px;border-left:4px solid #478b87;background:#f0f7f5}code{font-family:Consolas,monospace;background:#edf2f3;border-radius:3px;padding:2px 5px;overflow-wrap:anywhere}li{margin:8px 0}figure{margin:26px 0}figure svg{width:100%;height:auto;display:block}button{font:inherit;padding:9px 18px;border:1px solid #bfd5d2;background:white;color:#153d42;border-radius:5px;margin-top:16px;cursor:pointer}.note{font-size:14px;color:#4d6872}footer{padding:0 30px 30px;text-align:center;color:#526773}@media(max-width:650px){main{padding:20px 18px;margin:12px 0}body{font-size:15px}table{font-size:13px}th,td{padding:8px}header{padding:24px}h1{font-size:26px}h2{font-size:22px}figure{overflow:auto}figure svg{min-width:650px}}@media print{body{background:white;font-size:11pt}header,nav,button,footer{display:none}main{max-width:none;margin:0;padding:0}h1{font-size:24pt}h2{font-size:18pt;break-before:page}h3{break-after:avoid}figure{break-inside:avoid}figure svg{max-height:245mm;min-width:0}table{font-size:9pt}tr{break-inside:avoid}a{color:inherit}blockquote{break-inside:avoid}}`;
const result=`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ExcelFlow 项目讲解与演示讲义</title><style>${css}</style></head><body><header><strong>ExcelFlow 项目演示准备</strong><p>先讲业务闭环，再解释核心实现 · 依据 2026 年 10 月 26 日工作区代码</p><button type="button" onclick="window.print()">打印或保存 PDF</button></header><nav aria-label="讲义目录">${headings.map(h=>`<a href="#${h.id}">${esc(h.text)}</a>`).join('')}</nav><main>${html}</main><footer>阅读顺序：定位 → 架构 → 流程 → 实例 → 核心原理 → 彩排与答辩</footer></body></html>`;
fs.writeFileSync(path.join(dir,'演示讲义.html'),result);
console.log(JSON.stringify({html:path.join(dir,'演示讲义.html'),diagrams:3,sections:headings.length,bytes:Buffer.byteLength(result)}));
