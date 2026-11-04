<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue';
import { ArrowRight, LockKeyhole } from 'lucide-vue-next';
import { ApiError, api, type AuthSession } from './api';

const emit = defineEmits<{ authenticated: [session: AuthSession] }>();
const username = ref('');
const password = ref('');
const busy = ref(false);
const error = ref('');
const input = ref<HTMLInputElement>();

onMounted(() => void nextTick(() => input.value?.focus()));

async function submit() {
  if (!username.value.trim() || !password.value) {
    error.value = '请输入账号和密码。';
    await nextTick(() => input.value?.focus());
    return;
  }
  busy.value = true;
  error.value = '';
  try {
    const session = await api<AuthSession>('/auth/login', 'POST', {
      username: username.value.trim(),
      password: password.value,
    });
    emit('authenticated', session);
  } catch (cause) {
    error.value = cause instanceof ApiError && cause.status === 401
      ? '账号或密码不正确。'
      : cause instanceof Error ? cause.message : '登录失败，请稍后重试。';
    password.value = '';
    await nextTick(() => input.value?.focus());
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <main class="auth-page" aria-labelledby="login-title">
    <section class="auth-card">
      <div class="auth-mark" aria-hidden="true"><LockKeyhole :size="22" /></div>
      <p class="auth-kicker">ExcelFlow</p>
      <h1 id="login-title">登录数据工作空间</h1>
      <p class="auth-subtitle">使用账号和密码进入 ExcelFlow。</p>
      <form class="auth-form" @submit.prevent="submit">
        <label for="username">账号</label>
        <input id="username" ref="input" v-model="username" type="text" autocomplete="username" spellcheck="false" placeholder="输入账号" :disabled="busy" aria-describedby="login-help login-error" />
        <label for="password">密码</label>
        <input id="password" v-model="password" type="password" autocomplete="current-password" placeholder="输入密码" :disabled="busy" />
        <p id="login-help" class="auth-help">登录后只使用 HttpOnly 会话 Cookie，密码不会保存到浏览器。</p>
        <p v-if="error" id="login-error" class="auth-error" role="alert">{{ error }}</p>
        <button class="btn primary auth-submit" type="submit" :disabled="busy">
          <ArrowRight :size="17" />{{ busy ? '正在验证…' : '进入工作台' }}
        </button>
      </form>
    </section>
  </main>
</template>
