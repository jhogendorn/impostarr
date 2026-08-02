import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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

  it('renders a fallback row instead of throwing when candidates are malformed', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      plugin_results: [
        {
          name: 'flaky-plugin',
          version: '0.1.0',
          status: 'ok',
          reason: null,
          candidates: [
            { confidence: 'not-a-number', ident: null, numbering: null }, // malformed: confidence not numeric
            null, // malformed: not an object
            { confidence: 0.7, ident: null, numbering: 'tvdb', evidence: {} }, // valid
          ],
          normalized: [],
        },
      ],
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/flaky-plugin/)).toBeInTheDocument()
    expect(screen.getAllByText('unrecognized entry')).toHaveLength(2)
    expect(screen.getByText(/conf 0\.70/)).toBeInTheDocument()
  })

  it('renders a Close button that calls onClose', async () => {
    const user = userEvent.setup()
    getJobMock.mockResolvedValue(jobDetailFixture)
    const onClose = vi.fn()
    render(<InspectModal jobId={42} open onClose={onClose} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    await user.click(screen.getByRole('button', { name: 'Close' }))

    expect(onClose).toHaveBeenCalled()
  })
})
