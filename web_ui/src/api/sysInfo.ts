import http from './http'

export const getSysInfo = async (): Promise<any> => {
  const data = await http.get('/wx/sys/info')
  return data
}

export const getSysResources = async (): Promise<any> => {
  const data = await http.get('/wx/sys/resources')
  return data
}

export const refreshArticleStats = async (): Promise<any> => {
  const data = await http.post('/wx/sys/article/refresh')
  return data
}

export const getWxAccountStatuses = async (): Promise<any> => {
  return await http.get('/wx/auth/accounts/status')
}

export const getWxAccountToken = async (accountId: string): Promise<any> => {
  return await http.get(`/wx/auth/accounts/${encodeURIComponent(accountId)}/token`)
}

export const refreshWxAccountStatuses = async (): Promise<any> => {
  return await http.post('/wx/auth/accounts/refresh')
}

export const switchWxAccount = async (accountId: string, accountName = ''): Promise<any> => {
  return await http.post('/wx/auth/switch', null, {
    params: { account_id: accountId, account_name: accountName }
  })
}

export const getWxAccountStatusJob = async (jobId: string): Promise<any> => {
  return await http.get('/wx/auth/switch/status', { params: { job_id: jobId } })
}
