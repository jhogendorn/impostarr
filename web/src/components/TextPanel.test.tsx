import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import TextPanel from './TextPanel'

const sources = [
  {
    label: 'en',
    cues: [
      { start_s: 0, text: 'first line' },
      { start_s: 10, text: 'second line' },
      { start_s: 20, text: 'third line' },
    ],
  },
]

/** Item 7 (v5): scrubbing must keep the active (nearest-timestamp) line
 * vertically CENTERED in its panel. The bug was computing the scroll
 * target from `el.offsetTop`, which is relative to `el`'s nearest
 * POSITIONED ancestor (offsetParent) — not necessarily this panel's own
 * scroll container, since the container has no `position` set. That sent
 * the scroll target in the wrong coordinate space, overshooting the
 * highlighted line clean off-screen.
 *
 * jsdom does no real layout, so these tests stub the handful of geometry
 * getters the effect reads (getBoundingClientRect/clientHeight/scrollTop)
 * to prove the computed `scrollTo` target derives from the container's
 * own bounding-rect + scrollTop (immune to offsetParent) rather than from
 * `offsetTop` — stubbing `offsetTop` to an absurd value that would blow up
 * the old formula but has no bearing on the new one. */
describe('TextPanel scroll-to-center (item 7)', () => {
  it('computes the scroll target from getBoundingClientRect deltas, not from offsetTop', () => {
    // Mount with scrubTimeS null first — nearestIdx is null, so the effect
    // returns early and never calls scrollTo yet. This lets the container/
    // cue DOM nodes get stubbed BEFORE the effect runs against them; a
    // rerender (same mounted instance, same DOM nodes) then flips
    // scrubTimeS to trigger it for real.
    const { rerender } = render(<TextPanel title="Transcript" sources={sources} emptyText="none" scrubTimeS={null} />)

    const container = screen.getByText('first line').parentElement!
    const activeLine = screen.getByText('second line')

    Object.defineProperty(container, 'getBoundingClientRect', {
      value: () => ({ top: 500, bottom: 900 }),
      configurable: true,
    })
    Object.defineProperty(container, 'clientHeight', { value: 400, configurable: true })
    Object.defineProperty(container, 'scrollTop', { value: 1000, configurable: true, writable: true })
    const scrollTo = vi.fn()
    container.scrollTo = scrollTo

    Object.defineProperty(activeLine, 'getBoundingClientRect', {
      value: () => ({ top: 620, bottom: 640 }),
      configurable: true,
    })
    Object.defineProperty(activeLine, 'clientHeight', { value: 20, configurable: true })
    // Deliberately absurd — if the component still read this, the target
    // would be way off; the fix must not reference it at all.
    Object.defineProperty(activeLine, 'offsetTop', { value: 5000, configurable: true })

    rerender(<TextPanel title="Transcript" sources={sources} emptyText="none" scrubTimeS={10} />)

    // elTopInContainer = (620 - 500) + 1000 = 1120
    // target = 1120 - 400/2 + 20/2 = 1120 - 200 + 10 = 930
    expect(scrollTo).toHaveBeenCalledWith({ top: 930, behavior: 'smooth' })
  })
})
