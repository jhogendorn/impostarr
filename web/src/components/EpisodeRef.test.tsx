import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import EpisodeRef from './EpisodeRef'

describe('EpisodeRef', () => {
  it('renders a single episode with a tvdb id as a dereferrer link, season prefix plain', () => {
    render(<EpisodeRef season={5} episodes={[{ episode: 9, tvdbId: 378653 }]} />)

    const link = screen.getByRole('link', { name: 'E09' })
    expect(link).toHaveAttribute('href', 'https://thetvdb.com/dereferrer/episode/378653')
    expect(link.closest('body')).toHaveTextContent('S05E09')
  })

  it('renders plain text (no link) when the episode has no tvdb id', () => {
    render(<EpisodeRef season={5} episodes={[{ episode: 9, tvdbId: null }]} />)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByText('E09')).toBeInTheDocument()
  })

  it('links each episode of a multi-episode file independently', () => {
    render(
      <EpisodeRef
        season={2}
        episodes={[
          { episode: 3, tvdbId: 111 },
          { episode: 4, tvdbId: null },
        ]}
      />,
    )
    const link = screen.getByRole('link', { name: 'E03' })
    expect(link).toHaveAttribute('href', 'https://thetvdb.com/dereferrer/episode/111')
    expect(screen.getByText('E04')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'E04' })).not.toBeInTheDocument()
  })

  it('zero-pads season/episode numbers', () => {
    render(<EpisodeRef season={1} episodes={[{ episode: 2 }]} />)
    expect(screen.getByText('E02').closest('body')).toHaveTextContent('S01E02')
  })
})
