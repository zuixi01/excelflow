<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { Sparkles, CheckCircle2, Clock3, CircleAlert } from 'lucide-vue-next';
import type { ProcessingProgress } from './api';

const props = defineProps<{ progress?: ProcessingProgress; active?: boolean; fallbackStartedAt?: string }>();
const clock = ref(Date.now());
let ticker: number | undefined;
onMounted(() => { ticker = window.setInterval(() => { clock.value = Date.now(); }, 1000); });
onUnmounted(() => window.clearInterval(ticker));
const running = computed(() => props.active || props.progress?.status === 'running');
const stage = computed(() => props.progress?.stage || 'queued');
const title = computed(() => props.progress?.stage === 'completed' && (props.progress?.failed_batches || 0) > 0 ? '字段分析部分完成' : ({ queued: '等待开始处理', reading_excel: '正在读取原表', ai_matching: 'AI 正在匹配字段',
  assembling: '正在整理匹配结果', completed: '字段分析已完成', failed: '本次分析未完成' }[stage.value] || '正在处理'));
function elapsed(start?: string, end?: string) {
  if (!start) return 0;
  const first = Date.parse(start), last = end ? Date.parse(end) : clock.value;
  return Number.isFinite(first) && Number.isFinite(last) ? Math.max(0, Math.floor((last - first) / 1000)) : 0;
}
function duration(seconds: number) {
  const minutes = Math.floor(seconds / 60), remainder = seconds % 60;
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
}
const totalTime = computed(() => duration(elapsed(props.progress?.started_at || props.fallbackStartedAt, props.progress?.finished_at)));
const aiTime = computed(() => duration(elapsed(props.progress?.ai_started_at, props.progress?.ai_finished_at || props.progress?.finished_at)));
const completed = computed(() => props.progress?.completed_batches || 0);
const total = computed(() => props.progress?.total_batches || 0);
const failed = computed(() => props.progress?.failed_batches || 0);
const percent = computed(() => total.value ? Math.min(100, completed.value / total.value * 100) : 0);
</script>

<template>
  <section class="card processing-progress" :class="{'processing-compact': !running}" aria-label="处理进度" :aria-busy="!!running">
    <div class="processing-heading">
      <div class="processing-title"><span class="processing-symbol" :class="{'is-active': running}"><Sparkles v-if="running" :size="21" /><CircleAlert v-else-if="stage === 'failed' || failed" :size="21" /><CheckCircle2 v-else :size="21" /></span><div><h2>{{ title }}</h2><p v-if="running" class="subtle">{{ stage === 'ai_matching' ? `正在等待 ${progress?.model || '模型'} 返回结果，完成后自动进入字段确认。` : '原表正在处理中，进度会自动更新。' }}</p></div></div>
      <div class="processing-clock"><Clock3 :size="16" /><span>已用时</span><strong data-testid="processing-elapsed">{{ totalTime }}</strong><small v-if="progress?.ai_started_at">AI 用时 <b data-testid="ai-elapsed">{{ aiTime }}</b></small></div>
    </div>
    <template v-if="running">
      <ol class="processing-stages" aria-label="处理阶段"><li :class="{done: stage !== 'queued' && stage !== 'reading_excel', current: stage === 'queued' || stage === 'reading_excel'}">读取 Excel</li><li :class="{done: stage === 'assembling', current: stage === 'ai_matching'}">AI 语义匹配</li><li :class="{current: stage === 'assembling'}">整理结果</li></ol>
      <div v-if="total" class="processing-batches"><div class="processing-batch-label"><span>{{ total === 1 ? 'AI 整份工作簿分析' : 'AI 分析批次' }}</span><strong>已处理 {{ completed }} / {{ total }}<span v-if="failed" class="danger"> · {{ failed }} 批未成功</span></strong></div><div class="processing-track" role="progressbar" aria-label="AI 批次进度" :aria-valuenow="completed" :aria-valuemin="0" :aria-valuemax="total"><span :style="{width: `${percent}%`}"></span></div><div class="processing-sheets"><div v-for="sheet in progress?.sheets" :key="sheet.sheet"><span class="processing-sheet-dot" :class="sheet.status"></span><strong>{{ sheet.sheet }}</strong><span>{{ sheet.status === 'waiting' ? '等待分析' : sheet.status === 'running' ? '分析中' : sheet.failed_batches ? '部分批次未成功' : '已完成' }}</span><small>{{ total === 1 ? '同一次请求' : `${sheet.completed_batches} / ${sheet.total_batches} 批` }}</small></div></div></div>
      <p v-if="stage === 'ai_matching'" class="processing-note">{{ total === 1 ? '整份工作簿的表头与待匹配字段已合并为一次分析，计时持续更新，模型返回后显示结果。' : '模型响应时间可能有波动；计时持续更新，批次返回后进度随之更新。' }}</p>
    </template>
    <p v-else-if="total" class="processing-completion">已处理 {{ completed }} / {{ total }} 批<span v-if="failed" class="danger">，其中 {{ failed }} 批未成功，请检查下方提示。</span></p>
  </section>
</template>
