<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { api, dateLabel, labels, useAction, type TaskSummary, type Template, type Task } from "./api";
const emit = defineEmits<{open: [id: string]}>();
const {busy, error, message, run} = useAction();
const items = ref<TaskSummary[]>([]), templates = ref<Template[]>([]), total = ref(0), offset = ref(0);
const showNew = ref(false), name = ref(""), templateId = ref(""), excel = ref<File>(), search = ref(""), status = ref("");
const selectedTaskIds = ref<string[]>([]);
let timer: number | undefined;
async function load() {
  const data = await api<{items: TaskSummary[]; total: number}>(`/workflow-tasks?search=${encodeURIComponent(search.value)}&status=${status.value}&offset=${offset.value}`);
  items.value = data.items; total.value = data.total;
}
async function create() {
  if (!excel.value || !templateId.value) throw new Error("请选择原始 Excel 和业务流程。");
  const form = new FormData(); form.append("excel_file", excel.value); form.append("template_id", templateId.value); form.append("name", name.value);
  const task = await api<Task>("/workflow-tasks", "POST", form); emit("open", task.id);
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
  const data = await api<{items: Template[]}>("/workflow-definitions"); templates.value = data.items; templateId.value = data.items[0]?.id || ""; await load();
}); timer = window.setInterval(() => { if (items.value.some(item => item.status === "processing") && !busy.value) void load().catch(() => {}); }, 2000); });
onUnmounted(() => window.clearInterval(timer));
</script>

<template>
  <div class="page-heading"><div><p class="kicker">每批数据，都有清晰的下一步</p><h1>工作台</h1><p class="subtle">从原始表到最终交付，在一个流程中完成。</p></div><button class="btn primary" @click="showNew = !showNew">＋ 新建任务</button></div>
  <div v-if="error" class="notice error" role="alert">{{ error }}</div><div v-if="message" class="notice success" role="status">{{ message }}</div>
  <section v-if="showNew" class="card new-task">
    <div class="section-heading"><h2>导入一批新数据</h2><button class="btn text" @click="showNew = false">收起</button></div>
    <div class="form-grid"><label>任务名称<input v-model="name" placeholder="例如：九月灯具商品整理" maxlength="100" /></label><label>选择已发布模板<select v-model="templateId" :disabled="!templates.length"><option v-if="!templates.length" value="">暂无可用模板</option><option v-for="item in templates" :key="item.id" :value="item.id">{{ item.payload.config.name }} · V{{ item.payload.version }}</option></select><small>模板发布后会立即显示在这里。</small></label></div>
    <label class="upload-box"><span class="upload-symbol">↑</span><strong>{{ excel?.name || '选择原始 Excel 文件' }}</strong><span>支持多工作表的 .xlsx · 最大 20 MiB</span><input type="file" accept=".xlsx" aria-label="选择原始Excel" @change="excel = ($event.target as HTMLInputElement).files?.[0]" /></label>
    <div class="form-footer"><span class="subtle">原文件会保留，生成结果另存为新文件。</span><button class="btn primary" :disabled="busy || !excel" @click="run(create)">{{ busy ? '正在导入…' : '开始导入 →' }}</button></div>
  </section>
  <section class="flow-overview"><div><span>01</span><strong>导入原表</strong><p>识别数据与字段</p></div><i>→</i><div><span>02</span><strong>标准维护</strong><p>补充、修正与校验</p></div><i>→</i><div><span>03</span><strong>生成最终表</strong><p>按模板输出文件包</p></div></section>
  <section class="card task-list"><div class="section-heading"><h2>处理记录 <small>{{ total }}</small></h2><div class="toolbar"><button class="btn danger-button" :disabled="busy || !selectedTaskIds.length" @click="run(() => removeTasks(selectedTaskIds))">删除选中{{ selectedTaskIds.length ? `（${selectedTaskIds.length}）` : '' }}</button><button class="btn text" :disabled="busy" @click="run(load)">刷新</button></div></div>
    <form class="toolbar" @submit.prevent="offset = 0; run(load)"><input v-model="search" class="search" placeholder="搜索任务或原始文件" aria-label="搜索任务" /><select v-model="status" aria-label="任务状态" @change="offset = 0; run(load)"><option value="">全部状态</option><option value="mapping">待确认字段</option><option value="maintenance">待维护</option><option value="ready">可生成</option><option value="completed">已完成</option><option value="failed">处理失败</option></select><button class="btn" :disabled="busy">筛选</button></form>
    <div v-if="!items.length" class="empty-state"><div class="empty-mark">▤</div><h3>{{ search || status ? '没有符合条件的任务' : '从第一份 Excel 开始' }}</h3><p>{{ search || status ? '调整筛选条件，或新建一批数据。' : '导入原始表，系统会按流程带你完成维护和交付。' }}</p><button v-if="!search && !status" class="btn primary" @click="showNew = true">新建任务</button></div>
    <div v-else class="table-scroll"><table class="data-table"><thead><tr><th class="select-cell"></th><th>任务 / 原始文件</th><th>业务流程</th><th>进度</th><th>数据量</th><th>更新时间</th><th></th></tr></thead><tbody><tr v-for="item in items" :key="item.id"><td class="select-cell"><input v-model="selectedTaskIds" :value="item.id" type="checkbox" :aria-label="`选择${item.name}`" /></td><td><button class="title-link" @click="emit('open', item.id)">{{ item.name }}</button><span class="cell-sub">{{ item.filename }}</span></td><td>{{ item.template_name }}</td><td><span class="badge" :class="item.status">{{ labels[item.status] || item.status }}</span><span v-if="item.error_count" class="cell-sub danger">{{ item.error_count }} 个问题待处理</span></td><td>{{ item.record_count.toLocaleString() }} 条</td><td class="subtle">{{ dateLabel(item.updated_at) }}</td><td><div class="toolbar"><button class="btn text danger" :disabled="busy" @click="run(() => removeTasks([item.id]))">删除</button><button class="btn text" @click="emit('open', item.id)">{{ item.status === 'completed' ? '查看结果' : '继续处理' }} →</button></div></td></tr></tbody></table></div>
    <div v-if="total > 30" class="pagination"><span>共 {{ total }} 个任务</span><button class="btn" :disabled="offset === 0" @click="offset -= 30; run(load)">上一页</button><button class="btn" :disabled="offset + 30 >= total" @click="offset += 30; run(load)">下一页</button></div>
  </section>
</template>
