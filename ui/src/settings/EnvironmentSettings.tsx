import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { declareDevEnv, devEnv, saveDevEnv } from '../devApi'
import type { EnvVariable } from '../devApi'

const input = 'w-full rounded border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-sm text-slate-100'
const btn = 'px-3 py-1.5 rounded text-sm bg-slate-700 hover:bg-slate-600 disabled:opacity-40'

function badge(text: string, tone: 'slate' | 'amber' | 'sky' | 'rose') {
  const tones = {
    slate: 'bg-slate-800 text-slate-400',
    amber: 'bg-amber-500/10 text-amber-400',
    sky: 'bg-sky-500/10 text-sky-400',
    rose: 'bg-rose-500/10 text-rose-400',
  }
  return <span className={`px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wide ${tones[tone]}`}>{text}</span>
}

function Row({ v, onSet, onUnset, onDeclare, busy }: {
  v: EnvVariable
  onSet: (value: string) => void
  onUnset: () => void
  onDeclare: () => void
  busy: boolean
}) {
  const [value, setValue] = useState('')
  const status = v.is_set ? (v.restart_needed ? 'set — restart to apply' : 'set') : v.needed || v.required ? 'missing' : 'not set'
  return (
    <div className="px-6 py-3 border-b border-slate-800/70">
      <div className="flex items-center gap-2 flex-wrap">
        <code className="text-sm text-slate-100">{v.name}</code>
        {v.secret && badge('secret', 'amber')}
        {v.required && badge('required', 'sky')}
        {v.first_boot && badge('first boot only', 'slate')}
        {!v.declared && badge('undeclared', 'rose')}
        <span className={`ml-auto text-xs ${v.is_set ? 'text-emerald-400' : v.needed || v.required ? 'text-rose-400' : 'text-slate-500'}`}>{status}</span>
      </div>
      {v.description && <p className="mt-1 text-xs text-slate-500 leading-relaxed">{v.description}</p>}
      {v.referenced_by.length > 0 && (
        <p className="mt-1 text-[11px] text-slate-600">Used by {v.referenced_by.slice(0, 4).join(', ')}{v.referenced_by.length > 4 ? ` and ${v.referenced_by.length - 4} more` : ''}</p>
      )}
      <div className="mt-2 flex items-center gap-2">
        <input
          className={input}
          type={v.secret ? 'password' : 'text'}
          autoComplete="off"
          placeholder={v.secret ? (v.is_set ? 'Set — type to replace' : 'Value (kept in .env, never shown again)') : v.value ?? v.default ?? v.example ?? 'Value'}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          aria-label={`Value of ${v.name}`}
        />
        <button className={btn} disabled={busy || value === ''} onClick={() => { onSet(value); setValue('') }}>Save</button>
        {v.in_dotenv && <button className={btn} disabled={busy} onClick={onUnset}>Remove</button>}
        {!v.declared && <button className={btn} disabled={busy} onClick={onDeclare}>Declare</button>}
      </div>
    </div>
  )
}

/** The variables the project needs, which are set, and the project's `.env`. Development only. */
export function EnvironmentSettings() {
  const qc = useQueryClient()
  const state = useQuery({ queryKey: ['dev', 'env'], queryFn: devEnv })
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const [group, setGroup] = useState('all')
  const done = (text: string) => (s: unknown) => { qc.setQueryData(['dev', 'env'], s); setMessage({ ok: true, text }) }
  const fail = (e: unknown) => setMessage({ ok: false, text: (e as Error).message })

  const save = useMutation({ mutationFn: saveDevEnv, onSuccess: done('Saved to .env. Restart the dev server for it to take effect.'), onError: fail })
  const declare = useMutation({
    mutationFn: (v: EnvVariable) => declareDevEnv({ name: v.name, secret: v.secret, required: false }),
    onSuccess: (s) => { qc.setQueryData(['dev', 'env'], s); setMessage({ ok: true, text: `Declared in ${s.manifest_file}.` }) },
    onError: fail,
  })

  const s = state.data
  const rows = (s?.variables ?? []).filter((v) => group === 'all' || (group === 'project' ? v.source !== 'engine' : v.source === 'engine'))
  const missing = (s?.variables ?? []).filter((v) => !v.is_set && (v.needed || v.required)).length
  const undeclared = (s?.variables ?? []).filter((v) => !v.declared).length
  return (
    <div className="flex flex-col flex-1 min-h-0">
      <div className="px-6 py-5 border-b border-slate-800 flex-shrink-0">
        <h2 className="text-base font-semibold text-slate-100">Environment</h2>
        <p className="mt-1 text-xs text-slate-500 leading-relaxed max-w-xl">
          The variables this project reads: the engine&apos;s own, those declared in an <code>EnvConfig</code>, and every <code>${'{VAR}'}</code> found in your YAML.
          Values are saved to the project&apos;s <code>.env</code>; a secret&apos;s value is written but never shown again. In production the same variables come from the cluster Secret.
        </p>
        {s && !s.dotenv.gitignored && (
          <p className="mt-2 text-xs text-amber-400">.env is not in .gitignore. Add it before saving a secret, so it is not committed.</p>
        )}
        <div className="mt-3 flex items-center gap-3 text-xs">
          <span className="text-slate-400">{missing === 0 ? 'Nothing missing' : `${missing} missing`}</span>
          {undeclared > 0 && <span className="text-slate-500">{undeclared} not declared{s?.manifest_file ? '' : ' (no EnvConfig yet)'}</span>}
          <select className="ml-auto rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-300" value={group} onChange={(e) => setGroup(e.target.value)} aria-label="Show">
            <option value="all">All</option>
            <option value="project">This project</option>
            <option value="engine">Engine</option>
          </select>
        </div>
        {message && <p className={`mt-2 text-xs ${message.ok ? 'text-emerald-400' : 'text-rose-400'}`}>{message.text}</p>}
      </div>
      <div className="flex-1 overflow-y-auto">
        {state.isLoading && <p className="px-6 py-4 text-sm text-slate-500">Loading…</p>}
        {state.error && <p className="px-6 py-4 text-sm text-rose-400">{(state.error as Error).message}</p>}
        {rows.map((v) => (
          <Row
            key={v.name}
            v={v}
            busy={save.isPending || declare.isPending}
            onSet={(value) => save.mutate({ set: { [v.name]: value } })}
            onUnset={() => save.mutate({ unset: [v.name] })}
            onDeclare={() => declare.mutate(v)}
          />
        ))}
        {s && rows.length === 0 && <p className="px-6 py-4 text-sm text-slate-500">Nothing to show here yet.</p>}
      </div>
    </div>
  )
}
