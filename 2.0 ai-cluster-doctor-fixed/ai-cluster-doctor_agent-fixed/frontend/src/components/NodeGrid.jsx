import React from 'react'
import NodeCard from './NodeCard.jsx'

export default function NodeGrid({ nodes, histCache, onCardClick, error }) {
  if (nodes.length === 0) {
    return (
      <div className="grid">
        <div className="offline-note">{error || 'Connecting to cluster telemetry API…'}</div>
      </div>
    )
  }
  return (
    <div className="grid">
      {nodes.map((n) => (
        <NodeCard key={n.node_id} node={n} history={histCache[n.node_id]} onClick={onCardClick} />
      ))}
    </div>
  )
}
