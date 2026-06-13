import { create } from 'zustand'

interface UserInfo {
  id: number
  username: string
  role: string
  email?: string
}

interface AuthState {
  user: UserInfo | null
  token: string | null
  login: (token: string, user: UserInfo) => void
  logout: () => void
  isAuthenticated: () => boolean
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: (() => {
    try { return JSON.parse(localStorage.getItem('user') || 'null') } catch { return null }
  })(),
  token: localStorage.getItem('token'),

  login: (token, user) => {
    localStorage.setItem('token', token)
    localStorage.setItem('user', JSON.stringify(user))
    set({ token, user })
  },

  logout: () => {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    set({ token: null, user: null })
  },

  isAuthenticated: () => !!get().token,
}))
