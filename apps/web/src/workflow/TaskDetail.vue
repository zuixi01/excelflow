<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { api, dateLabel, download, labels, operations, useAction, type Binding, type Task, type Row, type Template } from "./api";
const props = defineProps<{id: string}>();
const emit = defineEmits<{back: []; dirty: [value: boolean]}>();
const {busy, error, message, run} = useAction();
const task = ref<Task>(), rows = ref<Row[]>([]), total = ref(0), offset = ref(0), pageSize = 20;
const plans = ref<Array<{sheet: string; include: boolean; header_row: number; header_depth: number; bindings: Binding[]; ignored_columns: number[]}>>([]);
const changes = ref<Record<string, Record<string, string>>>({}), baseRevision = ref(0), selected = ref<string[]>([]);
const search = ref(""), issuesOnly = ref(false), sort = ref(""), descending = ref(false), visibleIds = ref<string[]>([]);
const bulkField = ref(""), bulkValue = ref(""), panel = ref("maintenance"), detailRow = ref<Row>();
const activeStep = ref<number | null>(null);
const outputTemplates = ref<Template[]>([]), outputTemplateId = ref("");
const returnPreview = ref<{id: string; payload: {counts: {changed: number; added: number; missing: number}; samples: Array<{id: string; before: Record<string, string>; after: Record<string, string>}>}}>();
const deleteMissing = ref(false);
const outputPreview = ref<{name: string; sheets: Array<{name: string; rows: string[][]}>}>();
async function previewOutput(runId: string, filename: string) {
  outputPreview.value = await api(`/workflow-tasks/${props.id}/exports/${runId}/preview/${encodeURIComponent(filename)}`);
}
let timer: number | undefined;
const dirty = computed(() => Object.keys(changes.value).length > 0);
const fields = computed(() => task.value?.payload.config.fields || []);
const visibleFields = computed(() => fields.value.filter(field => visibleIds.value.includes(field.rule.name)));
const selectedOutputTemplate = computed(() => outputTemplates.value.find(template => template.id === outputTemplateId.value));
const displayedOutputs = computed(() => selectedOutputTemplate.value?.payload.config.outputs || []);
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
    return structuredClone(saved || {sheet: sheet.sheet, include: true, header_row: sheet.header_row, header_depth: sheet.header_depth,
      ignored_columns: [], bindings: sheet.suggested_bindings || sheet.columns.filter(c => c.field).map(c => ({field: c.field, columns: [c.column], operation: 'copy', argument: '', part: 0}))});
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
  return source ? `${source.column}. ${source.header}` : `第 ${column} 列`;
}
async function loadRows() {
  if (!task.value) return;
  const params = new URLSearchParams({offset: String(offset.value), limit: String(pageSize), search: search.value, issues_only: String(issuesOnly.value), sort: sort.value, descending: String(descending.value)});
  const result = await api<{items: Row[]; total: number; revision: number}>(`/workflow-tasks/${props.id}/maintenance?${params}`);
  rows.value = result.items; total.value = result.total; baseRevision.value = result.revision; selected.value = [];
}
async function load(initial = false) {
  const result = await api<Task>(`/workflow-tasks/${props.id}`); task.value = result;
  if (!outputTemplates.value.length) {
    const definitions = await api<{items: Template[]}>("/workflow-definitions");
    outputTemplates.value = definitions.items.filter(template => template.payload.purpose === "output" || !!template.payload.sources?.output);
  }
  if (result.status === 'mapping' && (initial || !plans.value.length)) plans.value = mappingPlans(result);
  if (initial && result.status === 'completed') panel.value = 'output';
  if (!visibleIds.value.length) {
    const common = ['product_name','sku','brand','product_category','sale_price','stock','sales_unit'];
    visibleIds.value = fields.value.filter(field => common.includes(field.rule.name) || field.rule.required).map(field => field.rule.name);
    if (!visibleIds.value.length) visibleIds.value = fields.value.slice(0, 7).map(field => field.rule.name);
  }
  if (initial && ['maintenance', 'ready', 'completed'].includes(result.status)) await loadRows();
}
async function confirmMapping() {
  task.value = await api<Task>(`/workflow-tasks/${props.id}/mapping`, 'POST', {base_revision: task.value?.revision, sheets: plans.value});
  activeStep.value = null; panel.value = 'maintenance'; await loadRows();
}
async function reopenMapping() {
  if (!task.value || !window.confirm('重新确认字段会清空当前维护数据，并按新的字段映射重新生成。确定继续吗？')) return;
  task.value = await api<Task>(`/workflow-tasks/${props.id}/mapping/reopen`, 'POST', {base_revision: task.value.revision});
  plans.value = mappingPlans(task.value); activeStep.value = null;
  message.value = '已回到确认字段步骤。请修改映射后重新生成维护表。';
}
function updateCell(row: Row, field: string, value: string) {
  if (value === (row.values_json[field] || '')) { if (changes.value[row.id]) delete changes.value[row.id][field]; }
  else { changes.value[row.id] ||= {}; changes.value[row.id][field] = value; }
  if (changes.value[row.id] && !Object.keys(changes.value[row.id]).length) delete changes.value[row.id];
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
async function reupload(event: Event) {
  const input = event.target as HTMLInputElement; const file = input.files?.[0]; input.value = '';
  if (!file) return;
  const form = new FormData(); form.append('excel_file', file);
  returnPreview.value = await api(`/workflow-tasks/${props.id}/maintenance/returns`, 'POST', form); deleteMissing.value = false;
}
async function applyReturn() {
  task.value = await api<Task>(`/workflow-tasks/${props.id}/maintenance/returns/apply`, 'POST', {preview_id: returnPreview.value?.id, delete_missing: deleteMissing.value});
  returnPreview.value = undefined; await loadRows(); message.value = '回传修改已保存为新修订。';
}
async function generate() {
  if (!outputTemplateId.value) {
    panel.value = 'output';
    throw new Error('请选择已发布的最终输出模板。');
  }
  await api(`/workflow-tasks/${props.id}/exports`, 'POST', {base_revision: task.value?.revision, output_template_id: outputTemplateId.value});
  panel.value = 'output'; await load();
}
async function filter() { offset.value = 0; await loadRows(); }
function unload(event: BeforeUnloadEvent) { if (dirty.value) { event.preventDefault(); event.returnValue = ''; } }
onMounted(() => { void run(() => load(true)); window.addEventListener('beforeunload', unload);
  timer = window.setInterval(() => { if (!busy.value && (task.value?.status === 'processing' || task.value?.exports.some(run => run.status === 'processing'))) void load().catch(cause => { error.value = cause.message; }); }, 1500);
});
onUnmounted(() => { window.clearInterval(timer); window.removeEventListener('beforeunload', unload); emit('dirty', false); });
</script>

<template>
  <button class="btn text breadcrumb" @click="emit('back')">← 返回工作台</button>
  <div v-if="error" class="notice error" role="alert">{{ error }}</div><div v-if="message" class="notice success" role="status">{{ message }}</div>
  <template v-if="task">
    <div class="page-heading"><div><h1>{{ task.payload.name }}</h1><p class="subtle">{{ task.payload.filename }} <span class="dot">·</span> {{ task.payload.config.name }} V{{ task.payload.template_version }}</p></div><span class="badge large" :class="task.status">{{ labels[task.status] }}</span></div>
    <ol class="stepper"><li v-for="(label, index) in ['导入原表','确认字段','维护数据','生成最终表']" :key="label" :class="{current: index === step, done: index < inferredStep}"><button type="button" :aria-current="index === step ? 'step' : undefined" :title="`查看${label}`" @click="selectStep(index)"><span>{{ index < inferredStep ? '✓' : index + 1 }}</span><strong>{{ label }}</strong></button></li></ol>
    <section v-if="task.status === 'processing'" class="card empty-state"><div class="spinner"></div><h2>正在识别工作表与字段</h2><p>识别完成后，即可确认字段并生成维护表。</p></section>
    <section v-else-if="['failed','cancelled'].includes(task.status)" class="card empty-state"><h2>{{ labels[task.status] }}</h2><p>{{ task.payload.error || '原始文件已保留，可以重新开始解析。' }}</p><button class="btn" :disabled="busy" @click="run(async () => { await api(`/workflow-tasks/${id}/retry`, 'POST', {base_revision: task!.revision}); await load(); })">重新解析</button></section>
    <section v-else-if="activeStep === 0" class="card step-review">
      <div class="section-heading"><div><h2>导入原表</h2><p class="subtle">{{ task.payload.filename }}。以下是识别到的工作表、表头和原表预览。</p></div><button class="btn text" @click="activeStep = null">返回当前步骤</button></div>
      <article v-for="sheet in task.payload.sheets" :key="sheet.sheet" class="source-sheet">
        <div class="section-heading"><h3>{{ sheet.sheet }}</h3><span class="subtle">{{ sheet.row_count }} 条数据 · 表头第 {{ sheet.header_row }} 行 · {{ sheet.header_depth }} 层</span></div>
        <div class="table-scroll preview-table"><table><tbody><tr v-for="(row, rowIndex) in sheet.preview" :key="rowIndex"><th>{{ rowIndex + 1 }}</th><td v-for="(value, columnIndex) in row" :key="columnIndex">{{ value }}</td></tr></tbody></table></div>
      </article>
    </section>
    <section v-else-if="task.status === 'mapping'" class="mapping-area">
      <div class="notice info">确认来源字段如何进入维护表。未匹配的商家原表列不会导入维护表；不需要的工作表可取消勾选。</div>
      <article v-for="(plan, index) in plans" :key="plan.sheet" class="card">
        <div class="section-heading"><label class="inline-check"><input v-model="plan.include" type="checkbox" /><h2>{{ plan.sheet }}</h2></label><span class="subtle">识别到 {{ task.payload.sheets?.[index].row_count }} 条数据</span></div>
        <template v-if="plan.include">
          <div class="toolbar"><label class="compact-label">表头行<input v-model.number="plan.header_row" type="number" min="1" max="50" /></label><label class="compact-label">表头层数<input v-model.number="plan.header_depth" type="number" min="1" max="3" /></label><details><summary>查看原表预览</summary><div class="table-scroll preview-table"><table><tbody><tr v-for="(row, r) in task.payload.sheets?.[index].preview" :key="r"><th>{{ r + 1 }}</th><td v-for="(value,c) in row" :key="c">{{ value }}</td></tr></tbody></table></div></details></div>
          <p v-for="note in task.payload.sheets?.[index].mapping_notes" :key="note" class="danger">{{ note }}</p>
          <div class="table-scroll"><table class="data-table mapping-table"><thead><tr><th>来源列</th><th></th><th>维护字段</th><th>处理规则</th><th>参数</th><th></th></tr></thead><tbody><tr v-for="(binding,b) in plan.bindings" :key="b"><td><select v-model="binding.columns" multiple aria-label="来源列"><option v-for="column in task.payload.sheets?.[index].columns" :key="column.column" :value="column.column">{{ column.column }}. {{ column.header }}{{ column.example ? ' · ' + column.example.slice(0, 16) : '' }}</option></select></td><td class="subtle">→</td><td><select v-model="binding.field" aria-label="维护字段"><option v-for="field in fields" :key="field.rule.name" :value="field.rule.name">{{ field.rule.title }}{{ field.rule.required ? ' *' : '' }}</option></select></td><td><select v-model="binding.operation" aria-label="处理规则"><option v-for="op in operations" :key="op.value" :value="op.value">{{ op.label }}</option><option value="trim">去除首尾空格</option></select></td><td><input v-if="!['copy','trim'].includes(binding.operation)" v-model="binding.argument" placeholder="系数 / 固定值 / 分隔符" /><input v-if="binding.operation === 'split'" v-model.number="binding.part" type="number" min="0" max="50" aria-label="拆分索引" /></td><td><button class="icon-button" aria-label="移除映射" @click="plan.bindings.splice(b, 1)">×</button></td></tr></tbody></table></div>
          <div class="form-footer"><span class="subtle">多选来源列可配置拼接；移除映射后，该列不会导入维护表。</span><button class="btn" @click="plan.bindings.push({field: fields[0].rule.name, columns: [1], operation: 'copy', argument: '', part: 0})">＋ 添加映射</button></div>
        </template>
      </article>
      <div class="sticky-actions"><span>确认后会生成独立维护数据，原表保持完整。</span><button class="btn primary" :disabled="busy" @click="run(confirmMapping)">{{ busy ? '正在生成…' : '确认并生成维护表 →' }}</button></div>
    </section>
    <section v-else-if="activeStep === 1" class="card step-review">
      <div class="section-heading"><div><h2>确认字段</h2><p class="subtle">这是当前维护表使用的字段映射。需要调整时，重新确认后会按新映射重建维护数据。</p></div><div class="toolbar"><button class="btn text" @click="activeStep = null">返回当前步骤</button><button class="btn primary" :disabled="busy" @click="run(reopenMapping)">重新确认字段</button></div></div>
      <article v-for="mapping in savedMappings" :key="mapping.sheet" class="source-sheet">
        <div class="section-heading"><h3>{{ mapping.sheet }}</h3><span class="subtle">{{ mapping.include ? '已导入' : '未导入此工作表' }}</span></div>
        <div v-if="mapping.include" class="table-scroll"><table class="data-table mapping-review-table"><thead><tr><th>原表字段</th><th>维护字段</th><th>处理方式</th></tr></thead><tbody><tr v-for="(binding, bindingIndex) in mapping.bindings" :key="bindingIndex"><td>{{ binding.columns.map(column => sourceColumnLabel(mapping.sheet, column)).join('、') }}</td><td>{{ fields.find(field => field.rule.name === binding.field)?.rule.title || binding.field }}</td><td>{{ operationLabel(binding) }}</td></tr><tr v-if="!mapping.bindings.length"><td colspan="3" class="empty-inline">没有配置字段映射。</td></tr></tbody></table></div>
      </article>
    </section>
    <template v-else>
      <div class="section-tabs"><button :class="{active: panel === 'maintenance'}" @click="panel = 'maintenance'">维护数据</button><button :class="{active: panel === 'output'}" :disabled="dirty" @click="panel = 'output'">最终表与下载</button><span class="subtle">修订 R{{ task.revision }} · {{ task.payload.record_count }} 条记录</span></div>
      <template v-if="panel === 'maintenance'">
        <div class="maintenance-summary" :class="{clean: !task.payload.error_count}"><div><strong>{{ task.payload.error_count ? `${task.payload.error_count} 个问题需要处理` : '数据校验通过，可以生成最终表' }}</strong><p>{{ task.payload.error_count ? '可先保存草稿，处理完必填与格式问题后再生成。' : '选择最终输出模板后，再生成文件。' }}</p></div><button v-if="task.payload.error_count" class="btn" :disabled="dirty" @click="issuesOnly = true; run(filter)">只看问题记录</button><button v-else class="btn primary" :disabled="busy || dirty" @click="panel = 'output'">选择输出模板 →</button></div>
        <section class="card maintenance-card">
          <form class="toolbar" @submit.prevent="run(filter)"><input v-model="search" class="search" placeholder="搜索商品或字段内容" aria-label="搜索维护数据" :disabled="dirty" /><label class="inline-check"><input v-model="issuesOnly" type="checkbox" :disabled="dirty" @change="run(filter)" />只看问题</label><button class="btn" :disabled="dirty || busy">筛选</button><details class="column-picker"><summary>显示字段 {{ visibleIds.length }}</summary><div><label v-for="field in fields" :key="field.rule.name" class="inline-check"><input v-model="visibleIds" type="checkbox" :value="field.rule.name" />{{ field.rule.title }}</label></div></details><select v-model="sort" :disabled="dirty" aria-label="排序字段" @change="run(filter)"><option value="">原始顺序</option><option v-for="field in fields" :key="field.rule.name" :value="field.rule.name">按{{ field.rule.title }}排序</option></select><button v-if="sort" type="button" class="btn" :disabled="dirty" @click="descending = !descending; run(filter)">{{ descending ? '降序 ↓' : '升序 ↑' }}</button></form>
          <div class="toolbar batch-toolbar"><select v-model="bulkField" aria-label="批量填充字段"><option value="">选择批量填充字段</option><option v-for="field in fields.filter(f => f.editable)" :key="field.rule.name" :value="field.rule.name">{{ field.rule.title }}</option></select><input v-model="bulkValue" placeholder="批量填充值" aria-label="批量填充值" /><button class="btn" type="button" :disabled="busy" @click="batchFill">填充{{ selected.length ? `选中 ${selected.length} 条` : '当前页' }}</button><span class="toolbar-spacer"></span><button class="btn" :disabled="busy || dirty" @click="run(addRow)">＋ 新增记录</button><button v-if="selected.length" class="btn danger-button" :disabled="busy || dirty" @click="run(removeRows)">删除选中</button></div>
          <div class="table-scroll editor-scroll"><table class="data-table editor-table"><thead><tr><th class="select-cell"><input type="checkbox" aria-label="选择当前页" :checked="rows.length > 0 && selected.length === rows.length" @change="selected = ($event.target as HTMLInputElement).checked ? rows.map(r => r.id) : []" /></th><th class="row-number">序号</th><th v-for="field in visibleFields" :key="field.rule.name">{{ field.rule.title }}<b v-if="field.rule.required" class="danger"> *</b><span v-if="!field.editable"> 🔒</span></th><th>来源 / 问题</th></tr></thead><tbody><tr v-for="row in rows" :key="row.id"><td><input v-model="selected" type="checkbox" :value="row.id" :aria-label="`选择第${row.ordinal}条`" /></td><td class="subtle">{{ row.ordinal }}</td><td v-for="field in visibleFields" :key="field.rule.name" :class="{'invalid-cell': row.issues.some(i => i.field === field.rule.name), 'edited-cell': changes[row.id]?.[field.rule.name] !== undefined}"><select v-if="field.rule.type === 'enum' || field.rule.type === 'boolean'" :value="changes[row.id]?.[field.rule.name] ?? row.values_json[field.rule.name] ?? ''" :disabled="!field.editable || busy" :aria-label="`${field.rule.title} 第${row.ordinal}条`" @change="updateCell(row, field.rule.name, ($event.target as HTMLSelectElement).value)"><option value="">请选择</option><template v-if="field.rule.type === 'boolean'"><option value="true">是</option><option value="false">否</option></template><option v-for="value in field.rule.enum_values" :key="value" :value="value">{{ value }}</option></select><input v-else :value="changes[row.id]?.[field.rule.name] ?? row.values_json[field.rule.name] ?? ''" :readonly="!field.editable" :disabled="busy" :aria-label="`${field.rule.title} 第${row.ordinal}条`" :aria-invalid="row.issues.some(i => i.field === field.rule.name)" @input="updateCell(row, field.rule.name, ($event.target as HTMLInputElement).value)" /><span v-for="issue in row.issues.filter(i => i.field === field.rule.name)" :key="issue.message" class="field-error">{{ issue.message }}</span></td><td><button class="btn text source-link" @click="detailRow = row">{{ row.source.sheet }}{{ row.source.row ? ' · ' + row.source.row + ' 行' : '' }}<span v-if="row.issues.length" class="danger"> · {{ row.issues.length }} 个问题</span></button></td></tr><tr v-if="!rows.length"><td :colspan="visibleFields.length + 3" class="empty-inline">没有符合条件的记录。</td></tr></tbody></table></div>
          <div class="pagination"><span>第 {{ total ? offset + 1 : 0 }}—{{ Math.min(offset + pageSize, total) }} 条，共 {{ total }} 条</span><span v-if="dirty" class="warning-text">请先保存修改再翻页</span><button class="btn" :disabled="offset === 0 || dirty || busy" @click="offset -= pageSize; run(loadRows)">上一页</button><button class="btn" :disabled="offset + pageSize >= total || dirty || busy" @click="offset += pageSize; run(loadRows)">下一页</button></div>
        </section>
        <div class="sticky-actions"><div class="toolbar"><button class="btn" :disabled="busy || dirty" @click="run(() => download(`/workflow-tasks/${id}/maintenance/download`, '商品维护表.xlsx'))">下载维护表</button><label class="btn file-button" :class="{disabled: busy || dirty}">回传维护表<input type="file" accept=".xlsx" :disabled="busy || dirty" @change="run(() => reupload($event))" /></label><span class="subtle">{{ dirty ? `有 ${Object.keys(changes).length} 条未保存修改` : '修改后保存，校验会同步更新' }}</span></div><button class="btn primary" :disabled="busy || !dirty" @click="run(save)">{{ busy ? '正在保存…' : '保存并校验' }}</button></div>
      </template>
      <template v-else>
        <section class="card"><div class="section-heading"><div><h2>本次输出方案</h2><p class="subtle">使用维护修订 R{{ task.revision }}，选择最终输出模板后生成整套文件。</p></div><button class="btn primary" :disabled="busy || !outputTemplateId || !!task.payload.error_count || task.exports.some(r => r.status === 'processing')" @click="run(generate)">生成最终表</button></div><div class="output-template"><label>选择最终输出模板<select v-model="outputTemplateId" :disabled="!outputTemplates.length" aria-label="选择最终输出模板"><option value="">{{ outputTemplates.length ? '请选择已发布的最终输出模板' : '暂无已发布的最终输出模板' }}</option><option v-for="template in outputTemplates" :key="template.id" :value="template.id">{{ template.payload.config.name }} · V{{ template.payload.version }}</option></select></label><p class="subtle">标准维护模板只负责维护数据；这里可选择任一已发布的最终输出模板。</p></div><div v-if="!outputTemplates.length" class="notice error">请先在模板中心发布一份用途为“最终输出表模板”的模板。</div><div v-if="task.payload.error_count" class="notice error">还有 {{ task.payload.error_count }} 个问题，请回到维护数据处理后再生成。</div><div v-if="selectedOutputTemplate" class="output-grid"><article v-for="file in displayedOutputs" :key="file.name" class="output-card"><span class="file-icon">X</span><div><h3>{{ file.name }}</h3><p v-for="sheet in file.sheets" :key="sheet.name" class="subtle">{{ sheet.name }} · {{ sheet.columns.length }} 个字段{{ sheet.group_by ? ' · 按字段拆表' : '' }}{{ sheet.sku_fields.length ? ' · 展开 SKU' : '' }}</p></div></article></div></section>
        <section class="card"><div class="section-heading"><h2>生成记录</h2><button class="btn text" @click="run(() => load())">刷新</button></div><div v-if="!task.exports.length" class="empty-state"><h3>最终表尚未生成</h3><p>生成成功后，可单独下载或下载整套文件。</p></div><article v-for="item in task.exports" :key="item.id" class="export-run"><div class="section-heading"><div><strong>维护修订 R{{ item.payload.revision }}</strong><span class="subtle"> · {{ dateLabel(item.created_at) }}</span></div><span class="badge" :class="item.status">{{ item.status === 'processing' ? '正在生成' : labels[item.status] }}</span></div><p v-if="item.payload.error" class="danger">{{ item.payload.error }}</p><template v-if="item.status === 'completed'"><div class="toolbar"><button v-for="file in item.payload.artifacts" :key="`preview-${file.name}`" class="btn" @click="run(() => previewOutput(item.id, file.name))">预览 {{ file.name }}</button><button v-for="file in item.payload.artifacts" :key="file.name" class="btn" @click="run(() => download(`/workflow-tasks/${id}/exports/${item.id}/files/${encodeURIComponent(file.name)}`, file.name))">↓ {{ file.name }}</button><button class="btn primary" @click="run(() => download(`/workflow-tasks/${id}/exports/${item.id}/files/${encodeURIComponent('最终表.zip')}`, '最终表.zip'))">下载整套文件</button></div><p v-if="item.payload.revision !== task.revision" class="subtle">此结果属于历史修订，未包含当前维护修改。</p></template></article></section>
      </template>
      <details class="history"><summary>修订记录与来源说明</summary><p v-for="item in task.history" :key="item.revision" class="subtle">R{{ item.revision }} · {{ dateLabel(item.created_at) }} · {{ item.detail.action === 'import' ? `导入 ${item.detail.records} 条记录` : `修改 ${item.detail.changed_records || 0} 条，新增 ${item.detail.added || 0} 条，删除 ${item.detail.deleted || 0} 条` }}</p><p v-for="(item,i) in task.payload.ignored" :key="i">{{ item.sheet }} / {{ item.column || '整表' }}：{{ item.reason }}</p></details>
    </template>
  </template>
  <div v-if="returnPreview" class="modal-backdrop"><section class="dialog" role="dialog" aria-modal="true" aria-label="维护表回传预览"><div class="section-heading"><h2>确认回传修改</h2><button class="icon-button" aria-label="关闭预览" @click="returnPreview = undefined">×</button></div><div class="summary-pills"><span>修改 {{ returnPreview.payload.counts.changed }} 条</span><span>新增 {{ returnPreview.payload.counts.added }} 条</span><span>文件中缺少 {{ returnPreview.payload.counts.missing }} 条</span></div><div class="return-samples"><article v-for="sample in returnPreview.payload.samples" :key="sample.id"><p v-for="(value,key) in sample.after" :key="key"><strong>{{ fields.find(f => f.rule.name === key)?.rule.title || key }}</strong>：{{ sample.before[key] || '空' }} → {{ value || '空' }}</p></article></div><label v-if="returnPreview.payload.counts.missing" class="inline-check"><input v-model="deleteMissing" type="checkbox" />同时删除回传文件中缺少的记录（默认保留）</label><div class="form-footer"><button class="btn" @click="returnPreview = undefined">取消</button><button class="btn primary" :disabled="busy" @click="run(applyReturn)">确认并保存新修订</button></div></section></div>
  <div v-if="outputPreview" class="modal-backdrop"><section class="dialog" role="dialog" aria-modal="true" aria-label="最终表预览"><div class="section-heading"><h2>{{ outputPreview.name }}</h2><button class="icon-button" aria-label="关闭最终表预览" @click="outputPreview = undefined">×</button></div><p class="subtle">每表显示前 12 行；公式计算结果请下载后在 Excel 中查看。</p><section v-for="sheet in outputPreview.sheets" :key="sheet.name"><h3>{{ sheet.name }}</h3><div class="table-scroll"><table class="data-table"><tbody><tr v-for="(row,i) in sheet.rows" :key="i"><td v-for="(value,j) in row" :key="j">{{ value }}</td></tr></tbody></table></div></section></section></div>
  <div v-if="detailRow" class="modal-backdrop"><section class="dialog" role="dialog" aria-modal="true" aria-label="记录来源"><div class="section-heading"><h2>来源与原始值</h2><button class="icon-button" aria-label="关闭来源" @click="detailRow = undefined">×</button></div><p>{{ detailRow.source.sheet }} · 第 {{ detailRow.source.row || '新增' }} 行</p><p v-for="issue in detailRow.issues" :key="issue.field + issue.message" class="notice error"><button class="btn text" @click="if (!visibleIds.includes(issue.field)) visibleIds.push(issue.field); detailRow = undefined">{{ fields.find(f => f.rule.name === issue.field)?.rule.title }}：{{ issue.message }} → 定位字段</button></p><div class="table-scroll"><table class="data-table"><thead><tr><th>字段</th><th>导入值</th><th>当前值</th></tr></thead><tbody><tr v-for="field in fields" :key="field.rule.name"><td>{{ field.rule.title }}</td><td>{{ detailRow.original[field.rule.name] }}</td><td>{{ detailRow.values_json[field.rule.name] }}</td></tr></tbody></table></div></section></div>
</template>
