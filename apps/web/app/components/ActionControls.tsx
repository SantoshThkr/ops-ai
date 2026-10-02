'use client';

import React, { useState } from 'react';
import { ActionStatus, apiRequest, Role } from '../lib/api';

const STATUS_TEXT: Record<ActionStatus, string> = {
  pending: 'Awaiting administrator approval',
  approved: 'Approved — ready to execute',
  rejected: 'Rejected — this action will not run',
  executed: 'Executed',
  expired: 'Expired — the approval window closed',
};

const STATUS_STYLE: Record<ActionStatus, string> = {
  pending: 'border-amber-600 text-amber-200',
  approved: 'border-cyan-600 text-cyan-200',
  rejected: 'border-rose-600 text-rose-200',
  executed: 'border-emerald-600 text-emerald-200',
  expired: 'border-slate-600 text-slate-300',
};

type ActionControlsProps = {
  actionId: string;
  status: ActionStatus;
  role: Role;
  /** True when the signed-in user proposed this action (they cannot approve it). */
  ownProposal?: boolean;
  onStatusChange: (actionId: string, status: ActionStatus) => void;
  onError: (error: unknown, fallback: string) => void;
};

/**
 * Shows an action's state and, for administrators, the next allowed step.
 * The API enforces every rule; hiding buttons here only avoids dead-end clicks.
 */
export function ActionControls({
  actionId,
  status,
  role,
  ownProposal = false,
  onStatusChange,
  onError,
}: ActionControlsProps) {
  const [busy, setBusy] = useState(false);

  async function run(request: () => Promise<ActionStatus>, fallback: string) {
    setBusy(true);
    try {
      onStatusChange(actionId, await request());
    } catch (error) {
      onError(error, fallback);
      // Another administrator may have acted first; show the server's current state.
      await apiRequest<{ status: ActionStatus }>(`/actions/${actionId}`)
        .then((action) => onStatusChange(actionId, action.status))
        .catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  const decide = (decision: 'approved' | 'rejected') =>
    run(async () => {
      const approval = await apiRequest<{ decision: 'approved' | 'rejected' }>(
        `/actions/${actionId}/approve`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': `web-${decision}-${actionId}` },
          body: JSON.stringify({ decision }),
        },
      );
      return approval.decision;
    }, 'Unable to record the decision.');

  const execute = () =>
    run(async () => {
      const action = await apiRequest<{ status: ActionStatus }>(
        `/actions/${actionId}/execute`,
        { method: 'POST' },
      );
      return action.status;
    }, 'Unable to execute the action.');

  const buttonClass =
    'rounded px-2 py-1 text-xs font-medium text-slate-950 disabled:opacity-50';

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span
        className={`rounded-full border px-2 py-0.5 text-xs ${STATUS_STYLE[status]}`}
        role="status"
      >
        {status === 'pending' && ownProposal && role === 'admin'
          ? 'Awaiting approval from another administrator'
          : STATUS_TEXT[status]}
      </span>
      {role === 'admin' && status === 'pending' && (
        <>
          {!ownProposal && (
            <button
              type="button"
              className={`${buttonClass} bg-amber-400`}
              disabled={busy}
              onClick={() => void decide('approved')}
            >
              Approve
            </button>
          )}
          <button
            type="button"
            className={`${buttonClass} bg-slate-300`}
            disabled={busy}
            onClick={() => void decide('rejected')}
          >
            Reject
          </button>
        </>
      )}
      {role === 'admin' && status === 'approved' && (
        <button
          type="button"
          className={`${buttonClass} bg-emerald-400`}
          disabled={busy}
          onClick={() => void execute()}
        >
          Execute
        </button>
      )}
    </div>
  );
}

export function describeAction(
  kind: string | undefined,
  parameters: Record<string, unknown> | undefined,
) {
  if (kind === 'restart_service') {
    return `Restart the ${String(parameters?.service ?? 'unknown')} service`;
  }
  if (kind === 'rollback_deployment') {
    return `Roll back deployment ${String(parameters?.deployment ?? '')}`.trim();
  }
  return kind ?? 'Operational action';
}
