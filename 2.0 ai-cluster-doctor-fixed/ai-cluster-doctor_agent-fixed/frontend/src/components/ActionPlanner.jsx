import React from 'react'

const PRIORITY_COLOR = {
  NOW: 'var(--critical, #e5484d)',
  TODAY: 'var(--warning, #f5a623)',
  LATER: 'var(--healthy, #30a46c)',
}

export default function ActionPlanner({ action }) {
  if (!action) return null

  return (
    <div
      className="action-planner"
      style={{ marginTop: 10, padding: 8, borderLeft: `3px solid ${PRIORITY_COLOR[action.priority] || '#999'}`, fontSize: 12 }}
    >
      <div style={{ fontWeight: 600, marginBottom: 2 }}>
        Recommended Action <span style={{ color: PRIORITY_COLOR[action.priority] || '#999' }}>[{action.priority}]</span>
      </div>
      <div>{action.action}</div>
      {action.reason && <div style={{ opacity: 0.7, marginTop: 2 }}>{action.reason}</div>}
    </div>
  )
}
