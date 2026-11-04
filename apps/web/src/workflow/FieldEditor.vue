<script setup lang="ts">
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-vue-next';
import type { ConfiguredField } from './api';
const props = defineProps<{ fields: ConfiguredField[]; disabled: boolean; fixed?: boolean }>();
function add() {
  props.fields.push({id: `field_${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`, title: '新字段', group: '', description: '',
    aliases: [], type: 'string', required: false, editable: true, enabled: true, default: null, enum_values: [], min: null, max: null});
}
function move(index: number, direction: number) {
  const [field] = props.fields.splice(index, 1); props.fields.splice(index + direction, 0, field);
}
</script>

<template>
  <div class="table-scroll field-editor"><table class="data-table"><thead><tr><th>启用</th><th>字段名称</th><th v-if="fixed">分组</th><th>类型</th><th>必填</th><th>允许编辑</th><th>规则与取值</th><th>顺序</th></tr></thead>
    <tbody><tr v-for="(field,index) in fields" :key="field.id" :class="{'field-disabled':!field.enabled}">
      <td><input v-model="field.enabled" type="checkbox" :disabled="disabled" :aria-label="`启用${field.title}`" /></td>
      <td><input v-model="field.title" :disabled="disabled" :aria-label="`字段名称 ${index+1}`" maxlength="100" /><small>{{field.id}}</small></td>
      <td v-if="fixed"><input v-model="field.group" :disabled="disabled" :aria-label="`${field.title}分组`" maxlength="80" /></td>
      <td><select v-model="field.type" :disabled="disabled" :aria-label="`${field.title}类型`"><option value="string">文本</option><option value="id_code">编码</option><option value="integer">整数</option><option value="decimal">数值</option><option value="boolean">是 / 否</option><option value="date">日期</option><option value="datetime">日期时间</option><option value="enum">选项</option></select></td>
      <td><input v-model="field.required" type="checkbox" :disabled="disabled" :aria-label="`${field.title}必填`" /></td>
      <td><input v-model="field.editable" type="checkbox" :disabled="disabled" :aria-label="`${field.title}允许编辑`" /></td>
      <td><details><summary>配置规则</summary><div class="field-rule-popover">
        <label>字段说明<textarea v-model="field.description" :disabled="disabled" maxlength="1000" /></label>
        <label>别名（每行一个）<textarea :value="field.aliases.join('\n')" :disabled="disabled" @change="field.aliases=($event.target as HTMLTextAreaElement).value.split('\n').map(s=>s.trim()).filter(Boolean)" /></label>
        <label>默认值<input :value="field.default ?? ''" :disabled="disabled" @input="field.default=($event.target as HTMLInputElement).value || null" /></label>
        <label v-if="field.type==='enum'">可选值（每行一个）<textarea :value="field.enum_values.join('\n')" :disabled="disabled" @change="field.enum_values=($event.target as HTMLTextAreaElement).value.split('\n').map(s=>s.trim()).filter(Boolean)" /></label>
        <div v-if="['integer','decimal'].includes(field.type)" class="form-grid"><label>最小值<input :value="field.min ?? ''" :disabled="disabled" @input="field.min=($event.target as HTMLInputElement).value || null" /></label><label>最大值<input :value="field.max ?? ''" :disabled="disabled" @input="field.max=($event.target as HTMLInputElement).value || null" /></label></div>
      </div></details></td>
      <td><div class="toolbar"><button class="btn icon-only" title="上移" aria-label="上移字段" :disabled="disabled || index===0" @click="move(index,-1)"><ArrowUp :size="15" /></button><button class="btn icon-only" title="下移" aria-label="下移字段" :disabled="disabled || index===fields.length-1" @click="move(index,1)"><ArrowDown :size="15" /></button><button v-if="!fixed" class="btn icon-only" title="移除本地字段" aria-label="移除本地字段" :disabled="disabled" @click="fields.splice(index,1)"><Trash2 :size="15" /></button></div></td>
    </tr><tr v-if="!fields.length"><td :colspan="fixed?8:7" class="empty-cell">暂无字段</td></tr></tbody></table></div>
  <div class="field-add"><button class="btn" :disabled="disabled" @click="add"><Plus :size="16" />添加字段</button></div>
</template>
