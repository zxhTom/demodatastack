import client from './client'

export interface LoginRequest {
  username: string
  password: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
  user_id: number
  username: string
  role: string
}

export const login = (data: LoginRequest) =>
  client.post<TokenResponse>('/auth/login', data).then((r) => r.data)

export const getMe = () =>
  client.get('/auth/me').then((r) => r.data)
