const API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000'
const TOKEN_KEY = 'cluster_doctor_token'

function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export function isLoggedIn() {
  return !!getToken()
}

export function logout() {
  setToken(null)
}

async function apiFetch(path, options = {}) {
  const token = getToken()
  const headers = { ...(options.headers || {}) }
  if (token) headers['Authorization'] = `Bearer ${token}`
  const res = await fetch(API_BASE + path, { ...options, headers })
  if (res.status === 401) {
    setToken(null)
    const err = new Error('Session expired - please sign in again.')
    err.status = 401
    throw err
  }
  return res
}

export async function login(username, password) {
  const res = await fetch(API_BASE + '/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    throw new Error('Invalid username or password')
  }
  const data = await res.json()
  setToken(data.access_token)
  return data
}

export async function fetchNodes() {
  const res = await apiFetch('/api/nodes')
  const data = await res.json()
  if (!Array.isArray(data)) throw new Error('bad payload')
  return data
}

export async function fetchHistory(nodeId, limit = 40) {
  const res = await apiFetch(`/api/nodes/${encodeURIComponent(nodeId)}/history?limit=${limit}`)
  const data = await res.json()
  return data.history
}

export async function sendChat(message) {
  const res = await apiFetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  })
  const data = await res.json()
  return data.reply
}

export { API_BASE }
