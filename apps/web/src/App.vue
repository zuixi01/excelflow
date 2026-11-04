<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref } from 'vue';
import LoginPage from './workflow/LoginPage.vue';
import Settings from './workflow/Settings.vue';
import { ClipboardCheck, Settings as SettingsIcon, SlidersHorizontal } from 'lucide-vue-next';
import { ApiError, api, clearAuthToken, type AuthSession } from './workflow/api';
import './workflow/style.css';
const Workbench = defineAsyncComponent(() => import('./workflow/Workbench.vue'));
const TaskDetail = defineAsyncComponent(() => import('./workflow/TaskDetail.vue'));
const FieldTemplates = defineAsyncComponent(() => import('./workflow/FieldTemplates.vue'));
const FieldConfiguration = defineAsyncComponent(() => import('./workflow/FieldConfiguration.vue'));
function activeRoute(value:string){
  const [name,query] = value.split('?');
  if (['legacy','reconciliation'].includes(name) ||
      (name === 'workbench' && new URLSearchParams(query || '').get('mode') === 'reconciliation')) return 'workbench';
  return value || 'workbench';
}
const route = ref(activeRoute(location.hash.slice(1)));
const dirty = ref(false), authVersion = ref(0), canManage = ref(false);
const authState = ref<'checking' | 'authenticated' | 'unauthenticated' | 'error'>('checking');
const authError = ref('');
const session = ref<AuthSession>();
const canViewAllTasks = computed(() => ['owner', 'admin', 'service'].includes(session.value?.role || ''));
const canEdit = computed(() => session.value?.role !== 'viewer');
const active = computed(() => route.value.split('?')[0].split('/')[0]);
const taskId = computed(() => route.value.split('?')[0].split('/')[1]);
const inWorkbench = computed(() => ['workbench','task'].includes(active.value));
function navigate(value: string) {
  if (dirty.value && !window.confirm('还有未保存的修改，确认离开当前页面？')) return;
  dirty.value = false; location.hash = value;
}
function hashChange() {
  const next = activeRoute(location.hash.slice(1));
  if (next === 'workbench' && location.hash !== '#workbench') history.replaceState(history.state, '', '#workbench');
  if (next === route.value) return;
  if (dirty.value && !window.confirm('还有未保存的修改，确认离开当前页面？')) {
    history.replaceState(history.state, '', '#' + route.value);
    return;
  }
  dirty.value = false;
  route.value = next;
}
async function permissions() {
  try { canManage.value = (await api<{can_manage_templates: boolean}>('/workflow-settings')).can_manage_templates; }
  catch { canManage.value = false; }
}
async function checkSession() {
  authState.value = 'checking';
  authError.value = '';
  try {
    session.value = await api<AuthSession>('/auth/session');
    authState.value = 'authenticated';
    await permissions();
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 401) {
      authState.value = 'unauthenticated';
      return;
    }
    authState.value = 'error';
    authError.value = cause instanceof Error ? cause.message : '无法读取登录状态，请稍后重试。';
  }
}
function authenticated(nextSession: AuthSession) {
  clearAuthToken();
  session.value = nextSession;
  authState.value = 'authenticated';
  authVersion.value++;
  void permissions();
}
function unauthorized() {
  clearAuthToken();
  session.value = undefined;
  canManage.value = false;
  authState.value = 'unauthenticated';
  authVersion.value++;
}
async function logout() {
  if (dirty.value && !window.confirm('还有未保存的修改，确认退出登录？')) return;
  dirty.value = false;
  try { await api('/auth/logout', 'POST'); } catch { /* Clear local UI even if the session already expired. */ }
  unauthorized();
}
onMounted(() => {
  if (route.value === 'workbench' && location.hash && location.hash !== '#workbench') history.replaceState(history.state, '', '#workbench');
  window.addEventListener('hashchange',hashChange);
  window.addEventListener('excel-auditor:unauthorized', unauthorized);
  void checkSession();
});
onUnmounted(() => {
  window.removeEventListener('hashchange',hashChange);
  window.removeEventListener('excel-auditor:unauthorized', unauthorized);
});
</script>
<template>
  <LoginPage v-if="authState === 'unauthenticated'" @authenticated="authenticated" />
  <main v-else-if="authState === 'checking'" class="auth-loading" aria-live="polite">正在验证登录状态…</main>
  <main v-else-if="authState === 'error'" class="auth-loading"><p>{{ authError }}</p><button class="btn primary" @click="checkSession">重新检查</button></main>
  <div v-else class="sop">
    <aside class="sidebar"><a href="#workbench" class="brand" @click.prevent="navigate('workbench')"><span class="brand-mark">E<span>F</span></span><span>ExcelFlow<small>数据维护与字段核对</small></span></a><nav aria-label="主导航"><button :class="{active:inWorkbench}" @click="navigate('workbench')"><ClipboardCheck :size="19" />工作台</button><button :class="{active:['templates','fields'].includes(active)}" @click="navigate('templates')"><SlidersHorizontal :size="19" />模板管理</button><button :class="{active:active === 'settings'}" @click="navigate('settings')"><SettingsIcon :size="19" />系统设置</button></nav></aside>
    <div class="app-body"><header class="app-header"><div><span class="header-dot"></span>{{['templates','fields'].includes(active) ? '业务模板与字段配置' : active === 'settings' ? '连接与设置' : '数据整理与核对'}}</div><div class="header-actions"><span class="header-label">{{session?.username || '管理员'}}</span><button class="btn text header-logout" @click="logout">退出登录</button></div></header><main class="workspace">
      <Workbench v-if="active === 'workbench'" :key="'workspace'+authVersion" :show-creator="canViewAllTasks" :can-edit="canEdit" @open="navigate('task/'+$event)" @configure="navigate('templates')" />
      <Settings v-else-if="active === 'settings'" :key="'settings'+authVersion" @dirty="dirty=$event" />
      <FieldTemplates v-else-if="active === 'templates' && !taskId" :key="'templates'+authVersion" :can-manage="canManage" @open="navigate('templates/'+$event)" />
      <div v-else-if="active === 'fields' || (active === 'templates' && taskId)" :key="route+authVersion"><button class="btn text breadcrumb" @click="navigate('templates')">← 返回模板管理</button><FieldConfiguration :profile-id="active === 'templates' ? taskId : undefined" :can-manage="canManage" @dirty="dirty=$event" /></div>
      <TaskDetail v-else-if="active === 'task' && taskId" :id="taskId" :show-creator="canViewAllTasks" :can-edit="canEdit" :key="taskId+authVersion" @back="navigate('workbench')" @dirty="dirty=$event" />
      <div v-else class="card empty-state"><h1>页面不存在</h1><button class="btn" @click="navigate('workbench')">返回工作台</button></div>
    </main></div>
  </div>
</template>
