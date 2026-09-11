import React, { useState, useRef, useImperativeHandle, forwardRef, useEffect } from 'react'
import { sendChat } from '../api'

const SUGGESTIONS = [
  { q: 'cluster status', label: 'cluster status' },
  { q: 'which nodes are critical', label: 'critical nodes' },
  { q: 'any predicted failures', label: 'predictions' },
]

const ChatWidget = forwardRef(function ChatWidget({ onUnauthorized }, ref) {
  const [open, setOpen] = useState(false)
  const [messages, setMessages] = useState([
    {
      text: 'Ask me things like "cluster status", "which nodes are critical", or "tell me about gpu-node-03".',
      cls: 'bot',
    },
  ])
  const [input, setInput] = useState('')
  const bodyRef = useRef(null)

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight
  }, [messages])

  async function send(text) {
    if (!text || !text.trim()) return
    setMessages((m) => [...m, { text, cls: 'user' }])
    setInput('')
    try {
      const reply = await sendChat(text)
      setMessages((m) => [...m, { text: reply, cls: 'bot' }])
    } catch (e) {
      if (e.status === 401) {
        onUnauthorized()
        return
      }
      setMessages((m) => [...m, { text: "Can't reach the backend API right now.", cls: 'bot' }])
    }
  }

  useImperativeHandle(ref, () => ({
    askAbout(nodeId) {
      setOpen(true)
      send(`tell me about ${nodeId}`)
    },
  }))

  return (
    <>
      <button className="chat-fab" onClick={() => setOpen((o) => !o)} title="Ask the cluster doctor">
        💬
      </button>
      <div className={`chat-panel ${open ? 'open' : ''}`}>
        <div className="chat-head">
          <span>Cluster Doctor Assistant</span>
          <span className="tag">AI-backed · offline fallback</span>
        </div>
        <div className="chat-body" ref={bodyRef}>
          {messages.map((m, i) => (
            <div className={`msg ${m.cls}`} key={i}>
              {m.text}
            </div>
          ))}
        </div>
        <div className="chat-suggest">
          {SUGGESTIONS.map((sugg) => (
            <div className="chip-btn" key={sugg.q} onClick={() => send(sugg.q)}>
              {sugg.label}
            </div>
          ))}
        </div>
        <div className="chat-input">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') send(input)
            }}
            placeholder="Ask about the cluster…"
          />
          <button onClick={() => send(input)}>Send</button>
        </div>
      </div>
    </>
  )
})

export default ChatWidget
