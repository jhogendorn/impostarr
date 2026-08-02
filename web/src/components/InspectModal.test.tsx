import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import InspectModal from './InspectModal'
import { jobDetailFixture } from './testFixtures'

const { getJobMock } = vi.hoisted(() => ({ getJobMock: vi.fn() }))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, getJob: getJobMock }
})

describe('InspectModal', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('renders a full JobDetail fixture: plugin results, transcript excerpt, framegrabs', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/whisper-transcript/)).toBeInTheDocument()
    expect(screen.getByText(/ocr-subs/)).toBeInTheDocument()
    expect(screen.getByText('no subtitle track')).toBeInTheDocument()

    expect(screen.getByText(/Hello world/)).toBeInTheDocument()
    expect(screen.getByText(/Second line/)).toBeInTheDocument()

    const frames = screen.getAllByAltText(/frame \d/)
    expect(frames).toHaveLength(2)
    expect(frames[0]).toHaveAttribute('src', '/api/v1/jobs/42/assets/2')
    expect(frames[0]).toHaveAttribute('loading', 'lazy')
    expect(frames[1]).toHaveAttribute('src', '/api/v1/jobs/42/assets/3')
  })

  it('shows the remediation log when present', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/interruption_guard/)).toBeInTheDocument()
    expect(screen.getByText(/no interruption in progress/)).toBeInTheDocument()
  })

  it('omits the remediation log section when absent', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      verdict: { ...jobDetailFixture.verdict!, remediation_log: null },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    expect(screen.queryByText('Remediation log')).not.toBeInTheDocument()
  })
})
