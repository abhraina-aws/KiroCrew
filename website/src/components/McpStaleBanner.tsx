/**
 * McpStaleBanner — global banner shown when the MCP configuration changed
 * after the last full session restart.
 *
 * A kiro-cli session's MCP tool table is frozen at process spawn time, so no
 * config edit ever reaches a live session — the ONLY remedy is a session
 * restart, and until now nothing told the user that. The backend's config
 * watcher (kiro_crew/mcp_watch.py) tracks a generation counter; this banner
 * polls GET /api/mcp/config-status via react-query and renders when
 * `sessionsStale` is true. react-query rather than a hand-rolled interval so
 * a throttled background tab refetches immediately on focus instead of
 * serving the pre-change answer until its next timer.
 *
 * Dismissal is per generation and persisted (localStorage): dismissing hides
 * the banner for the CURRENT change only — a later config change surfaces it
 * again, while a page reload does not resurrect one the user already waved
 * off. A failed restart is reported inline (matching RestartButton) — the
 * banner staying up is the truth, but silence about WHY reads as a no-op.
 */
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Zap } from 'lucide-react'
import { api } from '../api/client'
import { Btn } from './ui'

import { i18nT } from '../i18n/t'

const POLL_MS = 30_000
const DISMISS_KEY = 'mcpStaleBanner.dismissedGeneration'

interface ConfigStatus { generation: number; sessionsStale: boolean }

function readDismissedGen(): number {
  const raw = Number(localStorage.getItem(DISMISS_KEY))
  return Number.isFinite(raw) ? raw : -1
}

export default function McpStaleBanner() {
  const queryClient = useQueryClient()
  const [dismissedGen, setDismissedGen] = useState(readDismissedGen)

  const { data: status } = useQuery<ConfigStatus>({
    queryKey: ['mcp-config-status'],
    queryFn: api.mcpConfigStatus,
    refetchInterval: POLL_MS,
  })

  const restart = useMutation({
    mutationFn: api.restartSessions,
    onSuccess: (data: { mcp_sync_ok?: boolean }) => {
      // The restart endpoint returns 200 even when its pre-restart MCP sync
      // failed (the restart itself did run) — but a failed sync means the
      // on-disk config may still be stale, so only a confirmed sync clears
      // the banner. The reset handler records the generation server-side on
      // the same condition; this mirrors it instead of waiting out the poll.
      if (data?.mcp_sync_ok !== false) {
        queryClient.setQueryData<ConfigStatus>(
          ['mcp-config-status'],
          s => (s ? { ...s, sessionsStale: false } : s),
        )
      }
    },
    // On error the banner stays up — the restart did not happen, so the
    // staleness it reports is still true; the message below says why.
  })

  // Generations are gateway-lifetime and restart at zero: a persisted
  // dismissal from a previous gateway life (e.g. 5) must not hide the new
  // life's generations. Dismissal only ever matches EXACTLY (so a regressed
  // generation renders again), and an observed generation BELOW the stored
  // dismissal proves the counter reset — drop the stale record so it cannot
  // shadow a future generation that happens to reach the same number.
  useEffect(() => {
    if (status && status.generation < dismissedGen) {
      localStorage.removeItem(DISMISS_KEY)
      setDismissedGen(-1)
    }
  }, [status, dismissedGen])

  if (!status?.sessionsStale || status.generation === dismissedGen) return null

  const dismiss = () => {
    localStorage.setItem(DISMISS_KEY, String(status.generation))
    setDismissedGen(status.generation)
  }

  return (
    <div className="mx-6 mt-4 mb-2 bg-warn/10 border border-warn/30 rounded-lg p-4 flex items-start gap-3 animate-rise" role="status">
      <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5" />
      <div className="flex-1 min-w-0">
        <div className="text-[13px] font-medium text-text">
          {i18nT('components.mcpStaleBanner.mcp_configuration_changed')}
        </div>
        <div className="text-[13px] text-muted mt-1">
          {i18nT('components.mcpStaleBanner.live_sessions_keep_the_tools_they_started_with_r')}
        </div>
        {restart.isError && (
          <div className="text-[13px] text-danger mt-1" role="alert">
            {restart.error instanceof Error && restart.error.message
              ? restart.error.message
              : i18nT('components.restartButton.restart_failed')}
          </div>
        )}
      </div>
      <Btn primary onClick={() => restart.mutate()} disabled={restart.isPending} className="shrink-0">
        <Zap size={14} className={restart.isPending ? 'animate-spin' : ''} />
        {restart.isPending
          ? i18nT('components.restartButton.restarting')
          : i18nT('components.restartButton.apply_restart')}
      </Btn>
      <Btn onClick={dismiss} className="shrink-0">
        {i18nT('app.dismiss')}
      </Btn>
    </div>
  )
}
