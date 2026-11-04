<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { FileSpreadsheet, X } from 'lucide-vue-next';
import type { OutputColumn, OutputFile } from './api';

const props = defineProps<{
  result?: { fields: number; outputs: OutputFile[] };
  configurationName: string;
  unsaved: boolean;
  loading: boolean;
  error: string;
}>();
const emit = defineEmits<{ close: []; retry: [] }>();
const dialog = ref<HTMLDialogElement>();
const selectedSheet = ref(0);
const sheets = computed(() => props.result?.outputs.flatMap(file => file.sheets.map(sheet => ({ file: file.name, sheet }))) || []);
const current = computed(() => sheets.value[selectedSheet.value]);
const counts = computed(() => {
  const columns = current.value?.sheet.columns || [];
  return {
    fixed: columns.filter(c => c.title.startsWith('固定字段/')).length,
    attributes: columns.filter(c => c.title.startsWith('动态字段/商品属性/')).length,
    specifications: columns.filter(c => c.title.startsWith('动态字段/商品规格/')).length,
  };
});
interface HeaderCell { title: string; column: number; colspan: number; rowspan: number; required: boolean; zone: string }
const headerRows = computed(() => {
  const sheet = current.value?.sheet;
  if (!sheet) return [];
  const paths = sheet.columns.map(column => column.title.split('/').map(part => part.trim()).filter(Boolean));
  const depth = Math.max(sheet.data_start_row - sheet.header_row, ...paths.map(path => path.length), 1);
  const rows: HeaderCell[][] = Array.from({ length: depth }, () => []);
  // Mirror the export writer: merge adjacent equal prefixes; shorter leaves span the remaining rows.
  for (let level = 0; level < depth; level++) {
    for (let column = 0; column < paths.length;) {
      const path = paths[column]!;
      if (level >= path.length) { column++; continue; }
      const prefix = JSON.stringify(path.slice(0, level + 1));
      let end = column + 1;
      while (end < paths.length && paths[end]!.length > level && JSON.stringify(paths[end]!.slice(0, level + 1)) === prefix) end++;
      const leaf = level === path.length - 1;
      rows[level]!.push({ title: path[level]!, column, colspan: end - column, rowspan: leaf ? depth - level : 1,
        required: leaf && sheet.columns[column]!.required,
        zone: path[0] === '动态字段' ? (level === 0 ? 'dynamic' : path[1] === '商品规格' ? 'specification' : 'attribute') : 'fixed' });
      column = end;
    }
  }
  return rows;
});
function columnLetter(index: number): string {
  let label = '';
  for (let value = index + 1; value > 0; value = Math.floor((value - 1) / 26)) label = String.fromCharCode(65 + (value - 1) % 26) + label;
  return label;
}
function sheetLabel(index: number): string {
  const item = sheets.value[index];
  if (!item) return '';
  return item.sheet.name === '固定字段' ? '固定字段（未指定类目）' : item.sheet.name;
}
function columnTitle(column: OutputColumn): string { return `${column.title}${column.required ? ' · 必填' : ''}`; }
function keepFocus(event: KeyboardEvent) {
  if (event.key !== 'Tab' || !dialog.value) return;
  const controls = Array.from(dialog.value.querySelectorAll<HTMLElement>('button:not(:disabled),select:not(:disabled),[tabindex="0"]')).filter(node => node.getClientRects().length);
  const first = controls[0], last = controls.at(-1);
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
}
watch(() => props.result, () => { selectedSheet.value = sheets.value.length > 1 ? 1 : 0; });
let previousOverflow = '';
onMounted(() => {
  previousOverflow = document.body.style.overflow;
  document.body.style.overflow = 'hidden';
  dialog.value?.showModal();
});
onBeforeUnmount(() => {
  dialog.value?.close();
  document.body.style.overflow = previousOverflow;
});
</script>

<template>
  <dialog ref="dialog" class="field-structure-dialog" aria-label="整体结构预览" aria-describedby="schema-preview-description" @cancel.prevent="emit('close')" @keydown="keepFocus">
    <header class="schema-dialog-heading">
      <div><p class="schema-eyebrow">{{ configurationName }} · {{ unsaved ? '当前编辑配置（含未保存修改）' : '当前编辑配置' }}</p><h2>输出结构预览</h2></div>
      <button class="btn icon-only" autofocus aria-label="关闭结构预览" @click="emit('close')"><X :size="20" /></button>
    </header>
    <div class="schema-dialog-body">
      <p v-if="loading" class="schema-state" role="status">正在生成结构预览…</p>
      <div v-else-if="error" class="notice error schema-state" role="alert"><p>{{ error }}</p><button class="btn" @click="emit('retry')">重新预览</button></div>
      <template v-else-if="result && current">
        <div class="schema-workbook-bar">
          <span class="schema-workbook-name"><FileSpreadsheet :size="20" />{{ current.file }}</span>
          <label v-if="sheets.length > 1">预览工作表<select v-model.number="selectedSheet" aria-label="预览工作表"><option v-for="(item,index) in sheets" :key="item.file + item.sheet.name" :value="index">{{ sheetLabel(index) }}</option></select></label><span v-else class="badge">完整字段结构</span>
        </div>
        <div class="schema-sheet-summary"><strong>{{ current.sheet.name }}</strong><span>{{ current.sheet.columns.length }} 个字段 · 固定 {{ counts.fixed }} · 属性 {{ counts.attributes }} · 规格 {{ counts.specifications }}</span></div>
        <div :key="current.file + current.sheet.name" class="schema-grid-scroll" tabindex="0" role="region" aria-label="输出表头横向预览">
          <table class="schema-grid" :aria-label="`${current.sheet.name}输出表头`">
            <colgroup><col class="schema-row-index" /><col v-for="column in current.sheet.columns" :key="column.field" /></colgroup>
            <thead>
              <tr class="schema-column-letters"><th aria-label="Excel行号"></th><th v-for="(column,index) in current.sheet.columns" :key="column.field" scope="col" :title="columnTitle(column)">{{ columnLetter(index) }}</th></tr>
              <tr v-for="(row,index) in headerRows" :key="index"><th scope="row" class="schema-row-index">{{ current.sheet.header_row + index }}</th><th v-for="cell in row" :key="cell.column" :colspan="cell.colspan" :rowspan="cell.rowspan" :scope="cell.colspan > 1 ? 'colgroup' : 'col'" :class="`schema-header-${cell.zone}`"><span :class="{'schema-group-label':cell.colspan > 1}">{{ cell.title }}<span v-if="cell.required" class="danger" aria-label="必填">*</span></span></th></tr>
            </thead>
            <tbody><tr v-for="row in 2" :key="row"><th scope="row" class="schema-row-index">{{ current.sheet.data_start_row + row - 1 }}</th><td v-for="column in current.sheet.columns" :key="column.field" :title="columnTitle(column)">—</td></tr></tbody>
          </table>
        </div>
        <p v-if="!counts.attributes && !counts.specifications" class="schema-local-note">尚未配置动态字段，本次只展示固定字段。</p>
        <p v-else class="schema-local-note">固定字段与全部动态字段共同组成目标结构，所有来源工作表均使用此结构。动态字段为本地模拟接口数据，平台接口尚未接入。</p>
      </template>
    </div>
    <footer class="schema-dialog-footer"><p id="schema-preview-description">仅预览表头结构，不包含商品数据；预览不会保存或发布配置。<span class="danger">*</span> 为必填字段。</p><button class="btn" @click="emit('close')">返回配置</button></footer>
  </dialog>
</template>
