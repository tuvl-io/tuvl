// Dev API v2 (`/dev/*`, dev mode only): project files as text, validation,
// codegen, tests and the spec workflow. Shapes mirror the CLI's `--json` output.
import { apiFetch, request, toApiError } from './api'
import type { ValidationResult } from './types'

export type TextArea =
  | 'workflows'
  | 'models'
  | 'specs'
  | 'tests'
  | 'artifacts'
  | 'llms'
  | 'datasources'
  | 'storage'

export interface ProjectSummary {
  project: string
  workflows: { name: string; description: string | null; agents: number; pending: number; unavailable?: string[] }[]
  models: string[]
  specs: { path: string; name: string; has_plan: boolean }[]
  tests: string[]
  lock: boolean
}

export interface TaskStatus {
  id: string
  title: string
  type: string
  status: 'todo' | 'in_progress' | 'implemented' | 'tested' | 'done' | 'untracked'
  blocked: boolean
  stale: boolean
  obsolete: boolean
  reasons: string[]
  target: Record<string, string>
  depends_on?: string[]
  /** the open tasks this one is waiting for */
  waiting_on?: string[]
  /** what a coding agent is doing with it right now (queued, running, failed…) */
  live?: { state: string; attempt?: number; max_attempts?: number; gate?: string; finding?: string } | null
  /** the last failed run of the task, attempt by attempt */
  failure?: TaskFailure | null
}

export interface TaskFailure {
  when?: number
  summary: string
  agent: string
  max_attempts: number
  cost_usd: number
  attempts: { attempt: number; gate: 'agent' | 'scope' | 'validate' | 'review'; problems: string[]; output?: string }[]
}

export interface SpecStatusReport {
  done: boolean
  plans: { spec: string; tasks: TaskStatus[] }[]
}

export interface TaskDetail {
  id: string
  title: string
  type: string
  target: Record<string, string>
  obsolete: boolean
  depends_on: { id: string; title: string }[]
  failure?: TaskFailure | null
  spec: string
  /** the part of the spec this task was derived from; `changed` once that section was edited */
  section: { anchor: string; title: string; line: number; text: string; changed: boolean } | null
}

export interface SpecDiffReport {
  specs: { spec: string; changed: boolean; stale: { task: string; why: string }[]; uncovered: string[] }[]
}

export interface TestResult {
  name: string
  workflow: string
  /** stubbed: expectations met only through unmocked pending agents — not a pass */
  status: 'passed' | 'stubbed' | 'failed'
  passed: boolean
  stubbed: string[]
  warnings: string[]
  failures: string[]
  end: string | null
  path: string[]
  tokens: number
}

export interface TestReport {
  passed: boolean
  tests: TestResult[]
  warnings: string[]
}

export interface CodegenResult {
  would_change?: { path: string; action: 'create' | 'update' | 'delete'; content?: string }[]
  written?: { path: string; action: 'create' | 'update' | 'delete' }[]
  warnings: string[]
}

const enc = (path: string) => path.split('/').map(encodeURIComponent).join('/')
const body = (value: unknown): RequestInit => ({ body: JSON.stringify(value) })

export interface SchedulePreview { ok: boolean; error?: string; next?: string[] }

export interface ScheduleRow {
  workflow: string
  cron: string
  enabled: boolean
  /** this process will fire it (scheduler on, mode and tenants allow it) */
  firing: boolean
  next: string[]
  tenants: string[]
  last_run: { run_id: string; status: string; at: string } | null
  warnings: string[]
}

export interface SchedulesReport {
  scheduler: { enabled: boolean; multi_tenant: boolean }
  workflows: ScheduleRow[]
}

export const schedulePreview = (cron: string) =>
  request<SchedulePreview>(`/dev/schedule/preview?cron=${encodeURIComponent(cron)}&count=3`)
export const fetchSchedules = () => request<SchedulesReport>('/dev/schedules')

export const fetchProject = () => request<ProjectSummary>('/dev/project/summary')
export const listText = (area: TextArea) => request<string[]>(`/dev/text/${area}`)

export async function readText(area: TextArea, name: string): Promise<string> {
  const res = await apiFetch(`/dev/text/${area}/${enc(name)}`)
  if (!res.ok) throw await toApiError(res)
  return res.text()
}

export async function writeText(area: TextArea, name: string, text: string): Promise<void> {
  const res = await apiFetch(`/dev/text/${area}/${enc(name)}`, {
    method: 'PUT',
    body: text,
    headers: { 'content-type': 'text/plain' },
  })
  if (!res.ok) throw await toApiError(res)
}

export const deleteText = (area: TextArea, name: string) =>
  request<{ deleted: string }>(`/dev/text/${area}/${enc(name)}`, { method: 'DELETE' })

export const validateProject = () => request<ValidationResult>('/dev/validate', { method: 'POST' })
export const runCodegen = (write: boolean) => request<CodegenResult>('/dev/codegen', { method: 'POST', ...body({ write }) })
export const runTests = (only?: string) => request<TestReport>('/dev/tests/run', { method: 'POST', ...body({ only }) })

export const specStatus = (spec?: string, tests = true) =>
  request<SpecStatusReport>(`/dev/specs/status?tests=${tests}${spec ? `&spec=${encodeURIComponent(spec)}` : ''}`)
export interface SpecFinding {
  code: string
  severity: 'error' | 'warning' | 'waived'
  line: number
  message: string
  /** set when the finding is in the task list rather than the spec */
  file?: string | null
}

export interface SpecCheck {
  spec: string
  path: string
  ok: boolean
  errors: number
  warnings: number
  findings: SpecFinding[]
  unchecked: string[]
}

export interface StepTrace {
  step: string
  line: number
  engine: string
  agent: string | null
  impl: 'ok' | 'pending' | 'mismatch' | 'missing'
  tests: { passed: string[]; mocked: string[] }
}

export interface ExampleTrace {
  name: string
  line: number
  flow: string | null
  test: string
  status: 'passed' | 'stubbed' | 'failed' | 'missing' | 'not run'
  failures: string[]
}

export interface SpecVerify {
  spec: string
  path: string
  ok: boolean
  errors: number
  warnings: number
  findings: SpecFinding[]
  flows: { flow: string; line: number; workflow: boolean; steps: StepTrace[] }[]
  examples: ExampleTrace[]
  summary: Record<string, number>
  tests_run: boolean
}

const specQuery = (spec?: string, extra = '') => `${spec ? `spec=${encodeURIComponent(spec)}` : ''}${spec && extra ? '&' : ''}${extra}`
export const specCheck = (spec?: string) =>
  request<{ ok: boolean; specs: SpecCheck[] }>(`/dev/specs/check?${specQuery(spec)}`)
/** Zero tokens; `tests` runs the project's tests offline to fill the tests column. */
export const specVerify = (spec?: string, tests = false) =>
  request<{ ok: boolean; specs: SpecVerify[] }>(`/dev/specs/verify?${specQuery(spec, `tests=${tests}`)}`)
export const specDiff = (spec?: string) =>
  request<SpecDiffReport>(`/dev/specs/diff${spec ? `?spec=${encodeURIComponent(spec)}` : ''}`)
export const specTask = (spec: string, id: string) =>
  request<TaskDetail>(`/dev/specs/${encodeURIComponent(spec)}/tasks/${encodeURIComponent(id)}`)
export interface JobState { label: string; status: 'running' | 'ok' | 'failed' | 'stopped'; exit_code: number | null; lines: string[]; next: number }
export const buildTask = (spec: string, id: string, scaffold = false) =>
  request<{ job: string }>(`/dev/specs/${encodeURIComponent(spec)}/tasks/${encodeURIComponent(id)}/build`, { method: 'POST', ...body({ consent: true, scaffold }) })
export const jobState = (job: string, after: number) => request<JobState>(`/dev/jobs/${encodeURIComponent(job)}?after=${after}`)
export const stopJob = (job: string) => request<{ status: string }>(`/dev/jobs/${encodeURIComponent(job)}/stop`, { method: 'POST', ...body({}) })
const specAction = (name: string, action: string, payload?: object) =>
  request<{ ok: boolean; output: string }>(`/dev/specs/${encodeURIComponent(name)}/${action}`, { method: 'POST', ...body(payload ?? {}) })
export const approveSpec = (name: string) => specAction(name, 'approve')
export const rejectSpec = (name: string, reason: string) => specAction(name, 'reject', { reason })
export const reopenSpec = (name: string) => specAction(name, 'reopen')
export const deriveTasks = (name: string) => specAction(name, 'tasks')
export const newSpec = (name: string, workflow?: string) =>
  request<{ created: string }>('/dev/specs/new', { method: 'POST', ...body({ name, workflow }) })

export interface AgentSource {
  run: string
  /** a tuvl built-in (tuvl.data_search, tuvl.data_ingest): no project Python */
  builtin: boolean
  file: string | null
  line: number | null
  source: string | null
  /** the whole file (imports and helpers included) — what the editor edits */
  file_text: string | null
  /** false: no file yet — file_text is codegen's stub; saving creates it */
  exists: boolean
}
/** The Python function behind a code agent's `code.run`, read from agents/ (dev mode). */
export const agentSource = (run: string, description = '') =>
  request<AgentSource>(`/dev/agents/${encodeURIComponent(run)}/source?description=${encodeURIComponent(description)}`)

export interface SavedSource extends AgentSource {
  /** set when codegen restored the decorator, signature or docstring */
  note: string | null
  /** set when the saved file doesn't import */
  load_error: string | null
}
export const saveAgentSource = (run: string, text: string) =>
  request<SavedSource>(`/dev/agents/${encodeURIComponent(run)}/source`, { method: 'PUT', ...body({ text }) })

export interface AssistModel { name: string; model: string; configured: boolean; missing: string[] }
export const assistModels = () => request<AssistModel[]>('/dev/agents/assist/models')

export interface AssistMessage { role: 'user' | 'assistant'; content: string; code?: string | null }
export interface AssistReply { reply: string; code: string | null; model: string | null; tokens: number }
/** One AI-assist turn; returns the full file (nothing is saved). */
export const assistCode = (run: string, req: { prompt: string; code: string; model?: string; messages: AssistMessage[]; agent?: Record<string, unknown> }) =>
  request<AssistReply>(`/dev/agents/${encodeURIComponent(run)}/assist`, { method: 'POST', ...body(req) })

export const getLayout = (workflow: string) => request<Record<string, { x: number; y: number }>>(`/dev/layout/${workflow}`)
export const putLayout = (workflow: string, layout: Record<string, { x: number; y: number }>) =>
  request<{ saved: string }>(`/dev/layout/${workflow}`, { method: 'PUT', ...body(layout) })

// ── developer controls ──────────────────────────────────────────────────────

export const getBreakpoints = (workflow: string) => request<string[]>(`/dev/breakpoints/${workflow}`)
export const putBreakpoints = (workflow: string, agents: string[]) =>
  request<string[]>(`/dev/breakpoints/${workflow}`, { method: 'PUT', ...body(agents) })
export interface UploadedFile { store: string; key: string; name: string; size: number; content_type: string; sha256: string }
export function uploadForRun(workflow: string, file: File): Promise<UploadedFile> {
  const form = new FormData()
  form.append('file', file)
  return request<UploadedFile>(`/dev/workflows/${encodeURIComponent(workflow)}/upload`, { method: 'POST', body: form })
}
export const startDevRun = (workflow: string, input: Record<string, unknown>, step: boolean) =>
  request<{ run_id: string }>('/dev/runs/start', { method: 'POST', ...body({ workflow, input, step }) })

export interface RunContext {
  status: string
  next: string | null
  context: Record<string, unknown>
  view: Record<string, unknown>
  editable: boolean
}
export const runContext = (runId: string) => request<RunContext>(`/dev/runs/${runId}/context`)
export const editRunContext = (runId: string, values: Record<string, unknown>) =>
  request<RunContext>(`/dev/runs/${runId}/context`, { method: 'PUT', ...body(values) })

export interface ProbeResult {
  run_id: string
  status: string
  signal: string | null
  outputs: Record<string, unknown>
  error: string | null
  tokens: number
}
export const probeAgent = (workflow: string, agent: string, inputs: Record<string, unknown>) =>
  request<ProbeResult>('/dev/agents/probe', { method: 'POST', ...body({ workflow, agent, inputs }) })

export interface ChangeCheck {
  fixtures: number
  passed: boolean | null
  replays: TestResult[]
  before: string | null
  after: string | null
}
export const changeCheck = (name: string, text: string, agent: string, live = false) =>
  request<ChangeCheck>('/dev/change/check', { method: 'POST', ...body({ name, text, agent, live }) })

export interface JudgeVerdict { verdict: 'pass' | 'fail' | 'uncertain'; score: number; reasons: string[]; cached: boolean }
export interface JudgeCalibration {
  agreement: number
  trusted: boolean
  results: { input: string; expect: string; got: string; agree: boolean }[]
}
export const judgeTry = (name: string, text: string, subject: unknown) =>
  request<JudgeVerdict>('/dev/judges/try', { method: 'POST', ...body({ name, text, subject }) })
export const judgeCalibrate = (name: string, text: string) =>
  request<JudgeCalibration>('/dev/judges/calibrate', { method: 'POST', ...body({ name, text }) })
export const judgeLabel = (label: string, subject: unknown) =>
  request<{ input: string }>('/dev/judges/label', { method: 'POST', ...body({ label, subject }) })
export const rulesTry = (text: string, context: Record<string, unknown>) =>
  request<{ rule: number | null; then: string | null; error: string | null }>('/dev/rules/try', { method: 'POST', ...body({ text, context }) })

// ── Storage ───────────────────────────────────────────────────────────────────

export interface StoreRow {
  name: string
  file: string
  error: string | null
  provider: string | null
  target: string | null
  prefix: string | null
  readonly: boolean | null
  missing: string[]
}

export interface StoreCheck {
  ok: boolean
  steps: { step: string; error: string | null }[]
}

export interface StoredObject {
  store: string
  key: string
  size: number
  content_type: string
  etag: string
  sha256: string
  modified: string
}

export const listStores = () => request<StoreRow[]>('/dev/storage')

export const checkStore = (name: string) =>
  request<StoreCheck>(`/dev/storage/${enc(name)}/check`, { method: 'POST' })

export const listObjects = (name: string, prefix: string, cursor?: string | null) => {
  const q = new URLSearchParams({ prefix })
  if (cursor) q.set('cursor', cursor)
  return request<{ items: StoredObject[]; cursor: string | null }>(
    `/dev/storage/${enc(name)}/objects?${q}`,
  )
}

// ── agent memory ──

export interface MemoryCollection {
  collection: string
  memories: number
  owners: number
  oldest: string
  newest: string
}

export interface MemoryRow {
  id: string
  key: string | null
  content: string
  at: string
  run_id: string | null
  agent: string | null
}

export const listMemoryCollections = () => request<MemoryCollection[]>('/dev/memory')

export const listMemories = (collection: string, key?: string | null) => {
  const q = new URLSearchParams()
  if (key) q.set('key', key)
  return request<MemoryRow[]>(`/dev/memory/${enc(collection)}?${q}`)
}

export const forgetMemory = (collection: string, id: string) =>
  request<{ removed: number }>(`/dev/memory/${enc(collection)}/${enc(id)}`, { method: 'DELETE' })

export const forgetOwner = (collection: string, key: string) =>
  request<{ removed: number }>(`/dev/memory/${enc(collection)}?${new URLSearchParams({ key })}`, {
    method: 'DELETE',
  })

// ── ideas, spec lifecycle, audit (read-only; the repository is the source of truth) ──

export interface SpecLifecycle {
  path: string
  name: string
  id: string
  title: string
  /** false for a spec that has no `status:` (it predates approval) */
  managed: boolean
  status: 'draft' | 'approved' | 'superseded'
  stale: boolean
  /** `approved` only while the body is unchanged */
  effective: 'draft' | 'approved' | 'superseded'
  approved_by: string
  approved_at: string
  ideas: string[]
  generated_by: string
  review_note: string
}

export interface IdeaSummary {
  id: string
  title: string
  imported_at: string
  imported_by: string
  supersedes: string | null
  superseded_by: string | null
  files: number
  specs: string[]
}

export interface IdeaFile {
  path: string
  sha256: string
  bytes: number
  extracted: string | null
  warning: string | null
  origin?: { url: string; kind: string; fetched_at: string } | null
  preview: string
  truncated: boolean
}

export interface IdeaDetail extends IdeaSummary {
  files_detail: IdeaFile[]
  specs_detail: SpecLifecycle[]
}

export interface AuditEvent {
  id: string
  ts: string
  actor: { initials?: string; git?: string }
  agent?: { name?: string; version?: string }
  command: string
  kind: string
  entities: string[]
  result: 'ok' | 'refused' | 'failed'
  detail: string
  refs: Record<string, unknown>
  prev: string
}

export interface AuditReport {
  events: AuditEvent[]
  chain: { ok: boolean; events: number; problem: string; where: string }
  kinds: string[]
}

export interface AuditQuery {
  entity?: string
  kind?: string
  actor?: string
  limit?: number
  after?: string
}

export const specLifecycle = () => request<{ specs: SpecLifecycle[] }>('/dev/specs/lifecycle')
/** Who you are to the project: what an upload needs (initials) and what generating a spec needs (an agent, consent given). */
export interface IdeasMe { initials: string; agent: string; consent: boolean }
export const listIdeas = () => request<{ ideas: IdeaSummary[]; me: IdeasMe }>('/dev/ideas')
export interface IdeaUpload { id: string; idea: IdeaDetail; output: string }
/** Files (named by their folder-relative path) and/or text, imported by `tuvl import idea`. */
export const uploadIdea = (form: FormData) => request<IdeaUpload>('/dev/ideas', { method: 'POST', body: form })
export const generateSpec = (idea: string, where: 'here' | 'branch', title: string) =>
  request<{ job: string }>(`/dev/ideas/${encodeURIComponent(idea)}/spec`, { method: 'POST', ...body({ consent: true, where, title }) })
export const getIdea = (id: string) => request<IdeaDetail>(`/dev/ideas/${encodeURIComponent(id)}`)
export const auditEvents = (q: AuditQuery = {}) => {
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== '') params.set(k, String(v))
  const qs = params.toString()
  return request<AuditReport>(`/dev/audit${qs ? `?${qs}` : ''}`)
}

export interface BranchNode {
  kind: 'spec' | 'scaffold' | 'task'
  branch: string
  title: string
  task: string | null
  /** merged: on the base branch (the branch itself may be deleted); open: built, not merged; none: not built */
  state: 'merged' | 'open' | 'none'
  ahead: number
  behind: number
  deleted?: boolean
  worktree: string
  pr: string
  merge_first: string[]
  next: string
  last: { sha: string; subject: string; when: string; author: string } | null
  /** the branch this one grew from (a branch name or the base) */
  parent: string
  /** other branches grown from the same parent: built apart from each other */
  parallel_with: string[]
  ref: string
  /** what the branch adds to its parent, newest first, with the full messages */
  commits: { sha: string; subject: string; body: string; when: string; author: string }[]
}

export interface BranchReport {
  base: string | null
  current: string
  dirty: number
  specs: { spec: string; id: string; uncommitted: string[]; merge_next: string | null; stack: BranchNode[] }[]
}

export const specBranches = (spec?: string) =>
  request<BranchReport>(`/dev/specs/branches${spec ? `?spec=${encodeURIComponent(spec)}` : ''}`)

export interface DevTelemetryState {
  configured: boolean
  enabled: boolean
  server: string
  token_env: string
  token_set: boolean
  /** env: the environment variable; registered: this developer's own token; none */
  token_source: 'env' | 'registered' | 'none'
  developer: { name?: string; email?: string; team?: string }
  /** who this machine says you are: initials from tuvl init, name and email from git config */
  local: { initials: string; name: string; email: string }
  identity: 'initials' | 'full'
  commands: boolean
  queued: number
  last_ok: number | null
  last_error: string
  sent: number
}

export interface TeamEvent {
  id: string
  ts: string
  kind: string
  command: string
  result: string
  entities: string[]
  detail: string
  branch: string
  project: string
  developer: { id?: string; name: string }
}

export interface EnvVariable {
  name: string
  description: string
  secret: boolean
  required: boolean
  default: string | null
  example: string | null
  group: string
  first_boot: boolean
  /** engine: built in; project: declared by an EnvConfig; referenced: only used by a ${VAR} */
  source: 'engine' | 'project' | 'referenced'
  declared: boolean
  referenced_by: string[]
  /** a ${VAR} with no default points at it */
  needed: boolean
  is_set: boolean
  in_dotenv: boolean
  /** a plain variable's current value; a secret's is never sent */
  value: string | null
  restart_needed: boolean
}
export interface EnvState {
  variables: EnvVariable[]
  dotenv: { path: string; exists: boolean; gitignored: boolean }
  manifest_file: string | null
}
export interface EnvDeclaration { name: string; description?: string; secret?: boolean; required?: boolean; default?: string | null }

export const devEnv = () => request<EnvState>('/dev/env')
export const saveDevEnv = (b: { set?: Record<string, string>; unset?: string[] }) =>
  request<EnvState>('/dev/env', { method: 'PUT', ...body(b) })
export const declareDevEnv = (b: EnvDeclaration) => request<EnvState>('/dev/env/declare', { method: 'POST', ...body(b) })

export const devTelemetry = () => request<DevTelemetryState>('/dev/dev-telemetry')
export const saveDevTelemetry = (b: Pick<DevTelemetryState, 'enabled' | 'server' | 'identity' | 'commands' | 'token_env'>) =>
  request<DevTelemetryState>('/dev/dev-telemetry', { method: 'PUT', ...body(b) })
export const registerDeveloper = (b: { name: string; email: string; team_code: string }) =>
  request<DevTelemetryState>('/dev/dev-telemetry/register', { method: 'POST', ...body(b) })
export const rotateDevToken = () => request<DevTelemetryState>('/dev/dev-telemetry/rotate', { method: 'POST', ...body({}) })
export const forgetDevToken = () => request<DevTelemetryState>('/dev/dev-telemetry/token', { method: 'DELETE' })
export const checkDevTelemetry = (flush: boolean) =>
  request<DevTelemetryState & { ok: boolean; error: string; sent: number }>(`/dev/dev-telemetry/check?flush=${flush}`, { method: 'POST', ...body({}) })
export const teamEvents = (f: { actor?: string; entity?: string; kind?: string; limit?: number }) => {
  const q = new URLSearchParams()
  Object.entries(f).forEach(([k, v]) => { if (v !== undefined && v !== '') q.set(k, String(v)) })
  return request<{ events: TeamEvent[] }>(`/dev/team-events?${q}`)
}
