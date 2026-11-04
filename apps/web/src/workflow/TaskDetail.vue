<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { Sparkles } from 'lucide-vue-next';
import ProcessingProgress from './ProcessingProgress.vue';
import ImageValue from './ImageValue.vue';
import ReturnDiffDetails from './ReturnDiffDetails.vue';
import {imageIds,imageDisplayText} from './api';
import { ApiError, api, cloneData, dateLabel, executionLabel, download, labels, operations, useAction, type MaintenanceField, type Binding, type Task, type Row, type Template } from "./api";
const props = withDefaults(defineProps<{id: string; showCreator?: boolean;canEdit?:boolean}>(),{canEdit:true});
const emit = defineEmits<{back: []; dirty: [value: boolean]}>();
const {busy, error, message, run} = useAction();
const task = ref<Task>(), rows = ref<Row[]>([]), total = ref(0), offset = ref(0), pageSize = 20;
const plans = ref<Array<{sheet: string; include: boolean; header_row: number; header_depth: number; bindings: Binding[]; ignored_columns: number[]; category_id?:string}>>([]);
const changes = ref<Record<string, Record<string, string>>>({}), baseRevision = ref(0), selected = ref<string[]>([]);
const search = ref(""), issuesOnly = ref(false), sort = ref(""), descending = ref(false), visibleIds = ref<string[]>([]);
const bulkField = ref(""), bulkValue = ref(""), panel = ref("maintenance"), detailRow = ref<Row>();
const activeStep = ref<number | null>(null);
const outputTemplates = ref<Template[]>([]), outputTemplateId = ref("");
const returnPreview = ref<{id: string; status?:string; payload: {
  detail_manifest?: {token:string};
  sync_mode?: string;
  stale_revision?: boolean; source_revision?: number; base_revision?: number;
  field_changes?: Array<{kind:string;field:string;before:string;after:string;location:string;original_column:string}>;
  structure_summary?: string[];
  field_options?: Array<{id:string; title:string}>;
  counts: {changed: number; added: number; missing: number};
  samples: Array<{id: string; before: Record<string, string>; after: Record<string, string>}>;
  added_samples?: Array<{values: Record<string, string>}>;
  missing_samples?: Array<{id: string; values: Record<string, string>}>;
  identity_warnings?: Array<{code: string; count: number; message: string}>;
}}>();
type ReturnField = {id:string; title:string; label:string; kind:string; required:boolean; editable:boolean};
type ReturnColumn = {key:string; sheet:string; column:number; path:string; title:string; action:'existing'|'new'|'ignore'; field_id:string; kind:'normal'|'attribute'|'specification'; type:string; matched_by:string; candidates?:string[]};
const returnStructure = ref<{id:string; status?:string; payload:{columns:ReturnColumn[]; field_options:ReturnField[]; record_count:number; applied_revision?:number}}>();
const staleAcknowledged=ref(false);
const autoPreviewing=ref(false),showAllReturnColumns=ref(false);
const missingReturnFields = computed(()=>returnStructure.value?.payload.field_options.filter(f=>!returnStructure.value?.payload.columns.some(c=>c.action==='existing' && c.field_id===f.id)) || []);
const uncertainReturnColumns=computed(()=>{
 const columns=returnStructure.value?.payload.columns||[];
 const matches=new Map<string,number>();
 for(const c of columns)if(c.action==='existing')matches.set(c.field_id,(matches.get(c.field_id)||0)+1);
 return columns.filter(c=>c.action!=='ignore' && (!c.title.trim() ||
  (c.action==='existing' && (!c.field_id||(matches.get(c.field_id)||0)>1)) ||
  (c.action==='new' && ((c.candidates?.length||0)>1 || missingReturnFields.value.length>0))));
});
const reviewColumns=computed(()=>showAllReturnColumns.value?returnStructure.value?.payload.columns||[]:uncertainReturnColumns.value);
async function acceptReturnInspection(result:NonNullable<typeof returnStructure.value>){
 if(result.status==='already_applied'){
  await load();
  visibleIds.value=fields.value.map(field=>field.rule.name);offset.value=0;
  if(sort.value&&!fields.value.some(field=>field.rule.name===sort.value))sort.value='';
  await showReturnSaved(`这份文件的修改已保存到 R${result.payload.applied_revision}，本次没有重复修改数据。当前显示 R${task.value?.revision}。`);
  try{await loadRows();}catch{error.value='此文件已保存成功，但维护数据列表刷新失败。可刷新页面重新查看。';}
  return;
 }
 returnStructure.value=result;showAllReturnColumns.value=false;
 if(!uncertainReturnColumns.value.length){
  autoPreviewing.value=true;
  try{await previewStructure();}finally{autoPreviewing.value=false;}
 }
}
function closeReturn(){returnPreview.value=undefined;returnStructure.value=undefined;rowReview.value=[];resolvedReturnRows.value={};staleAcknowledged.value=false;}
const structureChanges = computed(()=>{
  const value=returnStructure.value?.payload;
  if(!value)return [];
  const changes:Array<{key:string;kind:string;field:string;detail:string}>=[];
  const linked=value.columns.filter(c=>c.action==='existing' && value.field_options.some(f=>f.id===c.field_id));
  const original=value.field_options.filter(f=>linked.some(c=>c.field_id===f.id)).map(f=>f.id);
  const labels:Record<string,string>={normal:'普通字段',attribute:'商品属性',specification:'商品规格'};
  for(const column of value.columns){
    const prior=value.field_options.find(f=>f.id===column.field_id);
    if(column.action==='new')changes.push({key:column.key+'new',kind:'新增列',field:column.title||'未命名列',detail:`回传第 ${column.column} 列，将作为${labels[column.kind]}新增`});
    else if(column.action==='ignore')changes.push({key:column.key+'ignore',kind:'忽略列',field:column.title||column.path,detail:'此列不导入；未再对应的原字段将同步删除'});
    else if(!prior)changes.push({key:column.key+'pending',kind:'待确认',field:column.title,detail:'请选择对应原字段或设为新增字段'});
    else{
      if(prior.label!==column.title)changes.push({key:column.key+'rename',kind:'改名',field:prior.title,detail:`${prior.label} → ${column.title}`});
      if(prior.kind!==column.kind)changes.push({key:column.key+'kind',kind:'调整归属',field:column.title,detail:`${labels[prior.kind]} → ${labels[column.kind]}`});
      const before=original.indexOf(column.field_id),after=linked.indexOf(column);
      if(before!==after)changes.push({key:column.key+'order',kind:'调序',field:column.title,detail:`原有字段中的第 ${before+1} 位 → 第 ${after+1} 位（回传第 ${column.column} 列）`});
    }
  }
  for(const field of missingReturnFields.value){
    changes.push({key:field.id+'missing',kind:'将删除列',field:field.title,detail:'确认保存后，从维护数据和最终表中同步删除'});
  }
  return changes;
});
const structureChangeCounts=computed(()=>structureChanges.value.reduce((counts,item)=>{counts[item.kind]=(counts[item.kind]||0)+1;return counts;},{} as Record<string,number>));
const rowReview = ref<Array<{key:string; sheet:string; row:number; id:string; summary:string; reason:string; suggested_id:string; action:''|'existing'|'new'|'ignore'; record_id:string}>>([]);
const resolvedReturnRows=ref<Record<string,{key:string;action:string;record_id:string}>>({});
const returnRecordOptions = ref<Array<{id:string; label:string}>>([]);
const reviewOffset = ref(0);
function chooseReturnField(column: ReturnColumn) {
  const target = returnStructure.value?.payload.field_options.find(f=>f.id===column.field_id);
  if (target) column.kind = target.kind as ReturnColumn['kind'];
}
async function previewStructure() {
  if (!returnStructure.value) return;
  staleAcknowledged.value=false;
  if (rowReview.value.some(r=>!r.action || (r.action==='existing' && !r.record_id))) throw new Error('请先确认每一条待识别商品的处理方式。');
  const columns = returnStructure.value.payload.columns.map(({key,action,field_id,title,kind,type})=>({key,action,field_id,title,kind,type}));
  for(const {key,action,record_id} of rowReview.value)resolvedReturnRows.value[key]={key,action,record_id};
  const result = await api<{id:string; status:string; payload:any}>(`/workflow-tasks/${props.id}/maintenance/returns/${returnStructure.value.id}/preview`, 'POST', {columns, rows:Object.values(resolvedReturnRows.value)});
  if (result.status==='row_review') {
    const prior = new Map(rowReview.value.map(r=>[r.key,r]));
    rowReview.value = result.payload.row_review.map((r:any)=>({...r,action:prior.get(r.key)?.action||'',record_id:prior.get(r.key)?.record_id||r.suggested_id}));
    reviewOffset.value=0;
    returnRecordOptions.value = result.payload.record_options;
    returnPreview.value = result;
    return;
  }
  returnPreview.value = result;
  rowReview.value=[];
  transferMessage.value = '已读取改动，请核对后保存。';
}
const returnFieldTitles=computed(()=>Object.fromEntries([
  ...(task.value?.payload.config.fields||[]).map(f=>[f.rule.name,f.rule.title]),
  ...(returnPreview.value?.payload.field_options||[]).map(f=>[f.id,f.title]),
]));
const returnDeletedColumns=computed(()=>(returnPreview.value?.payload.field_changes||[]).filter(change=>['删除列','文件缺列'].includes(change.kind)));
const returnOtherFieldChanges=computed(()=>(returnPreview.value?.payload.field_changes||[]).filter(change=>!['删除列','文件缺列'].includes(change.kind)));
const returnChangeSummary=computed(()=>{
  const preview=returnPreview.value;
  if(!preview)return [];
  const {counts,field_changes=[],structure_summary=[]}=preview.payload;
  const items:string[]=[];
  if(counts.changed)items.push(`内容修改 ${counts.changed} 行`);
  if(counts.added)items.push(`新增 ${counts.added} 行`);
  if(counts.missing)items.push(`${preview.status==='row_review'?'暂未对应':'文件中删除'} ${counts.missing} 行`);
  const deleted=field_changes.filter(change=>['文件缺列','删除列'].includes(change.kind));
  if(deleted.length)items.push(`文件中删除 ${deleted.length} 列`);
  for(const [kind,label] of [['新增列','新增'],['改名','字段改名'],['移动列','移动'],['调整归属','调整归属']]){
    const count=field_changes.filter(change=>change.kind===kind).length;
    if(count)items.push(`${label} ${count} 列`);
  }
  const groups=structure_summary.filter(text=>text.startsWith('表头分组变化：')).length;
  if(groups)items.push(`表头分组调整 ${groups} 列`);
  if(structure_summary.some(text=>text.startsWith('商品顺序')))items.push('商品行顺序有调整');
  if(structure_summary.some(text=>text.includes('合并关系变化')))items.push('合并单元格有调整');
  if(preview.status==='row_review')items.push(`待核对 ${rowReview.value.length} 行`);
  return items;
});
const returnDeletionSummary=computed(()=>{
  const preview=returnPreview.value;
  if(!preview)return '';
  const changes=preview.payload.field_changes||[],notes:string[]=[];
  const retained=changes.filter(change=>change.kind==='文件缺列').length;
  const removed=changes.filter(change=>change.kind==='删除列').length;
  if(retained)notes.push(`${retained} 列使用旧规则，请重新上传核对同步删除`);
  if(removed)notes.push(`${removed} 列将在确认保存后删除`);
  if(preview.payload.counts.missing && preview.status!=='row_review')notes.push(`${preview.payload.counts.missing} 行将在确认保存后删除`);
  return notes.length?`删除项的本次处理：${notes.join('；')}。`:'';
});
const identityAcknowledged = ref(false);
const needsIdentityConfirmation = computed(() => !!returnPreview.value?.payload.identity_warnings?.length);
function recordSummary(values: Record<string, string>) {
  return Object.entries(values).filter(([,value]) => value !== '').slice(0, 12)
    .map(([id,value]) => `${fields.value.find(field=>field.rule.name===id)?.rule.title || id}：${imageDisplayText(value)}`).join('；');
}
const outdatedReturnPreview=computed(()=>!!returnPreview.value && standardization.value && returnPreview.value.payload.sync_mode!=='replace');
const namePreview = ref<{id:string;payload:{base_revision:number;counts:{changed:number;blank:number;unchanged:number;overwritten:number};samples:Array<{id:string;before:string;after:string;filename:string;sheet:string;row:number|null;excel_row:number}>}}>();
const nameOffset = ref(0), nameFilename = ref(''), nameError = ref(''), nameMessage = ref('');
async function applyNames() {
  if (!namePreview.value) return;
  nameError.value='';
  try {
    const count=namePreview.value.payload.counts.changed;
    task.value=await api<Task>(`/workflow-tasks/${props.id}/maintenance/name-confirmations/apply`,'POST',{preview_id:namePreview.value.id});
    namePreview.value=undefined; offset.value=0; await loadRows();
    nameMessage.value=`已按记录ID更新 ${count} 条商品名称。${task.value.payload.error_count ? `仍有 ${task.value.payload.error_count} 个校验问题，请处理后生成。` : '校验通过，可进入“生成最终表”。'}`;
  } catch(cause) {
    nameError.value=cause instanceof ApiError && cause.code==='REVISION_CONFLICT'?'任务已更新，本次没有覆盖数据。请关闭预览，重新上传同一确认清单以核对最新差异。':cause instanceof Error?cause.message:'名称更新失败，请重试。';
  }
}
const transferAction = ref<'download' | 'upload' | 'apply' | ''>('');
const transferError = ref(''), transferMessage = ref(''), transferFilename = ref(''), transferConflict = ref(false);
const returnSavedRevision=ref<number|null>(null);
const returnSavedMessage=ref('');
const returnSaveNotice=ref<HTMLElement|null>(null);
async function showReturnSaved(text=''){
 closeReturn();returnSavedRevision.value=task.value?.revision??null;returnSavedMessage.value=text;
 activeStep.value=null;panel.value='output';
 await nextTick();returnSaveNotice.value?.scrollIntoView({behavior:'smooth',block:'start'});
 returnSaveNotice.value?.focus({preventScroll:true});
}
const backgroundStatus = ref('');
const recentOperations = computed<Array<{id:string; status:string; operation:string; error?:string}>>(() => (task.value as any)?.operations || []);
const operationNames:Record<string,string> = {maintenance_download:'维护表下载',return_inspect:'回传结构识别',return_preview:'回传差异预览',return_flexible:'回传差异预览',name_preview:'名称确认预览',name_apply:'名称更新',return_apply:'回传保存',mapping_analyze:'字段分析',mapping_upgrade:'字段升级',mapping_confirm:'字段确认',edit:'数据保存'};
async function resumeOperation(job: {id:string; operation:string}) {
  const path = `/workflow-tasks/${props.id}/operations/${job.id}/result`;
  if (job.operation === 'maintenance_download') { await download(path); return; }
  const result = await api<any>(path);
  if (job.operation === 'return_inspect') {closeReturn();await acceptReturnInspection(result);}
  else if (['return_preview','return_flexible'].includes(job.operation)) {
    if(result.status==='row_review')throw new Error('这份回传还有商品需要确认，请重新上传维护表继续核对。');
    closeReturn();identityAcknowledged.value=false;returnPreview.value = result;
  }
  else if (job.operation === 'name_preview') namePreview.value = result;
  else { await load(); await loadRows(); message.value='已读取最新任务数据。'; }
}
function operationEvent(event: Event) {
  const detail = (event as CustomEvent).detail;
  if (!detail.path?.startsWith(`/workflow-tasks/${props.id}/operations/`)) return;
  backgroundStatus.value = detail.status === 'processing' ? '操作已提交，正在排队或后台处理，请勿重复提交。' : '';
}
const outputPreview = ref<{name: string; sheets: Array<{name: string; rows: string[][]}>}>();
async function previewOutput(runId: string, filename: string) {
  outputPreview.value = await api(`/workflow-tasks/${props.id}/exports/${runId}/preview/${encodeURIComponent(filename)}`);
}
let timer: number | undefined;
const analysisActive = ref(false);
let polling = false;
const dirty = computed(() => Object.keys(changes.value).length > 0);
const fields = computed(() => task.value?.payload.config.fields.filter(f=>!f.hidden) || []);
const standardization=computed(()=>task.value?.payload.workflow==='standardization');
const categories=computed(()=>task.value?.payload.field_snapshot?.profile.local_categories || []);
const needsUnifiedAnalysis=computed(()=>standardization.value && !!task.value?.payload.config.category_field);
const preserveExtras=computed(()=>standardization.value && task.value?.payload.config.preserve_extras);
const targetFields=computed(()=>fields.value.filter(f=>!f.source_column));
const retainedFields=computed(()=>fields.value.filter(f=>!!f.source_column));
const dynamicFieldCount=computed(()=>fields.value.filter(f=>f.rule.title.startsWith('动态字段/') || f.category_id).length);
const formulaReports=computed(()=>(task.value?.payload.sheets || []).flatMap(sheet=>(sheet.formula_results || []).map(item=>({...item,source_sheet:sheet.sheet}))));
const correctedFormulas=computed(()=>formulaReports.value.filter(item=>item.status!=='cache_verified'));
function fieldsFor(){return targetFields.value;}
function fieldLabel(field:MaintenanceField) {
  const origin = field.source_column;
  if (origin && ['compact', 'grouped'].includes(task.value?.payload.config.source_header_layout || '')) return origin.header || `未命名列 ${origin.column}`;
  return origin ? `原表保留列/${origin.sheet} · 第${origin.column}列/${origin.header || `未命名列 ${origin.column}`}` : field.rule.title;
}
function unmatchedFor(plan:{bindings:Binding[]}) {return targetFields.value.filter(f=>!plan.bindings.some(b=>b.field===f.rule.name));}
function retainedFor(plan:{sheet:string; bindings:Binding[]}) {
  const used = new Set(plan.bindings.filter(binding=>binding.operation !== 'constant').flatMap(binding=>binding.columns));
  const source = task.value?.payload.sheets?.find(sheet=>sheet.sheet===plan.sheet);
  for (const column of source?.verbatim_columns || []) used.delete(column);
  return source?.columns.filter(column=>!used.has(column.column) && !(task.value?.payload.config.exclude_source_sequence && ['序号','行号'].includes(column.header.trim()))) || [];
}
function mappingNotes(index:number) {return (task.value?.payload.sheets?.[index]?.mapping_notes || []).filter(note=>!note.includes('尚未选择动态字段类目') && !note.includes('已复用此前确认'));}
const visibleFields = computed(() => fields.value.filter(field => visibleIds.value.includes(field.rule.name)));
const groupedSourceHeaders = computed(() => task.value?.payload.config.source_header_layout === 'grouped' && visibleFields.value.some(f => f.source_column));
const sourceCellSpans = computed(() => {
  const spans: Record<string, Record<string, number>> = {};
  if (!task.value?.payload.config.preserve_source_merges) return spans;
  for (const field of visibleFields.value) {
    const key = field.rule.name;
    for (let start = 0; start < rows.value.length;) {
      const first = rows.value[start]!;
      const group = first.source.merged_fields?.[key];
      let end = start + 1;
      if (group) while (end < rows.value.length && rows.value[end]!.source.merged_fields?.[key] === group && rows.value[end]!.values_json[key] === first.values_json[key]) end++;
      if (end - start > 1) {
        (spans[first.id] ||= {})[key] = end - start;
        for (let index = start + 1; index < end; index++) (spans[rows.value[index]!.id] ||= {})[key] = 0;
      }
      start = end;
    }
  }
  return spans;
});
function cellColumnSpan(row:Row, field:MaintenanceField) {
  if(!task.value?.payload.config.preserve_source_merges)return 1;
  const index=visibleFields.value.findIndex(f=>f.rule.name===field.rule.name);
  for(const rect of row.source.rectangular_merges || []){
    if(!rect.fields.includes(field.rule.name))continue;
    const positions=visibleFields.value.map((f,i)=>({f,i})).filter(({f})=>rect.fields.includes(f.rule.name));
    if(new Set(positions.map(({f})=>changes.value[row.id]?.[f.rule.name] ?? row.values_json[f.rule.name] ?? '')).size!==1)continue;
    let first=index,last=index;
    const indices=new Set(positions.map(p=>p.i));while(indices.has(first-1))first--;while(indices.has(last+1))last++;
    return index===first ? last-first+1 : 0;
  }
  return 1;
}
function cellSpan(row: Row, field: MaintenanceField) { return cellColumnSpan(row,field)===0 ? 0 : sourceCellSpans.value[row.id]?.[field.rule.name] ?? 1; }
const maintenanceHeaderGroups = computed(() => {
  const groups: Array<{key: string; title: string; fields: MaintenanceField[]; source: boolean}> = [];
  for (const field of visibleFields.value) {
    const source = groupedSourceHeaders.value ? field.source_column : undefined;
    const previous = groups.at(-1);
    if (source && previous?.source && previous.title === source.sheet) previous.fields.push(field);
    else groups.push({key: field.rule.name, title: source?.sheet || fieldLabel(field), fields: [field], source: !!source});
  }
  return groups;
});
const selectedOutputTemplate = computed(() => outputTemplates.value.find(template => template.id === outputTemplateId.value));
const displayedOutputs = computed(() => standardization.value?(task.value?.final_outputs || task.value?.payload.config.outputs || []):selectedOutputTemplate.value?.payload.config.outputs || []);
const inferredStep = computed(() => !task.value || ['processing','failed','cancelled'].includes(task.value.status) ? 0 : task.value.status === 'mapping' ? 1 : panel.value === 'output' ? 3 : 2);
const step = computed(() => activeStep.value ?? inferredStep.value);
const savedMappings = computed(() => task.value?.payload.mapping?.sheets || []);
const omitted = computed(() => {
  const used = new Set(task.value?.payload.config.outputs.flatMap(file => file.sheets.flatMap(sheet => sheet.columns.flatMap(column => [column.field, ...column.sources]))) || []);
  return fields.value.filter(field => !used.has(field.rule.name)).map(field => field.rule.title);
});
watch(dirty, value => emit("dirty", value));
function mappingPlans(source: Task) {
  return (source.payload.sheets || []).map(sheet => {
    const saved = source.payload.mapping?.sheets?.find((item: {sheet: string}) => item.sheet === sheet.sheet);
    return cloneData(saved || {sheet: sheet.sheet, include: true, header_row: sheet.header_row, header_depth: sheet.header_depth,
      ignored_columns: [], category_id:sheet.category_id || 'unclassified', bindings: sheet.suggested_bindings || sheet.columns.filter(c => c.field).map(c => ({field: c.field, columns: [c.column], operation: 'copy', argument: '', part: 0}))});
  });
}
function selectStep(index: number) {
  if (!task.value) return;
  if (dirty.value && index !== 2) { error.value = '请先保存维护数据，再查看其他步骤。'; return; }
  if (index === 0 || index === 1) { activeStep.value = index; return; }
  if (task.value.status === 'mapping') { error.value = '请先确认字段，再进入后续步骤。'; return; }
  activeStep.value = null;
  panel.value = index === 3 ? 'output' : 'maintenance';
}
function operationLabel(binding: Binding) { return operations.find(item => item.value === binding.operation)?.label || binding.operation; }
function sourceColumnLabel(sheetName: string, column: number) {
  const source = task.value?.payload.sheets?.find(sheet => sheet.sheet === sheetName)?.columns.find(item => item.column === column);
  return source ? `${source.source_column || source.column}. ${source.header}` : `第 ${column} 列`;
}
async function loadRows() {
  if (!task.value) return;
  const params = new URLSearchParams({offset: String(offset.value), limit: String(pageSize), search: search.value, issues_only: String(issuesOnly.value), sort: sort.value, descending: String(descending.value)});
  const result = await api<{items: Row[]; total: number; revision: number}>(`/workflow-tasks/${props.id}/maintenance?${params}`);
  rows.value = result.items; total.value = result.total; baseRevision.value = result.revision; selected.value = [];
}
async function load(initial = false) {
  const result = await api<Task>(`/workflow-tasks/${props.id}`); task.value = result;
  if (!standardization.value && !outputTemplates.value.length) {
    const definitions = await api<{items: Template[]}>("/workflow-definitions");
    outputTemplates.value = definitions.items.filter(template => template.payload.purpose === "output" || !!template.payload.sources?.output);
  }
  if (result.status === 'mapping' && (initial || !plans.value.length)) plans.value = mappingPlans(result);
  if (initial && result.status === 'completed') panel.value = 'output';
  if (!visibleIds.value.length) {
    const common = ['product_name','sku','brand','product_category','sale_price','stock','sales_unit'];
    visibleIds.value = fields.value.filter(field => common.includes(field.rule.name) || field.rule.required).map(field => field.rule.name);
    if (preserveExtras.value) visibleIds.value = fields.value.map(field=>field.rule.name);
    else if (!visibleIds.value.length) visibleIds.value = fields.value.slice(0, 7).map(field => field.rule.name);
  }
  if (initial && ['maintenance', 'ready', 'completed'].includes(result.status)) await loadRows();
}
async function confirmMapping() {
  task.value = await api<Task>(`/workflow-tasks/${props.id}/mapping`, 'POST', {base_revision: task.value?.revision, sheets: plans.value});
  if (preserveExtras.value) visibleIds.value = fields.value.map(field=>field.rule.name);
  activeStep.value = null; panel.value = 'maintenance'; await loadRows();
}
async function analyzeMapping(upgrade = false){
  if(!task.value)return;
  analysisActive.value = true;
  task.value.payload.processing_progress = {run_id:'pending',status:'running',stage:'reading_excel',started_at:new Date().toISOString(),updated_at:new Date().toISOString(),total_batches:0,completed_batches:0,failed_batches:0,sheets:[]};
  try {
    task.value=await api<Task>(`/workflow-tasks/${props.id}/mapping/${upgrade ? 'upgrade' : 'analyze'}`,'POST',{base_revision:task.value.revision});
    plans.value=mappingPlans(task.value);message.value=upgrade ? '字段快照已升级，尺寸和照射面积按文本识别，质保保留原文，左右独立表分别匹配。请确认映射后继续。' : '全部固定字段与动态字段已重新分析，请确认映射后继续。';
  } finally { analysisActive.value = false; }
}
async function reopenMapping() {
  if (!task.value || !window.confirm('重新确认字段会清空当前维护数据，并按新的字段映射重新生成。确定继续吗？')) return;
  task.value = await api<Task>(`/workflow-tasks/${props.id}/mapping/reopen`, 'POST', {base_revision: task.value.revision});
  plans.value = mappingPlans(task.value); activeStep.value = null;
  message.value = '已回到确认字段步骤。请修改映射后重新生成维护表。';
}
function updateCell(row: Row, field: string, value: string) {
  const rectangle=row.source.rectangular_merges?.find(rect=>rect.fields.includes(field));
  const linked=rectangle && rectangle.fields.every(key=>(changes.value[row.id]?.[key] ?? row.values_json[key] ?? '')===(changes.value[row.id]?.[field] ?? row.values_json[field] ?? '')) ? rectangle.fields : [field];
  for(const key of linked)updateSingleCell(row,key,value);
}
function updateSingleCell(row: Row, field: string, value: string) {
  const group = task.value?.payload.config.preserve_source_merges && row.source.merged_fields?.[field];
  const members = group ? rows.value.filter(other => other.source.merged_fields?.[field] === group && other.values_json[field] === row.values_json[field]) : [row];
  for (const member of members) {
    if (value === (member.values_json[field] || '')) { if (changes.value[member.id]) delete changes.value[member.id][field]; }
    else { changes.value[member.id] ||= {}; changes.value[member.id][field] = value; }
    if (changes.value[member.id] && !Object.keys(changes.value[member.id]).length) delete changes.value[member.id];
  }
}
async function save() {
  task.value = await api<Task>(`/workflow-tasks/${props.id}/maintenance`, 'PATCH', {base_revision: baseRevision.value, changes: changes.value});
  changes.value = {}; await loadRows(); message.value = '已保存并完成校验。';
}
function batchFill() {
  if (!bulkField.value) { error.value = '请选择批量填充的字段。'; return; }
  for (const row of rows.value.filter(row => !selected.value.length || selected.value.includes(row.id))) updateCell(row, bulkField.value, bulkValue.value);
}
async function addRow() {
  task.value = await api<Task>(`/workflow-tasks/${props.id}/maintenance`, 'PATCH', {base_revision: baseRevision.value, additions: [{}]});
  issuesOnly.value = true; offset.value = 0; await loadRows();
}
async function removeRows() {
  if (!window.confirm(`确认删除选中的 ${selected.value.length} 条维护记录？原文件仍会保留。`)) return;
  task.value = await api<Task>(`/workflow-tasks/${props.id}/maintenance`, 'PATCH', {base_revision: baseRevision.value, deletions: selected.value});
  offset.value = 0; await loadRows();
}
function transferFailed(cause: unknown) {
  transferMessage.value = '';
  transferError.value = cause instanceof Error ? cause.message : '回传失败，请重试。';
  transferConflict.value = cause instanceof ApiError && cause.code === 'REVISION_CONFLICT';
}
async function downloadMaintenance() {
  transferAction.value = 'download'; transferError.value = ''; transferMessage.value = '';
  try {
    // Refresh the visible revision; use the filename of the actual server snapshot.
    await load(); await loadRows();
    const filename = await download(`/workflow-tasks/${props.id}/maintenance/download`);
    transferConflict.value = false;
    transferMessage.value = `已下载最新维护表：${filename}。请编辑此文件后回传，不要选择之前下载的旧文件。`;
  } catch (cause) { transferFailed(cause); }
  finally { transferAction.value = ''; }
}
async function downloadReturnLatest() {
  returnPreview.value = undefined;
  returnStructure.value = undefined; rowReview.value=[];
  await downloadMaintenance();
}
async function reupload(event: Event) {
  const input = event.target as HTMLInputElement; const file = input.files?.[0]; input.value = '';
  if (!file) return;
  transferAction.value = 'upload'; transferFilename.value = file.name;
  transferError.value = ''; transferMessage.value = ''; transferConflict.value = false;
  returnPreview.value = undefined;
  returnStructure.value = undefined; rowReview.value=[];resolvedReturnRows.value={};
  const form = new FormData(); form.append('excel_file', file);
  try {
    if (standardization.value) await acceptReturnInspection(await api(`/workflow-tasks/${props.id}/maintenance/returns/inspect`, 'POST', form));
    else returnPreview.value = await api(`/workflow-tasks/${props.id}/maintenance/returns`, 'POST', form);
    identityAcknowledged.value = false;
    if(panel.value!=='output')transferMessage.value = returnPreview.value?'已读取改动，请核对后保存。':'有少量内容无法自动识别，请确认标出的项目。';
  } catch (cause) { transferFailed(cause); }
  finally { transferAction.value = ''; }
}
async function applyReturn() {
  transferAction.value = 'apply'; transferError.value = ''; transferMessage.value = ''; transferConflict.value = false;
  try {
    task.value = await api<Task>(`/workflow-tasks/${props.id}/maintenance/returns/apply`, 'POST', {preview_id: returnPreview.value?.id, delete_missing: true, acknowledge_identity_warnings: identityAcknowledged.value, confirm_structure:true, confirm_stale:staleAcknowledged.value});
    visibleIds.value=fields.value.map(field=>field.rule.name); offset.value=0;
    if (sort.value && !fields.value.some(field=>field.rule.name===sort.value)) sort.value='';
    await showReturnSaved();
    // Saving has already succeeded. A list-refresh failure must not report
    // the return as failed or invite the user to submit the same revision again.
    try{await loadRows();}catch{error.value='回传已保存成功，但维护数据列表刷新失败。可刷新页面重新查看。';}
  } catch (cause) { transferFailed(cause); }
  finally { transferAction.value = ''; }
}
async function generate() {
  if (!standardization.value && !outputTemplateId.value) {
    panel.value = 'output';
    throw new Error('请选择已发布的最终输出模板。');
  }
  await api(`/workflow-tasks/${props.id}/exports`, 'POST', {base_revision: task.value?.revision, output_template_id: standardization.value?null:outputTemplateId.value});
  returnSavedRevision.value=null;
  panel.value = 'output'; await load();
}
async function filter() { offset.value = 0; await loadRows(); }
function unload(event: BeforeUnloadEvent) { if (dirty.value) { event.preventDefault(); event.returnValue = ''; } }
onMounted(() => { void run(() => load(true)); window.addEventListener('beforeunload', unload);
  window.addEventListener('excel-auditor:operation', operationEvent);
  timer = window.setInterval(async () => {
    if (polling || (busy.value && !analysisActive.value)) return;
    if (task.value?.status !== 'processing' && !task.value?.exports.some(run => run.status === 'processing') && !recentOperations.value.some(job => job.status === 'processing') && !analysisActive.value && task.value?.payload.processing_progress?.status !== 'running') return;
    polling = true;
    try {
      if (analysisActive.value && task.value) {
        const latest = await api<Task>(`/workflow-tasks/${props.id}`);
        if (latest.payload.processing_progress?.status === 'running') task.value.payload.processing_progress = latest.payload.processing_progress;
      } else await load();
    } catch (cause) { error.value = cause instanceof Error ? cause.message : '进度暂时更新失败'; }
    finally { polling = false; }
  }, 1500);
});
onUnmounted(() => { window.clearInterval(timer); window.removeEventListener('beforeunload', unload); window.removeEventListener('excel-auditor:operation', operationEvent); emit('dirty', false); });
</script>

<template>
  <button class="btn text breadcrumb" @click="emit('back')">← 返回工作台</button>
  <div v-if="backgroundStatus" class="notice" role="status">{{ backgroundStatus }}</div>
  <div v-if="error" class="notice error" role="alert">{{ error }}</div><div v-if="message" class="notice success" role="status">{{ message }}</div>
  <template v-if="task">
    <details v-if="recentOperations.length" class="card">
      <summary>最近处理记录（刷新或重新打开页面后可继续查看结果）</summary>
      <p v-for="job in recentOperations" :key="job.id">
        {{ operationNames[job.operation] || '数据处理' }}：{{ job.status === 'ready' ? '已完成' : job.status === 'failed' ? '失败' : '排队或处理中' }}
        <span v-if="job.error"> · {{ job.error }}</span>
        <button v-if="job.status === 'ready'" class="btn" :disabled="busy" @click="run(() => resumeOperation(job))">{{ job.operation === 'maintenance_download' ? '下载维护表' : '查看结果' }}</button>
      </p>
    </details>
    <div class="page-heading"><div><h1>{{ task.payload.name }}</h1><p class="subtle">{{ task.payload.filename }} <span class="dot">·</span> {{ task.payload.field_snapshot?.template_name || task.payload.config.name }} V{{ task.payload.template_version }}</p><p v-if="showCreator" class="subtle task-creator">创建账号：{{ task.created_by_username || task.created_by_id || '未知账号' }}</p></div><span class="badge large" :class="task.status">{{ labels[task.status] }}</span></div>
    <ol class="stepper"><li v-for="(label, index) in ['导入原表','确认字段','维护数据','生成最终表']" :key="label" :class="{current: index === step, done: index < inferredStep}"><button type="button" :aria-current="index === step ? 'step' : undefined" :title="`查看${label}`" @click="selectStep(index)"><span>{{ index < inferredStep ? '✓' : index + 1 }}</span><strong>{{ label }}</strong></button></li></ol>
    <ProcessingProgress v-if="task.status !== 'processing' && task.payload.processing_progress" :progress="task.payload.processing_progress" :active="analysisActive" :fallback-started-at="task.created_at" />
    <details v-if="formulaReports.length" class="card formula-review"><summary>已校验 {{formulaReports.length}} 个计算公式，{{correctedFormulas.length}} 个缓存结果已重新计算</summary><p class="subtle">系统独立计算后核对结果，原文件保持不变。请在确认字段时核对这些结果。</p><div class="table-scroll"><table class="data-table"><thead><tr><th>来源</th><th>公式</th><th>文件缓存</th><th>采用结果</th><th>校验</th></tr></thead><tbody><tr v-for="item in formulaReports" :key="item.source_sheet+item.cell"><td>{{item.source_sheet}} · {{item.cell}}</td><td>{{item.formula}}</td><td>{{item.cached_value || '无缓存'}}</td><td>{{item.value}}</td><td>{{item.status==='cache_verified'?'一致':'已重新计算'}}</td></tr></tbody></table></div></details>
    <ProcessingProgress v-if="task.status === 'processing'" :progress="task.payload.processing_progress" active :fallback-started-at="task.created_at" />
    <section v-else-if="['failed','cancelled'].includes(task.status)" class="card empty-state"><h2>{{ labels[task.status] }}</h2><p>{{ task.payload.error || '原始文件已保留，可以重新开始解析。' }}</p><button class="btn" :disabled="busy" @click="run(async () => { await api(`/workflow-tasks/${id}/retry`, 'POST', {base_revision: task!.revision}); await load(); })">重新解析</button></section>
    <section v-else-if="activeStep === 0" class="card step-review">
      <div class="section-heading"><div><h2>导入原表</h2><p class="subtle">{{ task.payload.filename }}。以下是识别到的工作表、表头和原表预览。</p></div><button class="btn text" @click="activeStep = null">返回当前步骤</button></div>
      <article v-for="sheet in task.payload.sheets" :key="sheet.sheet" class="source-sheet">
        <div class="section-heading"><h3>{{ sheet.sheet }}</h3><span class="subtle">{{ sheet.row_count }} 条数据 · 表头第 {{ sheet.header_row }} 行 · {{ sheet.header_depth }} 层</span></div>
        <div class="table-scroll preview-table"><table><tbody><tr v-for="(row, rowIndex) in sheet.preview" :key="rowIndex"><th>{{ rowIndex + 1 }}</th><td v-for="(value, columnIndex) in row" :key="columnIndex"><ImageValue v-if="imageIds(value).length" :value="value" :task-id="id" /><template v-else>{{ value }}</template></td></tr></tbody></table></div>
      </article>
    </section>
    <section v-else-if="task.status === 'mapping'" class="mapping-area">
      <div v-if="standardization" class="standardization-status"><span class="badge">{{task.payload.field_snapshot?.template_name || task.payload.field_snapshot?.profile.name || '字段快照'}} · V{{task.payload.field_snapshot?.version}}</span><span class="subtle">{{dynamicFieldCount?'固定字段 + 全部动态字段联合匹配 · 动态字段为本地模拟接口数据':'尚未配置动态字段，当前目标只有固定字段'}}</span><button v-if="!task.payload.field_snapshot?.compatibility_version" class="btn" :disabled="!canEdit || busy" @click="run(() => analyzeMapping(true))">升级规格与分块识别</button><button class="btn" :disabled="!canEdit || busy" @click="run(() => analyzeMapping())"><Sparkles :size="16" />重新分析全部字段</button></div>
      <div v-if="needsUnifiedAnalysis" class="notice info">此任务使用旧的类目匹配范围。请点击「重新分析全部字段」切换为联合匹配；仍使用本任务的字段快照，不读取后续发布配置。</div>
      <div class="notice info">{{preserveExtras ? '匹配成功的原表列填入固定字段或动态字段；未匹配列完整保留在动态字段后面，支持维护、回传和导出。' : '确认来源字段如何进入维护表。未匹配的商家原表列不会导入维护表。'}} 不需要的工作表可取消勾选。</div>
      <article v-for="(plan, index) in plans" :key="plan.sheet" class="card">
        <div class="section-heading"><label class="inline-check"><input v-model="plan.include" type="checkbox" /><h2>{{ plan.sheet }}</h2></label><span class="subtle">识别到 {{ task.payload.sheets?.[index].row_count }} 条数据</span></div>
        <template v-if="plan.include">
          <div class="toolbar"><label class="compact-label">表头行<input v-model.number="plan.header_row" type="number" min="1" max="50" /></label><label class="compact-label">表头层数<input v-model.number="plan.header_depth" type="number" min="1" max="3" /></label><details><summary>查看原表预览</summary><div class="table-scroll preview-table"><table><tbody><tr v-for="(row, r) in task.payload.sheets?.[index].preview" :key="r"><th>{{ r + 1 }}</th><td v-for="(value,c) in row" :key="c"><ImageValue v-if="imageIds(value).length" :value="value" :task-id="id" /><template v-else>{{ value }}</template></td></tr></tbody></table></div></details></div>
          <p v-if="targetFields.some(f=>f.rule.name==='product_name') && !plan.bindings.some(b=>b.field==='product_name')" class="danger">尚未设置商品名称来源，请选择名称来源/拼接规则，或留待运营补录。没有可靠来源时不会自动用型号替代名称。</p>
          <p v-for="note in mappingNotes(index)" :key="note" class="danger">{{ note }}</p>
          <p v-for="note in task.payload.sheets?.[index]?.mapping_notices || []" :key="note" class="subtle">{{ note }}</p>
          <template v-if="standardization"><p class="mapping-coverage">目标 {{targetFields.length}} 个字段（固定 {{targetFields.length-dynamicFieldCount}} + 动态 {{dynamicFieldCount}}） · 已匹配 {{targetFields.length-unmatchedFor(plan).length}} · 未匹配 {{unmatchedFor(plan).length}}</p><details v-if="unmatchedFor(plan).length" class="unmatched-targets"><summary>查看 {{unmatchedFor(plan).length}} 个未匹配目标字段</summary><p class="subtle">未找到可靠来源的字段不会自动编造数据，可添加映射或在维护表中补录。</p><ul><li v-for="field in unmatchedFor(plan)" :key="field.rule.name">{{fieldLabel(field)}}<span v-if="field.rule.required" class="danger">（必填，待补录）</span><span v-else class="subtle">（未匹配）</span></li></ul></details></template>
          <div class="table-scroll"><table class="data-table mapping-table"><thead><tr><th>来源列</th><th></th><th>目标字段</th><th>处理规则</th><th>参数 / 依据</th><th></th></tr></thead><tbody><tr v-for="(binding,b) in plan.bindings" :key="b"><td><select v-model="binding.columns" multiple aria-label="来源列"><option v-for="column in task.payload.sheets?.[index].columns" :key="column.column" :value="column.column">{{ column.source_column || column.column }}. {{ column.header }}{{ column.example ? ' · ' + imageDisplayText(column.example).slice(0, 16) : '' }}</option></select></td><td class="subtle">→</td><td><select v-model="binding.field" aria-label="维护字段"><option v-for="field in fieldsFor()" :key="field.rule.name" :value="field.rule.name">{{ fieldLabel(field) }}{{ field.rule.required ? ' *' : '' }}</option></select></td><td><select v-model="binding.operation" aria-label="处理规则"><option v-for="op in operations" :key="op.value" :value="op.value">{{ op.label }}</option><option value="trim">去除首尾空格</option></select><small v-if="binding.method">{{binding.method==='confirmed_reuse'?'历史映射':binding.method==='semantic_model'?'模型建议':binding.method==='exact'?'同名 / 别名':binding.method==='header_rule'?'表头规则建议':'人工配置'}}</small></td><td><input v-if="!['copy','trim'].includes(binding.operation)" v-model="binding.argument" placeholder="系数 / 固定值 / 分隔符" /><input v-if="binding.operation === 'split'" v-model.number="binding.part" type="number" min="0" max="50" aria-label="拆分索引" /><small>{{binding.reason}}</small></td><td><button class="icon-button" aria-label="移除映射" @click="plan.bindings.splice(b, 1)">×</button></td></tr></tbody></table></div>
          <details v-if="preserveExtras && retainedFor(plan).length" class="unmatched-targets"><summary>原表保留列 {{retainedFor(plan).length}} 列（追加在动态字段后）</summary><ul><li v-for="column in retainedFor(plan)" :key="column.column">第 {{column.source_column || column.column}} 列 · {{column.header}}</li></ul><p class="subtle">按原表顺序保留；同名列按工作表和列位置区分，不会覆盖或合并。</p></details>
          <div class="form-footer"><span class="subtle">多选来源列可配置拼接；{{preserveExtras ? '移除映射后，来源列转入原表保留列。' : '移除映射后，该列不会导入维护表。'}}</span><button class="btn" @click="plan.bindings.push({field: targetFields[0].rule.name, columns: [1], operation: 'copy', argument: '', part: 0})">＋ 添加映射</button></div>
        </template>
      </article>
      <div class="sticky-actions"><span>确认后会生成独立维护数据，原表保持完整。</span><button class="btn primary" :disabled="!canEdit || busy || needsUnifiedAnalysis" @click="run(confirmMapping)">{{ busy ? '正在生成…' : '确认并生成维护表 →' }}</button></div>
    </section>
    <section v-else-if="activeStep === 1" class="card step-review">
      <div class="section-heading"><div><h2>确认字段</h2><p class="subtle">这是当前维护表使用的字段映射。需要调整时，重新确认后会按新映射重建维护数据。</p></div><div class="toolbar"><button class="btn text" @click="activeStep = null">返回当前步骤</button><button class="btn primary" :disabled="!canEdit || busy" @click="run(reopenMapping)">重新确认字段</button></div></div>
      <article v-for="mapping in savedMappings" :key="mapping.sheet" class="source-sheet">
        <div class="section-heading"><h3>{{ mapping.sheet }}</h3><span class="subtle">{{ mapping.include ? '已导入' : '未导入此工作表' }}</span></div>
        <div v-if="mapping.include" class="table-scroll"><table class="data-table mapping-review-table"><thead><tr><th>原表字段</th><th>维护字段</th><th>处理方式</th></tr></thead><tbody><tr v-for="(binding, bindingIndex) in mapping.bindings" :key="bindingIndex"><td>{{ binding.columns.map(column => sourceColumnLabel(mapping.sheet, column)).join('、') }}</td><td>{{ fields.find(field => field.rule.name === binding.field)?.rule.title || binding.field }}</td><td>{{ operationLabel(binding) }}</td></tr><tr v-if="!mapping.bindings.length"><td colspan="3" class="empty-inline">没有配置字段映射。</td></tr></tbody></table></div>
      </article>
    </section>
    <template v-else>
      <div class="section-tabs"><button :class="{active: panel === 'maintenance'}" @click="panel = 'maintenance'">维护数据</button><button :class="{active: panel === 'output'}" :disabled="dirty || busy" @click="panel = 'output'">最终表与下载</button><span class="subtle">修订 R{{ task.revision }} · {{ task.payload.record_count }} 条记录</span></div>
      <template v-if="panel === 'maintenance'">
        <div class="maintenance-summary" :class="{clean: !task.payload.error_count}"><div><strong>{{ task.payload.error_count ? `${task.payload.error_count} 个问题需要处理` : '数据校验通过，可以生成最终表' }}</strong><p>{{ task.payload.error_count ? '必填与格式问题待处理。' : standardization?'输出使用本任务的字段快照。':'选择最终输出模板后，再生成文件。' }}</p></div><button v-if="task.payload.error_count" class="btn" :disabled="dirty || busy" @click="issuesOnly = true; run(filter)">只看问题记录</button><button v-else class="btn primary" :disabled="busy || dirty" @click="panel = 'output'">{{standardization?'预览输出结构':'选择输出模板'}} →</button></div>
        <p v-if="preserveExtras" class="notice info">字段顺序：固定 {{targetFields.length-dynamicFieldCount}} → 动态 {{dynamicFieldCount}} → 原表保留 {{retainedFields.length}}。原表保留列可编辑，下载、回传和最终导出均包含这些列。</p>
        <div v-if="task.payload.source_review" class="notice info"><p v-if="task.payload.source_review.missing_product_names.length" class="danger">有 {{task.payload.source_review.missing_product_names.length}} 条商品缺少名称。请确认原表名称来源/拼接规则，或在维护表补录；不会自动用型号冒充名称。</p><p>原表说明、费用及空行共 {{task.payload.source_review.context_rows}} 行，保留在对应来源区域，不填入商品字段、不参加商品必填校验。来源列只在自己的来源行填值，其他来源行留空。</p><details v-if="task.payload.source_review.empty_source_columns.length"><summary>原表本身没有数据的保留列 {{task.payload.source_review.empty_source_columns.length}} 列</summary><ul><li v-for="item in task.payload.source_review.empty_source_columns" :key="item.field">{{item.source.sheet}} · 第{{item.source.column}}列 · {{item.source.header || '未命名列'}}</li></ul></details></div>
        <section class="card maintenance-card">
          <form class="toolbar" @submit.prevent="run(filter)"><input v-model="search" class="search" placeholder="搜索商品或字段内容" aria-label="搜索维护数据" :disabled="dirty || busy" /><label class="inline-check"><input v-model="issuesOnly" type="checkbox" :disabled="dirty || busy" @change="run(filter)" />只看问题</label><button class="btn" :disabled="dirty || busy">筛选</button><details class="column-picker"><summary>显示字段 {{ visibleIds.length }}</summary><div><label v-for="field in fields" :key="field.rule.name" class="inline-check"><input v-model="visibleIds" type="checkbox" :value="field.rule.name" />{{ fieldLabel(field) }}</label></div></details><select v-model="sort" :disabled="dirty || busy" aria-label="排序字段" @change="run(filter)"><option value="">原始顺序</option><option v-for="field in fields" :key="field.rule.name" :value="field.rule.name">按{{ fieldLabel(field) }}排序</option></select><button v-if="sort" type="button" class="btn" :disabled="dirty || busy" @click="descending = !descending; run(filter)">{{ descending ? '降序 ↓' : '升序 ↑' }}</button></form>
          <div v-if="canEdit" class="toolbar batch-toolbar"><select :disabled="busy" v-model="bulkField" aria-label="批量填充字段"><option value="">选择批量填充字段</option><option v-for="field in fields.filter(f => f.editable)" :key="field.rule.name" :value="field.rule.name">{{ fieldLabel(field) }}</option></select><input :disabled="busy" v-model="bulkValue" placeholder="批量填充值" aria-label="批量填充值" /><button class="btn" type="button" :disabled="!canEdit || busy" @click="batchFill">填充{{ selected.length ? `选中 ${selected.length} 条` : '当前页' }}</button><span class="toolbar-spacer"></span><button class="btn" :disabled="!canEdit || busy || dirty" @click="run(addRow)">＋ 新增记录</button><button v-if="selected.length" class="btn danger-button" :disabled="busy || dirty" @click="run(removeRows)">删除选中</button></div>
          <div class="table-scroll editor-scroll"><table class="data-table editor-table" :class="{'grouped-source-headers': groupedSourceHeaders}"><thead>
            <tr><th class="select-cell" :rowspan="groupedSourceHeaders ? 2 : 1"><input type="checkbox" aria-label="选择当前页" :checked="rows.length > 0 && selected.length === rows.length" @change="selected = ($event.target as HTMLInputElement).checked ? rows.map(r => r.id) : []" /></th><th v-if="!standardization" class="row-number" :rowspan="groupedSourceHeaders ? 2 : 1">序号</th>
              <th v-for="group in maintenanceHeaderGroups" :key="group.key" :colspan="group.fields.length" :rowspan="groupedSourceHeaders && !group.source ? 2 : 1" :scope="group.source ? 'colgroup' : 'col'" :class="{'source-header-group': group.source}">{{ group.title }}<template v-if="!group.source"><b v-if="group.fields[0]?.rule.required" class="danger"> *</b><span v-if="!group.fields[0]?.editable"> 🔒</span></template></th><th :rowspan="groupedSourceHeaders ? 2 : 1">来源 / 问题</th>
            </tr>
            <tr v-if="groupedSourceHeaders" class="source-header-fields"><th v-for="field in visibleFields.filter(f => f.source_column)" :key="field.rule.name" scope="col">{{ fieldLabel(field) }}<b v-if="field.rule.required" class="danger"> *</b><span v-if="!field.editable"> 🔒</span></th></tr>
          </thead><tbody><tr v-for="row in rows" :key="row.id"><td><input v-model="selected" type="checkbox" :value="row.id" :aria-label="`选择第${row.ordinal}条`" /></td><td v-if="!standardization" class="subtle">{{ row.ordinal }}</td><template v-for="field in visibleFields" :key="field.rule.name"><td v-if="cellSpan(row, field) > 0" :rowspan="cellSpan(row, field)" :colspan="cellColumnSpan(row,field)" :class="{'invalid-cell': row.issues.some(i => i.field === field.rule.name), 'edited-cell': changes[row.id]?.[field.rule.name] !== undefined}"><ImageValue v-if="imageIds(changes[row.id]?.[field.rule.name] ?? row.values_json[field.rule.name] ?? '').length" :value="changes[row.id]?.[field.rule.name] ?? row.values_json[field.rule.name] ?? ''" :task-id="id" :editable="canEdit && field.editable && !busy" @clear="updateCell(row, field.rule.name, '')" /><select v-else-if="field.rule.type === 'enum' || field.rule.type === 'boolean'" :value="changes[row.id]?.[field.rule.name] ?? row.values_json[field.rule.name] ?? ''" :disabled="!canEdit || !field.editable || busy" :aria-label="`${fieldLabel(field)} 第${row.ordinal}条`" @change="updateCell(row, field.rule.name, ($event.target as HTMLSelectElement).value)"><option value="">请选择</option><template v-if="field.rule.type === 'boolean'"><option value="true">是</option><option value="false">否</option></template><option v-for="value in field.rule.enum_values" :key="value" :value="value">{{ value }}</option></select><textarea v-else-if="field.source_column || (field.rule.type === 'string' && cellSpan(row, field) > 1)" :readonly="!canEdit || !field.editable" :rows="cellSpan(row, field) > 1 ? 3 : 2" :value="changes[row.id]?.[field.rule.name] ?? row.values_json[field.rule.name] ?? ''" :disabled="busy" :aria-label="`${fieldLabel(field)} 第${row.ordinal}条`" @input="updateCell(row, field.rule.name, ($event.target as HTMLTextAreaElement).value)"></textarea><input v-else :value="changes[row.id]?.[field.rule.name] ?? row.values_json[field.rule.name] ?? ''" :readonly="!canEdit || !field.editable" :disabled="busy" :aria-label="`${fieldLabel(field)} 第${row.ordinal}条`" :aria-invalid="row.issues.some(i => i.field === field.rule.name)" @input="updateCell(row, field.rule.name, ($event.target as HTMLInputElement).value)" /><span v-for="issue in row.issues.filter(i => i.field === field.rule.name)" :key="issue.message" class="field-error">{{ issue.message }}</span></td></template><td><button class="btn text source-link" @click="detailRow = row"><span v-if="row.source.kind==='context'" class="badge">{{row.source.context_reason || '原表说明'}}</span>{{ row.source.sheet }}{{ row.source.row ? ' · ' + row.source.row + ' 行' : '' }}<span v-if="row.issues.length" class="danger"> · {{ row.issues.length }} 个问题</span></button></td></tr><tr v-if="!rows.length"><td :colspan="visibleFields.length + (standardization ? 2 : 3)" class="empty-inline">没有符合条件的记录。</td></tr></tbody></table></div>
          <div class="pagination"><span>第 {{ total ? offset + 1 : 0 }}—{{ Math.min(offset + pageSize, total) }} 条，共 {{ total }} 条</span><span v-if="dirty" class="warning-text">请先保存修改再翻页</span><button class="btn" :disabled="offset === 0 || dirty || busy" @click="offset -= pageSize; run(loadRows)">上一页</button><button class="btn" :disabled="offset + pageSize >= total || dirty || busy" @click="offset += pageSize; run(loadRows)">下一页</button></div>
        </section>
        <div class="sticky-actions maintenance-transfer">
          <div class="transfer-controls"><div class="toolbar">
            <button class="btn" :disabled="busy || dirty" @click="run(downloadMaintenance)">{{ transferAction === 'download' ? '正在下载…' : '下载最新维护表' }}</button>
            <label class="btn file-button" :class="{disabled: !canEdit || busy || dirty}">{{ transferAction === 'upload' ? '正在读取…' : '回传维护表' }}<input type="file" accept=".xlsx" aria-label="回传维护表" :disabled="!canEdit || busy || dirty" @change="run(() => reupload($event))" /></label>
            <span class="subtle">{{ dirty ? `有 ${Object.keys(changes).length} 条未保存修改，请先保存再回传` : `当前维护版本 R${task.revision} · 请编辑本次下载的文件后回传` }}</span>
          </div><button class="btn primary" :disabled="!canEdit || busy || !dirty" @click="run(save)">{{ busy && !transferAction ? '正在保存…' : '保存并校验' }}</button></div>
          <p class="subtle">改好 Excel 后直接回传，系统自动对比字段和单元格，核对改动后保存。只有无法识别的项目需要单独确认。新增行留空 ID；排序请移动整行。保留分层表头和隐藏的任务信息工作表。</p>
          <p v-if="transferAction" class="transfer-feedback" role="status">{{ transferAction === 'upload' ? `正在上传并校验 ${transferFilename}…` : transferAction === 'download' ? '正在获取最新维护表…' : '正在保存回传修改…' }}</p>
          <div v-else-if="transferError" class="notice error transfer-feedback" role="alert"><span>{{ transferError }}{{ transferConflict ? ' 当前数据未被覆盖。请下载最新版，将修改填入新文件后重新回传。' : '' }}</span><button v-if="transferConflict" class="btn" :disabled="busy || dirty" @click="run(downloadReturnLatest)">下载最新版</button></div>
          <p v-else-if="transferMessage" class="notice success transfer-feedback" role="status">{{ transferMessage }}</p>
        </div>
      </template>
      <template v-else>
        <section v-if="returnSavedRevision===task.revision" ref="returnSaveNotice" class="notice success" role="status" tabindex="-1" aria-label="回传保存成功">
          <strong>维护表回传已保存成功（R{{returnSavedRevision}}）</strong>
          <p v-if="returnSavedMessage">{{returnSavedMessage}}</p>
          <p>{{task.payload.error_count?`已进入最终表页面。还有 ${task.payload.error_count} 个数据问题，请先修正后再生成。`:'已进入最终表页面。请点击“生成最终表”开始生成。'}}</p>
          <button v-if="task.payload.error_count" class="btn" :disabled="busy" @click="selectStep(2);issuesOnly=true;run(filter)">返回处理问题</button>
        </section>
        <section class="card"><div class="section-heading"><div><h2>本次输出方案</h2><p class="subtle">维护修订 R{{ task.revision }}{{standardization?` · 字段快照 V${task.payload.field_snapshot?.version}`:' · 请选择输出模板'}}</p></div><button class="btn primary" :disabled="!canEdit || busy || (!standardization && !outputTemplateId) || !!task.payload.error_count || task.exports.some(r => r.status === 'processing')" @click="run(generate)">生成最终表</button></div><div v-if="!standardization" class="output-template"><label>选择最终输出模板<select v-model="outputTemplateId" :disabled="!outputTemplates.length" aria-label="选择最终输出模板"><option value="">{{ outputTemplates.length ? '请选择已发布的最终输出模板' : '暂无已发布的最终输出模板' }}</option><option v-for="template in outputTemplates" :key="template.id" :value="template.id">{{ template.payload.config.name }} · V{{ template.payload.version }}</option></select></label></div><div v-if="standardization" class="notice info">{{categories.length?(task.payload.field_snapshot?.dynamic_source==='platform_import'?'动态字段来自任务创建时导入的平台字段快照。':'动态字段来自模板中的本地配置。'):'本次仅输出固定字段。'}}</div><div v-if="!standardization && !outputTemplates.length" class="notice error">请先发布最终输出表模板。</div><div v-if="task.payload.error_count" class="notice error">还有 {{ task.payload.error_count }} 个问题，请回到维护数据处理后再生成。</div><p v-if="standardization && dynamicFieldCount" class="notice info">最终表将动态字段归入“商品属性”和“商品规格”两列，每条商品按顺序显示“字段名：值”，空值保留字段名。</p><div v-if="standardization || selectedOutputTemplate" class="output-grid"><article v-for="file in displayedOutputs" :key="file.name" class="output-card"><span class="file-icon">X</span><div><h3>{{ file.display_name || file.name }}</h3><p v-for="sheet in file.sheets" :key="sheet.name" class="subtle">{{ sheet.name }} · {{ sheet.columns.length }} 个字段{{ sheet.group_by ? ' · 按字段拆表' : '' }}{{ sheet.sku_fields.length ? ' · 展开 SKU' : '' }}</p></div></article></div></section>
        <section class="card"><div class="section-heading"><h2>生成记录</h2><button class="btn text" @click="run(() => load())">刷新</button></div><div v-if="!task.exports.length" class="empty-state"><h3>最终表尚未生成</h3><p>生成成功后，点击文件下载按钮即可获取 Excel。</p></div><article v-for="item in task.exports" :key="item.id" class="export-run"><div class="section-heading"><div><strong>维护修订 R{{ item.payload.revision }}</strong><span class="subtle"> · {{ dateLabel(item.created_at) }}</span></div><span class="badge" :class="item.status">{{ executionLabel(item) }}</span></div><p v-if="item.payload.error" class="danger">{{ item.payload.error }}</p><template v-if="item.status === 'completed'"><div class="toolbar"><button v-for="file in item.payload.artifacts" :key="`preview-${file.name}`" class="btn" @click="run(() => previewOutput(item.id, file.name))">预览 {{ file.download_name || file.name }}</button><button v-for="file in item.payload.artifacts" :key="file.name" class="btn primary" @click="run(() => download(`/workflow-tasks/${id}/exports/${item.id}/files/${encodeURIComponent(file.name)}`))">↓ {{ file.download_name || file.name }}</button><button class="btn" @click="run(() => download(`/workflow-tasks/${id}/exports/${item.id}/files/${encodeURIComponent('最终表.zip')}`, '最终表.zip'))">下载完整文件包</button></div><p v-if="item.payload.revision !== task.revision" class="subtle">此结果属于历史修订，未包含当前维护修改。</p></template></article></section>
      </template>
      <details class="history"><summary>修订记录与来源说明</summary><p v-for="item in task.history" :key="item.revision" class="subtle">R{{ item.revision }} · {{ dateLabel(item.created_at) }} · {{ item.detail.action === 'import' ? `导入 ${item.detail.records} 条记录` : `修改 ${item.detail.changed_records || 0} 条，新增 ${item.detail.added || 0} 条，删除 ${item.detail.deleted || 0} 条` }}</p><p v-for="(item,i) in task.payload.ignored" :key="i">{{ item.sheet }} / {{ item.column || '整表' }}：{{ item.reason }}</p></details>
    </template>
  </template>
  <div v-if="namePreview" class="modal-backdrop">
    <section class="dialog return-structure-dialog" role="dialog" aria-modal="true" aria-label="确认商品名称更新">
      <div class="section-heading"><h2>确认商品名称更新</h2><button class="icon-button" aria-label="关闭名称确认" :disabled="busy" @click="namePreview=undefined">×</button></div>
      <p class="subtle">{{nameFilename}} · 基于任务修订 R{{namePreview.payload.base_revision}}</p>
      <p>更新 {{namePreview.payload.counts.changed}} 条；空白跳过 {{namePreview.payload.counts.blank}} 条；名称相同 {{namePreview.payload.counts.unchanged}} 条。</p>
      <p v-if="namePreview.payload.counts.overwritten" class="notice warning">其中 {{namePreview.payload.counts.overwritten}} 条已有名称将被替换，请逐条核对原名称和确认名称。</p>
      <p class="subtle">仅更新下列记录的商品名称；未列出的记录及其他字段保持原样。</p>
      <div class="table-scroll"><table class="data-table"><thead><tr><th>原文件 / 工作表 / 行号</th><th>原名称</th><th>确认名称</th><th>记录ID</th></tr></thead><tbody><tr v-for="item in namePreview.payload.samples.slice(nameOffset,nameOffset+30)" :key="item.id"><td>{{item.filename}}<br />{{item.sheet}} · {{item.row ?? '手工新增'}}</td><td>{{item.before || '（空）'}}</td><td>{{item.after}}</td><td>{{item.id}}</td></tr></tbody></table></div>
      <div class="pagination"><span>共 {{namePreview.payload.samples.length}} 条待更新</span><button class="btn" :disabled="busy || nameOffset===0" @click="nameOffset-=30">上一页名称</button><button class="btn" :disabled="busy || nameOffset+30>=namePreview.payload.samples.length" @click="nameOffset+=30">下一页名称</button></div>
      <p v-if="nameError" class="notice error" role="alert">{{nameError}}</p>
      <div class="toolbar"><button class="btn" :disabled="busy" @click="namePreview=undefined">取消名称更新</button><button class="btn primary" :disabled="!canEdit || busy || !namePreview.payload.counts.changed" @click="run(applyNames)">确认更新商品名称</button></div>
    </section>
  </div>
  <div v-if="returnStructure && !returnPreview && !autoPreviewing" class="modal-backdrop">
    <section class="dialog return-structure-dialog" role="dialog" aria-modal="true" aria-label="确认回传字段结构">
      <div class="section-heading"><h2>{{showAllReturnColumns?'调整字段对应':'这几处需要你确认'}}</h2><button class="icon-button" :disabled="busy" aria-label="关闭结构确认" @click="closeReturn">×</button></div>
      <p class="subtle">{{showAllReturnColumns?'这里可以调整对应关系，修改后重新查看改动；未对应的原字段将同步删除。':'其他内容已自动识别。这里只处理无法确定的列或商品，确认后直接查看改动。'}}</p>
      <p v-if="transferError" class="notice error" role="alert">{{transferError}}</p>
      <div v-if="error" class="notice error" role="alert">{{error}}</div>
      <details v-if="showAllReturnColumns" class="return-structure-summary" aria-label="字段增删改预览"><summary>查看字段调整摘要</summary>
        <h3>字段变化预览</h3>
        <p class="subtle">这里只预览，不会立即修改。完成字段对应后，下一步查看商品逐条增删改明细。</p>
        <div class="summary-pills"><span v-for="(count,kind) in structureChangeCounts" :key="kind">{{kind}} {{count}} 列</span></div>
        <p v-if="!structureChanges.length" class="subtle">字段名称和相对顺序未发生变化。</p>
        <div v-else class="table-scroll"><table class="data-table"><thead><tr><th>变化类型</th><th>字段</th><th>确认后的处理</th></tr></thead><tbody><tr v-for="change in structureChanges" :key="change.key"><td><strong>{{change.kind}}</strong></td><td>{{change.field}}</td><td>{{change.detail}}</td></tr></tbody></table></div>
      </details>
      <div v-if="reviewColumns.length" class="table-scroll return-column-review"><table class="data-table"><thead><tr><th>回传位置 / 表头</th><th>处理</th><th>对应原字段</th><th>字段名称</th><th>字段归属</th><th>新增字段类型</th></tr></thead><tbody>
        <tr v-for="column in reviewColumns" :key="column.key" :data-column-key="column.key">
          <td>{{column.sheet}} · 第 {{column.column}} 列<br /><strong>{{column.path || '未命名'}}</strong><small>{{column.matched_by}}</small></td>
          <td><select v-model="column.action" aria-label="回传列处理"><option value="existing">对应已有字段</option><option value="new">新增字段</option><option value="ignore">忽略此列</option></select></td>
          <td><select v-if="column.action==='existing'" v-model="column.field_id" aria-label="对应原字段" @change="chooseReturnField(column)"><option value="">请选择</option><option v-for="field in returnStructure.payload.field_options" :key="field.id" :value="field.id">{{field.title}}</option></select></td>
          <td><input v-if="column.action!=='ignore'" v-model="column.title" aria-label="确认字段名称" maxlength="255" /></td>
          <td><select v-if="column.action!=='ignore'" v-model="column.kind" aria-label="最终输出归属"><option value="normal">普通字段（独立列）</option><option value="attribute">动态字段 / 商品属性</option><option value="specification">动态字段 / 商品规格</option></select></td>
          <td><select v-if="column.action==='new'" v-model="column.type" aria-label="新增字段类型"><option value="string">文本</option><option value="id_code">编号</option><option value="integer">整数</option><option value="decimal">数值</option><option value="boolean">是/否</option><option value="date">日期</option><option value="datetime">日期时间</option></select></td>
        </tr>
      </tbody></table></div>
      <div v-if="missingReturnFields.length" class="return-missing-fields"><h3>确认后同步删除 {{missingReturnFields.length}} 列</h3><p class="subtle">必填、只读及业务规则引用字段若被删除，系统会明确报错，不会悄悄恢复。</p><p v-for="field in missingReturnFields" :key="field.id">{{field.title}}</p></div>
      <div v-if="rowReview.length"><h3>确认商品对应关系（{{rowReview.filter(r=>!r.action || (r.action==='existing' && !r.record_id)).length}} 条待确认）</h3><p class="subtle">ID 缺失、重复或疑似错位时，系统不会自动覆盖商品。选择已有商品、明确新增或忽略该行。</p>
        <datalist id="return-record-options"><option v-for="record in returnRecordOptions" :key="record.id" :value="record.id">{{record.label}}</option></datalist>
        <div class="table-scroll"><table class="data-table"><thead><tr><th>位置与内容</th><th>待确认原因</th><th>处理方式</th><th>对应原商品</th></tr></thead><tbody><tr v-for="row in rowReview.slice(reviewOffset,reviewOffset+50)" :key="row.key"><td>{{row.sheet}} 第 {{row.row}} 行<br />{{row.summary}}</td><td>{{row.reason}}</td><td><select v-model="row.action" aria-label="商品行处理"><option value="">请选择</option><option value="existing">对应已有商品</option><option value="new">作为新增商品</option><option value="ignore">忽略此行</option></select></td><td><input v-if="row.action==='existing'" v-model="row.record_id" list="return-record-options" aria-label="对应原商品" placeholder="选择原商品" /><small v-if="row.record_id">{{returnRecordOptions.find(r=>r.id===row.record_id)?.label}}</small></td></tr></tbody></table></div>
        <div v-if="rowReview.length>50" class="toolbar"><button class="btn" :disabled="reviewOffset===0" @click="reviewOffset-=50">上一页</button><span>{{reviewOffset+1}}–{{Math.min(reviewOffset+50,rowReview.length)}} / {{rowReview.length}}</span><button class="btn" :disabled="reviewOffset+50>=rowReview.length" @click="reviewOffset+=50">下一页</button></div>
      </div>
      <button v-if="!showAllReturnColumns" class="btn text" @click="showAllReturnColumns=true">需要时调整其他字段</button>
      <div class="form-footer"><button class="btn" :disabled="busy" @click="closeReturn">取消</button><button class="btn primary" :disabled="busy" @click="run(previewStructure)">{{busy ? '正在核对…' : '查看改动'}}</button></div>
    </section>
  </div>
  <div v-if="returnPreview" class="modal-backdrop">
    <section class="dialog" role="dialog" aria-modal="true" aria-label="维护表回传预览">
      <div class="section-heading"><h2>这次改了什么</h2><button class="icon-button" :disabled="busy" aria-label="关闭预览" @click="closeReturn">×</button></div>
      <div v-if="returnPreview.payload.stale_revision" class="notice warning" role="alert">
        <strong>这是旧版 R{{returnPreview.payload.source_revision}}，当前数据为 R{{returnPreview.payload.base_revision}}</strong>
        <p>下面展示与当前数据的全部差异。“原来”是当前保存的值，“改成”是本次回传的值。旧表可能覆盖后来修改的内容，或删除后来新增的商品和字段；请逐项核对。本次按确认后的回传内容保存，不会自动合并两版。</p>
        <p>删除行、列的位置按当前版本维护表显示；修改和新增行的位置按本次上传文件显示。</p>
      </div>
      <p class="subtle">回传文件：{{ transferFilename }}</p>
      <p v-if="outdatedReturnPreview" class="notice error">此预览使用旧的保留规则。请重新上传维护表，核对同步删除的行列后再保存。</p>
      <p v-else class="notice">以本次回传内容为准：确认保存后，文件中删除的行列同步删除，最终表按回传后的数据和字段生成。</p>
      <div v-if="transferError" class="notice error" role="alert">{{ transferError }}<button v-if="transferConflict" class="btn" :disabled="busy" @click="run(downloadReturnLatest)">下载最新版</button></div>
      <section aria-label="本次改动汇总">
        <h3>本次改动汇总</h3>
        <div v-if="returnChangeSummary.length" class="summary-pills"><span v-for="item in returnChangeSummary" :key="item">{{item}}</span></div>
        <p v-else class="subtle">没有识别到改动。</p>
        <p v-if="returnDeletionSummary" class="subtle">{{returnDeletionSummary}}</p>
      </section>
      <template v-if="returnOtherFieldChanges.length"><h3>其他字段改动</h3>
      <div class="table-scroll return-field-diff"><table class="data-table" aria-label="字段修改对照"><thead><tr><th>位置</th><th>变化</th><th>原来</th><th>改成 / 本次处理</th></tr></thead><tbody><tr v-for="(change,index) in returnOtherFieldChanges" :key="index"><td>{{change.location}}<small v-if="change.original_column">（原 {{change.original_column}} 列）</small></td><td>{{change.kind}}</td><td>{{change.before}}</td><td>{{change.after}}</td></tr></tbody></table></div></template>
      <p v-else-if="!returnPreview.payload.field_changes" class="subtle">此旧预览未记录列位置，重新上传可查看完整对照。</p>
      <details v-if="returnPreview.payload.structure_summary?.length"><summary>其他表头和排序说明</summary><ul><li v-for="(item,index) in returnPreview.payload.structure_summary" :key="index">{{item}}</li></ul></details>
      <div v-for="warning in returnPreview.payload.identity_warnings" :key="warning.code" class="notice error" role="alert">{{ warning.message }}</div>
      <p v-if="returnPreview.status==='row_review'" class="notice">已识别的改动先展示在下面。仍有 {{rowReview.length}} 行需要核对对应关系，确认后更新清单，暂不会保存或删除任何数据。</p>
      <template v-for="kind in (['changed','added','missing'] as const)" :key="kind"><ReturnDiffDetails v-if="returnPreview.payload.counts[kind]" :only-kind="kind" :current-baseline="returnPreview.payload.stale_revision" :pending-identity="returnPreview.status==='row_review'" :task-id="id" :preview-id="returnPreview.id" :counts="returnPreview.payload.counts" :delete-missing="true" :field-titles="returnFieldTitles" :complete="!!returnPreview.payload.detail_manifest" :fallback="{changed:returnPreview.payload.samples,added:returnPreview.payload.added_samples||[],missing:returnPreview.payload.missing_samples||[]}" /></template>
      <section v-if="returnDeletedColumns.length" aria-label="删除列明细">
        <h3>文件中删除的列（{{returnDeletedColumns.length}} 列）</h3>
        <p class="subtle">下面是下载时的列位置；这些列已不在回传文件中。</p>
        <div class="table-scroll"><table class="data-table" aria-label="删除列对照"><thead><tr><th>原维护表列位置</th><th>字段名称</th><th>确认保存后的处理</th></tr></thead><tbody><tr v-for="(change,index) in returnDeletedColumns" :key="index"><td><strong>{{change.original_column?`${change.original_column} 列`:'旧预览未记录位置'}}</strong></td><td>{{change.before||change.field}}</td><td>{{outdatedReturnPreview?'旧预览，请重新上传核对':'删除字段及整列数据，最终表不再包含该列'}}</td></tr></tbody></table></div>
      </section>
      <section v-if="returnPreview.status==='row_review'" aria-label="待核对商品行"><h3>以下行需要核对对应关系</h3><datalist id="pending-return-records"><option v-for="record in returnRecordOptions" :key="record.id" :value="record.id">{{record.label}}</option></datalist>
        <div class="table-scroll"><table class="data-table"><thead><tr><th>回传位置 / 内容</th><th>原因</th><th>处理方式</th><th>对应原商品</th></tr></thead><tbody><tr v-for="row in rowReview.slice(reviewOffset,reviewOffset+50)" :key="row.key"><td>{{row.sheet}} 第 {{row.row}} 行<br />{{row.summary}}</td><td>{{row.reason}}</td><td><select v-model="row.action" aria-label="商品行处理"><option value="">请选择</option><option value="existing">对应已有商品</option><option value="new">作为新增商品</option><option value="ignore">忽略此行</option></select></td><td><input v-if="row.action==='existing'" v-model="row.record_id" list="pending-return-records" aria-label="对应原商品" /></td></tr></tbody></table></div>
        <div v-if="rowReview.length>50" class="toolbar"><button class="btn" :disabled="reviewOffset===0" @click="reviewOffset-=50">上一页待核对行</button><span>{{reviewOffset+1}}–{{Math.min(reviewOffset+50,rowReview.length)}} / {{rowReview.length}}</span><button class="btn" :disabled="reviewOffset+50>=rowReview.length" @click="reviewOffset+=50">下一页待核对行</button></div>
        <button class="btn primary" :disabled="busy || rowReview.some(r=>!r.action || (r.action==='existing'&&!r.record_id))" @click="run(previewStructure)">确认对应并刷新改动</button>
      </section>
      <label v-if="needsIdentityConfirmation" class="inline-check"><input v-model="identityAcknowledged" type="checkbox" :disabled="busy" />我已核对 ID 风险提示及差异，确认这些新增和修改符合预期</label>
      <label v-if="returnPreview.payload.stale_revision" class="inline-check"><input v-model="staleAcknowledged" type="checkbox" :disabled="busy" />我已核对与当前版本的差异，确认按本次回传覆盖修改并同步删除列出的行列</label>
      <div class="form-footer"><button class="btn" :disabled="busy" @click="closeReturn">取消</button><button class="btn primary" :disabled="busy || outdatedReturnPreview || returnPreview.status==='row_review' || transferConflict || (needsIdentityConfirmation && !identityAcknowledged) || (returnPreview.payload.stale_revision && !staleAcknowledged)" @click="run(applyReturn)">{{transferAction === 'apply' ? '正在保存…' : '确认保存这些改动'}}</button></div>
    </section>
  </div>
  <div v-if="outputPreview" class="modal-backdrop"><section class="dialog" role="dialog" aria-modal="true" aria-label="最终表预览"><div class="section-heading"><h2>{{ outputPreview.name }}</h2><button class="icon-button" aria-label="关闭最终表预览" @click="outputPreview = undefined">×</button></div><p class="subtle">每表显示前 12 行；公式计算结果请下载后在 Excel 中查看。</p><section v-for="sheet in outputPreview.sheets" :key="sheet.name"><h3>{{ sheet.name }}</h3><div class="table-scroll"><table class="data-table"><tbody><tr v-for="(row,i) in sheet.rows" :key="i"><td v-for="(value,j) in row" :key="j"><ImageValue v-if="imageIds(value).length" :value="value" :task-id="id" /><template v-else>{{ value }}</template></td></tr></tbody></table></div></section></section></div>
  <div v-if="detailRow" class="modal-backdrop"><section class="dialog" role="dialog" aria-modal="true" aria-label="记录来源"><div class="section-heading"><h2>来源与原始值</h2><button class="icon-button" aria-label="关闭来源" @click="detailRow = undefined">×</button></div><p v-if="detailRow.source.filename">原始文件：{{ detailRow.source.filename }} · 工作表：{{ detailRow.source.original_sheet }}</p><p>{{ detailRow.source.sheet }} · 第 {{ detailRow.source.row || '新增' }} 行</p><p v-for="issue in detailRow.issues" :key="issue.field + issue.message" class="notice error"><button class="btn text" @click="if (!visibleIds.includes(issue.field)) visibleIds.push(issue.field); detailRow = undefined">{{ fields.find(f => f.rule.name === issue.field)?.rule.title }}：{{ issue.message }} → 定位字段</button></p><div class="table-scroll"><table class="data-table"><thead><tr><th>字段</th><th>导入值</th><th>当前值</th></tr></thead><tbody><tr v-for="field in fields" :key="field.rule.name"><td>{{ fieldLabel(field) }}</td><td>{{ imageDisplayText(detailRow.original[field.rule.name] || '') }}</td><td>{{ imageDisplayText(detailRow.values_json[field.rule.name] || '') }}</td></tr></tbody></table></div></section></div>
</template>
