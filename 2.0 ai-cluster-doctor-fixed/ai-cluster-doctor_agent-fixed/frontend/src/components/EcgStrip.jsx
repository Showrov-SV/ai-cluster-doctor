import React, { useEffect, useRef, useImperativeHandle, forwardRef } from 'react'

function computeEcgPath(counts, phase) {
  const worstStatus = counts.critical ? 'critical' : counts.warning ? 'warning' : 'healthy'
  const jitter = worstStatus === 'critical' ? 14 : worstStatus === 'warning' ? 7 : 3
  let d = 'M0,23 '
  for (let x = 0; x <= 1000; x += 20) {
    const spike = Math.floor((x + phase) / 140) % 7 === 0
    const y = spike ? 23 - jitter - Math.random() * jitter : 23 + (Math.random() * 2 - 1)
    d += `L${x},${y.toFixed(1)} `
  }
  return { d, worstStatus }
}

const EcgStrip = forwardRef(function EcgStrip(_, ref) {
  const pathRef = useRef(null)
  const phaseRef = useRef(0)

  function update(counts) {
    const { d, worstStatus } = computeEcgPath(counts, phaseRef.current)
    if (pathRef.current) {
      pathRef.current.setAttribute('class', 'ecg-line ' + worstStatus)
      pathRef.current.setAttribute('d', d)
    }
    phaseRef.current += 6
  }

  useImperativeHandle(ref, () => ({ update }))

  useEffect(() => {
    // Idle jitter between real data fetches, ported exactly from the
    // original - it intentionally always passes healthy counts here (a
    // pre-existing quirk carried over unchanged; not something Phase 6
    // was asked to fix).
    const id = setInterval(() => update({ critical: 0, warning: 0 }), 800)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="ecg-wrap">
      <svg id="ecg" viewBox="0 0 1000 46" preserveAspectRatio="none">
        <path ref={pathRef} className="ecg-line" />
      </svg>
    </div>
  )
})

export default EcgStrip
