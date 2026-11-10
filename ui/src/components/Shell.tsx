import { Outlet } from 'react-router-dom'
import { Sidebar } from './Sidebar'
import { ValidationPanel } from './ValidationPanel'

export function Shell() {
  return (
    <div className="flex h-full bg-slate-950 text-slate-100 overflow-hidden">
      <Sidebar />
      <div className="flex flex-col flex-1 min-w-0">
        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
      <ValidationPanel />
    </div>
  )
}
