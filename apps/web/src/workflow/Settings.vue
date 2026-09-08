<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { api, useAction } from './api';
const emit = defineEmits<{auth: []}>();
const {busy,error,message,run} = useAction();
const token = ref(sessionStorage.getItem('excel-auditor-api-token') || '');
const settings = ref({base_url:'',model:'',api_key:'',clear_key:false,json_mode:true,include_samples:false});
const hasKey = ref(false), canManage = ref(false), models = ref<string[]>([]), storage = ref('local');
async function load() {
  const data = await api<{ai: {base_url: string; model: string; has_key: boolean; json_mode: boolean; include_samples: boolean}; can_manage_templates: boolean; storage: string}>('/workflow-settings');
  settings.value = {...settings.value,base_url:data.ai.base_url,model:data.ai.model,json_mode:data.ai.json_mode,include_samples:data.ai.include_samples,api_key:''}; hasKey.value=data.ai.has_key;canManage.value=data.can_manage_templates;storage.value=data.storage;
}
function auth() { if(token.value.trim()) sessionStorage.setItem('excel-auditor-api-token',token.value.trim()); else sessionStorage.removeItem('excel-auditor-api-token'); emit('auth'); }
async function save() { await api('/workflow-settings/ai','PUT',settings.value); settings.value.api_key=''; settings.value.clear_key=false;await load();message.value='模型配置已保存，密钥不会返回浏览器。'; }
async function connect() { await save(); models.value=(await api<{models:string[]}>('/workflow-settings/ai/models','POST')).models;message.value=`连接成功，找到 ${models.value.length} 个模型。请选择模型后保存。`; }
onMounted(() => void run(load));
</script>
<template>
  <div class="page-heading"><div><p class="kicker">连接与运行配置</p><h1>系统设置</h1><p class="subtle">模型仅用于模板辅助配置，已发布流程可独立执行。</p></div></div>
  <div v-if="error" class="notice error" role="alert">{{error}}</div><div v-if="message" class="notice success" role="status">{{message}}</div>
  <section class="card settings-card"><h2>访问凭证</h2><p class="subtle">已启用鉴权的环境需要填写访问令牌，仅保存在当前浏览器会话。</p><label>访问令牌<input v-model="token" type="password" autocomplete="off" placeholder="本地未启用鉴权时可留空" /></label><button class="btn" @click="auth">保存访问凭证</button></section>
  <section class="card settings-card"><div class="section-heading"><h2>AI 模型服务</h2><span class="badge" :class="hasKey ? 'ready' : 'draft'">{{hasKey ? '已保存密钥' : '尚未配置密钥'}}</span></div><div v-if="!canManage" class="notice info">当前账户可使用已发布流程，模型配置需要管理员权限。</div><div class="form-grid"><label>兼容接口地址<input v-model="settings.base_url" :disabled="!canManage" placeholder="https://your-provider.example/v1" autocomplete="off" /></label><label>API 密钥<input v-model="settings.api_key" :disabled="!canManage" type="password" autocomplete="new-password" :placeholder="hasKey ? '已保存；留空保留原密钥' : '填写模型服务密钥'" /></label><label>模型名称<input v-model="settings.model" :disabled="!canManage" list="available-models" placeholder="先连接服务获取模型列表" /><datalist id="available-models"><option v-for="model in models" :key="model" :value="model" /></datalist></label><div class="setting-toggles"><label class="inline-check"><input v-model="settings.json_mode" type="checkbox" :disabled="!canManage" />使用 JSON 响应模式</label><label class="inline-check"><input v-model="settings.include_samples" type="checkbox" :disabled="!canManage" />学习时发送少量非敏感样例值</label><label v-if="hasKey" class="inline-check"><input v-model="settings.clear_key" type="checkbox" :disabled="!canManage" />清除已保存密钥</label></div></div><p class="subtle">默认仅发送模板结构和业务说明。更换接口地址时，需要重新填写密钥。</p><div class="toolbar"><button class="btn" :disabled="busy || !canManage" @click="run(connect)">保存并连接 / 获取模型</button><button class="btn primary" :disabled="busy || !canManage" @click="run(save)">保存模型配置</button></div></section>
  <section class="card settings-card"><h2>当前运行范围</h2><dl class="settings-facts"><div><dt>数据保存</dt><dd>{{storage === 'local' ? '本机持久化存储' : '已配置数据库'}}</dd></div><div><dt>单次导入</dt><dd>.xlsx · 20 MiB · 20,000 条记录 · 200 列</dd></div><div><dt>平台数据接口</dt><dd>当前 SOP 使用上传数据，尚未连接真实商品平台</dd></div></dl></section>
</template>
