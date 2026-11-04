<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { Plus, Upload, Clock3 } from 'lucide-vue-next';
import { ApiError, api, dateLabel, executionLabel, labels, useAction, type TaskSummary, type Task, type FieldProfileRecord } from "./api";
withDefaults(defineProps<{showCreator?: boolean;canEdit?:boolean}>(),{canEdit:true});
const emit = defineEmits<{open: [id: string];configure:[]}>();
const {busy, error, message, run} = useAction();
const items = ref<TaskSummary[]>([]), total = ref(0), offset = ref(0);
const showNew = ref(false), name = ref(""), excelFiles = ref<File[]>([]), search = ref(""), status = ref("");
const uploadBytes = computed(() => excelFiles.value.reduce((total, file) => total + file.size, 0));
const uploadError = computed(() => {
  if (excelFiles.value.length > 30) return '每个任务最多选择 30 个 Excel 文件，请移除多余文件。';
  if (excelFiles.value.some(file => !file.name.toLowerCase().endsWith('.xlsx'))) return '请选择 .xlsx 文件，其他格式请先转换。';
  if (excelFiles.value.some(file => file.size > 100 * 1024 * 1024)) return '单个 Excel 文件不能超过 100 MiB，请拆分文件后上传。';
  if (uploadBytes.value > 500 * 1024 * 1024) return '所选文件合计超过 500 MiB，请移除部分文件或拆分任务。';
  return '';
});
function selectFiles(event: Event) {
  const input = event.target as HTMLInputElement;
  excelFiles.value = [...excelFiles.value, ...Array.from(input.files || [])];
  input.value = '';
}
const selectedTaskIds = ref<string[]>([]);
const profiles=ref<FieldProfileRecord[]>([]), selectedProfileId=ref('');
const publishedProfiles=computed(()=>profiles.value.filter(item=>item.payload.published));
const profile=computed(()=>publishedProfiles.value.find(item=>item.id===selectedProfileId.value));
const fieldCounts=computed(()=>{
  const published=profile.value?.payload.published;
  return {fixed:published?.fixed_fields.filter(f=>f.enabled).length || 0,
    dynamic:published?.local_categories.flatMap(c=>[...c.attributes,...c.specifications]).filter(f=>f.enabled).length || 0};
});
let timer: number | undefined;
let clockTimer: number | undefined;
let loading = 0, loadVersion = 0;
const clock = ref(Date.now());
function aiTime(item: TaskSummary) {
  const timing = item.ai_timing;
  if (!timing) return {text:'未记录', detail:'', running:false};
  const running = timing.status === 'running' && item.status !== 'failed';
  if (!timing.ai_started_at) return {text:running ? '等待 AI' : '未调用 AI', detail:'', running:false};
  const end = timing.ai_finished_at || timing.finished_at;
  if (!end && !running) return {text:'未记录', detail:'', running:false};
  const start = Date.parse(timing.ai_started_at), finish = end ? Date.parse(end) : clock.value;
  if (!Number.isFinite(start) || !Number.isFinite(finish)) return {text:'未记录', detail:'', running:false};
  const seconds = Math.max(0, Math.floor((finish - start) / 1000));
  const parts = [Math.floor(seconds / 60) % 60, seconds % 60];
  if (seconds >= 3600) parts.unshift(Math.floor(seconds / 3600));
  return {text:parts.map(value=>String(value).padStart(2,'0')).join(':'),
    detail:!end ? '分析中' : timing.status === 'failed' ? '分析中断' : '已结束', running:!end};
}
const timingById = computed(() => Object.fromEntries(items.value.map(item=>[item.id,aiTime(item)])));
async function load() {
  const version = ++loadVersion;
  loading++;
  try {
    const data = await api<{items: TaskSummary[]; total: number}>(`/workflow-tasks?search=${encodeURIComponent(search.value)}&status=${status.value}&offset=${offset.value}`);
    if (version === loadVersion) { items.value = data.items; total.value = data.total; selectedTaskIds.value = selectedTaskIds.value.filter(id=>data.items.some(item=>item.id===id)); }
  } finally { loading--; }
}
async function create() {
  if (!excelFiles.value.length || !profile.value?.payload.published) throw new Error("请选择供应商 Excel，并确认字段配置已发布。");
  if (uploadError.value) throw new Error(uploadError.value);
  const form = new FormData();
  for (const file of excelFiles.value) form.append("excel_file", file);
  form.append("name", name.value);
  form.append("field_profile_id", selectedProfileId.value);
  for (let attempt = 1; attempt <= 6; attempt++) {
    try {
      const task = await api<Task>("/standardization-tasks", "POST", form);
      message.value = '';
      emit("open", task.id);
      return;
    } catch (cause) {
      if (!(cause instanceof ApiError) || cause.status !== 429 || cause.code !== 'RESOURCE_BUSY' || attempt === 6) throw cause;
      message.value = `同时上传的人较多，正在等待空闲上传通道（第 ${attempt} 次重试，约 10 秒后继续）。已选择的文件会保留。`;
      await new Promise(resolve => window.setTimeout(resolve, 10000 + Math.floor(Math.random() * 2000)));
    }
  }
}
async function removeTasks(ids: string[]) {
  const targets = ids.filter(id => items.value.some(item => item.id === id));
  if (!targets.length) return;
  if (!window.confirm(`确认删除选中的 ${targets.length} 条处理记录？原始文件、维护记录和导出结果都会删除，无法恢复。`)) return;
  await api<{deleted: string[]}>("/workflow-tasks", "DELETE", {ids: targets});
  selectedTaskIds.value = selectedTaskIds.value.filter(id => !targets.includes(id));
  if (items.value.length === targets.length && offset.value > 0) offset.value = Math.max(0, offset.value - 30);
  await load();
  message.value = `已删除 ${targets.length} 条处理记录。`;
}
onMounted(() => { void run(async () => {
  await load();
  profiles.value=(await api<{items:FieldProfileRecord[]}>('/field-profiles')).items;
  selectedProfileId.value=(publishedProfiles.value.find(item=>item.is_default) || publishedProfiles.value[0])?.id || '';
}); timer = window.setInterval(() => { if (items.value.some(item => item.status === "processing" || item.ai_timing?.status === 'running') && !busy.value && !loading) void load().catch(() => {}); }, 2000);
clockTimer = window.setInterval(() => { clock.value = Date.now(); }, 1000); });
onUnmounted(() => { window.clearInterval(timer); window.clearInterval(clockTimer); });
</script>

<template>
  <div class="page-heading"><div><h1>工作台</h1><p class="subtle">商品标准化 · 按业务模板整理数据</p></div><button class="btn primary" :disabled="!canEdit" @click="showNew = !showNew"><Plus :size="17" />新建任务</button></div>
  <div v-if="error" class="notice error" role="alert">{{ error }}</div><div v-if="message" class="notice success" role="status">{{ message }}</div>
  <section v-if="showNew && canEdit" class="config-section new-task">
    <div class="section-heading"><h2>导入一批新数据</h2><button class="btn text" @click="showNew = false">收起</button></div>
    <div class="form-grid"><label>业务模板<select v-model="selectedProfileId" :disabled="busy || !publishedProfiles.length" aria-label="选择业务模板"><option value="" disabled>请选择已发布模板</option><option v-for="item in publishedProfiles" :key="item.id" :value="item.id">{{item.payload.published_template_name}} · V{{item.payload.version}}</option></select></label><label>任务名称<input v-model="name" placeholder="例如：灯具商品整理" maxlength="100" /></label><div class="field-source-summary"><span>目标字段 · 固定 {{fieldCounts.fixed}} + 动态 {{fieldCounts.dynamic}}</span><strong>{{profile?.payload.published_template_name || '尚未发布'}} {{profile?.payload.version ? `V${profile.payload.version}`:''}}</strong><small>{{fieldCounts.dynamic ? '全部字段自动联合匹配 · 动态字段为本地模拟接口数据':'尚未配置动态字段，当前目标只有固定字段'}}</small></div></div>
    <div v-if="!profile?.payload.published" class="notice info">请先在模板管理中发布一个业务模板。<button class="btn text" @click="emit('configure')">管理模板</button></div>
    <label class="upload-box"><span class="upload-symbol">↑</span><strong>{{ excelFiles.length ? '继续添加 Excel 文件' : '选择一个或多个 Excel 文件' }}</strong><span>支持多选 .xlsx，每个文件可含多个工作表 · 最多 30 个文件，单个 100 MiB，合计 500 MiB</span><span>多个文件合并为一个任务，统一维护和导出</span><input type="file" accept=".xlsx" multiple :disabled="busy" aria-label="选择原始Excel" @change="selectFiles" /></label>
    <div v-if="excelFiles.length" class="selected-files"><p class="subtle" role="status">已选择 {{ excelFiles.length }} 个文件 · 合计 {{ (uploadBytes / 1024 / 1024).toFixed(2) }} MiB</p><ul><li v-for="(file, index) in excelFiles" :key="index"><span>{{ file.name }}</span><button class="btn text danger" :disabled="busy" :aria-label="`移除第${index + 1}个文件 ${file.name}`" @click="excelFiles.splice(index, 1)">移除</button></li></ul></div>
    <div v-if="uploadError" class="notice error" role="alert">{{ uploadError }}</div>
    <div class="form-footer"><button class="btn primary" :disabled="busy || !excelFiles.length || !!uploadError || !profile?.payload.published" @click="run(create)"><Upload :size="16" />{{ busy ? '正在导入…' : '上传并分析' }}</button></div>
  </section>
  <section class="card task-list"><div class="section-heading"><h2>处理记录 <small>{{ total }}</small></h2><div class="toolbar"><button class="btn danger-button" :disabled="busy || !canEdit || !selectedTaskIds.length" @click="run(() => removeTasks(selectedTaskIds))">删除选中{{ selectedTaskIds.length ? `（${selectedTaskIds.length}）` : '' }}</button><button class="btn text" :disabled="busy" @click="run(load)">刷新</button></div></div>
    <form class="toolbar" @submit.prevent="offset = 0; run(load)"><input v-model="search" class="search" placeholder="搜索任务或原始文件" aria-label="搜索任务" /><select v-model="status" aria-label="任务状态" @change="offset = 0; run(load)"><option value="">全部状态</option><option value="mapping">待确认字段</option><option value="maintenance">待维护</option><option value="ready">可生成</option><option value="completed">已完成</option><option value="failed">处理失败</option></select><button class="btn" :disabled="busy">筛选</button></form>
    <div v-if="!items.length" class="empty-state"><div class="empty-mark">▤</div><h3>{{ search || status ? '没有符合条件的任务' : '从第一份 Excel 开始' }}</h3><p>{{ search || status ? '调整筛选条件，或新建一批数据。' : '导入原始表，系统会按流程带你完成维护和交付。' }}</p><button v-if="!search && !status && canEdit" class="btn primary" @click="showNew = true">新建任务</button></div>
    <div v-else class="table-scroll"><table class="data-table"><thead><tr><th class="select-cell"></th><th>任务 / 原始文件</th><th v-if="showCreator">创建账号</th><th>业务流程</th><th>进度</th><th title="最近一次 AI 分析耗时，不含人工维护和导出时间">AI 处理耗时</th><th>数据量</th><th>更新时间</th><th></th></tr></thead><tbody><tr v-for="item in items" :key="item.id"><td class="select-cell"><input v-model="selectedTaskIds" :value="item.id" type="checkbox" :aria-label="`选择${item.name}`" /></td><td><button class="title-link" @click="emit('open', item.id)">{{ item.name }}</button><span class="cell-sub">{{ item.filename }}</span></td><td v-if="showCreator" class="task-creator">{{ item.created_by_username || item.created_by_id || '未知账号' }}</td><td>{{ item.template_name }}</td><td><span class="badge" :class="item.status">{{ executionLabel(item) }}</span><span v-if="item.error_count" class="cell-sub danger">{{ item.error_count }} 个问题待处理</span></td><td class="task-ai-time" title="最近一次 AI 分析耗时，不含人工维护和导出时间"><span :class="{'is-running': timingById[item.id]?.running}"><Clock3 v-if="timingById[item.id]?.running" :size="14" />{{ timingById[item.id]?.text }}</span><small v-if="timingById[item.id]?.detail" class="cell-sub">{{ timingById[item.id]?.detail }}</small></td><td>{{ item.record_count.toLocaleString() }} 条</td><td class="subtle">{{ dateLabel(item.updated_at) }}</td><td><div class="toolbar"><button class="btn text danger" :disabled="busy || !canEdit" @click="run(() => removeTasks([item.id]))">删除</button><button class="btn text" @click="emit('open', item.id)">{{ item.status === 'completed' ? '查看结果' : '继续处理' }} →</button></div></td></tr></tbody></table></div>
    <div v-if="total > 30" class="pagination"><span>共 {{ total }} 个任务</span><button class="btn" :disabled="offset === 0" @click="offset -= 30; run(load)">上一页</button><button class="btn" :disabled="offset + 30 >= total" @click="offset += 30; run(load)">下一页</button></div>
  </section>
</template>

<style scoped>
.new-task .form-footer { justify-content: flex-end; }
.selected-files ul { list-style: none; margin: 0; padding: 0; }
.selected-files li { display: flex; align-items: center; gap: 12px; justify-content: space-between; }
.selected-files li > span { min-width: 0; overflow-wrap: anywhere; }
.selected-files button { flex-shrink: 0; }
.task-ai-time { min-width: 112px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.task-ai-time > span { display: inline-flex; align-items: center; gap: 6px; }
.task-ai-time .is-running { color: var(--primary, #216b54); font-weight: 600; }
</style>
