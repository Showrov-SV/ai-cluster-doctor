import React, { useEffect, useRef, useState, useCallback } from 'react'
import { fetchNodes, fetchHistory, API_BASE } from '../api'
import EcgStrip from './EcgStrip.jsx'
import SummaryPills from './SummaryPills.jsx'
import AlertBar from './AlertBar.jsx'
import NodeGrid from './NodeGrid.jsx'
import ChatWidget from './ChatWidget.jsx'

export default function Dashboard({ onUnauthorized, onLogout }) {
  const [nodes, setNodes] = useState([])
  const [error, setError] = useState(null)
  const histCacheRef = useRef({})
  const ecgRef = useRef(null)
  const chatRef = useRef(null)
  const firstLoadRef = useRef(true)

  const load = useCallback(async () => {
    try {
      const data = await fetchNodes()

      // fetch short history for sparklines (only temp, small limit) - same
      // approach as the original: keep the previous cache entry on failure
      await Promise.all(
        data.map(async (n) => {
          try {
            histCacheRef.current[n.node_id] = await fetchHistory(n.node_id, 40)
          } catch (e) {
            // keep previous cache
          }
        }),
      )

      setNodes(data)
      setError(null)
      firstLoadRef.current = false

      const counts = { healthy: 0, warning: 0, critical: 0 }
      data.forEach((n) => {
        counts[n.status] = (counts[n.status] || 0) + 1
      })
      ecgRef.current?.update(counts)
    } catch (e) {
      if (e.status === 401) {
        onUnauthorized()
        return
      }
      if (firstLoadRef.current) {
        setError(
          `Can't reach the API at ${API_BASE}. Start the backend with: uvicorn main:app --reload --port 8000 (see README.md)`,
        )
      }
    }
  }, [onUnauthorized])

  useEffect(() => {
    load()
    const id = setInterval(load, 3000)
    return () => clearInterval(id)
  }, [load])

  return (
    <>
      <header>
        <div className="header-top">
          <div className="brand">
            <svg className="brand-mark" viewBox="0 0 34 34" fill="none">
              <circle cx="17" cy="17" r="16" stroke="#35d488" strokeWidth="1.4" opacity="0.5" />
              <path
                d="M6 17h5l2.2-7 4 14 2.4-9.5L21.5 17H28"
                stroke="#35d488"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            <div>
              <h1>AI Cluster Doctor</h1>
              <div className="subtitle">predictive GPU cluster health monitoring</div>
            </div>
          </div>
          <SummaryPills nodes={nodes} onLogout={onLogout} />
        </div>
        <EcgStrip ref={ecgRef} />
      </header>
      <main>
        <AlertBar nodes={nodes} />
        <NodeGrid
          nodes={nodes}
          histCache={histCacheRef.current}
          onCardClick={(id) => chatRef.current?.askAbout(id)}
          error={error}
        />
      </main>
      <ChatWidget ref={chatRef} onUnauthorized={onUnauthorized} />
    </>
  )
}
