import React from 'react'

export default function AlertBar({ nodes }) {
  const alerts = nodes.filter((n) => n.status !== 'healthy').sort((a, b) => a.health_score - b.health_score)
  if (!alerts.length) return null
  return (
    <div className="alert-bar">
      {alerts.map((n) => (
        <div className={`alert-chip ${n.status}`} key={n.node_id}>
          {n.node_id}: {n.root_cause || 'degraded health score'}
        </div>
      ))}
    </div>
  )
}
