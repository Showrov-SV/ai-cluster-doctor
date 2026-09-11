import React, { useState } from 'react'
import { isLoggedIn, logout } from './api'
import LoginScreen from './components/LoginScreen.jsx'
import Dashboard from './components/Dashboard.jsx'

export default function App() {
  const [loggedIn, setLoggedIn] = useState(isLoggedIn())

  if (!loggedIn) {
    return <LoginScreen onLoggedIn={() => setLoggedIn(true)} />
  }

  return (
    <Dashboard
      onUnauthorized={() => setLoggedIn(false)}
      onLogout={() => {
        logout()
        setLoggedIn(false)
      }}
    />
  )
}
