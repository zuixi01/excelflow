<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { Check, Eye, Link2, Plus, Save, Trash2 } from 'lucide-vue-next';
import FieldEditor from './FieldEditor.vue';
import FieldStructurePreview from './FieldStructurePreview.vue';
import { api, cloneData, useAction, type FieldProfile, type FieldProfileRecord, type LocalCategory, type OutputFile } from './api';
const props=defineProps<{canManage:boolean;profileId?:string}>();
const endpoint=computed(()=>props.profileId ? `/field-profiles/${props.profileId}` : '/field-profile');
const emit=defineEmits<{dirty:[value:boolean]}>();
const {busy,error,message,run}=useAction();
const record=ref<FieldProfileRecord>(), profile=ref<FieldProfile>(), baseline=ref('');
const categoryIndex=ref(0), preview=ref<{fields:number;outputs:OutputFile[]}>();
const previewOpen=ref(false), previewError=ref(''), previewName=ref(''), previewUnsaved=ref(false);
const previewButton=ref<HTMLButtonElement>(), restorePreviewFocus=ref(false);
const activeCategory=computed(()=>profile.value?.local_categories[categoryIndex.value]);
type PlatformPreview={connection_id:string;title:string;schema_version:string;fetched_at:string;categories:LocalCategory[];category_count:number;field_count:number};
const platformOpen=ref(false),platformConnections=ref<Array<{id:string;title:string}>>([]),platformConfigured=ref(false);
const platformConnectionId=ref(''),platformPreview=ref<PlatformPreview>(),platformError=ref('');
type DirectPlatform={base_url:string;path:string;method:'GET'|'POST';has_key:boolean;configured:boolean;http_allowed:boolean;private_network_allowed:boolean};
const direct=ref<DirectPlatform>({base_url:'',path:'/fields',method:'GET',has_key:false,configured:false,http_allowed:false,private_network_allowed:false});
const endpointUrl=ref('');
const directKey=ref(''),clearDirectKey=ref(false);
const savedDirect=ref('');
const directReady=computed(()=>direct.value.configured && savedDirect.value===JSON.stringify([endpointUrl.value.trim(),direct.value.method]) && !directKey.value && !clearDirectKey.value);
function fullEndpoint(config:DirectPlatform){return config.base_url ? config.base_url.replace(/\/+$/,'')+(config.path||'/') : '';}
const dirty=computed(()=>!!profile.value && JSON.stringify(profile.value)!==baseline.value);
watch(dirty,v=>emit('dirty',v));
function focusPreviewButton() {if(restorePreviewFocus.value && !previewOpen.value && !busy.value) {previewButton.value?.focus();restorePreviewFocus.value=false;}}
watch(busy,()=>{void nextTick(focusPreviewButton);});
function closePreview() {previewOpen.value=false;restorePreviewFocus.value=true;void nextTick(focusPreviewButton);}
function accept(value:FieldProfileRecord) {record.value=value;profile.value=cloneData(value.payload.draft);profile.value.name=value.payload.template_name;baseline.value=JSON.stringify(profile.value);preview.value=undefined;}
async function load() {
  accept(await api<FieldProfileRecord>(endpoint.value));
  if(props.canManage){
    try{
      direct.value=await api<DirectPlatform>('/field-profile/platform-direct');
      endpointUrl.value=fullEndpoint(direct.value);
      savedDirect.value=JSON.stringify([endpointUrl.value,direct.value.method]);
      const connections=await api<{items:Array<{id:string;title:string}>;configured:boolean}>('/field-profile/platform-connections');
      platformConnections.value=connections.items;platformConfigured.value=connections.configured;
      platformConnectionId.value=connections.items[0]?.id||'';
    }catch(cause){platformError.value=cause instanceof Error?cause.message:'平台连接列表读取失败。';}
  }
}
async function saveDirect(){
  platformError.value='';platformPreview.value=undefined;
  let url:URL;
  try{url=new URL(endpointUrl.value.trim());}catch{throw new Error('请输入完整的字段接口地址，例如 https://api.example.com/fields。');}
  if(!['https:','http:'].includes(url.protocol)||!url.hostname||url.username||url.password||url.search||url.hash){
    throw new Error('字段接口地址须为完整的 HTTP/HTTPS URL，不能包含账号、密码、查询参数或 # 片段。');
  }
  direct.value=await api<DirectPlatform>('/field-profile/platform-direct','PUT',{
    base_url:url.origin,path:url.pathname,method:direct.value.method,
    api_key:directKey.value,clear_key:clearDirectKey.value,
  });
  directKey.value='';clearDirectKey.value=false;
  endpointUrl.value=fullEndpoint(direct.value);
  savedDirect.value=JSON.stringify([endpointUrl.value,direct.value.method]);
  message.value='平台连接已保存在服务器。现在可点击“连接并预览字段”。';
}
async function readDirect(){
  platformError.value='';platformPreview.value=undefined;
  platformPreview.value=await api<PlatformPreview>('/field-profile/platform-direct/preview','POST');
}
async function readPlatform(){
  if(!platformConnectionId.value)throw new Error('请选择平台字段连接。');
  platformError.value='';platformPreview.value=undefined;
  platformPreview.value=await api<PlatformPreview>('/field-profile/platform-preview','POST',{connection_id:platformConnectionId.value});
}
async function importPlatform(){
  if(!profile.value||!platformPreview.value)return;
  const next=cloneData(profile.value),incoming=platformPreview.value;
  const imported=new Map(incoming.categories.map(category=>[category.id,category]));
  next.local_categories=next.local_categories.map(category=>imported.get(category.id)||category);
  for(const category of incoming.categories)if(!profile.value.local_categories.some(old=>old.id===category.id))next.local_categories.push(category);
  if(next.local_categories.length>19)throw new Error('导入后字段组超过 19 个，请先移除不需要的字段组。');
  if(new Set(next.local_categories.map(category=>category.title)).size!==next.local_categories.length)throw new Error('平台字段组名称与现有字段组重复，请先调整名称。');
  next.platform_source={connection_id:incoming.connection_id,schema_version:incoming.schema_version,fetched_at:incoming.fetched_at};
  profile.value=next;categoryIndex.value=next.local_categories.findIndex(category=>category.id===incoming.categories[0]?.id);
  platformOpen.value=false;platformPreview.value=undefined;
  message.value=`已将 ${incoming.category_count} 个字段组、${incoming.field_count} 个字段导入草稿。请检查并保存、发布模板。`;
}
async function openPreview() {
  if (!profile.value) return;
  restorePreviewFocus.value=false;
  previewOpen.value=true;preview.value=undefined;previewError.value='';
  previewName.value=profile.value.name;previewUnsaved.value=dirty.value;
  try {preview.value=await api('/field-profile/preview','POST',cloneData(profile.value));}
  catch(cause) {previewError.value=cause instanceof Error?cause.message:'结构预览失败，请重试。';}
}
async function save() {if(!profile.value || !record.value)return;accept(await api<FieldProfileRecord>(endpoint.value,'PUT',{base_revision:record.value.revision,profile:profile.value}));message.value='模板草稿已保存，发布后供新任务使用。';}
async function publish() {if(!record.value)return;if(dirty.value)await save();accept(await api<FieldProfileRecord>(endpoint.value+'/publish','POST',{base_revision:record.value!.revision}));message.value=`模板 V${record.value!.payload.version} 已发布，历史任务保持原快照。`;}
function addCategory() {if(!profile.value)return;profile.value.local_categories.push({id:`category_${crypto.randomUUID().replaceAll('-','').slice(0,10)}`,title:'新字段组',attributes:[],specifications:[]});categoryIndex.value=profile.value.local_categories.length-1;}
function removeCategory() {if(!profile.value || !window.confirm('移除此本地字段组？已创建任务不受影响。'))return;profile.value.local_categories.splice(categoryIndex.value,1);categoryIndex.value=0;}
function unload(e:BeforeUnloadEvent) {if(dirty.value){e.preventDefault();e.returnValue='';}}
onMounted(()=>{void run(load);window.addEventListener('beforeunload',unload);});
onUnmounted(()=>{emit('dirty',false);window.removeEventListener('beforeunload',unload);});
</script>

<template>
  <div class="page-heading"><div><h1>{{record?.payload.template_name || '模板字段配置'}}</h1><p v-if="record" class="subtle">{{record.payload.version ? `已发布 V${record.payload.version}`:'尚未发布'}} · {{dirty?'有未保存修改':record.status==='draft'?'草稿':'已发布'}}</p></div><div class="toolbar"><button ref="previewButton" class="btn" :disabled="busy || !profile || !canManage" @click="run(openPreview)"><Eye :size="16" />预览结构</button><button class="btn" :disabled="busy || !dirty || !canManage" @click="run(save)"><Save :size="16" />保存草稿</button><button class="btn primary" :disabled="busy || !profile || !canManage" @click="run(publish)"><Check :size="16" />发布模板</button></div></div>
  <p class="notice info">维护表沿用分层表头，固定字段独立成列，最终表仅将动态字段汇总为“字段：值”。模板发布只影响之后创建的任务。</p>
  <div v-if="error" class="notice error" role="alert">{{error}}</div><div v-if="message" class="notice success" role="status">{{message}}</div>
  <div v-if="!canManage" class="notice info">当前账户为只读权限。</div>
  <template v-if="profile">
    <section class="config-section"><div class="section-heading"><h2>固定字段 <small>{{profile.fixed_fields.filter(f=>f.enabled).length}}</small></h2><label class="profile-name">模板名称<input v-model="profile.name" :disabled="!canManage || busy" maxlength="80" /></label></div><FieldEditor :fields="profile.fixed_fields" :disabled="!canManage || busy" fixed /></section>
    <section class="config-section"><div class="section-heading"><div><h2>动态字段</h2><p class="subtle">可从平台读取商品属性和商品规格，也可手工维护。启用的字段参与联合匹配，分组不限制来源工作表。</p><p v-if="profile.platform_source" class="subtle">上次导入：{{profile.platform_source.connection_id}} · {{profile.platform_source.schema_version||'未提供版本'}}</p></div><div class="toolbar"><button v-if="canManage" class="btn" :aria-expanded="platformOpen" aria-controls="platform-field-panel" @click="platformOpen=!platformOpen"><Link2 :size="16" />对接平台字段</button><button class="btn" :disabled="!canManage || busy || profile.local_categories.length>=19" @click="addCategory"><Plus :size="16" />添加字段组</button></div></div>
      <div v-if="platformOpen" id="platform-field-panel" class="card platform-field-panel" role="region" aria-label="对接平台字段">
        <h3>从平台读取动态字段</h3>
        <p class="subtle">只需填写一次返回全部商品属性和商品规格的接口完整地址；每个字段无需配置路径。保存后连接预览，确认无误再导入模板草稿。</p>
        <div class="platform-direct-form">
          <label>字段接口完整地址<input v-model.trim="endpointUrl" type="url" autocomplete="url" placeholder="https://api.example.com/fields" :disabled="busy" @input="platformPreview=undefined" /></label>
          <label>请求方式<select v-model="direct.method" :disabled="busy" @change="platformPreview=undefined"><option value="GET">GET</option><option value="POST">POST</option></select></label>
          <label>API 密钥<input v-model="directKey" type="password" autocomplete="off" :placeholder="direct.has_key?'已保存密钥；留空保持原密钥':'如接口无需鉴权，可留空'" :disabled="busy" /></label>
        </div>
        <label v-if="direct.has_key" class="platform-clear-key"><input v-model="clearDirectKey" type="checkbox" :disabled="busy" />清除已保存密钥</label>
        <p class="subtle">默认只允许公网 HTTPS 地址。{{direct.http_allowed?'本开发环境已允许 HTTP。':''}}{{direct.private_network_allowed?'服务器已允许内网地址。':''}}</p>
        <div class="toolbar"><button class="btn" :disabled="busy || !endpointUrl" @click="run(saveDirect)">保存连接</button><button class="btn" :disabled="busy || !directReady" @click="run(readDirect)">连接并预览字段</button><span v-if="direct.configured" class="subtle">{{directReady?'已保存连接':'请先保存当前连接设置'}}{{directReady && direct.has_key?'和密钥':''}}</span></div>
        <details v-if="platformConnections.length" class="platform-managed"><summary>使用服务器已有连接</summary><div class="toolbar"><label>平台连接<select v-model="platformConnectionId" aria-label="选择平台字段连接" :disabled="busy" @change="platformPreview=undefined"><option v-for="connection in platformConnections" :key="connection.id" :value="connection.id">{{connection.title}}（{{connection.id}}）</option></select></label><button class="btn" :disabled="busy || !platformConnectionId" @click="run(readPlatform)">读取该连接字段</button></div></details>
        <p v-if="platformError" class="notice error" role="alert">{{platformError}}</p>
        <div v-if="platformPreview" class="platform-field-preview"><p><strong>{{platformPreview.title}}</strong> · {{platformPreview.category_count}} 个字段组 · {{platformPreview.field_count}} 个字段 · 版本 {{platformPreview.schema_version||'未提供'}}</p><p class="subtle">同 ID 字段组将更新为平台内容，其余本地字段组保留。导入后仍可编辑。</p><ul><li v-for="category in platformPreview.categories" :key="category.id"><strong>{{category.title}}</strong>：商品属性 {{category.attributes.length}} 个，商品规格 {{category.specifications.length}} 个<details><summary>查看字段名称</summary><p>商品属性：{{category.attributes.map(field=>field.title).join('、')||'无'}}</p><p>商品规格：{{category.specifications.map(field=>field.title).join('、')||'无'}}</p></details></li></ul><button class="btn primary" :disabled="busy" @click="run(importPlatform)">导入到模板草稿</button></div>
      </div>
      <div v-if="!profile.local_categories.length" class="config-empty">暂无动态字段定义。当前任务仅使用固定字段。</div>
      <template v-else-if="activeCategory"><div class="toolbar category-bar"><label>编辑字段组<select v-model.number="categoryIndex"><option v-for="(category,index) in profile.local_categories" :key="category.id" :value="index">{{category.title}}</option></select></label><label>字段组名称<input v-model="activeCategory.title" :disabled="!canManage || busy" maxlength="80" /></label><span class="badge">可编辑字段</span><button class="btn icon-only" title="移除字段组" aria-label="移除字段组" :disabled="!canManage || busy" @click="removeCategory"><Trash2 :size="16" /></button></div>
        <h3>商品属性</h3><FieldEditor :fields="activeCategory.attributes" :disabled="!canManage || busy" />
        <h3>商品规格</h3><FieldEditor :fields="activeCategory.specifications" :disabled="!canManage || busy" />
      </template>
    </section>
    <FieldStructurePreview v-if="previewOpen" :result="preview" :configuration-name="previewName" :unsaved="previewUnsaved" :loading="busy" :error="previewError" @close="closePreview" @retry="run(openPreview)" />
  </template>
</template>
