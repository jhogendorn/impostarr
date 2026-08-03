import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import type { EpisodeLabel } from '../api/types'
import { type AlternateCandidate, episodeOptionLabel } from '../lib/inspectData'
import { formatPercent, formatSeasonEpisode } from '../lib/format'

interface EpisodeComboboxProps {
  ariaLabel: string
  value: number
  onChange: (id: number) => void
  candidates: AlternateCandidate[]
  allEpisodes: EpisodeLabel[]
  episodeLabels: Record<string, EpisodeLabel>
  disabled?: boolean
  className?: string
}

type Row =
  | { kind: 'candidate'; label: EpisodeLabel }
  | { kind: 'season-header'; season: number; expanded: boolean; count: number }
  | { kind: 'season-option'; label: EpisodeLabel }

function resolveLabel(
  id: number,
  episodeLabels: Record<string, EpisodeLabel>,
  allEpisodes: EpisodeLabel[],
): EpisodeLabel | undefined {
  return episodeLabels[String(id)] ?? allEpisodes.find((ep) => ep.id === id)
}

function matchesQuery(label: EpisodeLabel, query: string): boolean {
  return episodeOptionLabel(label).toLowerCase().includes(query.toLowerCase())
}

/** "S05E09" (distinct, monospace/medium) + " - Title" (normal weight) — the
 * old native `<select>`'s "S05E09 - Stan Time" presentation (item 1),
 * split across two spans so the SxxEyy portion reads visually distinct.
 * The option button carries its own `aria-label` (exact
 * `episodeOptionLabel` string) rather than relying on name-from-content,
 * since the accessible-name computation trims each child span's own
 * leading/trailing whitespace before concatenating — splitting the text
 * visually would otherwise swallow the " - " separator's leading space. */
function OptionLabel({ label }: { label: EpisodeLabel }) {
  return (
    <>
      <span className="shrink-0 font-mono text-[11px] font-medium text-slate-300">
        {formatSeasonEpisode(label.season, [label.episode])}
      </span>
      <span className="truncate font-normal text-slate-200">{label.title ? ` - ${label.title}` : ''}</span>
    </>
  )
}

const OPTION_ROW_CLASS = 'flex w-full items-center gap-1.5 px-2 py-1.5 text-left'

/** Custom searchable combobox replacing the native `<select>` (item 3) —
 * 405 raw options is unusable as a flat list. Text input filters as you
 * type; results grouped "Candidates" (always expanded, descending
 * confidence — already sorted that way by `collectAlternates`) first,
 * then one collapsed-by-default group per season (click header to
 * expand). While filtering, a season group with any match auto-expands
 * so its matches are visible — collapse state only governs the
 * empty-query browsing view.
 *
 * Selecting an option only calls `onChange` (a preview) — this component
 * never mutates anything itself; see ActionBar for the separate confirm
 * step. Built from scratch (not a native select) for full control over
 * grouping/collapsing; keyboard nav (arrows/enter/escape) and
 * outside-click-close are hand-rolled below over a single flattened
 * `rows` array so index math stays in one place. */
function EpisodeCombobox({
  ariaLabel,
  value,
  onChange,
  candidates,
  allEpisodes,
  episodeLabels,
  disabled,
  className,
}: EpisodeComboboxProps) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [expandedSeasons, setExpandedSeasons] = useState<Set<number>>(new Set())
  // -1 = nothing highlighted yet (just opened/typed) — the first ArrowDown
  // moves to row 0 rather than row 1.
  const [activeIndex, setActiveIndex] = useState(-1)
  const containerRef = useRef<HTMLDivElement>(null)
  const listboxId = useRef(`episode-combobox-${Math.random().toString(36).slice(2)}`).current

  const selectedLabel = resolveLabel(value, episodeLabels, allEpisodes)
  const displayText = selectedLabel ? episodeOptionLabel(selectedLabel) : ''

  const seasons = useMemo(() => {
    const bySeason = new Map<number, EpisodeLabel[]>()
    for (const ep of allEpisodes) {
      const list = bySeason.get(ep.season)
      if (list) list.push(ep)
      else bySeason.set(ep.season, [ep])
    }
    return [...bySeason.entries()].sort((a, b) => b[0] - a[0])
  }, [allEpisodes])

  const filtering = query.trim().length > 0

  const filteredCandidates = useMemo(() => {
    const withLabel = candidates
      .map((c) => ({ candidate: c, label: resolveLabel(c.episodeIds[0], episodeLabels, allEpisodes) }))
      .filter((c): c is { candidate: AlternateCandidate; label: EpisodeLabel } => c.label != null)
    return filtering ? withLabel.filter((c) => matchesQuery(c.label, query)) : withLabel
  }, [candidates, episodeLabels, allEpisodes, filtering, query])

  const rows = useMemo<Row[]>(() => {
    const out: Row[] = []
    for (const { label } of filteredCandidates) out.push({ kind: 'candidate', label })
    for (const [season, episodes] of seasons) {
      const visible = filtering ? episodes.filter((ep) => matchesQuery(ep, query)) : episodes
      if (filtering && visible.length === 0) continue
      const expanded = filtering || expandedSeasons.has(season)
      out.push({ kind: 'season-header', season, expanded, count: visible.length })
      if (expanded) {
        for (const label of visible) out.push({ kind: 'season-option', label })
      }
    }
    return out
  }, [filteredCandidates, seasons, filtering, query, expandedSeasons])

  useEffect(() => {
    if (activeIndex >= rows.length) setActiveIndex(Math.max(0, rows.length - 1))
  }, [rows.length, activeIndex])

  useEffect(() => {
    if (!open) return
    function onDocMouseDown(event: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false)
        setQuery('')
      }
    }
    document.addEventListener('mousedown', onDocMouseDown)
    return () => document.removeEventListener('mousedown', onDocMouseDown)
  }, [open])

  function toggleSeason(season: number) {
    setExpandedSeasons((current) => {
      const next = new Set(current)
      if (next.has(season)) next.delete(season)
      else next.add(season)
      return next
    })
  }

  function selectRow(row: Row) {
    if (row.kind === 'season-header') {
      toggleSeason(row.season)
      return
    }
    onChange(row.label.id)
    setOpen(false)
    setQuery('')
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      if (!open) {
        setOpen(true)
        return
      }
      setActiveIndex((i) => Math.min(i + 1, rows.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex((i) => Math.max(i - 1, 0))
    } else if (event.key === 'Enter') {
      event.preventDefault()
      const row = activeIndex >= 0 ? rows[activeIndex] : undefined
      if (open && row) selectRow(row)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      setOpen(false)
      setQuery('')
    }
  }

  const activeRow = rows[activeIndex]
  const activeId =
    open && activeRow && activeRow.kind !== 'season-header'
      ? `${listboxId}-opt-${activeRow.label.id}`
      : open && activeRow?.kind === 'season-header'
        ? `${listboxId}-season-${activeRow.season}`
        : undefined

  return (
    <div ref={containerRef} className={`relative ${className ?? ''}`}>
      <input
        type="text"
        role="combobox"
        aria-label={ariaLabel}
        aria-expanded={open}
        aria-controls={listboxId}
        aria-activedescendant={activeId}
        aria-autocomplete="list"
        disabled={disabled}
        value={open ? query : displayText}
        placeholder={displayText || 'Select episode…'}
        onFocus={() => {
          setOpen(true)
          setQuery('')
          setActiveIndex(-1)
        }}
        onChange={(event) => {
          setQuery(event.target.value)
          setOpen(true)
          setActiveIndex(-1)
        }}
        onKeyDown={onKeyDown}
        className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-200 placeholder:text-slate-500 focus:border-indigo-500 focus:outline-none disabled:cursor-not-allowed disabled:opacity-40"
      />
      {open && (
        <div
          id={listboxId}
          role="listbox"
          aria-label={ariaLabel}
          className="absolute right-0 top-full z-30 mt-1 max-h-72 w-72 max-w-[90vw] overflow-y-auto rounded-lg border border-slate-700 bg-slate-900 py-1 text-xs shadow-2xl"
        >
          {filteredCandidates.length > 0 && (
            <div role="group" aria-label="Candidates">
              <div className="px-2 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500">Candidates</div>
              {filteredCandidates.map(({ candidate, label }) => (
                <button
                  key={label.id}
                  type="button"
                  id={`${listboxId}-opt-${label.id}`}
                  role="option"
                  aria-selected={label.id === value}
                  aria-label={episodeOptionLabel(label)}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => selectRow({ kind: 'candidate', label })}
                  className={`${OPTION_ROW_CLASS} ${
                    label.id === value ? 'bg-indigo-500/20 text-indigo-200' : 'hover:bg-slate-800'
                  } ${activeRow?.kind !== 'season-header' && activeRow?.label.id === label.id ? 'bg-slate-800' : ''}`}
                >
                  <OptionLabel label={label} />
                  <span className="ml-auto shrink-0 text-[11px] text-slate-500">{formatPercent(candidate.confidence)}</span>
                </button>
              ))}
            </div>
          )}
          {seasons.map(([season, episodes]) => {
            const visible = filtering ? episodes.filter((ep) => matchesQuery(ep, query)) : episodes
            if (filtering && visible.length === 0) return null
            const expanded = filtering || expandedSeasons.has(season)
            return (
              <div key={season} role="group" aria-label={`Season ${season}`}>
                <button
                  type="button"
                  id={`${listboxId}-season-${season}`}
                  aria-expanded={expanded}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => toggleSeason(season)}
                  className={`flex w-full items-center justify-between border-t border-slate-800 px-2 py-1.5 text-left text-[10px] font-semibold uppercase tracking-wide text-slate-500 hover:bg-slate-800 hover:text-slate-300 ${
                    activeRow?.kind === 'season-header' && activeRow.season === season ? 'bg-slate-800' : ''
                  }`}
                >
                  <span>Season {season}</span>
                  <span>{expanded ? '▾' : '▸'}</span>
                </button>
                {expanded &&
                  visible.map((label) => (
                    <button
                      key={label.id}
                      type="button"
                      id={`${listboxId}-opt-${label.id}`}
                      role="option"
                      aria-selected={label.id === value}
                      aria-label={episodeOptionLabel(label)}
                      onMouseDown={(event) => event.preventDefault()}
                      onClick={() => selectRow({ kind: 'season-option', label })}
                      className={`${OPTION_ROW_CLASS} pl-4 ${
                        label.id === value ? 'bg-indigo-500/20 text-indigo-200' : 'hover:bg-slate-800'
                      } ${activeRow?.kind === 'season-option' && activeRow.label.id === label.id ? 'bg-slate-800' : ''}`}
                    >
                      <OptionLabel label={label} />
                    </button>
                  ))}
              </div>
            )
          })}
          {filteredCandidates.length === 0 && rows.length === 0 && (
            <div className="px-2 py-1 text-slate-500">No matching episodes.</div>
          )}
        </div>
      )}
    </div>
  )
}

export default EpisodeCombobox
