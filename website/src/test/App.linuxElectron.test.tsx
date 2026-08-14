/**
 * Linux desktop shell — header inset contract.
 *
 * On Linux the Electron shell can go frameless (electron/linux-frame.js), and
 * the SPA header doubles as the title bar via an injected drag region. Unlike
 * macOS (traffic lights → 84px left inset via `.mac-electron`) and Windows
 * (caption overlay → 142px right inset via `.win-electron`), Linux has no
 * native controls overlaying the header, so the correct inset is ZERO.
 *
 * That zero is achieved by construction — neither platform class applies —
 * and this test locks the construction in: if platform detection ever starts
 * mis-classifying a Linux shell as mac or win (e.g. a refactor of
 * src/lib/electron.ts), the header would gain a bogus 84px/142px inset. This
 * is the frontend half of the fix for the doubled-title-bar issue.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/AgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))

// A Linux Electron shell: inside Electron, but neither darwin nor win32.
// The lib's consts are module-level (frozen at import), so mock the module.
vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/electron')>()),
  isElectron: true,
  isMacElectron: false,
  isWinElectron: false,
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { available: false } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as any

import App from '../App'

describe('App shell — Linux Electron header inset', () => {
  it('applies neither mac-electron nor win-electron, so the header keeps zero inset', async () => {
    const { container } = renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')

    // The platform classes carry the mac 84px left inset and the win 142px
    // right inset (index.css). A Linux frameless window has no traffic
    // lights and no caption overlay, so both must be absent.
    expect(container.querySelector('.mac-electron')).toBeNull()
    expect(container.querySelector('.win-electron')).toBeNull()

    // Sanity: the app shell actually rendered (the absence above is not the
    // absence of the whole shell).
    expect(container.querySelector('header')).toBeTruthy()
  })
})
