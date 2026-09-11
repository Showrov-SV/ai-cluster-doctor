import React from 'react'

export default function SummaryPills({ nodes, onLogout }) {
  const counts = { healthy: 0, warning: 0, critical: 0 }
  nodes.forEach((n) => {
    counts[n.status] = (counts[n.status] || 0) + 1
  })
  return (
    <div className="summary-pills">
      <div className="pill">
        <span className="dot healthy"></span>
        {counts.healthy || 0} healthy
      </div>
      <div className="pill">
        <span className="dot warning"></span>
        {counts.warning || 0} warning
      </div>
      <div className="pill">
        <span className="dot critical"></span>
        {counts.critical || 0} critical
      </div>
      <div className="pill logout-pill" onClick={onLogout}>
        sign out
      </div>
    </div>
  )
}
