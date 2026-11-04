<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { api } from '../api'

type LoginStatus = { loggedIn: boolean; uname: string; mid: number; savedAt: number }
type QrPayload = { key: string; url: string; image: string }
type Phase = 'idle' | 'loading' | 'waiting' | 'scanned' | 'expired' | 'ok' | 'error'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: []; changed: [] }>()

const status = ref<LoginStatus | null>(null)
const qr = ref<QrPayload | null>(null)
const phase = ref<Phase>('idle')
const message = ref('')
const error = ref('')
let timer: number | undefined

function stopPolling() {
  if (timer !== undefined) { window.clearInterval(timer); timer = undefined }
}

async function loadStatus() {
  try { status.value = await api<LoginStatus>('/bilibili/login/status') } catch { status.value = null }
}

async function start() {
  stopPolling()
  phase.value = 'loading'
  error.value = ''
  message.value = '正在向 B 站申请二维码…'
  try {
    qr.value = await api<QrPayload>('/bilibili/login/qrcode', { method: 'POST' })
    phase.value = 'waiting'
    message.value = '请用 B 站手机客户端扫码'
    timer = window.setInterval(() => { void poll() }, 2000)
  } catch (e) {
    phase.value = 'error'
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function poll() {
  if (!qr.value) return
  try {
    const result = await api<{ state: string; message: string; uname: string; mid: number }>(
      `/bilibili/login/poll?key=${encodeURIComponent(qr.value.key)}`,
    )
    if (result.state === 'ok') {
      stopPolling()
      phase.value = 'ok'
      message.value = `登录成功${result.uname ? '：' + result.uname : ''}`
      await loadStatus()
      emit('changed')
      return
    }
    if (result.state === 'expired') {
      stopPolling()
      phase.value = 'expired'
      message.value = result.message || '二维码已失效'
      return
    }
    phase.value = result.state === 'scanned' ? 'scanned' : 'waiting'
    message.value = result.message || (phase.value === 'scanned' ? '已扫码，请在手机上确认' : '等待扫码')
  } catch (e) {
    // 单次轮询失败不终止：网络抖一下不该让用户重扫
    message.value = '轮询出错，稍后自动重试：' + (e instanceof Error ? e.message : String(e))
  }
}

async function logout() {
  stopPolling()
  try {
    await api('/bilibili/logout', { method: 'POST' })
    qr.value = null
    phase.value = 'idle'
    message.value = '已退出，本地凭证已删除'
    await loadStatus()
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function close() {
  stopPolling()
  qr.value = null
  phase.value = 'idle'
  emit('close')
}

watch(() => props.open, (value) => {
  if (!value) { stopPolling(); return }
  error.value = ''
  message.value = ''
  qr.value = null
  phase.value = 'idle'
  void loadStatus().then(() => { if (!status.value?.loggedIn) void start() })
})

onUnmounted(stopPolling)
</script>

<template>
  <div v-if="open" class="bili-mask" @click.self="close">
    <section class="bili-panel" role="dialog" aria-label="B 站扫码登录">
      <header class="bili-head">
        <div>
          <h2>B 站扫码登录</h2>
          <p class="bili-sub">用你自己的 B 站手机客户端扫码确认。软件不会知道你的密码，只保存登录后的凭证。</p>
        </div>
        <button class="icon-button small" aria-label="关闭" @click="close">×</button>
      </header>

      <div v-if="status?.loggedIn" class="bili-logged">
        <p class="bili-ok">
          已登录<span v-if="status.uname">：{{ status.uname }}</span>
          <span v-if="status.mid" class="bili-mid">UID {{ status.mid }}</span>
        </p>
        <p class="bili-note">现在取字幕会带上这个登录态；退出后立刻回到匿名。</p>
        <div class="bili-actions">
          <button class="button subtle small" @click="logout">退出登录</button>
          <button class="button small" @click="close">完成</button>
        </div>
      </div>

      <div v-else class="bili-body">
        <div class="bili-qr">
          <img v-if="qr" :src="qr.image" alt="B 站登录二维码" width="220" height="220" />
          <div v-else class="bili-qr-empty">{{ phase === 'loading' ? '正在获取二维码…' : '还没有二维码' }}</div>
        </div>
        <div class="bili-side">
          <p :class="['bili-state', 'state-' + phase]">{{ message || '等待操作' }}</p>
          <ol class="bili-steps">
            <li>打开 B 站手机客户端</li>
            <li>右上角「扫一扫」</li>
            <li>扫码并在手机上确认登录</li>
          </ol>
          <p v-if="error" class="bili-error">{{ error }}</p>
          <div class="bili-actions">
            <button class="button subtle small" :disabled="phase === 'loading'" @click="start">
              {{ phase === 'expired' ? '刷新二维码' : '重新获取' }}
            </button>
            <button class="button small" @click="close">取消</button>
          </div>
          <p class="bili-note">不想登录也可以：用扩展的「只用画面识别」，全程本地、不碰账号。</p>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.bili-mask { position: fixed; inset: 0; background: rgba(32, 29, 25, .35); display: flex; align-items: center; justify-content: center; z-index: 60; }
.bili-panel { width: min(640px, 92vw); background: var(--panel, #fffdf8); border-radius: 14px; padding: 20px 22px; box-shadow: 0 18px 48px rgba(32, 29, 25, .25); }
.bili-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.bili-head h2 { margin: 0 0 4px; font-size: 16px; }
.bili-sub { margin: 0; font-size: 12px; color: var(--muted, #857c70); line-height: 1.6; }
.bili-body { display: grid; grid-template-columns: 220px 1fr; gap: 20px; margin-top: 16px; }
.bili-qr { width: 220px; height: 220px; border: 1px solid var(--line, #e3ddd1); border-radius: 10px; background: #fff; display: flex; align-items: center; justify-content: center; overflow: hidden; }
.bili-qr img { width: 100%; height: 100%; }
.bili-qr-empty { font-size: 12px; color: var(--muted, #857c70); }
.bili-side { display: flex; flex-direction: column; gap: 10px; }
.bili-state { margin: 0; font-size: 13px; padding: 7px 10px; border-radius: 8px; background: #faf7f0; }
.state-scanned { background: #f6ded6; color: #8c3520; }
.state-ok { background: #dfeee2; color: #2f5c3c; }
.state-expired, .state-error { background: #f7e0dc; color: #96331f; }
.bili-steps { margin: 0; padding-left: 18px; font-size: 12.5px; line-height: 1.9; color: var(--muted, #857c70); }
.bili-error { margin: 0; font-size: 12px; color: #96331f; }
.bili-actions { display: flex; gap: 8px; margin-top: auto; }
.bili-logged { margin-top: 16px; }
.bili-ok { margin: 0 0 6px; font-size: 14px; }
.bili-mid { margin-left: 8px; font-size: 12px; color: var(--muted, #857c70); }
.bili-note { margin: 0; font-size: 11.5px; color: var(--muted, #857c70); line-height: 1.6; }
@media (max-width: 620px) { .bili-body { grid-template-columns: 1fr; } .bili-qr { margin: 0 auto; } }
</style>
