import React from 'react'

export default function ScoreBreakdown({ breakdown }) {
  if (!breakdown || breakdown.length === 0) return null

  return (
    <div className="score-breakdown" style={{ marginTop: 10, fontSize: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>Score Breakdown</div>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <tbody>
          {breakdown.map((row, i) => (
            <tr key={i}>
              <td style={{ padding: '2px 4px', opacity: 0.85 }}>{row.reason}</td>
              <td style={{ padding: '2px 4px', textAlign: 'right', color: 'var(--critical, #e5484d)' }}>
                {row.penalty}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
