<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref } from 'vue';
import Workbench from './workflow/Workbench.vue';
import TaskDetail from './workflow/TaskDetail.vue';
import TemplateCenter from './workflow/TemplateCenter.vue';
import Settings from './workflow/Settings.vue';
import { api } from './workflow/api';
import './workflow/style.css';
const LegacyTool = defineAsyncComponent(() => import('./LegacyTool.vue'));
const route = ref(location.hash.slice(1) || 'workbench');
const dirty = ref(false), canManage = ref(false), authVersion = ref(0);
const active = computed(() => route.value.startsWith('task/') ? 'workbench' : route.value.split('/')[0]);
const objectId = computed(() => route.value.split('/')[1]);
function navigate(value: string) {
  if (dirty.value && !window.confirm('还有未保存的修改，确认离开当前页面？')) return;
  dirty.value = false; location.hash = value;
}
function hashChange() { route.value = location.hash.slice(1) || 'workbench'; }
async function permissions() {
  try { canManage.value = (await api<{can_manage_templates: boolean}>('/workflow-settings')).can_manage_templates; }
  catch { canManage.value = false; }
}
async function authChanged() { authVersion.value++; await permissions(); }
onMounted(() => { window.addEventListener('hashchange',hashChange); void permissions(); });
onUnmounted(() => window.removeEventListener('hashchange',hashChange));
</script>
<template>
  <div class="sop">
    <aside class="sidebar"><a href="#workbench" class="brand" @click.prevent="navigate('workbench')"><span class="brand-mark">E<span>F</span></span><span>ExcelFlow<small>数据维护工作台</small></span></a><nav aria-label="主导航"><button :class="{active:active === 'workbench'}" @click="navigate('workbench')"><span class="nav-icon" aria-hidden="true">▤</span>工作台</button><button :class="{active:active === 'templates'}" @click="navigate('templates')"><span class="nav-icon" aria-hidden="true">▦</span>模板中心</button><button :class="{active:active === 'settings'}" @click="navigate('settings')"><span class="nav-icon" aria-hidden="true">⚙</span>系统设置</button></nav><div class="sidebar-bottom"><button :class="{active:active === 'legacy'}" @click="navigate('legacy')"><span class="nav-icon" aria-hidden="true">✓</span>标准核验工具</button><p>原表有来源<br />维护有记录 · 输出有标准</p></div></aside>
    <div class="app-body"><header class="app-header"><div><span class="header-dot"></span>{{active === 'templates' ? '模板与业务规则' : active === 'settings' ? '连接与设置' : active === 'legacy' ? '通用核验工具' : '标准作业流程'}}</div><span class="header-label">Excel 数据工作空间</span></header><main class="workspace">
      <Workbench v-if="active === 'workbench' && !objectId" :key="'workbench'+authVersion" @open="navigate('task/'+$event)" />
      <TaskDetail v-else-if="route.startsWith('task/') && objectId" :id="objectId" :key="objectId+authVersion" @back="navigate('workbench')" @dirty="dirty = $event" />
      <TemplateCenter v-else-if="active === 'templates'" :id="objectId" :can-manage="canManage" :key="'template'+objectId+authVersion" @open="navigate('templates/'+$event)" @back="navigate('templates')" @dirty="dirty = $event" />
      <Settings v-else-if="active === 'settings'" :key="'settings'+authVersion" @auth="authChanged" />
      <div v-else-if="active === 'legacy'" class="legacy-workbench"><LegacyTool :key="authVersion" /></div>
      <div v-else class="card empty-state"><h1>页面不存在</h1><button class="btn" @click="navigate('workbench')">返回工作台</button></div>
    </main></div>
  </div>
</template>
