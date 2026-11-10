import { API_BASE } from './config'
import { useUIStore } from './store'
import type {
  ConfigKind,
  DatasourceConfig,
  FilesIndex,
  ModelConfig,
  ValidationResult,
  AgentModelConfig,
  IamUser,
  IamRole,
  FederationProviderConfig,
  TelemetryConfig,
  SystemConfig,
  AdminModelsResponse,
  ModelVersionInfo,
  ForkVersionResponse,
  AdminScopesResponse,
} from './types'
export type { TelemetryConfig, SystemConfig } from './types'


// ── Request helper ───────────────────────────────────────────────────────────
// In dev mode the server accepts the dev API key as a superuser token,
// so every authenticated call (dev + IAM admin + Insight) uses the stored key.

function tok(): string { return useUIStore.getState().devKey ?? '' }

/** A non-2xx REST response. `message` is the server's `detail` when present. */
export class ApiError extends Error {
  readonly status: number
  readonly body: unknown
  constructor(status: number, message: string, body: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string> | undefined) }
  if (tok()) headers.authorization = `Bearer ${tok()}`
  if (init.body !== undefined && !(init.body instanceof FormData) && !headers['content-type']) headers['content-type'] = 'application/json'
  return fetch(`${API_BASE}${path}`, { ...init, headers })
}

export async function toApiError(res: Response): Promise<ApiError> {
  let body: unknown = null
  try { body = await res.json() } catch { /* non-JSON error body */ }
  const detail = (body as { detail?: unknown } | null)?.detail
  const message = typeof detail === 'string' ? detail : res.statusText || `HTTP ${res.status}`
  return new ApiError(res.status, message, body)
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await apiFetch(path, init)
  if (!res.ok) throw await toApiError(res)
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body) })
const yamlName = (name: string) => encodeURIComponent(name.endsWith('.yaml') ? name : `${name}.yaml`)

// ── Dev file bridge ───────────────────────────────────────────────────────────

export async function pingServer(): Promise<void> {
  await request('/dev/ping')
}

export async function fetchFilesIndex(kind?: ConfigKind): Promise<FilesIndex> {
  return request<FilesIndex>(kind ? `/dev/files?kind=${encodeURIComponent(kind)}` : '/dev/files')
}

export async function fetchFile<T = unknown>(kind: ConfigKind, name: string): Promise<T> {
  return request<T>(`/dev/files/${kind}/${yamlName(name)}`)
}

export async function saveFile(kind: ConfigKind, name: string, body: unknown): Promise<void> {
  await request(`/dev/files/${kind}/${yamlName(name)}`, { method: 'PUT', ...json(body) })
}

export async function deleteFile(kind: ConfigKind, name: string): Promise<void> {
  await request(`/dev/files/${kind}/${yamlName(name)}`, { method: 'DELETE' })
}

export async function validateFiles(): Promise<ValidationResult> {
  return request<ValidationResult>('/dev/validate', { method: 'POST' })
}

// ── Typed helpers ─────────────────────────────────────────────────────────────

export const fetchModel = (name: string) =>
  fetchFile<ModelConfig>('models', name)

export const fetchDatasource = (name: string) =>
  fetchFile<DatasourceConfig>('datasources', name)

export const fetchAgentModel = (name: string) =>
  fetchFile<AgentModelConfig>('llms', name)

// ── Health ────────────────────────────────────────────────────────────────────

/** A model the workflows use: configured, or what it is missing (details in dev mode only). */
export interface ModelReadiness {
  configured: boolean
  missing?: string[]
  problems?: string[]
  used_by?: string[]
}

export async function fetchHealth(): Promise<{ status: string; version?: string; models?: Record<string, ModelReadiness> }> {
  return request('/health')
}

// ── System metadata ───────────────────────────────────────────────────────────

export async function fetchSystemModels(): Promise<
  Record<string, { fields: string[]; relations: string[] }>
> {
  return request('/api/_system/models')
}

// ── AI chat ───────────────────────────────────────────────────────────────────


// ── IAM admin ─────────────────────────────────────────────────────────────────

export async function fetchIamUsers(): Promise<IamUser[]> {
  return request<IamUser[]>('/auth/admin/users')
}

export async function createIamUser(body: {
  email: string
  password?: string
  is_active?: boolean
  phone_number?: string
  first_name?: string
  last_name?: string
}): Promise<IamUser> {
  return request<IamUser>('/auth/admin/users', { method: 'POST', ...json(body) })
}

export async function deleteIamUser(userId: string): Promise<void> {
  await request(`/auth/admin/users/${encodeURIComponent(userId)}`, { method: 'DELETE' })
}

/** Omitted fields are left unchanged; an empty string clears the field. */
export async function updateIamUser(
  userId: string,
  body: { password?: string; is_active?: boolean; phone_number?: string | null; first_name?: string | null; last_name?: string | null },
): Promise<IamUser> {
  return request<IamUser>(`/auth/admin/users/${encodeURIComponent(userId)}`, { method: 'PATCH', ...json(body) })
}

export async function fetchIamRoles(): Promise<IamRole[]> {
  return request<IamRole[]>('/auth/admin/roles')
}

export async function createIamRole(body: {
  name: string
  description?: string
  scopes: string[]
}): Promise<IamRole> {
  return request<IamRole>('/auth/admin/roles', { method: 'POST', ...json(body) })
}

export async function deleteIamRole(roleId: string): Promise<void> {
  await request(`/auth/admin/roles/${encodeURIComponent(roleId)}`, { method: 'DELETE' })
}

export async function updateRoleScopes(roleId: string, scopes: string[]): Promise<IamRole> {
  return request<IamRole>(`/auth/admin/roles/${encodeURIComponent(roleId)}/scopes`, { method: 'PATCH', ...json({ scopes }) })
}

export async function assignRole(userId: string, roleId: string): Promise<void> {
  await request(`/auth/admin/users/${encodeURIComponent(userId)}/roles/${encodeURIComponent(roleId)}`, { method: 'POST' })
}

export async function revokeRole(userId: string, roleId: string): Promise<void> {
  await request(`/auth/admin/users/${encodeURIComponent(userId)}/roles/${encodeURIComponent(roleId)}`, { method: 'DELETE' })
}

export async function bootstrapAdmin(body: {
  email: string
  password: string
}): Promise<{ access_token: string; token_type: string }> {
  return request('/auth/bootstrap', { method: 'POST', ...json(body) })
}

// ── Federation providers ──────────────────────────────────────────────────────

export async function fetchFederationProviders(): Promise<string[]> {
  return request<string[]>('/auth/admin/federation')
}

export async function fetchFederationProvider(name: string): Promise<FederationProviderConfig> {
  return request<FederationProviderConfig>(`/auth/admin/federation/${encodeURIComponent(name)}`)
}

export async function saveFederationProvider(name: string, body: FederationProviderConfig): Promise<void> {
  await request(`/auth/admin/federation/${encodeURIComponent(name)}`, { method: 'PUT', ...json(body) })
}

export async function deleteFederationProvider(name: string): Promise<void> {
  await request(`/auth/admin/federation/${encodeURIComponent(name)}`, { method: 'DELETE' })
}

// ── Telemetry config ──────────────────────────────────────────────────────────

export async function fetchTelemetryConfig(): Promise<TelemetryConfig | null> {
  const res = await fetch('/dev/telemetry-config', {
    headers: tok() ? { authorization: `Bearer ${tok()}` } : {},
  })
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`Failed to fetch telemetry config: ${res.statusText}`)
  return res.json() as Promise<TelemetryConfig>
}

export async function saveTelemetryConfig(body: TelemetryConfig): Promise<void> {
  const res = await fetch('/dev/telemetry-config', {
    method: 'PUT',
    headers: {
      'content-type': 'application/json',
      ...(tok() ? { authorization: `Bearer ${tok()}` } : {}),
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`Failed to save telemetry config: ${res.statusText}`)
}

// ── System config ──────────────────────────────────────────────────────────────

export async function fetchSystemConfig(): Promise<SystemConfig | null> {
  const res = await fetch('/dev/system-config', {
    headers: tok() ? { authorization: `Bearer ${tok()}` } : {},
  })
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`Failed to fetch system config: ${res.statusText}`)
  return res.json() as Promise<SystemConfig>
}

export async function saveSystemConfig(body: SystemConfig): Promise<void> {
  const res = await fetch('/dev/system-config', {
    method: 'PUT',
    headers: {
      'content-type': 'application/json',
      ...(tok() ? { authorization: `Bearer ${tok()}` } : {}),
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`Failed to save system config: ${res.statusText}`)
}

// ── Project identity ──────────────────────────────────────────────────────────

export interface ProjectInfo {
  id: string
  name: string
  author: string
  email: string
  organization: string
  organization_url: string
  type: 'poc' | 'dev' | 'production'
  version: string
  description: string
  license: string
  repo: string
  registry?: {
    url?: string
    registered_at?: string
    label?: 'listed' | 'push_confirmed' | 'verified'
    verified_at?: string
  }
  registration_state?: 'invalid_id' | 'unregistered' | 'id_changed' | 'outdated' | 'current'
}

export interface RegisterPreview {
  nest: string
  payload: Record<string, string>
  verification: {
    remote: string
    remote_url: string
    repo: string
    matches_repo: boolean
    can_verify: boolean
    /** why verification cannot run; empty when it can */
    reason: string
  }
}

export interface RegisterResult {
  status: 'registered'
  /** listed: self-declared; push_confirmed: the tool pushed, the registry could not see it; verified: the registry saw the tag */
  label: 'listed' | 'push_confirmed' | 'verified'
  claim: boolean
  applied: boolean
  /** null when no push was attempted */
  pushed: boolean | null
  notes: string[]
  project: ProjectInfo
}

async function postProject<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`/dev/project/${path}`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      ...(tok() ? { authorization: `Bearer ${tok()}` } : {}),
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const detail = await res.json().then((j) => j?.detail).catch(() => null)
    throw new Error(typeof detail === 'string' ? detail : `Request failed: ${res.statusText}`)
  }
  return res.json() as Promise<T>
}

export const previewRegistration = (body: { nest?: string }) =>
  postProject<RegisterPreview>('register/preview', body)

/** `verify: true` is the developer's consent to push the Nest's one-time tag to the project's git remote. */
export const registerProject = (body: { nest?: string; verify?: boolean; remote?: string; keep_tag?: boolean }) =>
  postProject<RegisterResult>('register', body)

export async function fetchProject(): Promise<ProjectInfo | null> {
  const res = await fetch('/dev/project', {
    headers: tok() ? { authorization: `Bearer ${tok()}` } : {},
  })
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`Failed to fetch project: ${res.statusText}`)
  return res.json() as Promise<ProjectInfo>
}

export async function saveProject(body: Partial<ProjectInfo>): Promise<ProjectInfo> {
  const res = await fetch('/dev/project', {
    method: 'PUT',
    headers: {
      'content-type': 'application/json',
      ...(tok() ? { authorization: `Bearer ${tok()}` } : {}),
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const detail = await res.json().then((j) => j?.detail).catch(() => null)
    throw new Error(typeof detail === 'string' ? detail : `Failed to save project: ${res.statusText}`)
  }
  return res.json() as Promise<ProjectInfo>
}

// ── Embedding & Collection registries (system router) ─────────────────────────

export async function fetchSystemEmbeddings(): Promise<Record<string, import('./types').EmbeddingEntry>> {
  const res = await fetch('/api/_system/embeddings', {
    headers: tok() ? { authorization: `Bearer ${tok()}` } : {},
  })
  if (!res.ok) throw new Error(`Failed to fetch embeddings registry: ${res.statusText}`)
  return res.json()
}

export async function fetchSystemCollections(): Promise<Record<string, import('./types').CollectionEntry>> {
  const res = await fetch('/api/_system/collections', {
    headers: tok() ? { authorization: `Bearer ${tok()}` } : {},
  })
  if (!res.ok) throw new Error(`Failed to fetch collections registry: ${res.statusText}`)
  return res.json()
}

// ── Artifacts ────────────────────────────────────────────────────────────────
// Versioned prose (prompt/steering/skill) and structured (guardrail/hook/mcp)
// assets. Workflow YAML references them as artifact://<name>[@version].

const _authH = (): Record<string, string> => (tok() ? { authorization: `Bearer ${tok()}` } : {})
const _jsonH = (): Record<string, string> => ({
  'content-type': 'application/json',
  ...(tok() ? { authorization: `Bearer ${tok()}` } : {}),
})

export async function listArtifacts(
  type?: import('./types').ArtifactType,
): Promise<import('./types').ArtifactSummary[]> {
  const qs = type ? `?type=${encodeURIComponent(type)}` : ''
  const res = await fetch(`/dev/artifacts${qs}`, { headers: _authH() })
  if (!res.ok) throw new Error(`Failed to list artifacts: ${res.statusText}`)
  const data = await res.json()
  return Array.isArray(data?.artifacts) ? data.artifacts : []
}

export async function getArtifact(
  name: string,
  version?: number,
): Promise<import('./types').ArtifactDetail> {
  const qs = version != null ? `?version=${version}` : ''
  const res = await fetch(`/dev/artifacts/${encodeURIComponent(name)}${qs}`, { headers: _authH() })
  if (!res.ok) throw new Error(`Failed to fetch artifact: ${res.statusText}`)
  return res.json() as Promise<import('./types').ArtifactDetail>
}

export async function createArtifact(body: {
  name: string
  type: 'prompt' | 'steering' | 'skill'
  content: string
  description?: string
  version?: number
}): Promise<{ name: string; version: number; path: string }> {
  const res = await fetch('/dev/artifacts', {
    method: 'POST',
    headers: _jsonH(),
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`Failed to save artifact: ${res.statusText}`)
  return res.json()
}

export async function deleteArtifact(name: string): Promise<void> {
  const res = await fetch(`/dev/artifacts/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    headers: _authH(),
  })
  if (!res.ok) throw new Error(`Failed to delete artifact: ${res.statusText}`)
}

// ── Agent Orchestrator: live runs + control ──────────────────────────────────
export interface AgentRun {
  run_id: string
  workflow: string
  step_id: string
  iteration: number
  tokens_used: number
  token_budget: number | null
  status: string
  paused: boolean
  started_at: number
}

export interface VectorStatus {
  ready: boolean
  pgvector_package: boolean
  reason: string | null
  datasource: string | null
}

export async function fetchVectorStatus(): Promise<VectorStatus> {
  const res = await fetch('/api/_system/vector-status', {
    headers: tok() ? { authorization: `Bearer ${tok()}` } : {},
  })
  if (!res.ok) throw new Error(`Failed to fetch vector status: ${res.statusText}`)
  return res.json()
}

export interface PgDatasourcesInfo {
  datasources: string[]
  current_vector_datasource: string
}

export async function fetchPgDatasources(): Promise<PgDatasourcesInfo> {
  const res = await fetch('/api/_system/pg-datasources', {
    headers: tok() ? { authorization: `Bearer ${tok()}` } : {},
  })
  if (!res.ok) throw new Error(`Failed to fetch PG datasources: ${res.statusText}`)
  return res.json()
}

export async function updateVectorDatasource(datasource: string): Promise<{ ok: boolean; restart_required: boolean }> {
  const res = await fetch('/api/_system/vector-datasource', {
    method: 'PUT',
    headers: {
      'content-type': 'application/json',
      ...(tok() ? { authorization: `Bearer ${tok()}` } : {}),
    },
    body: JSON.stringify({ datasource }),
  })
  if (!res.ok) throw new Error(`Failed to update vector datasource: ${res.statusText}`)
  return res.json()
}

/** Fetch all model versions grouped by name from the admin API. */
export async function fetchAdminModels(): Promise<AdminModelsResponse> {
  const res = await fetch('/admin/models', {
    headers: tok() ? { authorization: `Bearer ${tok()}` } : {},
  })
  if (!res.ok) throw new Error(`Failed to fetch admin models: ${res.statusText}`)
  return res.json() as Promise<AdminModelsResponse>
}

/** Toggle enabled/disabled for a specific model version. */
export async function toggleModelVersion(
  name: string,
  version: string,
): Promise<ModelVersionInfo> {
  const res = await fetch(`/admin/models/${encodeURIComponent(name)}/${encodeURIComponent(version)}/toggle`, {
    method: 'PATCH',
    headers: tok() ? { authorization: `Bearer ${tok()}` } : {},
  })
  if (!res.ok) throw new Error(`Failed to toggle model: ${res.statusText}`)
  return res.json() as Promise<ModelVersionInfo>
}

/** Fork a model version to a new version tag. */
export async function forkModelVersion(
  name: string,
  version: string,
  newVersion: string,
): Promise<ForkVersionResponse> {
  const res = await fetch(`/admin/models/${encodeURIComponent(name)}/${encodeURIComponent(version)}/fork`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      ...(tok() ? { authorization: `Bearer ${tok()}` } : {}),
    },
    body: JSON.stringify({ new_version: newVersion }),
  })
  if (!res.ok) throw new Error(`Failed to fork model: ${res.statusText}`)
  return res.json() as Promise<ForkVersionResponse>
}

/** Fetch the scope catalogue from the admin API. */
export async function fetchAdminScopes(): Promise<AdminScopesResponse> {
  const res = await fetch('/admin/scopes', {
    headers: tok() ? { authorization: `Bearer ${tok()}` } : {},
  })
  if (!res.ok) throw new Error(`Failed to fetch admin scopes: ${res.statusText}`)
  return res.json() as Promise<AdminScopesResponse>
}

// ── Tuvl Insight ─────────────────────────────────────────────────────────────

// ── Testing config / LLM-as-a-Judge ──────────────────────────────────────────

