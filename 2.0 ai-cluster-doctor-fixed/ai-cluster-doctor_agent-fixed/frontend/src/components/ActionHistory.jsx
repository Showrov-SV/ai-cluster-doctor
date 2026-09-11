import React from 'react'

export default function ActionHistory({ actions }) {
  if (!actions || actions.length === 0) return null

  return (
    <div className="action-history" style={{ marginTop: 10, fontSize: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>Action History</div>
      {actions.map((a) => (
        <div key={a.id} style={{ padding: '4px 0', borderTop: '1px solid rgba(255,255,255,0.08)' }}>
          <div>{a.action}</div>
          <div style={{ opacity: 0.7 }}>
            {a.before_score ?? '--'} → {a.after_score ?? '--'} {a.result ? `(${a.result})` : ''}
          </div>
        </div>
      ))}
    </div>
  )
}
