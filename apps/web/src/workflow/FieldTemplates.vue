<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { Copy, Plus } from 'lucide-vue-next';
import { api, useAction, type FieldProfileRecord } from './api';
defineProps<{canManage:boolean}>();
const emit=defineEmits<{open:[id:string]}>();
const {busy,error,run}=useAction();
const items=ref<FieldProfileRecord[]>([]), creating=ref(false), name=ref(''), source=ref<FieldProfileRecord>();
async function load() {items.value=(await api<{items:FieldProfileRecord[]}>('/field-profiles')).items;}
function start(item?:FieldProfileRecord) {source.value=item;name.value=item ? `${item.payload.template_name.slice(0,75)} 副本` : '';creating.value=true;}
async function create() {
  const value=await api<FieldProfileRecord>('/field-profiles','POST',{name:name.value.trim(),source_id:source.value?.id || null});
  emit('open',value.id);
}
function counts(item:FieldProfileRecord) {
  const profile=item.payload.draft;
  return `固定 ${profile.fixed_fields.filter(f=>f.enabled).length} · 动态 ${profile.local_categories.flatMap(c=>[...c.attributes,...c.specifications]).filter(f=>f.enabled).length}`;
}
onMounted(()=>{void run(load);});
</script>
<template>
  <div class="page-heading"><div><h1>模板管理</h1><p class="subtle">按业务配置固定字段与动态字段，新任务选择模板，历史任务保持原快照。</p></div><button v-if="canManage" class="btn primary" :disabled="busy" @click="start()"><Plus :size="17" />新建模板</button></div>
  <div v-if="error" class="notice error" role="alert">{{error}}</div>
  <form v-if="creating && canManage" class="config-section" @submit.prevent="run(create)">
    <div class="section-heading"><h2>{{source ? '复制模板' : '新建模板'}}</h2><button type="button" class="btn text" :disabled="busy" @click="creating=false">取消</button></div>
    <p class="subtle">{{source ? `复制“${source.payload.template_name}”当前已保存的草稿，原模板保持不变。` : '从商品名称字段开始，按业务添加固定字段、商品属性和商品规格。' }} 新模板发布后才能创建任务。</p>
    <label>模板名称<input v-model="name" required maxlength="80" :disabled="busy" placeholder="例如：灯具商品模板" /></label>
    <div class="form-footer"><button class="btn primary" :disabled="busy || !name.trim()">创建并配置字段</button></div>
  </form>
  <section class="card"><div class="section-heading"><h2>业务模板 <small>{{items.length}}</small></h2><button class="btn text" :disabled="busy" @click="run(load)">刷新</button></div>
    <div class="table-scroll"><table class="data-table"><thead><tr><th>模板名称</th><th>草稿字段</th><th>发布版本</th><th>操作</th></tr></thead><tbody>
      <tr v-for="item in items" :key="item.id"><td><button class="title-link" @click="emit('open',item.id)">{{item.payload.template_name}}</button><span v-if="item.is_default" class="cell-sub">默认模板 · 原有字段配置</span></td><td>{{counts(item)}}</td><td><span class="badge">{{item.payload.published ? `V${item.payload.version}` : '未发布'}}</span><span v-if="item.status==='draft'" class="cell-sub">{{item.payload.published ? '有未发布草稿，新任务使用已发布版' : '发布后可用于新任务'}}</span></td><td><div class="toolbar"><button class="btn text" @click="emit('open',item.id)">{{canManage ? '编辑字段' : '查看字段'}}</button><button v-if="canManage" class="btn" :disabled="busy" @click="start(item)"><Copy :size="15" />复制</button></div></td></tr>
    </tbody></table></div>
  </section>
</template>
