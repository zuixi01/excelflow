import { ref } from "vue";

export interface FieldRule {
  name: string; title: string; type: string; required: boolean; aliases: string[];
  enum_values: string[]; sensitive?: boolean;
  validation: { min?: string | number | null; max?: string | number | null; unique: boolean; regex?: string | null; [key: string]: unknown };
  [key: string]: unknown;
}
export interface MaintenanceField { rule: FieldRule; editable: boolean; default: string | null }
export interface OutputColumn { title: string; field: string; operation: string; argument: string; sources: string[]; part: number; required: boolean; number_format: string; width: number }
export interface OutputSheet { name: string; header_row: number; data_start_row: number; columns: OutputColumn[]; group_by: string; sku_fields: string[]; sku_separator: string }
export interface OutputFile { name: string; sheets: OutputSheet[] }
export interface ImportRule { field: string; headers: string[]; operation: string; argument: string; part: number }
export interface Config { name: string; description: string; fields: MaintenanceField[]; import_rules: ImportRule[]; unique_key: string[]; cross_checks: Array<{kind: string; left: string; right: string; equals: string; message: string}>; outputs: OutputFile[]; preserve_extras: boolean }
export interface Template { id: string; revision: number; status: string; updated_at: string; payload: { config: Config; version: number; confirmed: boolean; notes: string[]; purpose?: 'maintenance' | 'output' | 'import'; error?: string; ai_model?: string; sample_count?: number; tested_hash?: string; sources?: Partial<Record<'maintenance' | 'output' | 'import', {filename: string; sheets: Array<{name: string; columns: number}>}>>; dynamic_columns?: Array<{sheet: string; columns: Array<{column: number; title: string; field: string}>}>; test_result?: {passed: boolean; records: number; errors: number; issues: Array<{row: number; field: string; message: string}>; artifacts?: Array<{name: string}>} } }
export interface Binding { field: string; columns: number[]; operation: string; argument: string; part: number }
export interface SheetMapping { sheet: string; header_row: number; header_depth: number; include: boolean; bindings: Binding[]; ignored_columns: number[] }
export interface SourceSheet { sheet: string; header_row: number; header_depth: number; row_count: number; columns: Array<{column: number; header: string; field: string; example: string; candidates: string[]}>; suggested_bindings?: Binding[]; mapping_notes?: string[]; preview: string[][] }
export interface Issue { field: string; title?: string; message: string; severity: string }
export interface Row { id: string; ordinal: number; values_json: Record<string, string>; original: Record<string, string>; source: {sheet: string; row: number | null}; issues: Issue[] }
export interface ExportRun { id: string; status: string; created_at: string; payload: { revision: number; omitted_fields: string[]; output_template_id?: string; output_template_version?: number; error?: string; artifacts?: Array<{name: string; size: number; sheets: Array<{sheet: string; records: number}>}> } }
export interface Task { id: string; revision: number; status: string; updated_at: string; payload: { name: string; filename: string; config: Config; template_version: number; template_hash: string; stage: string; error?: string; record_count: number; error_count: number; sheets?: SourceSheet[]; mapping?: {base_revision: number; sheets: SheetMapping[]}; ignored?: Array<{sheet: string; column?: string; reason: string}> }; exports: ExportRun[]; history: Array<{revision: number; created_at: string; actor: string; detail: {action: string; records?: number; changed_records?: number; added?: number; deleted?: number}}> }
export interface TaskSummary {id: string; name: string; filename: string; status: string; template_name: string; record_count: number; error_count: number; updated_at: string; revision: number; error: string}

const prefix = "/api/v1";
export async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = sessionStorage.getItem("excel-auditor-api-token");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(prefix + path, {...init, headers});
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    const details = problem.errors?.map((item: {loc: Array<string | number>}) => item.loc.join(".")).slice(0, 4).join("、");
    throw new Error(response.status === 401 ? "请在系统设置中填写有效访问令牌。" : (problem.detail || "请求失败，请稍后重试。") + (details ? `（${details}）` : ""));
  }
  return response;
}
export async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  return (await request(path, {method, body: body instanceof FormData ? body : body === undefined ? undefined : JSON.stringify(body)})).json();
}
export async function download(path: string, name: string) {
  const response = await request(path);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a"); link.href = url; link.download = name; link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
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
