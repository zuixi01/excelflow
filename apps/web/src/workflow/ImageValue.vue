<script setup lang="ts">
import { computed } from 'vue';
import { imageIds } from './api';
const props=defineProps<{value:string; taskId:string; editable?:boolean}>();
defineEmits<{clear:[]}>();
const images=computed(()=>imageIds(props.value));
const text=computed(()=>props.value.replace(/image:\/\/[a-f0-9]{64}/g,'').trim());
</script>
<template><div class="image-value"><span v-if="text">{{text}}</span><a v-for="id in images" :key="id" :href="`/api/v1/workflow-tasks/${taskId}/images/${id}`" target="_blank" rel="noopener noreferrer" title="查看原图"><img :src="`/api/v1/workflow-tasks/${taskId}/images/${id}`" alt="商品图片" loading="lazy" /></a><button v-if="editable" class="btn text" type="button" aria-label="清除此单元格图片" @click="$emit('clear')">清除图片</button></div></template>
