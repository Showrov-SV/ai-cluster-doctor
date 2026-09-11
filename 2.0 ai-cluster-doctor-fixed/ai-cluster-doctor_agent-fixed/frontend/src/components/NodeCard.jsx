import React from 'react'
import ScoreBreakdown from './ScoreBreakdown'
import ActionPlanner from './ActionPlanner'
import ActionHistory from './ActionHistory'

function sparkPath(history, key, w = 280, h = 36) {
  if (!history || history.length < 2) return ''
  const vals = history.map((r) => r[key])
  const min = Math.min(...vals)
  const max = Math.max(...vals)
  const range = max - min || 1
  const step = w / (vals.length - 1)
  return vals
    .map((v, i) => {
      const x = (i * step).toFixed(1)
      const y = (h - ((v - min) / range) * h).toFixed(1)
      return (i === 0 ? 'M' : 'L') + x + ',' + y
    })
    .join(' ')
}

function statusColor(s) {
  return s === 'critical' ? 'var(--critical)' : s === 'warning' ? 'var(--warning)' : 'var(--healthy)'
}

export default function NodeCard({ node, history, onClick }) {
  const s = node.status
  const latest = node.latest || {}
  const hotClass = latest.temperature_c >= 78 ? (latest.temperature_c >= 88 ? 'hot' : 'warn') : ''
  const memClass = latest.memory_utilization_pct >= 88 ? (latest.memory_utilization_pct >= 96 ? 'hot' : 'warn') : ''
  const fanClass = latest.fan_speed_pct <= 20 ? (latest.fan_speed_pct <= 10 ? 'hot' : 'warn') : ''

  const preds = node.predictions || {}
  const predTags = Object.entries(preds).map(([feat, p]) => {
    const label = feat.replace('_c', '').replace('_pct', '').replace('_', ' ')
    const eta = p.eta_ticks ? ` · ETA ~${Math.round(p.eta_ticks * 2)}s` : ''
    return (
      <span className="predict-tag" key={feat}>
        ⚠ predicted rise: {label}
        {eta}
      </span>
    )
  })

  const spark = sparkPath(history, 'temperature_c')

  return (
    <div className={`card ${s}`} onClick={() => onClick(node.node_id)}>
      <div className="card-top">
        <div>
          <div className="node-id">{node.node_id}</div>
          <div className="gpu-model">{node.gpu_model}</div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div className={`score-badge ${s}`}>{node.health_score}</div>
          <div className="score-label">{s}</div>
        </div>
      </div>
      <svg className="spark" viewBox="0 0 280 36" preserveAspectRatio="none">
        <path d={spark} stroke={statusColor(s)} />
      </svg>
      <div className="metrics">
        <div className="metric">
          <span className="k">Temp</span>
          <span className={`v ${hotClass}`}>{latest.temperature_c ?? '--'}°C</span>
        </div>
        <div className="metric">
          <span className="k">GPU Util</span>
          <span className="v">{latest.gpu_utilization_pct ?? '--'}%</span>
        </div>
        <div className="metric">
          <span className="k">Memory</span>
          <span className={`v ${memClass}`}>{latest.memory_utilization_pct ?? '--'}%</span>
        </div>
        <div className="metric">
          <span className="k">Power</span>
          <span className="v">{latest.power_watts ?? '--'} W</span>
        </div>
        <div className="metric">
          <span className="k">Fan</span>
          <span className={`v ${fanClass}`}>{latest.fan_speed_pct ?? '--'}%</span>
        </div>
        <div className="metric">
          <span className="k">Anomaly</span>
          <span className="v">{node.anomaly ? (node.anomaly.anomaly_score * 100).toFixed(0) + '%' : '--'}</span>
        </div>
      </div>
      {predTags}
      {node.root_cause && <div className={`root-cause ${s}`}>{node.root_cause}</div>}
      <ScoreBreakdown breakdown={node.score_audit?.score_breakdown} />
      <ActionPlanner action={node.planned_action} />
      <ActionHistory actions={node.action_history} />
    </div>
  )
}