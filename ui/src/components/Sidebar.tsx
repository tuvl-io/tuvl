import { NavLink } from 'react-router-dom'
import { useUIStore } from '../store'
import { useHealth } from '../queries'

// ── SVG icons (inline, no extra dep) ─────────────────────────────────────────
function IconWorkflows() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path d="M3 4a1 1 0 011-1h3a1 1 0 011 1v3a1 1 0 01-1 1H4a1 1 0 01-1-1V4zM3 13a1 1 0 011-1h3a1 1 0 011 1v3a1 1 0 01-1 1H4a1 1 0 01-1-1v-3zM12 4a1 1 0 011-1h3a1 1 0 011 1v3a1 1 0 01-1 1h-3a1 1 0 01-1-1V4z" />
      <path fillRule="evenodd" d="M7.5 7.5h1A1.5 1.5 0 0110 9v2a1.5 1.5 0 001.5 1.5h1" clipRule="evenodd" />
    </svg>
  )
}
function IconModels() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path d="M10 2a8 8 0 100 16A8 8 0 0010 2zm0 2a6 6 0 11-.001 12.001A6 6 0 0110 4zm0 2a4 4 0 100 8 4 4 0 000-8zm0 2a2 2 0 110 4 2 2 0 010-4z" />
    </svg>
  )
}
function IconDatasources() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path d="M3 12v3c0 1.657 3.134 3 7 3s7-1.343 7-3v-3c0 1.657-3.134 3-7 3s-7-1.343-7-3z" />
      <path d="M3 7v3c0 1.657 3.134 3 7 3s7-1.343 7-3V7c0 1.657-3.134 3-7 3S3 8.657 3 7z" />
      <path d="M17 5c0 1.657-3.134 3-7 3S3 6.657 3 5s3.134-3 7-3 7 1.343 7 3z" />
    </svg>
  )
}
function IconAgentModels() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path fillRule="evenodd" d="M2 5a2 2 0 012-2h12a2 2 0 012 2v8a2 2 0 01-2 2H4a2 2 0 01-2-2V5zm3.293 1.293a1 1 0 011.414 0l3 3a1 1 0 010 1.414l-3 3a1 1 0 01-1.414-1.414L7.586 10 5.293 7.707a1 1 0 010-1.414zM11 12a1 1 0 100 2h3a1 1 0 100-2h-3z" clipRule="evenodd" />
    </svg>
  )
}
function IconApiDocs() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path fillRule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4zm2 6a1 1 0 011-1h6a1 1 0 110 2H7a1 1 0 01-1-1zm1 3a1 1 0 100 2h6a1 1 0 100-2H7z" clipRule="evenodd" />
    </svg>
  )
}
function IconIam() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path fillRule="evenodd" d="M10 1a4.5 4.5 0 00-4.5 4.5V9H5a2 2 0 00-2 2v6a2 2 0 002 2h10a2 2 0 002-2v-6a2 2 0 00-2-2h-.5V5.5A4.5 4.5 0 0010 1zm3 8V5.5a3 3 0 10-6 0V9h6z" clipRule="evenodd" />
    </svg>
  )
}
function IconFederation() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path fillRule="evenodd" d="M12.586 4.586a2 2 0 112.828 2.828l-3 3a2 2 0 01-2.828 0 1 1 0 00-1.414 1.414 4 4 0 005.656 0l3-3a4 4 0 00-5.656-5.656l-1.5 1.5a1 1 0 101.414 1.414l1.5-1.5zm-5 5a2 2 0 012.828 0 1 1 0 101.414-1.414 4 4 0 00-5.656 0l-3 3a4 4 0 105.656 5.656l1.5-1.5a1 1 0 10-1.414-1.414l-1.5 1.5a2 2 0 11-2.828-2.828l3-3z" clipRule="evenodd" />
    </svg>
  )
}
function IconIdeas() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path d="M10 2a6 6 0 00-3.6 10.8c.4.3.6.8.6 1.2V15a1 1 0 001 1h4a1 1 0 001-1v-1c0-.4.2-.9.6-1.2A6 6 0 0010 2zM8 18a1 1 0 011-1h2a1 1 0 110 2H9a1 1 0 01-1-1z" />
    </svg>
  )
}
function IconAudit() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path fillRule="evenodd" d="M6 2a2 2 0 00-2 2v12a2 2 0 002 2h8a2 2 0 002-2V7.4a2 2 0 00-.6-1.4l-3.4-3.4A2 2 0 0010.6 2H6zm1 8a1 1 0 000 2h6a1 1 0 100-2H7zm0 3a1 1 0 100 2h4a1 1 0 100-2H7z" clipRule="evenodd" />
    </svg>
  )
}
function IconSettings() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path fillRule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clipRule="evenodd" />
    </svg>
  )
}
function IconStorage() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path d="M4 3a2 2 0 00-2 2v1a2 2 0 002 2h12a2 2 0 002-2V5a2 2 0 00-2-2H4zm0 7a2 2 0 00-2 2v1a2 2 0 002 2h12a2 2 0 002-2v-1a2 2 0 00-2-2H4zm9-4.5a1 1 0 110 2 1 1 0 010-2zm0 7a1 1 0 110 2 1 1 0 010-2z" />
    </svg>
  )
}
function IconMemory() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path d="M10 2a8 8 0 100 16 8 8 0 000-16zm.75 3.5v4.19l2.78 1.67-.77 1.28-3.51-2.1V5.5h1.5z" />
    </svg>
  )
}
function IconEmbeddings() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path d="M10 2a1 1 0 00-1 1v1.268A6.002 6.002 0 004 10a6 6 0 006 6 6 6 0 006-6 6.002 6.002 0 00-5-5.732V3a1 1 0 00-1-1zm0 4a4 4 0 110 8 4 4 0 010-8zm0 2a2 2 0 100 4 2 2 0 000-4z" />
    </svg>
  )
}
function IconArtifacts() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path d="M10 2 3 5.5v9L10 18l7-3.5v-9L10 2zm0 2.2 4.6 2.3L10 8.8 5.4 6.5 10 4.2zM5 8.1l4 2v5.2l-4-2V8.1zm6 7.2v-5.2l4-2v5.2l-4 2z" />
    </svg>
  )
}
function IconCollections() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path d="M7 3a1 1 0 000 2h6a1 1 0 100-2H7zM4 7a1 1 0 011-1h10a1 1 0 110 2H5a1 1 0 01-1-1zM2 11a2 2 0 012-2h12a2 2 0 012 2v4a2 2 0 01-2 2H4a2 2 0 01-2-2v-4z" />
    </svg>
  )
}

function IconSpecs() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path fillRule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4zm2 6a1 1 0 011-1h6a1 1 0 110 2H7a1 1 0 01-1-1zm1 3a1 1 0 100 2h6a1 1 0 100-2H7z" clipRule="evenodd" />
    </svg>
  )
}

function IconRuns() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z" clipRule="evenodd" />
    </svg>
  )
}

// Ordered by the dev loop: build → observe → configure → access → system.
const navGroups = [
  {
    title: 'Build',
    items: [
      { to: '/ideas', label: 'Ideas', Icon: IconIdeas },
      { to: '/specs', label: 'Specs', Icon: IconSpecs },
      { to: '/workflows', label: 'Workflows', Icon: IconWorkflows },
      { to: '/artifacts', label: 'Artifacts', Icon: IconArtifacts },
    ],
  },
  {
    title: 'Observe',
    items: [
      { to: '/runs', label: 'Runs', Icon: IconRuns },
      { to: '/audit', label: 'Audit', Icon: IconAudit },
    ],
  },
  {
    title: 'Configure',
    items: [
      { to: '/models', label: 'Models', Icon: IconModels },
      { to: '/datasources', label: 'Datasources', Icon: IconDatasources },
      { to: '/agent-models', label: 'AI Models', Icon: IconAgentModels },
      { to: '/embeddings', label: 'Embeddings', Icon: IconEmbeddings },
      { to: '/collections', label: 'Collections', Icon: IconCollections },
      { to: '/storage', label: 'Storage', Icon: IconStorage },
      { to: '/memory', label: 'Memory', Icon: IconMemory },
    ],
  },
  {
    title: 'Access',
    items: [
      { to: '/iam', label: 'IAM', Icon: IconIam },
      { to: '/federation', label: 'Federation', Icon: IconFederation },
    ],
  },
  {
    title: 'System',
    items: [
      { to: '/api-docs', label: 'API Docs', Icon: IconApiDocs },
      { to: '/settings', label: 'Settings', Icon: IconSettings },
    ],
  },
]

export function Sidebar() {
  const sidebarOpen = useUIStore((s) => s.sidebarOpen)
  const toggleSidebar = useUIStore((s) => s.toggleSidebar)
  // below 1024 px the expanded menu is an overlay: picking a page closes it
  const closeOnSmall = () => { if (sidebarOpen && !window.matchMedia('(min-width: 1024px)').matches) toggleSidebar() }
  const { data: health } = useHealth()
  const online = health?.status === 'healthy'

  // ── Collapsed icon rail ───────────────────────────────────────────────────
  if (!sidebarOpen) {
    return (
      <aside className="hidden lg:flex w-14 shrink-0 h-full min-h-0 bg-slate-900 border-r border-slate-700 flex-col items-center">
        {/* Logo and the expand button: together at the top, so the button is on screen however short the window */}
        <div className="shrink-0 py-3 flex flex-col items-center gap-2 w-full border-b border-slate-700">
          <span className="feather-mark w-9 h-9" role="img" aria-label="tuvl" />
          <button
            type="button"
            onClick={toggleSidebar}
            aria-label="Expand the menu"
            aria-expanded={false}
            title="Expand the menu"
            className="w-9 h-7 rounded-md text-slate-400 hover:bg-slate-800 hover:text-slate-100"
          >
            »
          </button>
        </div>

        {/* Nav icons: scroll when the window is shorter than the list */}
        <nav className="flex-1 min-h-0 overflow-y-auto overscroll-contain [scrollbar-width:thin] flex flex-col items-center py-3 gap-1 w-full">
          {navGroups.map((group, gi) => (
            <div key={group.title} className="flex flex-col items-center gap-1 w-full">
              {gi > 0 && <div className="w-6 border-t border-slate-700 my-1.5" />}
              {group.items.map(({ to, Icon, label }) => (
                <NavLink
                  key={to}
                  to={to}
                  title={label}
                  aria-label={label}
                  className={({ isActive }) =>
                    `flex items-center justify-center w-9 h-9 shrink-0 rounded-md transition-colors ${
                      isActive
                        ? 'bg-sky-600 text-white'
                        : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'
                    }`
                  }
                >
                  <Icon />
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        {/* Online status dot */}
        <div className="shrink-0 py-3 border-t border-slate-700 w-full flex justify-center">
          <span
            title={online ? 'Engine online' : 'Engine offline'}
            className={`w-2.5 h-2.5 rounded-full ${online ? 'bg-emerald-400' : 'bg-red-500'}`}
          />
        </div>
      </aside>
    )
  }

  // ── Expanded sidebar ──────────────────────────────────────────────────────
  return (
    <>
    <div className="lg:hidden fixed inset-0 z-[45] bg-black/50" onClick={toggleSidebar} aria-hidden="true" />
    <aside className="fixed inset-y-0 left-0 z-50 lg:static lg:z-auto w-52 shrink-0 h-full min-h-0 bg-slate-900 border-r border-slate-700 flex flex-col">
      <div className="px-4 py-4 border-b border-slate-700 flex items-center gap-2.5">
        <span className="feather-mark w-9 h-9 shrink-0" role="img" aria-label="tuvl" />
        <div className="flex flex-col leading-none">
          <span className="text-slate-100 font-bold text-xl tracking-tight">tuvl <span className="text-cyan-400">insight</span></span>
          <span
            lang="ml"
            title="തൂവൽ — “feather” in Malayalam, the name tuvl is drawn from"
            className="mt-0.5 text-xs text-cyan-500/70 select-none"
          >
            തൂവൽ
          </span>
        </div>
      </div>
      <p className="px-4 py-2 border-b border-slate-700 text-[10px] leading-snug font-medium text-cyan-400/80 select-none">
        Intelligent Workflows.<br />Deterministic Execution.
      </p>

      <nav className="flex-1 min-h-0 px-2 py-3 overflow-y-auto">
        {navGroups.map((group) => (
          <div key={group.title} className="mb-3">
            <div className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-500 select-none">
              {group.title}
            </div>
            <div className="space-y-1">
              {group.items.map(({ to, label, Icon }) => (
                <NavLink
                  key={to}
                  to={to}
                  onClick={closeOnSmall}
                  className={({ isActive }) =>
                    `flex items-center gap-2.5 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                      isActive
                        ? 'bg-sky-600 text-white'
                        : 'text-slate-300 hover:bg-slate-800 hover:text-slate-100'
                    }`
                  }
                >
                  <Icon />
                  {label}
                </NavLink>
              ))}
            </div>
          </div>
        ))}
      </nav>

      <div className="shrink-0 px-4 py-3 border-t border-slate-700 flex items-center gap-2 text-xs text-slate-400">
        <span
          className={`w-2 h-2 rounded-full ${online ? 'bg-emerald-400' : 'bg-red-500'}`}
        />
        {online ? 'Engine online' : 'Engine offline'}
        <button type="button" onClick={toggleSidebar} aria-label="Collapse the menu" aria-expanded={true} title="Collapse the menu"
          className="ml-auto w-7 h-7 rounded text-slate-500 hover:bg-slate-800 hover:text-slate-100">«</button>
      </div>
    </aside>
    </>
  )
}
