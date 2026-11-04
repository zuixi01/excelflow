<script setup lang="ts">
import {computed, ref, watch} from 'vue';
import {api, imageDisplayText, imageIds} from './api';
type Kind = 'changed'|'added'|'missing';
interface Position {sheet:string;row:number}
interface Cell extends Position {column:number;letter:string;address:string}
interface Item {id?:string;name?:string;sku?:string;brand?:string;location?:Position;original_location?:Position;cells?:Record<string,Cell>;before?:Record<string,string>;after?:Record<string,string>;values?:Record<string,string>}
const props=defineProps<{taskId:string;previewId:string;onlyKind?:Kind;pendingIdentity?:boolean;currentBaseline?:boolean;counts:{changed:number;added:number;missing:number};deleteMissing:boolean;fieldTitles:Record<string,string>;complete:boolean;fallback:{changed:Item[];added:Item[];missing:Item[]}}>();
const kind=ref<Kind>('changed'),offset=ref(0),items=ref<Item[]>([]),total=ref(0),loading=ref(false),error=ref('');
const names:Record<Kind,string>={changed:'修改内容',added:'新增行',missing:'文件中删除的行'};
let sequence=0;
watch([()=>props.previewId,()=>props.onlyKind,()=>props.complete],()=>{kind.value=props.onlyKind||(props.counts.changed?'changed':props.counts.added?'added':'missing');offset.value=0;},{immediate:true});
watch([()=>props.previewId,()=>props.counts,kind,offset],async()=>{
 const current=++sequence;loading.value=true;error.value='';items.value=[];
 try{
  const result=props.complete?await api<{items:Item[];total:number}>(`/workflow-tasks/${props.taskId}/maintenance/returns/${props.previewId}/details?kind=${kind.value}&offset=${offset.value}&limit=10`):{items:props.fallback[kind.value].slice(offset.value,offset.value+10),total:props.fallback[kind.value].length};
  if(current===sequence){items.value=result.items;total.value=result.total;}
 }catch(cause){if(current===sequence)error.value=cause instanceof Error?cause.message:'明细读取失败';}
 finally{if(current===sequence)loading.value=false;}
},{immediate:true});
function select(value:Kind){kind.value=value;offset.value=0;}
function title(item:Item){return item.name||item.values?.product_name||item.sku||item.values?.sku||'未填写商品名称';}
function text(value:string|undefined){return value===undefined||value===''?'（空）':imageDisplayText(value);}
function pictureChanged(before:string,after:string){return imageIds(before).join(',')!==imageIds(after).join(',');}
const fields=(item:Item)=>Object.entries(item.values||{}).filter(([,value])=>value!=='' && value!=null);
const fieldTitle=(id:string)=>(props.fieldTitles[id]||id).split('/').pop();
const changes=computed(()=>items.value.flatMap(item=>Object.entries(item.after||{}).map(([field,value])=>({item,field,value,cell:item.cells?.[field]}))));
const position=(item:Item)=>kind.value==='missing'?item.original_location:item.location;
</script>
<template>
 <section class="return-diff-details" aria-label="商品增删改明细">
  <h3>{{onlyKind?`${kind==='missing'&&pendingIdentity?'原表暂未对应的行':names[kind]}（${counts[kind]} 行）`:'商品改了什么'}}</h3>
  <div v-if="!onlyKind" class="diff-tabs"><button v-for="value in (['changed','added','missing'] as Kind[])" :key="value" type="button" class="btn" :aria-pressed="kind===value" @click="select(value)">{{names[value]}}（{{counts[value]}} 行）</button></div>
  <p class="subtle">{{kind==='missing'?(currentBaseline?'下面是当前版本维护表的行号；缺少的行会在确认保存后删除，待核对的行需先确认对应关系。':pendingIdentity?'下面是下载时的行号；尚有对应关系待确认，不能据此确定这些行已删除。':'下面是下载时的行号；这些行已不在回传文件中。'):'下面的行号、列号与本次上传的 Excel 一致，可按单元格位置核对。'}}</p>
  <p v-if="kind==='missing' && !pendingIdentity" class="notice" :class="{'error':deleteMissing}">{{deleteMissing?`确认后将删除 ${counts.missing} 条缺少记录`:`这 ${counts.missing} 条记录默认保留，不会自动删除`}}</p>
  <p v-if="!complete" class="subtle">旧预览只有部分明细；请重新上传查看完整的位置和变化。</p>
  <p v-if="loading" role="status">正在读取改动…</p><p v-else-if="error" class="notice error" role="alert">{{error}}</p><p v-else-if="!items.length" class="subtle">没有这类改动。</p>
  <div v-else class="table-scroll">
   <table v-if="kind==='changed'" class="data-table diff-table" aria-label="单元格修改对照">
    <thead><tr><th>工作表</th><th>哪一行</th><th>哪一列 / 字段</th><th>商品</th><th>原来</th><th>改成</th></tr></thead>
    <tbody><tr v-for="change in changes" :key="`${change.item.id}:${change.field}`"><td>{{change.cell?.sheet||change.item.location?.sheet||'—'}}</td><td>{{change.cell?.row||change.item.location?.row?`第 ${change.cell?.row||change.item.location?.row} 行`:'旧预览未记录位置'}}</td><td :title="fieldTitles[change.field]"><strong>{{change.cell?`${change.cell.letter} 列 · ${change.cell.address}`:'—'}}</strong><br />{{fieldTitle(change.field)}}</td><td>{{title(change.item)}}</td><td class="diff-before">{{text(change.item.before?.[change.field])}}</td><td class="diff-after">{{text(change.value)}}<small v-if="pictureChanged(change.item.before?.[change.field]||'',change.value)">图片有变化（原 {{imageIds(change.item.before?.[change.field]||'').length}} 张 → 现 {{imageIds(change.value).length}} 张）</small></td></tr></tbody>
   </table>
   <table v-else class="data-table diff-table" aria-label="增删行对照"><thead><tr><th>工作表</th><th>哪一行</th><th>商品</th><th>变化</th><th>行内内容</th></tr></thead><tbody><tr v-for="(item,index) in items" :key="item.id||index"><td>{{position(item)?.sheet||'—'}}</td><td>{{position(item)?.row?`第 ${position(item)?.row} 行`:'旧预览未记录位置'}}</td><td>{{title(item)}}<small v-if="item.sku">货号：{{item.sku}}</small></td><td>{{kind==='added'?'新增整行':pendingIdentity?'待核对，暂不认定删除':deleteMissing?'删除整行':'文件中已删除，本次保留'}}</td><td><details :open="kind==='added' && !!onlyKind"><summary>查看这一行内容</summary><p v-for="[field,value] in fields(item)" :key="field">{{fieldTitle(field)}}：{{text(value)}}</p></details></td></tr></tbody></table>
  </div>
  <div v-if="total>10" class="pagination"><span>共 {{total}} 行商品 · 当前 {{offset+1}}–{{Math.min(offset+10,total)}}</span><button type="button" class="btn" :disabled="loading||offset===0" @click="offset-=10">上一页</button><button type="button" class="btn" :disabled="loading||offset+10>=total" @click="offset+=10">下一页</button></div>
 </section>
</template>
<style scoped>
.diff-tabs{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.diff-tabs button[aria-pressed=true]{background:#e6f2eb;border-color:#246b52;color:#174c3a}.diff-table td,.diff-table th{white-space:pre-wrap;overflow-wrap:anywhere;vertical-align:top;max-width:350px}.diff-before{background:#fff6ed}.diff-after{background:#edf8ef}.diff-table small{display:block;margin-top:6px}.diff-table details{min-width:120px}.diff-table p{margin:6px 0}
</style>
