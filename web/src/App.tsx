import { useCallback } from 'react'
import { useEvents } from './api/sse'
import type { SseEvent } from './api/types'

function App() {
  const handleEvent = useCallback((event: SseEvent) => {
    // Queue UI lands in a later task; for now just observe the stream.
    console.debug('sse event', event)
  }, [])
  const connected = useEvents(handleEvent)

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="flex items-center justify-between border-b border-slate-800 px-6 py-3">
        <span className="text-lg font-semibold tracking-tight text-indigo-400">Impostarr</span>
        <div className="flex items-center gap-2 text-sm text-slate-400">
          <span
            className={`h-2.5 w-2.5 rounded-full ${connected ? 'bg-emerald-500' : 'bg-slate-600'}`}
            title={connected ? 'connected' : 'disconnected'}
          />
          {connected ? 'connected' : 'disconnected'}
        </div>
      </header>
      <main className="px-6 py-8 text-slate-400">Queue view coming soon.</main>
    </div>
  )
}

export default App
