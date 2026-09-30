<template>
  <div class="wechat-status-page">
    <a-page-header title="公众号状态" sub-title="微信公众号授权信息">
      <template #extra>
        <a-button type="primary" @click="refreshStatus">
          <template #icon><icon-refresh /></template>
          刷新状态
        </a-button>
      </template>
    </a-page-header>

    <!-- 授权状态卡片 -->
    <a-card :bordered="false" class="status-card">
      <template #title>
        <div class="card-title">
          <icon-wechat class="wechat-icon" />
          授权状态
        </div>
      </template>
      
      <div class="status-content" v-if="wxLoginInfo">
        <!-- 公众号基本信息 -->
        <div class="account-header">
          <a-avatar :size="80" class="account-avatar">
            <img v-if="wxLoginInfo.ext_data?.wx_logo" :src="wxLoginInfo.ext_data.wx_logo" alt="公众号头像">
            <icon-wechat v-else />
          </a-avatar>
          <div class="account-info">
            <h2 class="account-name">{{ wxLoginInfo.ext_data?.wx_app_name || '未知公众号' }}</h2>
            <a-tag :color="haswxLogined ? 'green' : 'red'" size="large">
              {{ haswxLogined ? '已授权' : '未授权' }}
            </a-tag>
          </div>
        </div>

        <!-- 详细信息 -->
        <a-descriptions :column="{ xs: 1, sm: 2, md: 3, lg: 4 }" bordered class="info-descriptions">
          <a-descriptions-item label="昨日阅读">
            <span class="stat-value">{{ wxLoginInfo.ext_data?.wx_read_yesterday || 0 }}</span>
          </a-descriptions-item>
          <a-descriptions-item label="昨日分享">
            <span class="stat-value">{{ wxLoginInfo.ext_data?.wx_share_yesterday || 0 }}</span>
          </a-descriptions-item>
          <a-descriptions-item label="昨日关注">
            <span class="stat-value">{{ wxLoginInfo.ext_data?.wx_watch_yesterday || 0 }}</span>
          </a-descriptions-item>
          <a-descriptions-item label="原创数">
            <span class="stat-value">{{ wxLoginInfo.ext_data?.wx_yuan_count || 0 }}</span>
          </a-descriptions-item>
          <a-descriptions-item label="粉丝数">
            <span class="stat-value">{{ wxLoginInfo.ext_data?.wx_user_count || 0 }}</span>
          </a-descriptions-item>
          <a-descriptions-item label="Token状态">
            <a-tag :color="haswxLogined ? 'green' : 'red'">
              {{ haswxLogined ? '有效' : '已失效' }}
            </a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="到期时间" :span="2">
            <span v-if="wxLoginInfo.expiry?.expiry_time">{{ wxLoginInfo.expiry.expiry_time }}</span>
            <span v-else class="text-muted">未知</span>
          </a-descriptions-item>
        </a-descriptions>

        <!-- Token 信息 -->
        <div class="token-section" v-if="wxLoginInfo.token">
          <div class="token-header">
            <span class="token-label">Token:</span>
            <a-button size="small" @click="copyToken">
              <template #icon><icon-copy /></template>
              复制
            </a-button>
          </div>
          <div class="token-value">{{ wxLoginInfo.token }}</div>
        </div>
      </div>

      <!-- 未授权状态 -->
      <div class="empty-status" v-else>
        <a-empty description="暂无公众号授权信息">
          <a-button type="primary" @click="showAuthQrcode">
            <template #icon><icon-scan /></template>
            扫码授权
          </a-button>
        </a-empty>
      </div>
    </a-card>

    <!-- Multi-account status is refreshed on demand because the backend has one active MP session. -->
    <a-card :bordered="false" class="accounts-card">
      <template #title>
        <div class="card-title">
          <icon-wechat class="wechat-icon" />
          公众号账号与 Token 有效期
        </div>
      </template>

      <template #extra>
        <span v-if="accountStatus.checked_at" class="text-muted">
          检查时间：{{ accountStatus.checked_at }}
        </span>
      </template>

      <div class="account-list">
        <div
          v-for="(account, index) in accountSlots"
          :key="account?.account_id || `empty-${index}`"
          class="account-row"
          :class="{
            'account-row--active': account?.is_active,
            'account-row--placeholder': !account
          }"
        >
          <template v-if="account">
            <div class="account-row-heading">
              <div>
                <h3 class="account-row-name">{{ account.name || '未命名公众号' }}</h3>
                <span v-if="account.account_id" class="account-row-id">{{ account.account_id }}</span>
              </div>
              <a-space wrap>
                <a-tag v-if="account.is_active" color="green">当前激活</a-tag>
                <a-tag v-if="account.collector_associated" color="green">当前抓取会话</a-tag>
                <a-tag :color="tokenStatusColor(account.token_status)">
                  {{ tokenStatusText(account.token_status) }}
                </a-tag>
                <a-button
                  v-if="account.account_id"
                  size="small"
                  :type="account.is_active ? 'primary' : 'outline'"
                  :loading="switchingAccountId === account.account_id"
                  :disabled="account.is_active || switching || refreshingAccounts"
                  @click="switchAccount(account)"
                >
                  {{ account.is_active ? '当前账号' : '切换到此账号' }}
                </a-button>
              </a-space>
            </div>
            <div class="account-row-expiry">
              <span class="token-label">Token 有效期</span>
              <span v-if="account.expiry?.expiry_time">{{ account.expiry.expiry_time }}</span>
              <span v-else class="text-muted">未知</span>
              <span v-if="account.expiry?.remaining_seconds != null" class="text-muted">
                （剩余 {{ formatRemaining(account.expiry.remaining_seconds) }}）
              </span>
              <span v-if="account.session_updated_at" class="text-muted">
                会话保存：{{ account.session_updated_at }}
              </span>
            </div>
            <div class="account-row-stats">
              <div><span>昨日阅读</span><strong>{{ displayMetric(account.ext_data?.wx_read_yesterday) }}</strong></div>
              <div><span>昨日分享</span><strong>{{ displayMetric(account.ext_data?.wx_share_yesterday) }}</strong></div>
              <div><span>昨日关注</span><strong>{{ displayMetric(account.ext_data?.wx_watch_yesterday) }}</strong></div>
              <div><span>原创数</span><strong>{{ displayMetric(account.ext_data?.wx_yuan_count) }}</strong></div>
              <div><span>粉丝数</span><strong>{{ displayMetric(account.ext_data?.wx_user_count) }}</strong></div>
            </div>
            <div v-if="account.has_saved_session" class="account-token">
              <div class="account-token-actions">
                <span class="token-label">该账号已保存独立 Token/Cookie 会话（Cookie 不在页面展示）</span>
                <a-space>
                  <a-button size="small" @click="toggleAccountToken(account)">
                    {{ revealedTokens[account.account_id] ? '隐藏 Token' : '查看 Token' }}
                  </a-button>
                  <a-button v-if="revealedTokens[account.account_id]" size="small" @click="copyAccountToken(account.account_id)">
                    <template #icon><icon-copy /></template>
                    复制
                  </a-button>
                </a-space>
              </div>
              <div v-if="revealedTokens[account.account_id]" class="account-token-value">
                {{ revealedTokens[account.account_id] }}
              </div>
            </div>
            <div v-else-if="account.account_id" class="account-session-missing">
              尚无完整可恢复会话；重新扫码授权后会自动保存，或手动刷新账号状态补齐。
            </div>
          </template>
          <div v-else class="account-row-empty">暂无第 {{ index + 1 }} 个账号；刷新时会保留此行框架</div>
        </div>
      </div>

      <div class="account-status-actions">
        <span class="account-rotation-hint">
          扫码授权成功后自动保存当前账号的 talking/token 与 Cookie，并保留最近三个有效账号；普通采集任务按有效 account_id 顺序轮转，微信读书模式不切换公众号账号。
        </span>
        <a-button type="primary" :loading="refreshingAccounts" :disabled="switching" @click="refreshAccountStatuses">
          <template #icon><icon-refresh /></template>
          手动刷新账号状态
        </a-button>
      </div>

      <div v-if="refreshingAccounts" class="account-refresh-progress">
        <a-progress :percent="accountRefreshProgress" />
        <span>{{ accountRefreshMessage || '正在读取账号状态…' }}</span>
      </div>

      <div v-if="switching" class="account-switch-progress">
        <a-progress :percent="accountSwitchProgress" />
        <span>{{ accountSwitchMessage || '正在切换公众号账号…' }}</span>
      </div>
    </a-card>

    <!-- 操作卡片 -->
    <a-card :bordered="false" class="action-card">
      <template #title>
        <div class="card-title">
          <icon-tool class="tool-icon" />
          操作
        </div>
      </template>
      
      <a-space wrap>
        <a-button type="primary" @click="showAuthQrcode">
          <template #icon><icon-scan /></template>
          扫码授权
        </a-button>
        <a-popconfirm content="确定要刷新Token吗？" @ok="refreshToken">
          <a-button type="outline" status="warning">
            <template #icon><icon-refresh /></template>
            刷新Token
          </a-button>
        </a-popconfirm>
      </a-space>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, inject } from 'vue'
import { Message } from '@arco-design/web-vue'
import {
  getSysInfo,
  getWxAccountStatuses,
  getWxAccountToken,
  refreshWxAccountStatuses,
  switchWxAccount,
  getWxAccountStatusJob
} from '@/api/sysInfo'

const haswxLogined = ref(false)
const wxLoginInfo = ref<any>(null)
const switchingAccountId = ref('')
const switching = computed(() => Boolean(switchingAccountId.value))
const accountStatus = ref<any>({ accounts: [], cached: false })
const refreshingAccounts = ref(false)
const accountRefreshProgress = ref(0)
const accountRefreshMessage = ref('')
const accountSwitchProgress = ref(0)
const accountSwitchMessage = ref('')
const revealedTokens = ref<Record<string, string>>({})
const accountSlots = computed(() => {
  const accounts = accountStatus.value?.accounts || []
  return Array.from({ length: Math.max(3, accounts.length) }, (_, index) => accounts[index] || null)
})

// 从父组件注入扫码授权方法
const showAuthQrcode = inject<() => void>('showAuthQrcode', () => {
  Message.warning('请从页面头部进行扫码授权')
})

const fetchSysInfo = async () => {
  try {
    const res = await getSysInfo()
    haswxLogined.value = res?.wx?.login || false
    wxLoginInfo.value = res?.wx?.info || null
  } catch (error) {
    console.error('获取系统信息失败', error)
  }
}

const fetchAccountStatuses = async () => {
  try {
    accountStatus.value = await getWxAccountStatuses()
  } catch (error) {
    console.error('获取公众号账号状态失败', error)
  }
}

const waitForAccountJob = async (job: any, onProgress?: (status: any) => void) => {
  const jobId = job?.job_id
  if (!jobId) throw new Error('后台任务未返回 job_id')

  for (let attempt = 0; attempt < 600; attempt += 1) {
    await new Promise(resolve => window.setTimeout(resolve, 1000))
    const status = await getWxAccountStatusJob(jobId)
    onProgress?.(status)
    if (status?.finished_at || status?.ok === true || status?.ok === false) {
      if (!status.ok) throw new Error(status.message || '后台账号任务失败')
      return status
    }
  }
  throw new Error('后台任务仍在运行；可稍后刷新页面查看结果')
}

const tokenStatusText = (status: string) => {
  if (status === 'valid') return '有效'
  if (status === 'expired') return '已过期'
  return '待核验'
}

const tokenStatusColor = (status: string) => {
  if (status === 'valid') return 'green'
  if (status === 'expired') return 'red'
  return 'orangered'
}

const formatRemaining = (seconds: number) => {
  const remaining = Math.max(0, Number(seconds) || 0)
  const days = Math.floor(remaining / 86400)
  const hours = Math.floor((remaining % 86400) / 3600)
  const minutes = Math.floor((remaining % 3600) / 60)
  if (days) return `${days}天 ${hours}小时`
  if (hours) return `${hours}小时 ${minutes}分钟`
  return `${minutes}分钟`
}

const refreshAccountStatuses = async () => {
  if (refreshingAccounts.value) return
  refreshingAccounts.value = true
  accountRefreshProgress.value = 0
  accountRefreshMessage.value = '任务启动中…'
  try {
    const job = await refreshWxAccountStatuses()
    await waitForAccountJob(job, status => {
      accountRefreshProgress.value = Number(status?.progress) || 0
      accountRefreshMessage.value = status?.message || '正在读取账号状态…'
    })
    await Promise.all([fetchSysInfo(), fetchAccountStatuses()])
    Message.success('公众号账号状态已刷新')
  } catch (error: any) {
    accountRefreshMessage.value = error?.message || '刷新账号状态失败'
    Message.error(accountRefreshMessage.value)
    await fetchAccountStatuses()
  } finally {
    refreshingAccounts.value = false
  }
}

const refreshStatus = () => {
  fetchSysInfo()
  fetchAccountStatuses()
  Message.success('状态已刷新')
}

const copyToken = () => {
  if (wxLoginInfo.value?.token) {
    navigator.clipboard.writeText(wxLoginInfo.value.token)
    Message.success('Token已复制到剪贴板')
  }
}

const switchAccount = async (account: any) => {
  const accountId = String(account?.account_id || '').trim()
  if (!accountId || switching.value || refreshingAccounts.value) return

  switchingAccountId.value = accountId
  accountSwitchProgress.value = 0
  accountSwitchMessage.value = `准备切换到 ${account.name || accountId}…`
  try {
    const job = await switchWxAccount(accountId)
    const completed = await waitForAccountJob(job, status => {
      accountSwitchProgress.value = Number(status?.progress) || 0
      accountSwitchMessage.value = status?.message || '正在切换公众号账号…'
    })
    Message.success(completed.message || `已切换到 ${account.name || accountId}`)
    await Promise.all([fetchSysInfo(), fetchAccountStatuses()])
  } catch (error: any) {
    const message = typeof error === 'string' ? error : error?.message
    accountSwitchMessage.value = message || '切换账号失败'
    Message.error(accountSwitchMessage.value)
  } finally {
    switchingAccountId.value = ''
  }
}

const displayMetric = (value: unknown) => {
  if (value == null || value === '') return '—'
  return String(value)
}

const toggleAccountToken = async (account: any) => {
  const accountId = String(account?.account_id || '')
  if (!accountId) return
  if (revealedTokens.value[accountId]) {
    const next = { ...revealedTokens.value }
    delete next[accountId]
    revealedTokens.value = next
    return
  }
  try {
    const result = await getWxAccountToken(accountId)
    const token = String(result?.token || '')
    if (!token) throw new Error('服务端没有返回已缓存的 Token')
    revealedTokens.value = { ...revealedTokens.value, [accountId]: token }
  } catch (error: any) {
    Message.error(error?.message || '读取 Token 失败')
  }
}

const copyAccountToken = async (accountId: string) => {
  const token = revealedTokens.value[accountId]
  if (!token) return
  try {
    await navigator.clipboard.writeText(token)
    Message.success('Token 已复制到剪贴板')
  } catch {
    Message.error('复制失败，请检查浏览器剪贴板权限')
  }
}

const refreshToken = async () => {
  try {
    Message.info('正在刷新Token...')
    // 这里可以调用刷新Token的逻辑
    await fetchSysInfo()
    Message.success('Token刷新成功')
  } catch (error) {
    Message.error('Token刷新失败')
  }
}

onMounted(() => {
  fetchSysInfo()
  fetchAccountStatuses()
  window.addEventListener('wx-account-updated', refreshAccountView)
})

const refreshAccountView = () => {
  fetchSysInfo()
  fetchAccountStatuses()
}

onUnmounted(() => {
  window.removeEventListener('wx-account-updated', refreshAccountView)
})
</script>

<style scoped>
.wechat-status-page {
  padding: 20px;
}

.status-card,
.accounts-card,
.action-card {
  margin-bottom: 16px;
  border-radius: 8px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.08);
}

.card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 500;
}

.wechat-icon {
  color: #07c160;
  font-size: 20px;
}

.tool-icon {
  color: #165dff;
  font-size: 20px;
}

.account-header {
  display: flex;
  align-items: center;
  gap: 24px;
  margin-bottom: 24px;
  padding: 16px;
  background: linear-gradient(135deg, #f5f7fa 0%, #e4e8ec 100%);
  border-radius: 12px;
}

.account-avatar {
  background: #07c160;
  color: white;
}

.account-name {
  margin: 0 0 8px 0;
  font-size: 24px;
  font-weight: 600;
}

.account-info {
  flex: 1;
}

.info-descriptions {
  margin-top: 16px;
}

.stat-value {
  font-size: 18px;
  font-weight: 600;
  color: #165dff;
}

.token-section {
  margin-top: 24px;
  padding: 16px;
  background: #f7f8fa;
  border-radius: 8px;
}

.token-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.token-label {
  font-weight: 500;
  color: #86909c;
}

.token-value {
  font-family: monospace;
  font-size: 13px;
  word-break: break-all;
  color: #4e5969;
  padding: 8px;
  background: white;
  border-radius: 4px;
}

.empty-status {
  padding: 60px 0;
  text-align: center;
}

.text-muted {
  color: #86909c;
}

.account-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.account-row {
  width: calc(100% - 32px);
  min-height: 88px;
  box-sizing: border-box;
  padding: 16px 20px;
  border: 1px solid #e5e6eb;
  border-radius: 10px;
  background: #fff;
}

.account-row:nth-child(even) {
  align-self: flex-end;
}

.account-row--active {
  border-color: #00a870;
  background: #f2fff8;
  box-shadow: 0 0 0 1px rgba(0, 168, 112, 0.08);
}

.account-row--placeholder {
  display: flex;
  align-items: center;
  border-style: dashed;
  background: #fafafa;
}

.account-row-heading,
.account-row-expiry,
.account-status-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.account-row-name {
  margin: 0 0 4px;
  font-size: 17px;
  font-weight: 600;
}

.account-row-id {
  color: #86909c;
  font-size: 12px;
  word-break: break-all;
}

.account-row-expiry {
  justify-content: flex-start;
  flex-wrap: wrap;
  margin-top: 12px;
}

.account-row-empty {
  color: #86909c;
}

.account-status-actions {
  margin-top: 18px;
}

.account-rotation-hint {
  max-width: 720px;
  color: #86909c;
  line-height: 1.6;
}

.account-refresh-progress {
  margin-top: 14px;
}

.account-row-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(105px, 1fr));
  gap: 10px;
  margin-top: 14px;
}

.account-row-stats > div {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 9px 11px;
  border-radius: 7px;
  background: rgba(22, 93, 255, 0.04);
}

.account-row-stats span {
  color: #86909c;
  font-size: 12px;
}

.account-row-stats strong {
  color: #1d2129;
  font-size: 15px;
  font-weight: 600;
}

.account-token {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid #e5e6eb;
}

.account-session-missing {
  margin-top: 12px;
  color: #86909c;
  font-size: 13px;
}

.account-token-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}

.account-token-value {
  margin-top: 10px;
  padding: 10px;
  border-radius: 6px;
  background: #f7f8fa;
  color: #4e5969;
  font-family: monospace;
  overflow-wrap: anywhere;
  user-select: all;
}

.account-switch-progress {
  margin-top: 14px;
}

.account-refresh-progress > span,
.account-switch-progress > span {
  display: block;
  margin-top: 6px;
  color: #4e5969;
}

@media (max-width: 768px) {
  .wechat-status-page {
    padding: 12px;
  }
  
  .account-header {
    flex-direction: column;
    text-align: center;
    gap: 16px;
  }
  
  .account-info {
    text-align: center;
  }

  .account-row {
    width: 100%;
  }

  .account-row:nth-child(even) {
    align-self: stretch;
  }

  .account-row-heading,
  .account-status-actions,
  .account-token-actions {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
