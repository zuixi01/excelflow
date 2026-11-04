<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { Check, Download, FileSpreadsheet, RefreshCw, Upload, X } from 'lucide-vue-next';
import { api, dateLabel, download, request, useAction } from '../workflow/api';

type Source = { id: string; title: string; scope_fields: string[] };
withDefaults(defineProps<{ showCreator?: boolean;canEdit?:boolean }>(),{canEdit:true});
type Job = { job_id: string; revision: number; status: string; progress: number; input_file_name: string; created_at: string; created_by_id?: string; created_by_username?: string; issue_count?: number; error_message_safe?: string; schema_version?: string; summary?: Record<string, number>; warnings?: string[] };
type Mapping = { sheet: string; column: number; header: string; field_id: string | null; status: string; method: string; confidence: number; reason: string };
type Target = { id: string; title: string; required: boolean };
type Issue = { severity: string; sheet?: string; row?: number; field_title?: string; supplier_value?: unknown; platform_value?: unknown; message: string };
const file = ref<File>(), sourceId = ref(''), scope = ref<Record<string, string>>({});
const sources = ref<Source[]>([]), jobs = ref<Job[]>([]), job = ref<Job>();
const mappings = ref<Mapping[]>([]), fields = ref<Target[]>([]), issues = ref<Issue[]>([]);
const choices = ref<Record<string, string>>({}), view = ref('issues'), severity = ref(''), issuePage = ref(1), issueTotal = ref(0);
const historyPage = ref(1), historyTotal = ref(0), newOpen = ref(true);
const { busy, error, run } = useAction();
const selectedSource = computed(() => sources.value.find(item => item.id === sourceId.value));
const terminal = computed(() => ['completed', 'manual_review', 'failed', 'cancelled'].includes(job.value?.status || ''));
const pending = computed(() => mappings.value.filter(item => item.status === 'manual_review').length);
const hasChanges = computed(() => pending.value > 0 || mappings.value.some(item => choices.value[key(item)] !== (item.field_id || '')));
const statusLabels: Record<string,string> = {queued:'等待处理',loading_standard:'读取平台字段',reading_excel:'识别供应商表',comparing:'正在核对',completed:'核对完成',manual_review:'待确认',failed:'处理失败',cancelled:'已取消'};
const scopeLabels: Record<string,string> = {merchant_id:'商户',category_id:'业务类目',supplier_id:'供应商',data_scope:'业务范围'};
const mappingLabels: Record<string,string> = {accepted:'已确认',manual_review:'待确认',ignored:'附加列',exact:'名称匹配',semantic_model:'语义建议',human:'人工确认',unmapped:'未匹配'};
let timer: number | undefined, pollingGeneration: number | undefined, generation = 0, stopped = false;
function stopTimer() { window.clearTimeout(timer); }
function schedule() { stopTimer(); if (!terminal.value && !stopped) timer = window.setTimeout(() => void refresh(), 1200); }
function key(item: Mapping) { return `${item.sheet}:${item.column}`; }
function display(value: unknown) { return value == null || value === '' ? '-' : typeof value === 'object' ? JSON.stringify(value) : String(value); }
async function loadHistory() { const data = await api<{items:Job[];total:number}>(`/field-reconciliations?page=${historyPage.value}&page_size=10`); jobs.value = data.items; historyTotal.value = data.total; }
async function loadIssues() {
  if (!job.value) return;
  const id=job.value.job_id, page=issuePage.value, filter=severity.value, current=generation;
  const data=await api<{items:Issue[];total:number}>(`/field-reconciliations/${id}/issues?page=${page}&page_size=30&severity=${filter}`);
  if (stopped || current!==generation || page!==issuePage.value || filter!==severity.value) return;
  issues.value=data.items; issueTotal.value=data.total;
}
async function loadDetails() {
  if (!job.value) return;
  const id=job.value.job_id, current=generation;
  const data = await api<{items:Mapping[];fields:Target[]}>(`/field-reconciliations/${id}/mappings`);
  if (stopped || current!==generation) return;
  mappings.value=data.items; fields.value=data.fields; choices.value=Object.fromEntries(data.items.map(item=>[key(item),item.field_id || '']));
  await loadIssues(); if (pending.value) view.value='mappings';
}
async function refresh() {
  if (!job.value || stopped || pollingGeneration===generation) return;
  const current=generation, id=job.value.job_id;
  pollingGeneration=current;
  try {
    const updated=await api<Job>(`/field-reconciliations/${id}`);
    if (stopped || current!==generation) return;
    job.value=updated;
    if (terminal.value) { if (['completed','manual_review'].includes(job.value.status)) await loadDetails(); await loadHistory(); }
  } catch (cause) { if (current===generation && !stopped) error.value=cause instanceof Error ? cause.message : '任务读取失败，请刷新。'; }
  finally { if (current===generation && !stopped) { pollingGeneration=undefined; schedule(); } }
}
async function open(item: Job) {
  stopTimer(); generation++; job.value=item; mappings.value=[]; issues.value=[]; issuePage.value=1; severity.value=''; view.value='issues'; newOpen.value=false;
  const hash = location.hash.split('?')[0], parameters = new URLSearchParams(location.hash.split('?')[1] || '');
  parameters.set('task',item.job_id); history.replaceState(null,'',`${hash}?${parameters}`);
  await refresh();
}
async function start() {
  if (!file.value || !selectedSource.value) throw new Error('请选择平台来源和供应商 Excel。');
  const values:Record<string,string>={};
  for (const field of selectedSource.value.scope_fields) { const value=scope.value[field]?.trim(); if (!value) throw new Error(`请填写${scopeLabels[field] || field}。`); values[field]=value; }
  const body=new FormData(); body.append('excel_file',file.value); body.append('connection_id',sourceId.value); body.append('scope_json',JSON.stringify(values));
  const response=await request('/field-reconciliations',{method:'POST',headers:{'Idempotency-Key':crypto.randomUUID()},body});
  await open(await response.json());
}
async function confirm() {
  if (!job.value) return;
  const decisions=mappings.value.filter(item=>item.status==='manual_review' || choices.value[key(item)]!==(item.field_id || '')).map(item=>({sheet:item.sheet,column:item.column,field_id:choices.value[key(item)] || null}));
  if (!decisions.length) throw new Error('没有待保存的字段确认。');
  job.value=await api<Job>(`/field-reconciliations/${job.value.job_id}/mappings/confirm`,'POST',{base_revision:job.value.revision,decisions});
  await refresh();
}
async function retry() { if (job.value) {job.value=await api<Job>(`/field-reconciliations/${job.value.job_id}/retry`,'POST'); await refresh();} }
async function output(kind:string) { if(job.value) await download(`/field-reconciliations/${job.value.job_id}/files/${kind}`,kind==='excel'?'字段核对结果.xlsx':'核对报告.json'); }
async function pageIssues(delta:number) {issuePage.value+=delta; await loadIssues();}
async function pageHistory(delta:number) {historyPage.value+=delta; await loadHistory();}
onMounted(()=>void run(async()=>{
  const data=await api<{items:Source[]}>('/field-reconciliations/connections'); sources.value=data.items; sourceId.value=data.items[0]?.id || '';
  await loadHistory(); const task=new URLSearchParams(location.hash.split('?')[1] || '').get('task');
  if(task) await open(await api<Job>(`/field-reconciliations/${encodeURIComponent(task)}`));
}));
onUnmounted(()=>{stopped=true;generation++;stopTimer();});
</script>

<template>
  <div class="reconcile-page">
    <div class="reconcile-heading"><div><p class="kicker">商户运营</p><h1>供应商 Excel 核对</h1></div><button v-if="!newOpen && canEdit" class="btn primary" @click="newOpen=true"><Upload :size="16" />上传供应商表</button></div>
    <div v-if="error" class="notice error" role="alert">{{error}}</div>
    <section v-if="newOpen && canEdit" class="reconcile-start" aria-label="上传供应商表">
      <div class="start-intro"><FileSpreadsheet :size="22" /><h2>新建核对</h2><button v-if="job" class="btn icon-only" title="收起上传" aria-label="收起上传" @click="newOpen=false"><X :size="18" /></button></div>
      <div v-if="!sources.length" class="notice info">尚未配置平台字段来源，请联系管理员接入平台接口。</div>
      <form @submit.prevent="run(start)">
        <div class="reconcile-form">
          <label v-if="sources.length>1">平台来源<select v-model="sourceId"><option v-for="item in sources" :key="item.id" :value="item.id">{{item.title}}</option></select></label>
          <div v-else-if="sources.length" class="source-label">字段来源<strong>{{selectedSource?.title}}</strong></div>
          <label v-for="field in selectedSource?.scope_fields || []" :key="field">{{scopeLabels[field] || field}}<input v-model="scope[field]" required maxlength="256" /></label>
          <label class="compact-upload"><Upload :size="20" /><span>{{file?.name || '选择供应商 Excel'}}</span><small>.xlsx / .xlsm · 50 MiB 以内</small><input type="file" accept=".xlsx,.xlsm" aria-label="选择供应商 Excel 文件" @change="file=($event.target as HTMLInputElement).files?.[0]" /></label>
        </div><div class="start-footer"><span></span><button type="submit" class="btn primary" :disabled="busy || !sources.length || !file">{{busy?'正在提交…':'开始核对'}}</button></div>
      </form>
    </section>
    <template v-if="job">
      <div v-if="showCreator" class="subtle">创建账号：{{job.created_by_username || job.created_by_id || '未知账号'}}</div>
      <div class="task-strip" aria-live="polite"><div><span>当前文件</span><strong>{{job.input_file_name}}</strong></div><div><span>状态</span><strong>{{statusLabels[job.status] || job.status}}</strong></div><div><span>字段版本</span><strong>{{job.schema_version || '-'}}</strong></div><div><span>{{terminal?'核对条数':'处理进度'}}</span><strong>{{terminal?(job.summary?.source_records ?? '-'):`${job.progress}%`}}</strong></div></div>
      <div v-if="job.error_message_safe && job.status==='failed'" class="notice error" role="alert">{{job.error_message_safe}} <button class="btn" :disabled="!canEdit || busy" @click="run(retry)"><RefreshCw :size="16" />重试</button></div>
      <div v-for="warning in job.warnings" :key="warning" class="notice info">{{warning}}</div>
      <section v-if="['completed','manual_review'].includes(job.status)" class="reconcile-results">
        <header><div><h2>核对结果</h2><p class="subtle">通过 {{job.summary?.passed_records || 0}} 条 · 不通过 {{job.summary?.failed_records || 0}} 条 · 待确认 {{job.summary?.review_records || 0}} 条</p></div><div class="toolbar"><button v-if="job.status==='manual_review' && !pending" class="btn" :disabled="!canEdit || busy" @click="run(retry)"><RefreshCw :size="16" />重新核对</button><button class="btn" :disabled="busy" @click="run(()=>output('excel'))"><Download :size="16" />结果 Excel</button><button class="btn icon-only" title="下载完整报告" aria-label="下载完整报告" :disabled="busy" @click="run(()=>output('report'))"><FileSpreadsheet :size="18" /></button></div></header>
        <div class="result-controls"><div class="result-tabs" role="tablist" aria-label="核对明细"><button role="tab" :aria-selected="view==='issues'" :class="{active:view==='issues'}" @click="view='issues'">问题 {{job.issue_count || 0}}</button><button role="tab" :aria-selected="view==='mappings'" :class="{active:view==='mappings'}" @click="view='mappings'">字段映射 <span v-if="pending">· {{pending}} 待确认</span></button></div><select v-if="view==='issues'" v-model="severity" aria-label="问题级别" @change="issuePage=1;run(loadIssues)"><option value="">全部问题</option><option value="error">不通过</option><option value="review">待核实</option></select></div>
        <template v-if="view==='issues'"><div class="table-scroll"><table class="data-table"><thead><tr><th>级别</th><th>位置</th><th>字段</th><th>供应商值</th><th>平台值</th><th>说明</th></tr></thead><tbody><tr v-for="(item,index) in issues" :key="index"><td><span class="badge" :class="item.severity==='review'?'manual_review':'error'">{{item.severity==='review'?'待核实':'不通过'}}</span></td><td>{{item.sheet || '-'}}<span v-if="item.row"> · {{item.row}} 行</span></td><td>{{item.field_title || '-'}}</td><td>{{display(item.supplier_value)}}</td><td>{{display(item.platform_value)}}</td><td>{{item.message}}</td></tr><tr v-if="!issues.length"><td colspan="6" class="empty-cell">{{pending?'字段确认后可继续核对。':'没有符合筛选条件的问题。'}}</td></tr></tbody></table></div><div class="result-footer"><span>共 {{issueTotal}} 项 · 第 {{issuePage}} 页</span><div class="toolbar"><button class="btn" :disabled="busy || issuePage===1" @click="run(()=>pageIssues(-1))">上一页</button><button class="btn" :disabled="busy || issuePage*30>=issueTotal" @click="run(()=>pageIssues(1))">下一页</button></div></div></template>
        <template v-else><div class="table-scroll"><table class="data-table mapping-table"><thead><tr><th>工作表 / 列</th><th>供应商字段</th><th>平台字段</th><th>状态</th><th>匹配依据</th></tr></thead><tbody><tr v-for="item in mappings" :key="key(item)"><td>{{item.sheet}} · {{item.column}}</td><td>{{item.header}}</td><td><select v-model="choices[key(item)]" :disabled="!canEdit || busy" :aria-label="`${item.sheet}第${item.column}列对应平台字段`"><option value="">保留为附加列</option><option v-for="target in fields" :key="target.id" :value="target.id">{{target.title}}{{target.required?' *':''}}</option></select></td><td><span class="badge" :class="item.status">{{mappingLabels[item.status] || item.status}}</span></td><td>{{mappingLabels[item.method] || item.method}} · {{item.reason}}</td></tr></tbody></table></div><div class="result-footer"><span>{{pending}} 列待确认</span><button class="btn primary" :disabled="!canEdit || busy || !hasChanges" @click="run(confirm)"><Check :size="16" />确认字段并继续</button></div></template>
      </section>
    </template>
    <section class="reconcile-history"><div class="section-heading"><h2>核对记录 <small>{{historyTotal}}</small></h2><button class="btn icon-only" title="刷新记录" aria-label="刷新核对记录" :disabled="busy" @click="run(loadHistory)"><RefreshCw :size="17" /></button></div><div class="table-scroll"><table class="data-table"><thead><tr><th>供应商文件</th><th v-if="showCreator">创建账号</th><th>状态</th><th>条数</th><th>问题</th><th>提交时间</th><th></th></tr></thead><tbody><tr v-for="item in jobs" :key="item.job_id"><td>{{item.input_file_name}}</td><td v-if="showCreator">{{item.created_by_username || item.created_by_id || '未知账号'}}</td><td><span class="badge" :class="item.status">{{statusLabels[item.status]}}</span></td><td>{{item.summary?.source_records ?? '-'}}</td><td>{{item.issue_count ?? '-'}}</td><td>{{dateLabel(item.created_at)}}</td><td><button class="btn text" :disabled="busy" @click="run(()=>open(item))">查看</button></td></tr><tr v-if="!jobs.length"><td :colspan="showCreator ? 7 : 6" class="empty-cell">暂无核对记录</td></tr></tbody></table></div><div v-if="historyTotal>10" class="result-footer"><span>第 {{historyPage}} 页</span><div class="toolbar"><button class="btn" :disabled="busy || historyPage===1" @click="run(()=>pageHistory(-1))">上一页</button><button class="btn" :disabled="busy || historyPage*10>=historyTotal" @click="run(()=>pageHistory(1))">下一页</button></div></div></section>
  </div>
</template>
