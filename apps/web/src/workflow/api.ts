import { ref } from "vue";

export function imageIds(value: string): string[] { return [...new Set(value.match(/image:\/\/[a-f0-9]{64}/g) || [])].map(value=>value.slice(8)); }
export function imageDisplayText(value: string): string { return value.replace(/image:\/\/[a-f0-9]{64}/g, '图片'); }

export interface FieldRule {
  name: string; title: string; type: string; required: boolean; aliases: string[];
  enum_values: string[]; sensitive?: boolean;
  validation: { min?: string | number | null; max?: string | number | null; unique: boolean; regex?: string | null; [key: string]: unknown };
  [key: string]: unknown;
}
export interface MaintenanceField { rule: FieldRule; editable: boolean; default: string | null; category_id?: string; hidden?: boolean; description?: string; source_column?: {sheet:string; column:number; header:string; context_only?:boolean} | null }
export interface ConfiguredField { id:string; title:string; group:string; description:string; aliases:string[]; type:string; required:boolean; editable:boolean; enabled:boolean; default:string|null; enum_values:string[]; min:string|null; max:string|null }
export interface LocalCategory { id:string; title:string; attributes:ConfiguredField[]; specifications:ConfiguredField[] }
export interface FieldProfile { name:string; fixed_fields:ConfiguredField[]; local_categories:LocalCategory[]; platform_source?:{connection_id:string;schema_version:string;fetched_at:string}|null }
export interface FieldProfileRecord { id:string; revision:number; status:string; is_default:boolean; payload:{draft:FieldProfile;published:FieldProfile|null;version:number;template_name:string;published_template_name:string} }
export interface OutputColumn { title: string; field: string; operation: string; argument: string; sources: string[]; part: number; required: boolean; number_format: string; width: number }
export interface OutputSheet { name: string; header_row: number; data_start_row: number; columns: OutputColumn[]; group_by: string; sku_fields: string[]; sku_separator: string }
export interface OutputFile { name: string; display_name?: string; sheets: OutputSheet[] }
export interface ImportRule { field: string; headers: string[]; operation: string; argument: string; part: number }
export interface Config { name: string; description: string; fields: MaintenanceField[]; import_rules: ImportRule[]; unique_key: string[]; cross_checks: Array<{kind: string; left: string; right: string; equals: string; message: string}>; outputs: OutputFile[]; preserve_extras: boolean; category_field?:string; source_header_layout?:'provenance'|'compact'|'grouped'; exclude_source_sequence?:boolean; preserve_source_merges?:boolean }
export interface Template { id: string; revision: number; status: string; updated_at: string; payload: { config: Config; version: number; confirmed: boolean; notes: string[]; purpose?: 'maintenance' | 'output' | 'import'; error?: string; ai_model?: string; sample_count?: number; tested_hash?: string; sources?: Partial<Record<'maintenance' | 'output' | 'import', {filename: string; sheets: Array<{name: string; columns: number}>}>>; dynamic_columns?: Array<{sheet: string; columns: Array<{column: number; title: string; field: string}>}>; test_result?: {passed: boolean; records: number; errors: number; issues: Array<{row: number; field: string; message: string}>; artifacts?: Array<{name: string}>} } }
export interface Binding { field: string; columns: number[]; operation: string; argument: string; part: number; method?:string; reason?:string; confidence?:number }
export interface SheetMapping { sheet: string; header_row: number; header_depth: number; include: boolean; bindings: Binding[]; ignored_columns: number[]; category_id?:string }
export interface SourceSheet { sheet: string; header_row: number; header_depth: number; row_count: number; category_id?:string; source_sheet?:string; source_block?:{index:number;label:string;range:string;start_column:number;end_column:number}; columns: Array<{column: number; source_column?:number; header: string; field: string; example: string; candidates: string[]}>; suggested_bindings?: Binding[]; mapping_notes?: string[]; mapping_notices?:string[]; verbatim_columns?:number[]; preview: string[][]; formula_results?:Array<{sheet:string; cell:string; formula:string; cached_value:string; value:string; status:string}> }
export interface Issue { field: string; title?: string; message: string; severity: string }
export interface Row { id: string; ordinal: number; values_json: Record<string, string>; original: Record<string, string>; source: {sheet: string; row: number | null; kind?:string; context_reason?:string; rectangular_merges?:Array<{fields:string[];group:string}>; filename?: string; original_sheet?: string; file_index?: number; merged_fields?:Record<string,string>}; issues: Issue[] }
export interface ExportRun { id: string; status: string; created_at: string; payload: { revision: number; omitted_fields: string[]; output_template_id?: string; output_template_version?: number; error?: string; artifacts?: Array<{name: string; download_name?: string; size: number; sheets: Array<{sheet: string; records: number}>}> } }
export interface TaskCreator { created_by_id?: string | null; created_by_username?: string; execution?: {status: string; attempts: number} }
export function executionLabel(item: {status: string; execution?: {status: string; attempts: number}}): string {
  if (item.status !== 'processing' || !item.execution) return labels[item.status] || item.status;
  const {status, attempts} = item.execution;
  if (status === 'running') return attempts > 1 ? `重试处理中（第 ${attempts} 次）` : '正在处理';
  return attempts > 0 ? '等待重试' : '等待处理';
}
export interface ProcessingProgress { run_id: string; status: 'running'|'completed'|'failed'; stage: string; started_at: string; updated_at: string; finished_at?: string; ai_started_at?: string; ai_finished_at?: string; model?: string; total_batches: number; completed_batches: number; failed_batches: number; elapsed_seconds?: number; sheets: Array<{sheet: string; total_batches: number; completed_batches: number; failed_batches: number; status: 'waiting'|'running'|'completed'}> }
export interface Task extends TaskCreator { final_outputs?: OutputFile[]; id: string; revision: number; status: string; created_at: string; updated_at: string; payload: { source_review?:{missing_product_names:Array<{id:string;source:{sheet:string;row:number}}> ;context_rows:number;empty_source_columns:Array<{field:string;source:{sheet:string;column:number;header:string}}>}; processing_progress?: ProcessingProgress; name: string; filename: string; config: Config; template_version: number; template_hash: string; stage: string; workflow?:string; field_snapshot?:{version:number;profile_id?:string;template_name?:string;compatibility_version?:number;dynamic_source:string;profile:FieldProfile}; error?: string; record_count: number; error_count: number; sheets?: SourceSheet[]; mapping?: {base_revision: number; sheets: SheetMapping[]}; ignored?: Array<{sheet: string; column?: string; reason: string}> }; exports: ExportRun[]; history: Array<{revision: number; created_at: string; actor: string; detail: {action: string; records?: number; changed_records?: number; added?: number; deleted?: number}}> }
export interface AITiming { status: 'running'|'completed'|'failed'; stage: string; ai_started_at?: string | null; ai_finished_at?: string | null; finished_at?: string | null }
export interface TaskSummary extends TaskCreator {id: string; name: string; filename: string; status: string; template_name: string; record_count: number; error_count: number; updated_at: string; revision: number; error: string; ai_timing?: AITiming | null}

const prefix = "/api/v1";
export const AUTH_TOKEN_KEY = "excel-auditor-api-token";
let csrfToken = "";
let csrfRequest: Promise<string> | undefined;
let authGeneration = 0;
export interface AuthSession {
  authenticated: boolean;
  auth_required: boolean;
  user_id: string;
  username?: string;
  role?: string;
  auth_method?: string;
  expires_at?: string | null;
}
export interface OperatorAccount {
  id: string; username: string; role: 'owner' | 'admin' | 'operator' | 'viewer'; status: 'active' | 'disabled';
  tenant_id?: string;
  last_login_at?: string | null; created_at: string;
}
export class ApiError extends Error {
  constructor(message: string, public status: number, public code?: string) { super(message); this.name = 'ApiError'; }
}
export function cloneData<T>(value:T):T { return JSON.parse(JSON.stringify(value)); }
export function setAuthToken(value: string) { sessionStorage.setItem(AUTH_TOKEN_KEY, value.trim()); }
export function clearAuthToken() { sessionStorage.removeItem(AUTH_TOKEN_KEY); csrfToken = ""; csrfRequest = undefined; authGeneration++; }
export async function ensureCsrfToken(): Promise<string> {
  if (csrfToken) return csrfToken;
  if (!csrfRequest) {
    const generation = authGeneration;
    const pending = (async () => {
      const response = await fetch(`${prefix}/auth/csrf`, {credentials: "include", cache: "no-store"});
      if (!response.ok) throw new ApiError("登录状态已失效，请重新登录。", response.status);
      const token = (await response.json()).csrf_token as string;
      if (generation !== authGeneration) throw new ApiError("登录状态已变化，请重试。", 401);
      csrfToken = token;
      return token;
    })();
    csrfRequest = pending;
    void pending.finally(() => { if (csrfRequest === pending) csrfRequest = undefined; }).catch(() => {});
  }
  return csrfRequest;
}
export async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set('Prefer', 'respond-async');
  if (init.method && !['GET', 'HEAD'].includes(init.method.toUpperCase()) && !headers.has('Idempotency-Key')) {
    headers.set('Idempotency-Key', crypto.randomUUID());
  }
  const token = sessionStorage.getItem(AUTH_TOKEN_KEY);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const method = (init.method || "GET").toUpperCase();
  if (!token && ["POST", "PUT", "PATCH", "DELETE"].includes(method) && path !== "/auth/login") {
    headers.set("X-CSRF-Token", await ensureCsrfToken());
  }
  const response = await fetch(prefix + path, {...init, headers, credentials: "include"});
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    const details = problem.errors?.map((item: {loc: Array<string | number>}) => item.loc.join(".")).slice(0, 4).join("、");
    if (response.status === 401) { csrfToken = ""; window.dispatchEvent(new CustomEvent("excel-auditor:unauthorized")); }
    throw new ApiError(response.status === 401 ? "访问令牌无效或已过期，请重新登录。" : (problem.detail || "请求失败，请稍后重试。") + (details ? `（${details}）` : ""), response.status, problem.code);
  }
  const location = response.headers.get('Location');
  if (response.status === 202 && location?.startsWith('/api/v1/workflow-tasks/') && location.includes('/operations/')) {
    const operationPath = location.slice(prefix.length);
    const deadline = Date.now() + 30 * 60 * 1000;
    window.dispatchEvent(new CustomEvent('excel-auditor:operation', {detail: {path: operationPath, status: 'processing'}}));
    while (Date.now() < deadline) {
      await new Promise(resolve => window.setTimeout(resolve, 2000));
      const job = await api<{status:string; error?:string; code?:string}>(operationPath);
      if (job.status === 'ready') {
        window.dispatchEvent(new CustomEvent('excel-auditor:operation', {detail: {path: operationPath, status: 'ready'}}));
        return request(operationPath + '/result', {cache: 'no-store'});
      }
      if (['failed', 'cancelled'].includes(job.status)) {
        window.dispatchEvent(new CustomEvent('excel-auditor:operation', {detail: {path: operationPath, status: 'failed'}}));
        throw new ApiError(job.error || '后台处理失败，请刷新任务后重试。', job.code === 'REVISION_CONFLICT' ? 409 : 422, job.code);
      }
    }
    throw new ApiError('任务仍在后台排队或处理，请稍后刷新查看，不要重复提交。', 408);
  }
  return response;
}
export async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  return (await request(path, {method, body: body instanceof FormData ? body : body === undefined ? undefined : JSON.stringify(body)})).json();
}
export async function download(path: string, name?: string): Promise<string> {
  const response = await request(path, {cache: 'no-store'});
  if (!name) {
    const disposition = response.headers.get('content-disposition') || '';
    const encoded = disposition.match(/filename\*=utf-8''([^;]+)/i)?.[1];
    try { name = encoded ? decodeURIComponent(encoded) : undefined; } catch { /* Use the plain filename fallback. */ }
    name ||= disposition.match(/filename="([^"]+)"/i)?.[1] || '下载文件.xlsx';
  }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a"); link.href = url; link.download = name; link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  return name;
}
export function useAction() {
  const busy = ref(false), error = ref(""), message = ref("");
  async function run(action: () => Promise<unknown>) {
    if (busy.value) return;
    busy.value = true; error.value = ""; message.value = "";
    try { await action(); } catch (cause) { error.value = cause instanceof Error ? cause.message : "操作失败，请重试。"; }
    finally { busy.value = false; }
  }
  return {busy, error, message, run};
}
export const labels: Record<string, string> = {processing: "正在解析", mapping: "待确认字段", maintenance: "待维护", ready: "可生成", completed: "已完成", failed: "处理失败", cancelled: "已取消", draft: "草稿", published: "已发布", learning: "AI 学习中"};
export const operations = [{value: "copy", label: "直接取值"}, {value: "constant", label: "固定值"}, {value: "multiply", label: "数值换算"}, {value: "concat", label: "拼接字段"}, {value: "split", label: "拆分取值"}];
export function dateLabel(value: string) { return new Date(value).toLocaleString("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"}); }
export function newColumn(field = "", title = "新字段"): OutputColumn { return {title, field, operation: "copy", argument: "", sources: [], part: 0, required: false, number_format: "", width: 18}; }
