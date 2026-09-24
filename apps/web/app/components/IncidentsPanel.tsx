'use client';

import React from 'react';
import { ActionStatus, Incident, Role } from '../lib/api';
import { ActionControls, describeAction } from './ActionControls';

type IncidentsPanelProps = {
  incidents: Incident[];
  loading: boolean;
  role: Role;
  actionStatuses: Record<string, ActionStatus>;
  onRefresh: () => void;
  onStatusChange: (actionId: string, status: ActionStatus) => void;
  onError: (error: unknown, fallback: string) => void;
};

export function IncidentsPanel({
  incidents,
  loading,
  role,
  actionStatuses,
  onRefresh,
  onStatusChange,
  onError,
}: IncidentsPanelProps) {
  return (
    <section
      aria-labelledby="incidents-heading"
      className="mt-6 rounded-xl border border-slate-800 bg-slate-900 p-6"
    >
      <div className="flex items-center justify-between gap-4">
        <h2 id="incidents-heading" className="text-xl font-medium">
          Incidents and approvals
        </h2>
        <button
          type="button"
          className="rounded border border-slate-600 px-3 py-1.5 text-sm hover:border-cyan-400 disabled:opacity-50"
          onClick={onRefresh}
          disabled={loading}
        >
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>
      <p className="mt-1 text-sm text-slate-400">
        {role === 'admin'
          ? 'Review proposed actions from every analyst. Nothing runs until you approve and execute it.'
          : 'Your incident proposals. An administrator must approve each action before it can run.'}
      </p>
      {loading && incidents.length === 0 ? (
        <p className="mt-4 text-sm text-slate-400">Loading incidents…</p>
      ) : incidents.length === 0 ? (
        <p className="mt-4 text-sm text-slate-400">
          No incidents yet. Ask the assistant to “create an incident and restart
          the api service”.
        </p>
      ) : (
        <ul className="mt-4 divide-y divide-slate-800">
          {incidents.map((incident) => (
            <li key={incident.id} className="py-3">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <p className="font-medium">{incident.title}</p>
                <p className="text-xs uppercase tracking-wide text-slate-400">
                  {incident.severity} · {incident.status} ·{' '}
                  {new Date(incident.created_at).toLocaleString()}
                </p>
              </div>
              {incident.actions.length === 0 ? (
                <p className="mt-1 text-sm text-slate-400">
                  No operational action attached.
                </p>
              ) : (
                <ul className="mt-2 space-y-2">
                  {incident.actions.map((action) => (
                    <li
                      key={action.id}
                      className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-sm"
                    >
                      <span>
                        {describeAction(action.kind, action.parameters)}
                      </span>
                      <ActionControls
                        actionId={action.id}
                        status={actionStatuses[action.id] ?? action.status}
                        role={role}
                        onStatusChange={onStatusChange}
                        onError={onError}
                      />
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
