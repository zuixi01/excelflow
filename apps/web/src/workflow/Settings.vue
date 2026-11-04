<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { LoaderCircle } from 'lucide-vue-next';
import { api, useAction, type OperatorAccount } from './api';
const {busy,error,message,run} = useAction();
const emit=defineEmits<{dirty:[value:boolean]}>();
const baseline=ref(''), limits=ref({upload_mib:100,total_upload_mib:500,source_files:30}), canManageOperators=ref(false);
const settings = ref({base_url:'',model:'',api_key:'',clear_key:false,json_mode:true,include_samples:false});
const hasKey = ref(false), canManage = ref(false), models = ref<string[]>([]), storage = ref('local');
const httpAllowed = ref(false), activeAction = ref(''), endpointError = ref('');
const operators = ref<OperatorAccount[]>([]), currentRole = ref('viewer');
const newOperator = ref({username:'', password:'', role:'operator'});
const dirty=computed(()=>!!baseline.value && JSON.stringify(settings.value)!==baseline.value);
watch(dirty,value=>emit('dirty',value));
function unload(event:BeforeUnloadEvent) {if(dirty.value){event.preventDefault();event.returnValue='';}}
const isHttp = computed(() => /^http:\/\//i.test(settings.value.base_url.trim()));
async function load() {
  const data = await api<{ai: {base_url: string; model: string; has_key: boolean; json_mode: boolean; include_samples: boolean; http_allowed?: boolean}; can_manage_templates: boolean; storage: string; limits:typeof limits.value}>('/workflow-settings');
  settings.value = {...settings.value,base_url:data.ai.base_url,model:data.ai.model,json_mode:data.ai.json_mode,include_samples:data.ai.include_samples,api_key:''}; hasKey.value=data.ai.has_key;canManage.value=data.can_manage_templates;storage.value=data.storage;
  httpAllowed.value=data.ai.http_allowed === true;
  limits.value=data.limits || limits.value;baseline.value=JSON.stringify(settings.value);
  const session = await api<{role?: string;auth_method?:string}>('/auth/session'); currentRole.value = session.role || 'viewer';
  canManageOperators.value=['owner','admin'].includes(currentRole.value) && session.auth_method==='session';
  if (canManageOperators.value) operators.value = (await api<{items: OperatorAccount[]}>('/operators')).items;
}
function validateEndpoint() {
  endpointError.value='';
  try {
    const url=new URL(settings.value.base_url.trim());
    if (!['http:','https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error();
    if (url.protocol==='http:' && !httpAllowed.value) endpointError.value='当前环境未允许 HTTP 模型接口，请改用 HTTPS 或联系管理员开启开发兼容。';
  } catch { endpointError.value='请填写有效的 HTTP/HTTPS 接口根地址，不要包含账号、密码或查询参数。'; }
  return !endpointError.value;
}
async function persist() {
  if (!validateEndpoint()) { document.getElementById('model-endpoint')?.focus(); throw new Error(endpointError.value); }
  if (!settings.value.api_key.trim() && !hasKey.value && !settings.value.clear_key) throw new Error('请填写模型服务的 API 密钥。');
  const data=await api<{base_url:string;model:string;has_key:boolean}>('/workflow-settings/ai','PUT',{...settings.value,base_url:settings.value.base_url.trim()});
  settings.value.base_url=data.base_url; settings.value.model=data.model; hasKey.value=data.has_key;
  settings.value.api_key=''; settings.value.clear_key=false;
  baseline.value=JSON.stringify(settings.value);
}
async function save() { activeAction.value='save'; try { await persist(); message.value='模型配置已保存。'; } finally { activeAction.value=''; } }
async function connect() {
  activeAction.value='connect';
  try {
    await persist();
    try { models.value=(await api<{models:string[]}>('/workflow-settings/ai/models','POST')).models; }
    catch (cause) { throw new Error(`配置已保存，但读取模型失败：${cause instanceof Error?cause.message:'请检查模型服务。'}`); }
    message.value=models.value.length ? `连接成功，读取到 ${models.value.length} 个模型。` : '连接成功，但服务未返回模型列表；可填写模型名称后保存。';
  } finally { activeAction.value=''; }
}
async function createOperator() {
  const created = await api<OperatorAccount>('/operators', 'POST', newOperator.value);
  operators.value = [...operators.value, created];
  newOperator.value = {username:'', password:'', role:'operator'};
  message.value = `运营账号 ${created.username} 已创建。`;
}
async function toggleOperator(operator: OperatorAccount) {
  const action = operator.status === 'active' ? 'disable' : 'enable';
  const updated = await api<OperatorAccount>(`/operators/${operator.id}/${action}`, 'POST');
  const index = operators.value.findIndex(item => item.id === operator.id); if (index >= 0) operators.value[index] = updated;
}
onMounted(() => {void run(load);window.addEventListener('beforeunload',unload);});
onUnmounted(()=>{emit('dirty',false);window.removeEventListener('beforeunload',unload);});
</script>
<template>
  <div class="page-heading"><div><p class="kicker">连接与运行配置</p><h1>系统设置</h1></div></div>
  <section class="card settings-card"><div class="section-heading"><h2>语义分析模型</h2><span class="badge" :class="hasKey ? 'ready' : 'draft'">{{hasKey ? '已保存密钥' : '尚未配置密钥'}}</span></div><div v-if="!canManage" class="notice info">模型配置需要管理员权限。</div><div class="form-grid"><label>接口地址<input id="model-endpoint" v-model="settings.base_url" :disabled="busy || !canManage" :aria-invalid="!!endpointError" :aria-describedby="endpointError?'model-endpoint-error':undefined" placeholder="https://your-provider.example/v1" autocomplete="off" @input="endpointError=''" /><small v-if="endpointError" id="model-endpoint-error" class="danger">{{endpointError}}</small></label><label>API 密钥<input v-model="settings.api_key" :disabled="busy || !canManage" type="password" autocomplete="new-password" :placeholder="hasKey ? '留空保留原密钥' : '填写模型服务密钥'" /></label><label>模型名称<input v-model="settings.model" :disabled="busy || !canManage" list="available-models" /><datalist id="available-models"><option v-for="model in models" :key="model" :value="model" /></datalist></label><div class="setting-toggles"><label class="inline-check"><input v-model="settings.json_mode" type="checkbox" :disabled="busy || !canManage" />JSON 响应模式</label><label class="inline-check"><input v-model="settings.include_samples" type="checkbox" :disabled="busy || !canManage" />授权发送非敏感字段值进行语义比对</label><label v-if="hasKey" class="inline-check"><input v-model="settings.clear_key" type="checkbox" :disabled="busy || !canManage" />清除已保存密钥</label></div></div><div v-if="isHttp" class="notice info model-http-warning" role="status">{{httpAllowed?'开发环境已允许 HTTP 接口。HTTP 会明文传输密钥和请求内容，请仅用于可信网络调试。':'当前环境不允许 HTTP 接口，请使用 HTTPS 或联系管理员开启开发兼容。'}}</div><p class="subtle">字段名识别仅发送字段定义。值比对需开启授权；敏感字段不会发送。更换接口地址时需重新填写密钥。</p><div class="toolbar"><button class="btn" :disabled="busy || !canManage" @click="run(connect)"><LoaderCircle v-if="activeAction==='connect'" :size="16" class="loading-icon" />{{activeAction==='connect'?'正在连接…':'连接并读取模型'}}</button><button class="btn primary" :disabled="busy || !canManage" @click="run(save)">{{activeAction==='save'?'正在保存…':'保存模型配置'}}</button></div><div class="model-action-status"><div v-if="error" class="notice error" role="alert">{{error}}</div><div v-else-if="message" class="notice success" role="status">{{message}}</div></div></section>
  <section class="card settings-card"><h2>当前运行范围</h2><dl class="settings-facts"><div><dt>数据保存</dt><dd>{{storage === 'local' ? '本机持久化存储' : '已配置数据库'}}</dd></div><div><dt>商品标准化导入</dt><dd>.xlsx · 最多 {{limits.source_files}} 个文件，单个 {{limits.upload_mib}} MiB，合计 {{limits.total_upload_mib}} MiB</dd></div><div><dt>平台数据接口</dt><dd>由服务器连接配置管理；运营任务只提交商户范围和 Excel 文件</dd></div></dl></section>
  <section v-if="canManageOperators" class="card settings-card">
    <div class="section-heading"><h2>运营账号</h2><span class="badge ready">单工作空间</span></div>
    <div class="operator-create form-grid">
      <label>账号<input v-model="newOperator.username" autocomplete="off" placeholder="例如：运营小王" /></label>
      <label>初始密码<input v-model="newOperator.password" type="password" autocomplete="new-password" placeholder="至少 8 位" /></label>
      <label>角色<select v-model="newOperator.role" aria-label="角色" :disabled="busy"><option value="operator">运营人员</option><option value="viewer">只读</option></select></label>
    </div>
    <div class="toolbar"><button class="btn primary" :disabled="busy || !newOperator.username.trim() || newOperator.password.length < 8" @click="run(createOperator)">创建运营账号</button></div>
    <div class="operator-list"><div v-for="operator in operators" :key="operator.id" class="operator-row"><div><strong>{{operator.username}}</strong><span class="subtle">{{operator.role === 'operator' ? '运营人员' : operator.role === 'viewer' ? '只读' : '管理员'}} · {{operator.status === 'active' ? '启用' : '停用'}}</span></div><span class="subtle">与管理员共享工作空间任务容量</span><button class="btn" :disabled="busy" @click="run(() => toggleOperator(operator))">{{operator.status === 'active' ? '停用' : '启用'}}</button></div></div>
  </section>
</template>
