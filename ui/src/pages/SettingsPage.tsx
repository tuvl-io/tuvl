import { useAdjustOnChange } from '../hooks/useAdjustOnChange'
import { useState, useEffect, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import { TopBar } from '../components/TopBar'
import { YamlPanel } from '../components/YamlPanel'
import { useResizablePanel } from '../hooks/useResizablePanel'
import { useDatasource, useSaveFile, useDeleteFile, useTelemetryConfig, useSaveTelemetryConfig, useSystemConfig, useSaveSystemConfig, useProject, useSaveProject } from '../queries'
import { useUIStore } from '../store'
import type { ProjectInfo } from '../api'
import { previewRegistration, registerProject, pollRegistration } from '../api'
import { DevTelemetrySettings } from '../settings/DevTelemetrySettings'
import { EnvironmentSettings } from '../settings/EnvironmentSettings'

// ── Setting sections registry — add future sections here ─────────────────────

interface SettingSection {
  id: string
  label: string
  category: string
  icon: React.ReactNode
}

const SECTIONS: SettingSection[] = [
  {
    id: 'project',
    label: 'Project',
    category: 'General',
    icon: (
      <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
        <path d="M2 6a2 2 0 012-2h4l2 2h6a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
      </svg>
    ),
  },
  {
    id: 'environment',
    label: 'Environment',
    category: 'General',
    icon: (
      <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
        <path fillRule="evenodd" d="M12.316 3.051a1 1 0 01.633 1.265l-4 12a1 1 0 11-1.898-.632l4-12a1 1 0 011.265-.633zM5.707 6.293a1 1 0 010 1.414L3.414 10l2.293 2.293a1 1 0 11-1.414 1.414l-3-3a1 1 0 010-1.414l3-3a1 1 0 011.414 0zm8.586 0a1 1 0 011.414 0l3 3a1 1 0 010 1.414l-3 3a1 1 0 11-1.414-1.414L16.586 10l-2.293-2.293a1 1 0 010-1.414z" clipRule="evenodd" />
      </svg>
    ),
  },
  {
    id: 'dev-telemetry',
    label: 'Developer telemetry',
    category: 'General',
    icon: (
      <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
        <path d="M3 4a1 1 0 011-1h12a1 1 0 011 1v2a1 1 0 01-1 1H4a1 1 0 01-1-1V4zM3 10a1 1 0 011-1h7a1 1 0 110 2H4a1 1 0 01-1-1zM3 15a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1z" />
      </svg>
    ),
  },
  {
    id: 'redis',
    label: 'Redis',
    category: 'Infrastructure',
    icon: (
      <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
        <path d="M2 5a1 1 0 011-1h3.5a1 1 0 010 2H3a1 1 0 01-1-1zM2 10a1 1 0 011-1h3.5a1 1 0 010 2H3a1 1 0 01-1-1zM2 15a1 1 0 011-1h3.5a1 1 0 010 2H3a1 1 0 01-1-1z" />
        <path fillRule="evenodd" d="M7 5a1 1 0 011-1h9a1 1 0 110 2H8a1 1 0 01-1-1zM7 10a1 1 0 011-1h9a1 1 0 110 2H8a1 1 0 01-1-1zM7 15a1 1 0 011-1h9a1 1 0 110 2H8a1 1 0 01-1-1z" clipRule="evenodd" />
      </svg>
    ),
  },
  {
    id: 'telemetry',
    label: 'Telemetry',
    category: 'Observability',
    icon: (
      <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
        <path fillRule="evenodd" d="M3 3a1 1 0 000 2v8a2 2 0 002 2h2.586l-1.293 1.293a1 1 0 101.414 1.414L10 15.414l2.293 2.293a1 1 0 001.414-1.414L12.414 15H15a2 2 0 002-2V5a1 1 0 100-2H3zm11 4a1 1 0 10-2 0v4a1 1 0 102 0V7zm-3 1a1 1 0 10-2 0v3a1 1 0 102 0V8zM8 9a1 1 0 00-2 0v2a1 1 0 102 0V9z" clipRule="evenodd" />
      </svg>
    ),
  },
  {
    id: 'api-access',
    label: 'API Access',
    category: 'Security',
    icon: (
      <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
        <path fillRule="evenodd" d="M10 1a4.5 4.5 0 00-4.5 4.5V9H5a2 2 0 00-2 2v6a2 2 0 002 2h10a2 2 0 002-2v-6a2 2 0 00-2-2h-.5V5.5A4.5 4.5 0 0010 1zm3 8V5.5a3 3 0 10-6 0V9h6z" clipRule="evenodd" />
      </svg>
    ),
  },
]

// ── Redis datasource YAML file name ──────────────────────────────────────────

const REDIS_FILE = 'redis-primary.yaml'

// ── Form state ────────────────────────────────────────────────────────────────

interface RedisFormState {
  enabled: boolean
  host: string
  port: string
  db: string
  password: string
}

const DEFAULT_REDIS: RedisFormState = {
  enabled: true,
  host: '${REDIS_HOST:localhost}',
  port: '${REDIS_PORT:6379}',
  db: '0',
  password: '${REDIS_PASSWORD:}',
}

function buildRedisYaml(form: RedisFormState): object {
  const port = parseInt(form.port, 10)
  const db = parseInt(form.db, 10)
  return {
    kind: 'RedisConfig',
    version: 'v1',
    enabled: form.enabled,
    metadata: { name: 'redis-primary' },
    spec: {
      type: 'redis',
      connection: {
        host: form.host.trim() || 'localhost',
        port: isNaN(port) ? 6379 : port,
        db: isNaN(db) ? 0 : db,
        ...(form.password.trim() ? { password: form.password.trim() } : {}),
      },
    },
  }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function yamlToRedisForm(parsed: any): RedisFormState {
  const conn = parsed?.spec?.connection ?? {}
  return {
    enabled: parsed?.enabled !== false,
    host: String(conn.host ?? 'localhost'),
    port: String(conn.port ?? 6379),
    db: String(conn.db ?? 0),
    password: '', // never pre-fill password from YAML
  }
}

// ── Shared field components ───────────────────────────────────────────────────

const inputCls =
  'w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 outline-none font-mono focus:border-sky-500 focus:ring-1 focus:ring-sky-500/20 transition-colors'

function Field({
  label,
  id,
  value,
  onChange,
  type = 'text',
  placeholder,
  hint,
}: {
  label: string
  id: string
  value: string
  onChange: (v: string) => void
  type?: string
  placeholder?: string
  hint?: string
}) {
  const [show, setShow] = useState(false)
  const inputType = type === 'password' ? (show ? 'text' : 'password') : type
  return (
    <div>
      <label htmlFor={id} className="block text-xs font-medium text-slate-400 mb-1.5">
        {label}
      </label>
      <div className="relative">
        <input
          id={id}
          type={inputType}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          autoComplete="off"
          spellCheck={false}
          className={inputCls}
        />
        {type === 'password' && (
          <button
            type="button"
            onClick={() => setShow((s) => !s)}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300 text-xs"
          >
            {show ? 'hide' : 'show'}
          </button>
        )}
      </div>
      {hint && <p className="mt-1 text-xs text-slate-600">{hint}</p>}
    </div>
  )
}

const ENV_VAR_RE = /^\$\{[^}]+\}$/

function EnvOrValueInput({
  id,
  label,
  value,
  onChange,
  defaultEnvKey,
  hint,
}: {
  id: string
  label: string
  value: string
  onChange: (v: string) => void
  defaultEnvKey: string
  hint?: string
}) {
  const [useEnv, setUseEnv] = useState(() => ENV_VAR_RE.test(value) || value === '')
  const isLight = useUIStore((s) => s.theme) === 'light'

  const handleToggle = () => {
    const next = !useEnv
    setUseEnv(next)
    onChange(next ? `\${${defaultEnvKey}:}` : '')
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <label htmlFor={id} className="text-xs font-medium text-slate-400">
          {label} <span className="text-slate-600">(optional)</span>
        </label>
        <button
          type="button"
          onClick={handleToggle}
          className={`text-xs px-2 py-0.5 rounded border transition-colors ${
            useEnv
              ? isLight
                ? 'border-emerald-500 text-emerald-700 bg-emerald-50'
                : 'border-emerald-700 text-emerald-400 bg-emerald-900/20'
              : 'border-slate-700 text-slate-500 hover:text-slate-300 hover:border-slate-600'
          }`}
        >
          {useEnv ? '✓ env var' : 'use env var'}
        </button>
      </div>
      <input
        id={id}
        type={useEnv ? 'text' : 'password'}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={useEnv ? `\${${defaultEnvKey}:}` : 'leave blank for no auth'}
        autoComplete="off"
        spellCheck={false}
        className={`${inputCls} ${useEnv ? (isLight ? 'border-emerald-400 text-emerald-300' : 'border-emerald-800 text-emerald-300') : ''}`}
      />
      {hint && <p className="mt-1 text-xs text-slate-600">{hint}</p>}
    </div>
  )
}

// ── Redis settings panel ──────────────────────────────────────────────────────

function RedisSettings() {
  const [form, setForm] = useState<RedisFormState>(DEFAULT_REDIS)
  const [advanced, setAdvanced] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [deleted, setDeleted] = useState(false)

  const { data: existing, isLoading, isError } = useDatasource(REDIS_FILE)
  const saveMut = useSaveFile()
  const deleteMut = useDeleteFile()

  const { width: panelWidth, handleMouseDown } = useResizablePanel(340, 260, 600)

  // Populate form when existing config is loaded
  useAdjustOnChange([existing], () => {
    if (existing) {
      setForm(yamlToRedisForm(existing))
      setDeleted(false)
    }
  })

  const yamlObj = buildRedisYaml(form)

  const handleSave = async () => {
    setSaveError(null)
    try {
      await saveMut.mutateAsync({ kind: 'datasources', name: REDIS_FILE, body: yamlObj })
      setDeleted(false)
    } catch (e: unknown) {
      setSaveError(e instanceof Error ? e.message : 'Save failed')
    }
  }

  const handleDelete = async () => {
    if (!confirm('Remove Redis configuration? This will clear the saved datasource file.')) return
    await deleteMut.mutateAsync({ kind: 'datasources', name: REDIS_FILE })
    setForm(DEFAULT_REDIS)
    setDeleted(true)
  }

  const isConfigured = !isError && !deleted && !!existing

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {/* Section header */}
      <div className="px-6 py-5 border-b border-slate-800 flex-shrink-0">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-slate-100">Redis</h2>
            <p className="mt-1 text-xs text-slate-500 leading-relaxed max-w-lg">
              Configure a Redis connection for multi-worker deployments. Required for shared OAuth
              state and token revocation across multiple processes.
            </p>
          </div>
          <div className="shrink-0 flex items-center gap-2 mt-0.5">
            <span
              className={`flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full border ${
                isLoading
                  ? 'border-slate-700 text-slate-500'
                  : isConfigured
                  ? 'border-emerald-700/50 bg-emerald-900/20 text-emerald-400'
                  : 'border-slate-700/50 bg-slate-800/40 text-slate-500'
              }`}
            >
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  isLoading ? 'bg-slate-600' : isConfigured ? 'bg-emerald-400' : 'bg-slate-600'
                }`}
              />
              {isLoading ? 'Loading…' : isConfigured ? 'Configured' : 'Not configured'}
            </span>
          </div>
        </div>
      </div>

      {/* Body: form + yaml panel */}
      <div className="flex flex-1 min-h-0">
        {/* Form */}
        <div className="flex-1 min-w-0 overflow-y-auto">
          <form
            onSubmit={(e) => { e.preventDefault(); void handleSave() }}
            className="px-6 py-6 flex flex-col gap-5 max-w-xl"
          >
            {/* Enabled toggle */}
            <div className="flex items-center justify-between py-3 px-4 rounded-lg border border-slate-800 bg-slate-900/40">
              <div>
                <p className="text-sm font-medium text-slate-200">Enabled</p>
                <p className="text-xs text-slate-600 mt-0.5">
                  Disable to keep the file but skip connection at startup.
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={form.enabled}
                onClick={() => setForm((f) => ({ ...f, enabled: !f.enabled }))}
                className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none ${
                  form.enabled ? 'bg-sky-600' : 'bg-slate-700'
                }`}
              >
                <span
                  className={`pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow-lg transition-transform ${
                    form.enabled ? 'translate-x-4' : 'translate-x-0'
                  }`}
                />
              </button>
            </div>

            {/* Connection fields */}
            <div className="space-y-4">
              <p className="text-xs font-semibold text-slate-500 uppercase tracking-widest">
                Connection
              </p>
              <Field
                label="Host"
                id="redis-host"
                value={form.host}
                onChange={(v) => setForm((f) => ({ ...f, host: v }))}
                placeholder="${REDIS_HOST:localhost}"
                hint="Supports ${ENV_VAR:default} syntax."
              />
              <div className="grid grid-cols-2 gap-4">
                <Field
                  label="Port"
                  id="redis-port"
                  value={form.port}
                  onChange={(v) => setForm((f) => ({ ...f, port: v }))}
                  placeholder="6379"
                />
                <Field
                  label="Database index"
                  id="redis-db"
                  value={form.db}
                  onChange={(v) => setForm((f) => ({ ...f, db: v }))}
                  placeholder="0"
                  hint="0–15"
                />
              </div>
              <EnvOrValueInput
                id="redis-password"
                label="Password"
                value={form.password}
                onChange={(v) => setForm((f) => ({ ...f, password: v }))}
                defaultEnvKey="REDIS_PASSWORD"
                hint="Leave blank for no authentication (no AUTH command sent)."
              />
            </div>

            {/* Actions */}
            <div className="flex gap-3 pt-2">
              <button
                type="submit"
                disabled={saveMut.isPending}
                className="flex-1 px-4 py-2 rounded-lg bg-sky-600 text-sm font-semibold text-white hover:bg-sky-500 disabled:opacity-40 transition-colors"
              >
                {saveMut.isPending ? 'Saving…' : 'Save'}
              </button>
              {isConfigured && (
                <button
                  type="button"
                  onClick={() => void handleDelete()}
                  disabled={deleteMut.isPending}
                  className="px-4 py-2 rounded-lg border border-rose-800/50 text-sm text-rose-400 hover:bg-rose-900/20 disabled:opacity-40 transition-colors"
                >
                  Remove
                </button>
              )}
            </div>

            {saveError && (
              <p className="text-xs text-rose-400 font-mono">{saveError}</p>
            )}
          </form>

          {/* Scaling note */}
          <div className="mx-6 mb-6 p-4 rounded-lg border border-amber-800/30 bg-amber-900/10">
            <p className="text-xs font-semibold text-amber-500 mb-1.5">
              Required for multi-worker deployments
            </p>
            <p className="text-xs text-slate-500 leading-relaxed">
              Without Redis, OAuth CSRF state and token revocation lists are stored only in each
              worker's memory. Any request routed to a different worker will fail the state check or
              accept a revoked token. Configure Redis before scaling beyond a single process.
            </p>
          </div>
        </div>

        {/* Resizable YAML panel */}
        <div className="shrink-0 relative border-l border-slate-800 flex flex-col" style={{ width: panelWidth }}>
          <div
            onMouseDown={handleMouseDown}
            className="absolute left-0 top-0 h-full w-1 cursor-col-resize hover:bg-sky-700/30 z-10"
          />
          <YamlPanel
            className="w-full bg-slate-950/50"
            obj={yamlObj}
            advanced={advanced}
            onAdvancedChange={setAdvanced}
            onParsed={(p) => setForm(yamlToRedisForm(p as Record<string, unknown>))}
            onSave={handleSave}
            savePending={saveMut.isPending}
            saveError={saveError}
          />
        </div>
      </div>
    </div>
  )
}

// ── Telemetry settings panel ──────────────────────────────────────────────────

interface TelemetryFormState {
  enabled: boolean
  otlp_endpoint: string
  otlp_protocol: string
  service_name: string
}

const DEFAULT_TELEMETRY: TelemetryFormState = {
  enabled: true,
  otlp_endpoint: 'http://localhost:4318',
  otlp_protocol: 'http/protobuf',
  service_name: 'tuvl',
}

function buildTelemetryYaml(form: TelemetryFormState): object {
  return {
    kind: 'TelemetryConfig',
    version: 'v1',
    enabled: form.enabled,
    metadata: { name: 'default' },
    spec: {
      otlp_endpoint: form.otlp_endpoint.trim() || 'http://localhost:4318',
      otlp_protocol: form.otlp_protocol || 'http/protobuf',
      service_name: form.service_name.trim() || 'tuvl',
    },
  }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function yamlToTelemetryForm(parsed: any): TelemetryFormState {
  // Support both new format (enabled at root) and legacy (enabled in spec)
  const src = parsed?.spec ?? {}
  return {
    enabled: (parsed?.enabled ?? src.enabled) !== false,
    otlp_endpoint: String(src.otlp_endpoint ?? 'http://localhost:4318'),
    otlp_protocol: String(src.otlp_protocol ?? 'http/protobuf'),
    service_name: String(src.service_name ?? 'tuvl'),
  }
}

function TelemetrySettings() {
  const [form, setForm] = useState<TelemetryFormState>(DEFAULT_TELEMETRY)
  const [advanced, setAdvanced] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const { data: existing, isLoading } = useTelemetryConfig()
  const saveMut = useSaveTelemetryConfig()

  const { width: panelWidth, handleMouseDown } = useResizablePanel(340, 260, 600)

  useAdjustOnChange([existing], () => {
    if (existing) {
      setForm({
        enabled: existing.enabled ?? true,
        otlp_endpoint: existing.otlp_endpoint ?? DEFAULT_TELEMETRY.otlp_endpoint,
        otlp_protocol: existing.otlp_protocol ?? DEFAULT_TELEMETRY.otlp_protocol,
        service_name: existing.service_name ?? DEFAULT_TELEMETRY.service_name,
      })
    }
  })

  const handleSave = async () => {
    setSaveError(null)
    setSaved(false)
    try {
      await saveMut.mutateAsync(form)
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch (e: unknown) {
      setSaveError(e instanceof Error ? e.message : 'Save failed')
    }
  }

  const isConfigured = !!existing

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {/* Section header */}
      <div className="px-6 py-5 border-b border-slate-800 flex-shrink-0">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-slate-100">Telemetry</h2>
            <p className="mt-1 text-xs text-slate-500 leading-relaxed max-w-lg">
              Configure OpenTelemetry span export. Spans are sent to your OTLP collector
              (Jaeger, Grafana Tempo, etc.) in production mode. Changes take effect after
              the engine restarts.
            </p>
          </div>
          <div className="shrink-0 flex items-center gap-2 mt-0.5">
            <span
              className={`flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full border ${
                isLoading
                  ? 'border-slate-700 text-slate-500'
                  : isConfigured
                  ? 'border-emerald-700/50 bg-emerald-900/20 text-emerald-400'
                  : 'border-slate-700/50 bg-slate-800/40 text-slate-500'
              }`}
            >
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  isLoading ? 'bg-slate-600' : isConfigured ? 'bg-emerald-400' : 'bg-slate-600'
                }`}
              />
              {isLoading ? 'Loading…' : isConfigured ? 'Configured' : 'Using defaults'}
            </span>
          </div>
        </div>
      </div>

      {/* Body */}
      <div className="flex flex-1 min-h-0">
        {/* Form */}
        <div className="flex-1 min-w-0 overflow-y-auto">
          <form
            onSubmit={(e) => { e.preventDefault(); void handleSave() }}
            className="px-6 py-6 flex flex-col gap-5 max-w-xl"
          >
            {/* Enabled toggle */}
            <div className="flex items-center justify-between py-3 px-4 rounded-lg border border-slate-800 bg-slate-900/40">
              <div>
                <p className="text-sm font-medium text-slate-200">Enabled</p>
                <p className="text-xs text-slate-600 mt-0.5">
                  Disable to suppress span export while keeping prod mode active.
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={form.enabled}
                onClick={() => setForm((f) => ({ ...f, enabled: !f.enabled }))}
                className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none ${
                  form.enabled ? 'bg-sky-600' : 'bg-slate-700'
                }`}
              >
                <span
                  className={`pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow-lg transition-transform ${
                    form.enabled ? 'translate-x-4' : 'translate-x-0'
                  }`}
                />
              </button>
            </div>

            {/* Collector fields */}
            <div className="space-y-4">
              <p className="text-xs font-semibold text-slate-500 uppercase tracking-widest">
                Collector
              </p>
              <Field
                label="OTLP Endpoint"
                id="otel-endpoint"
                value={form.otlp_endpoint}
                onChange={(v) => setForm((f) => ({ ...f, otlp_endpoint: v }))}
                placeholder="http://localhost:4318"
                hint="OTLP endpoint of your collector (4318 for HTTP, 4317 for gRPC). Supports ${ENV_VAR:default} syntax."
              />
              <div>
                <label htmlFor="otel-protocol" className="block text-xs font-medium text-slate-400 mb-1.5">
                  Protocol
                </label>
                <select
                  id="otel-protocol"
                  value={form.otlp_protocol}
                  onChange={(e) => setForm((f) => ({ ...f, otlp_protocol: e.target.value }))}
                  className={inputCls}
                >
                  <option value="http/protobuf">http/protobuf</option>
                  <option value="grpc">grpc (needs tuvl[otel-grpc])</option>
                </select>
              </div>
              <Field
                label="Service Name"
                id="otel-service"
                value={form.service_name}
                onChange={(v) => setForm((f) => ({ ...f, service_name: v }))}
                placeholder="tuvl"
                hint="Attached to every span as service.name. Useful when multiple instances share a collector."
              />
            </div>

            {/* Actions */}
            <div className="flex gap-3 pt-2">
              <button
                type="submit"
                disabled={saveMut.isPending}
                className="flex-1 px-4 py-2 rounded-lg bg-sky-600 text-sm font-semibold text-white hover:bg-sky-500 disabled:opacity-40 transition-colors"
              >
                {saveMut.isPending ? 'Saving…' : saved ? '✓ Saved' : 'Save'}
              </button>
            </div>

            {saveError && (
              <p className="text-xs text-rose-400 font-mono">{saveError}</p>
            )}
          </form>

          {/* Restart notice */}
          <div className="mx-6 mb-6 p-4 rounded-lg border border-amber-800/30 bg-amber-900/10">
            <p className="text-xs font-semibold text-amber-500 mb-1.5">
              Restart required to apply changes
            </p>
            <p className="text-xs text-slate-500 leading-relaxed">
              Telemetry is initialised once at startup. Save your config here, then restart
              the engine with <code className="font-mono text-slate-400">tuvl run</code> for
              changes to take effect.
              <br className="mb-1" />
              Environment variables (<code className="font-mono text-slate-400">TUVL_TELEMETRY_ENABLED</code>,{' '}
              <code className="font-mono text-slate-400">TUVL_OTLP_ENDPOINT</code>,{' '}
              <code className="font-mono text-slate-400">TUVL_OTLP_PROTOCOL</code>,{' '}
              <code className="font-mono text-slate-400">TUVL_SERVICE_NAME</code>) override these values
              when set.
            </p>
          </div>
        </div>

        {/* Resizable YAML panel */}
        <div className="shrink-0 relative border-l border-slate-800 flex flex-col" style={{ width: panelWidth }}>
          <div
            onMouseDown={handleMouseDown}
            className="absolute left-0 top-0 h-full w-1 cursor-col-resize hover:bg-sky-700/30 z-10"
          />
          <YamlPanel
            className="w-full bg-slate-950/50"
            obj={buildTelemetryYaml(form)}
            advanced={advanced}
            onAdvancedChange={setAdvanced}
            onParsed={(p) => setForm(yamlToTelemetryForm(p as Record<string, unknown>))}
            onSave={handleSave}
            savePending={saveMut.isPending}
            saveError={saveError}
          />
        </div>
      </div>
    </div>
  )
}

// ── Project identity panel ────────────────────────────────────────────────────

const PROJECT_TYPES = [
  { value: 'poc', label: 'PoC' },
  { value: 'dev', label: 'Development' },
  { value: 'production', label: 'Production' },
] as const

const FIELD_CLS =
  'w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-sky-600'

function ProjectSettings() {
  const { data, isLoading } = useProject()
  if (isLoading) return <div className="flex-1" />
  return <ProjectForm key={data?.id ?? 'new'} data={data ?? null} />
}

function ProjectForm({ data }: { data: ProjectInfo | null }) {
  const saveMut = useSaveProject()
  const [form, setForm] = useState({
    name: data?.name ?? '',
    author: data?.author ?? '',
    email: data?.email ?? '',
    organization: data?.organization ?? '',
    organization_url: data?.organization_url ?? '',
    type: data?.type ?? 'dev',
    version: data?.version ?? '0.1.0',
    description: data?.description ?? '',
    license: data?.license ?? '',
    repo: data?.repo ?? '',
  })
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [preview, setPreview] = useState(false)

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }))

  const save = async () => {
    setError(null)
    setSaved(false)
    try {
      await saveMut.mutateAsync(form as Parameters<typeof saveMut.mutateAsync>[0])
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Save failed')
    }
  }

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <div className="px-6 py-5 border-b border-slate-800 flex-shrink-0">
        <h2 className="text-base font-semibold text-slate-100">Project</h2>
        <p className="mt-1 text-xs text-slate-500 leading-relaxed max-w-lg">
          Who built this project and what it is. The id is created when the project is initialised and never changes.
        </p>
      </div>
      <div className="flex-1 min-w-0 overflow-y-auto">
        <form onSubmit={(e) => { e.preventDefault(); void save() }} className="px-6 py-6 flex flex-col gap-4 max-w-xl">
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-slate-400">Project id</span>
            <input readOnly value={data?.id ?? 'Not set — saving creates one'} className={`${FIELD_CLS} font-mono text-xs text-slate-500`} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-slate-400">Name</span>
            <input value={form.name} onChange={set('name')} className={FIELD_CLS} />
          </label>
          <div className="grid grid-cols-2 gap-4">
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-400">Author</span>
              <input value={form.author} onChange={set('author')} className={FIELD_CLS} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-400">Developer email</span>
              <input type="email" value={form.email} onChange={set('email')} className={FIELD_CLS} />
            </label>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-400">Organization</span>
              <input value={form.organization} onChange={set('organization')} className={FIELD_CLS} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-400">Organization URL (optional)</span>
              <input type="url" value={form.organization_url} onChange={set('organization_url')} placeholder="https://" className={FIELD_CLS} />
            </label>
          </div>
          <span className="text-[11px] text-slate-500 -mt-2">Shown in the catalogue. The author and email are kept private.</span>
          <div className="grid grid-cols-2 gap-4">
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-400">Type</span>
              <select value={form.type} onChange={set('type')} className={FIELD_CLS}>
                {PROJECT_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-400">Version</span>
              <input value={form.version} onChange={set('version')} placeholder="0.1.0" className={FIELD_CLS} />
            </label>
          </div>
          <div className="flex flex-col gap-1">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-slate-400">Description <span className="text-slate-600">· Markdown</span></span>
              <button
                type="button"
                onClick={() => setPreview((v) => !v)}
                className="text-xs text-sky-400 hover:underline"
              >
                {preview ? 'Edit' : 'Preview'}
              </button>
            </div>
            {preview ? (
              <div className="min-h-[7rem] rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300 leading-relaxed [&_h1]:text-base [&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_a]:text-sky-400 [&_a]:underline [&_code]:font-mono [&_code]:text-xs [&_code]:bg-slate-800 [&_code]:px-1 [&_pre]:overflow-x-auto [&_p]:my-2">
                {form.description.trim() ? (
                  <ReactMarkdown>{form.description}</ReactMarkdown>
                ) : (
                  <span className="text-slate-600">Nothing to preview.</span>
                )}
              </div>
            ) : (
              <textarea rows={6} maxLength={2000} value={form.description} onChange={set('description')} placeholder="What it does, who it is for… (Markdown)" className={`${FIELD_CLS} font-mono text-xs`} />
            )}
          </div>
          <div className="grid grid-cols-2 gap-4">
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-400">License</span>
              <input value={form.license} onChange={set('license')} placeholder="Apache-2.0" className={FIELD_CLS} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-400">Repository</span>
              <input value={form.repo} onChange={set('repo')} placeholder="https://github.com/…" className={FIELD_CLS} />
            </label>
          </div>
          <button
            type="submit"
            disabled={saveMut.isPending}
            className="px-4 py-2 rounded-lg bg-sky-600 text-sm font-semibold text-white hover:bg-sky-500 disabled:opacity-40 transition-colors"
          >
            {saveMut.isPending ? 'Saving…' : saved ? '✓ Saved' : 'Save'}
          </button>
          {error && <p className="text-xs text-rose-400 font-mono">{error}</p>}
        </form>
        <RegistryCard data={data} dirty={saveMut.isPending} />
      </div>
    </div>
  )
}

const REGISTRY_STATE: Record<string, { label: string; cls: string }> = {
  unregistered: { label: 'Not listed in the public catalogue.', cls: 'text-slate-500' },
  current: { label: 'Listed and up to date.', cls: 'text-emerald-400' },
  outdated: { label: 'Changed since the last registration.', cls: 'text-amber-400' },
  id_changed: { label: 'The project id differs from the one registered.', cls: 'text-amber-400' },
  invalid_id: {
    label: 'The project id is malformed — it was edited by hand. Run tuvl project init --new-id.',
    cls: 'text-rose-400',
  },
}

function RegistryCard({ data, dirty }: { data: ProjectInfo | null; dirty: boolean }) {
  const [open, setOpen] = useState(false)
  const reg = data?.registry
  const state = REGISTRY_STATE[data?.registration_state ?? 'unregistered']
  const canRegister = !!data && data.registration_state !== 'invalid_id' && !dirty
  const listed = data?.registration_state === 'current' || data?.registration_state === 'outdated'
  return (
    <div className="mx-6 mb-6 p-4 rounded-lg border border-slate-800 bg-slate-900/40 max-w-xl">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-xs font-semibold text-slate-300 mb-1">Catalogue</p>
          <p className={`text-xs leading-relaxed ${state.cls}`}>{state.label}</p>
          {reg?.registered_at && (
            <p className="mt-1 text-xs text-slate-500 font-mono break-all">
              {reg.url} · {reg.registered_at}
            </p>
          )}
        </div>
        <button
          type="button"
          disabled={!canRegister}
          onClick={() => setOpen(true)}
          className="flex-shrink-0 px-3 py-1.5 rounded-lg border border-slate-700 text-xs font-semibold text-slate-200 hover:border-sky-600 disabled:opacity-40 transition-colors"
        >
          {listed ? 'Update listing' : 'Register'}
        </button>
      </div>
      {open && <RegisterDialog onClose={() => setOpen(false)} />}
    </div>
  )
}

type RegisterPhase =
  | { kind: 'review' }
  | { kind: 'sending' }
  | { kind: 'login'; handle: string; code: string; uri: string; interval: number }
  | { kind: 'done' }
  | { kind: 'error'; message: string }

function RegisterDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [phase, setPhase] = useState<RegisterPhase>({ kind: 'review' })
  const preview = useQuery({
    queryKey: ['project-register-preview'],
    queryFn: () => previewRegistration({}),
  })

  const finish = useCallback(() => {
    setPhase({ kind: 'done' })
    void qc.invalidateQueries({ queryKey: ['project'] })
  }, [qc])

  const send = async () => {
    setPhase({ kind: 'sending' })
    try {
      const step = await registerProject({})
      if (step.status === 'registered') finish()
      else if (step.status === 'login_required')
        setPhase({
          kind: 'login',
          handle: step.handle,
          code: step.user_code,
          uri: step.verification_uri,
          interval: Math.max(1, step.interval),
        })
    } catch (e: unknown) {
      setPhase({ kind: 'error', message: e instanceof Error ? e.message : 'Registration failed' })
    }
  }

  const handle = phase.kind === 'login' ? phase.handle : null
  const interval = phase.kind === 'login' ? phase.interval : 0
  useEffect(() => {
    if (!handle) return
    let stopped = false
    let wait = interval
    const tick = async () => {
      try {
        const step = await pollRegistration({ handle })
        if (stopped) return
        if (step.status === 'registered') return finish()
        if (step.status === 'pending' && step.slow_down) wait += 5
        setTimeout(tick, wait * 1000)
      } catch (e: unknown) {
        if (!stopped) setPhase({ kind: 'error', message: e instanceof Error ? e.message : 'Login failed' })
      }
    }
    const timer = setTimeout(tick, wait * 1000)
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [handle, interval, finish])

  const payload = preview.data?.payload
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="dialog" aria-modal="true">
      <div className="w-full max-w-lg rounded-xl border border-slate-700 bg-slate-950 p-5 flex flex-col gap-4">
        <h3 className="text-sm font-semibold text-slate-100">Register with the catalogue</h3>

        {(phase.kind === 'review' || phase.kind === 'sending') && (
          <>
            <p className="text-xs text-slate-500 leading-relaxed">
              This is exactly what is sent to {preview.data?.nest ?? 'the Nest'}. Nothing else leaves this machine.
            </p>
            <pre className="max-h-56 overflow-auto rounded-lg border border-slate-800 bg-slate-900 p-3 text-[11px] text-slate-300 font-mono">
              {payload ? JSON.stringify(payload, null, 2) : 'Loading…'}
            </pre>
            <p className="text-xs text-slate-500 leading-relaxed">
              The catalogue lists the name, version, organization, type, licence, description and the optional repository link. The author and email are
              kept by the Nest for contact and are never shown.
            </p>
            <div className="flex justify-end gap-2">
              <button type="button" onClick={onClose} className="px-3 py-1.5 rounded-lg text-xs text-slate-400 hover:text-slate-200">
                Cancel
              </button>
              <button
                type="button"
                disabled={!payload || phase.kind === 'sending'}
                onClick={() => void send()}
                className="px-4 py-1.5 rounded-lg bg-sky-600 text-xs font-semibold text-white hover:bg-sky-500 disabled:opacity-40"
              >
                {phase.kind === 'sending' ? 'Sending…' : 'Send'}
              </button>
            </div>
          </>
        )}

        {phase.kind === 'login' && (
          <>
            <p className="text-xs text-slate-400 leading-relaxed">
              Sign in with GitHub to claim this project. Open the page below and enter the code.
            </p>
            <p className="text-center font-mono text-2xl tracking-widest text-slate-100">{phase.code}</p>
            <a href={phase.uri} target="_blank" rel="noreferrer" className="text-center text-xs text-sky-400 hover:underline break-all">
              {phase.uri}
            </a>
            <p className="text-center text-[11px] text-slate-500">Waiting for approval…</p>
            <div className="flex justify-end">
              <button type="button" onClick={onClose} className="px-3 py-1.5 rounded-lg text-xs text-slate-400 hover:text-slate-200">
                Cancel
              </button>
            </div>
          </>
        )}

        {phase.kind === 'done' && (
          <>
            <p className="text-xs text-emerald-400">Registered. Your project is listed in the catalogue.</p>
            <div className="flex justify-end">
              <button type="button" onClick={onClose} className="px-4 py-1.5 rounded-lg bg-sky-600 text-xs font-semibold text-white hover:bg-sky-500">
                Done
              </button>
            </div>
          </>
        )}

        {phase.kind === 'error' && (
          <>
            <p className="text-xs text-rose-400 font-mono break-words">{phase.message}</p>
            <div className="flex justify-end gap-2">
              <button type="button" onClick={onClose} className="px-3 py-1.5 rounded-lg text-xs text-slate-400 hover:text-slate-200">
                Close
              </button>
              <button
                type="button"
                onClick={() => setPhase({ kind: 'review' })}
                className="px-4 py-1.5 rounded-lg bg-sky-600 text-xs font-semibold text-white hover:bg-sky-500"
              >
                Try again
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

// ── API Access settings panel ─────────────────────────────────────────────────

interface ApiAccessFormState {
  expose_model_crud: boolean
  dev_expose_all: boolean
  /** groups beyond auth and runs that a production server also mounts */
  extra: string[]
  docs: string
}

const DEFAULT_API_ACCESS: ApiAccessFormState = {
  expose_model_crud: true,
  dev_expose_all: true,
  extra: [],
  docs: '',
}

const PRODUCTION_BASE = ['auth', 'runs']
const OPTIONAL_GROUPS: { key: string; label: string; hint: string }[] = [
  { key: 'iam', label: 'iam', hint: '/auth/bootstrap, /auth/admin/* — users, roles, federation; production uses `tuvl iam` instead' },
  { key: 'admin', label: 'admin', hint: '/admin/* — engine administration' },
  { key: 'artifacts', label: 'artifacts', hint: '/api/artifacts/* — artifact list and upload' },
  { key: 'system', label: 'system', hint: '/api/_system/* — introspection; only Insight uses it' },
]

function ApiAccessSettings() {
  const [form, setForm] = useState<ApiAccessFormState>(DEFAULT_API_ACCESS)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const { data: existing, isLoading } = useSystemConfig()
  const saveMut = useSaveSystemConfig()

  useAdjustOnChange([existing], () => {
    setForm({
      expose_model_crud: existing?.api?.expose_model_crud ?? DEFAULT_API_ACCESS.expose_model_crud,
      dev_expose_all: existing?.api?.dev_expose_all ?? DEFAULT_API_ACCESS.dev_expose_all,
      extra: (existing?.api?.expose ?? []).filter((g) => !PRODUCTION_BASE.includes(g)),
      docs: existing?.api?.docs ?? DEFAULT_API_ACCESS.docs,
    })
  })

  const handleSave = async () => {
    setSaveError(null)
    setSaved(false)
    try {
      await saveMut.mutateAsync({
        api: {
          expose_model_crud: form.expose_model_crud,
          dev_expose_all: form.dev_expose_all,
          expose: form.extra.length ? [...PRODUCTION_BASE, ...form.extra] : null,
          docs: form.docs || null,
        },
      })
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch (e: unknown) {
      setSaveError(e instanceof Error ? e.message : 'Save failed')
    }
  }

  const isConfigured = !!existing

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {/* Section header */}
      <div className="px-6 py-5 border-b border-slate-800 flex-shrink-0">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-slate-100">API Access</h2>
            <p className="mt-1 text-xs text-slate-500 leading-relaxed max-w-lg">
              Control which HTTP API surfaces are mounted. Changes take effect after
              the engine restarts.
            </p>
          </div>
          <div className="shrink-0 flex items-center gap-2 mt-0.5">
            <span
              className={`flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full border ${
                isLoading
                  ? 'border-slate-700 text-slate-500'
                  : isConfigured
                  ? 'border-emerald-700/50 bg-emerald-900/20 text-emerald-400'
                  : 'border-slate-700/50 bg-slate-800/40 text-slate-500'
              }`}
            >
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  isLoading ? 'bg-slate-600' : isConfigured ? 'bg-emerald-400' : 'bg-slate-600'
                }`}
              />
              {isLoading ? 'Loading…' : isConfigured ? 'Configured' : 'Using defaults'}
            </span>
          </div>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 min-w-0 overflow-y-auto">
        <form
          onSubmit={(e) => { e.preventDefault(); void handleSave() }}
          className="px-6 py-6 flex flex-col gap-5 max-w-xl"
        >
          {/* Expose model CRUD toggle */}
          <div className="flex items-center justify-between py-3 px-4 rounded-lg border border-slate-800 bg-slate-900/40">
            <div>
              <p className="text-sm font-medium text-slate-200">Expose model CRUD APIs</p>
              <p className="text-xs text-slate-600 mt-0.5">
                When off, only workflow APIs are mounted — /models/* CRUD endpoints return 404.
                Applies to production too; the TUVL_EXPOSE_MODEL_CRUD env var overrides this file.
              </p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={form.expose_model_crud}
              onClick={() => setForm((f) => ({ ...f, expose_model_crud: !f.expose_model_crud }))}
              className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none ${
                form.expose_model_crud ? 'bg-sky-600' : 'bg-slate-700'
              }`}
            >
              <span
                className={`pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow-lg transition-transform ${
                  form.expose_model_crud ? 'translate-x-4' : 'translate-x-0'
                }`}
              />
            </button>
          </div>

          {/* Dev server mounts everything */}
          <div className="flex items-center justify-between py-3 px-4 rounded-lg border border-slate-800 bg-slate-900/40">
            <div>
              <p className="text-sm font-medium text-slate-200">Dev server mounts every system API</p>
              <p className="text-xs text-slate-600 mt-0.5">
                On: Insight gets all of /auth, /admin, /api/_system, /api/artifacts and /api/runs.
                Off: the dev server mounts only what a production server would, so you can see that
                surface — parts of Insight that need the others will stop working.
              </p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={form.dev_expose_all}
              onClick={() => setForm((f) => ({ ...f, dev_expose_all: !f.dev_expose_all }))}
              className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none ${
                form.dev_expose_all ? 'bg-sky-600' : 'bg-slate-700'
              }`}
            >
              <span
                className={`pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow-lg transition-transform ${
                  form.dev_expose_all ? 'translate-x-4' : 'translate-x-0'
                }`}
              />
            </button>
          </div>

          {/* Production system groups */}
          <div className="py-3 px-4 rounded-lg border border-slate-800 bg-slate-900/40">
            <p className="text-sm font-medium text-slate-200">Mounted in production</p>
            <p className="text-xs text-slate-600 mt-0.5">
              A production server mounts auth and runs (runs is always on), plus your workflows. Tick the rest
              only if a deployment needs them; the TUVL_API_EXPOSE env var overrides this file.
            </p>
            <div className="mt-3 flex flex-col gap-2">
              {OPTIONAL_GROUPS.map((g) => (
                <label key={g.key} className="flex items-start gap-2.5 text-xs text-slate-300">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={form.extra.includes(g.key)}
                    onChange={(e) =>
                      setForm((f) => ({
                        ...f,
                        extra: e.target.checked ? [...f.extra, g.key] : f.extra.filter((k) => k !== g.key),
                      }))
                    }
                  />
                  <span>
                    <span className="font-mono text-slate-200">{g.label}</span>
                    <span className="text-slate-600"> — {g.hint}</span>
                  </span>
                </label>
              ))}
            </div>
          </div>

          {/* API documentation */}
          <div className="py-3 px-4 rounded-lg border border-slate-800 bg-slate-900/40">
            <label className="block text-sm font-medium text-slate-200" htmlFor="api-docs">
              API documentation
            </label>
            <p className="text-xs text-slate-600 mt-0.5">
              What /docs, /redoc and /openapi.json show. Workflows lists only your own routes.
            </p>
            <select
              id="api-docs"
              value={form.docs}
              onChange={(e) => setForm((f) => ({ ...f, docs: e.target.value }))}
              className="mt-2 w-full rounded-md border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-xs text-slate-200"
            >
              <option value="">Default (workflows in dev, off in production)</option>
              <option value="off">Off — serve none of it</option>
              <option value="workflows">Workflows — your routes only</option>
              <option value="all">All — include the system routes</option>
            </select>
          </div>

          {/* Actions */}
          <div className="flex gap-3 pt-2">
            <button
              type="submit"
              disabled={saveMut.isPending}
              className="flex-1 px-4 py-2 rounded-lg bg-sky-600 text-sm font-semibold text-white hover:bg-sky-500 disabled:opacity-40 transition-colors"
            >
              {saveMut.isPending ? 'Saving…' : saved ? '✓ Saved' : 'Save'}
            </button>
          </div>

          {saveError && (
            <p className="text-xs text-rose-400 font-mono">{saveError}</p>
          )}
        </form>

        {/* Restart notice */}
        <div className="mx-6 mb-6 p-4 rounded-lg border border-amber-800/30 bg-amber-900/10">
          <p className="text-xs font-semibold text-amber-500 mb-1.5">
            Restart required to apply changes
          </p>
          <p className="text-xs text-slate-500 leading-relaxed">
            The API surface is mounted once at startup. Save your config here, then restart
            the engine with <code className="font-mono text-slate-400">tuvl run</code> for
            changes to take effect.
          </p>
        </div>
      </div>
    </div>
  )
}

// ── Settings nav (left submenu) ───────────────────────────────────────────────

function SettingsNav({
  active,
  onSelect,
}: {
  active: string
  onSelect: (id: string) => void
}) {
  const categories = [...new Set(SECTIONS.map((s) => s.category))]

  return (
    <nav className="w-48 shrink-0 border-r border-slate-800 flex flex-col py-4 gap-1 overflow-y-auto">
      {categories.map((cat) => (
        <div key={cat} className="mb-2">
          <p className="px-4 pb-1 text-[10px] font-semibold uppercase tracking-widest text-slate-600">
            {cat}
          </p>
          {SECTIONS.filter((s) => s.category === cat).map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => onSelect(s.id)}
              className={`w-full flex items-center gap-2.5 px-4 py-2 text-sm transition-colors text-left ${
                active === s.id
                  ? 'bg-sky-600/10 text-sky-400 border-r-2 border-sky-500'
                  : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-200'
              }`}
            >
              {s.icon}
              {s.label}
            </button>
          ))}
        </div>
      ))}
    </nav>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

export function SettingsPage() {
  const [active, setActive] = useState(SECTIONS[0].id)

  return (
    <div className="flex flex-col h-full">
      <TopBar title="Settings" />
      <div className="flex flex-1 min-h-0">
        <SettingsNav active={active} onSelect={setActive} />
        <div className="flex flex-1 min-w-0 min-h-0">
          {active === 'project' && <ProjectSettings />}
          {active === 'environment' && <EnvironmentSettings />}
          {active === 'dev-telemetry' && <DevTelemetrySettings />}
          {active === 'redis' && <RedisSettings />}
          {active === 'telemetry' && <TelemetrySettings />}
          {active === 'api-access' && <ApiAccessSettings />}
        </div>
      </div>
    </div>
  )
}
