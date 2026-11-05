<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, API_BASE } from '../api'
import MarkdownContent from './MarkdownContent.vue'

/**
 * 新建任务 = 一个能和 Agent 来回说话的会话。
 *
 * 界面按聊天来组织：用户的话在右边气泡里，Agent 的回答直接铺在左边，工具调用
 * 折成一行可展开的痕迹。之前把每一步都做成卡片，看着像调试面板，不像在对话。
 */

type Step = {
  turn: number
  kind: string
  tool?: string
  output?: string
  detail?: string
  ok?: boolean
}
type Pending = { tool: string; args: Record<string, unknown>; risk: string; reason: string } | null

type AssistantMessage = {
  role: 'assistant'
  text: string
  steps: Step[]
  status: 'running' | 'done' | 'failed' | 'blocked'
  seconds: number
  error: string
  pending: Pending
}
type UserMessage = { role: 'user'; text: string }
type Message = UserMessage | AssistantMessage

type OldTask = { id: string; title: string; message: string; status: string; created_at: string }
type ModelInfo = { model: string; model_name: string; provider_name: string; configured: boolean }
type RegistryModel = { id: string; name: string; api: string }
type RegistryProvider = { id: string; name: string; configured: boolean; models: RegistryModel[] }
type Registry = { providers: RegistryProvider[]; active: { provider_id: string; model_id: string } | null }

const props = defineProps<{ courseId: string; courseName: string; taskId: string }>()
const emit = defineEmits<{ 'select-task': [id: string]; navigate: [section: string]; 'open-models': []; changed: [] }>()

const draft = ref('')
const busy = ref(false)
const history = ref<Message[]>([])
const conversationId = ref('')
const oldTask = ref<OldTask | null>(null)
const model = ref<ModelInfo | null>(null)
// 底部那个模型名不只是显示，点开要能直接换一个
const modelMenuOpen = ref(false)
const registry = ref<Registry | null>(null)
const modelBusy = ref('')
const modelError = ref('')
const modelWrap = ref<HTMLElement | null>(null)

// 历史对话：后端把每轮对话落盘，这里只是读列表
type SessionRow = { id: string; title: string; course_id: string; updated_at: number; messages: number }
const sessions = ref<SessionRow[]>([])
const sessionsOpen = ref(false)
const sessionsLoading = ref(false)
const thread = ref<HTMLElement | null>(null)
const box = ref<HTMLTextAreaElement | null>(null)
const copied = ref(-1)

// ＋菜单：文件、技能、MCP、导出
const menuOpen = ref(false)
const menuWrap = ref<HTMLElement | null>(null)
const skills = ref<{ name: string; description: string }[]>([])
const servers = ref<{ name: string; tools: { name: string }[]; error: string }[]>([])
const menuLoaded = ref(false)
const menuLoading = ref(false)
const attached = ref<{ id: string; filename: string }[]>([])
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const exporting = ref('')
const notice = ref('')

let controller: AbortController | null = null
let ticker: ReturnType<typeof setInterval> | null = null

const suggestions = [
  '把这一章做成复习笔记',
  '出 10 道题，难度中等',
  '这个课件对应哪门课的视频',
  '按章整理这门课的考点',
]

const current = computed(() => {
  for (let index = history.value.length - 1; index >= 0; index -= 1) {
    const item = history.value[index]
    if (item.role === 'assistant') return item
  }
  return null
})
const canSend = computed(() => !!props.courseId && !!draft.value.trim() && !busy.value)
const modelLabel = computed(() => model.value?.model_name || model.value?.model || '未配置模型')

function closeMenuOnOutside(event: MouseEvent) {
  const target = event.target as Node | null
  if (menuOpen.value && !(menuWrap.value && target && menuWrap.value.contains(target))) menuOpen.value = false
  if (modelMenuOpen.value && !(modelWrap.value && target && modelWrap.value.contains(target))) modelMenuOpen.value = false
}
function closeMenuOnEscape(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    menuOpen.value = false
    modelMenuOpen.value = false
  }
}

onMounted(() => {
  document.addEventListener('mousedown', closeMenuOnOutside)
  document.addEventListener('keydown', closeMenuOnEscape)
  void loadModel()
})
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', closeMenuOnOutside)
  document.removeEventListener('keydown', closeMenuOnEscape)
  stop()
})

async function loadModel() {
  try {
    model.value = await api<ModelInfo>('/system/model-config')
  } catch {
    model.value = null
  }
}

async function loadRegistry() {
  try {
    registry.value = await api<Registry>('/system/model-registry')
  } catch {
    registry.value = null
  }
}

const TOOL_NOTE = /^\[用过 /

/** 后端存的是 user/assistant 流水，工具痕迹那条不入界面。 */
function toHistory(messages: { role: string; content: string }[]): Message[] {
  const result: Message[] = []
  for (const item of messages) {
    const text = String(item.content || '')
    if (item.role === 'user') result.push({ role: 'user', text })
    else if (item.role === 'assistant' && !TOOL_NOTE.test(text)) {
      result.push({ role: 'assistant', text, steps: [], status: 'done', seconds: 0, error: '', pending: null })
    }
  }
  return result
}

async function toggleSessions() {
  sessionsOpen.value = !sessionsOpen.value
  if (!sessionsOpen.value) return
  sessionsLoading.value = true
  try {
    const payload = await api<{ conversations: SessionRow[] }>(
      `/agent/conversations?course_id=${encodeURIComponent(props.courseId || '')}`,
    )
    sessions.value = payload.conversations
  } catch {
    sessions.value = []
  } finally {
    sessionsLoading.value = false
  }
}

async function openSession(id: string) {
  sessionsOpen.value = false
  stop()
  try {
    const payload = await api<{ messages: { role: string; content: string }[] }>(`/agent/conversations/${id}`)
    history.value = toHistory(payload.messages)
    conversationId.value = id
    scrollToEnd()
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  }
}

async function removeSession(id: string) {
  try {
    await api(`/agent/conversations/${id}`, { method: 'DELETE' })
    sessions.value = sessions.value.filter((item) => item.id !== id)
    if (conversationId.value === id) reset()
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  }
}

function timeLabel(stamp: number) {
  if (!stamp) return ''
  const date = new Date(stamp * 1000)
  const today = new Date()
  const sameDay = date.toDateString() === today.toDateString()
  return sameDay
    ? date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}

async function toggleModelMenu() {
  modelMenuOpen.value = !modelMenuOpen.value
  if (!modelMenuOpen.value) return
  modelError.value = ''
  // 每次打开都重新读：设置页可能刚改过凭据或加了模型
  await Promise.all([loadModel(), loadRegistry()])
}

function isActive(provider: RegistryProvider, item: RegistryModel) {
  return registry.value?.active?.provider_id === provider.id && registry.value?.active?.model_id === item.id
}

async function pickModel(provider: RegistryProvider, item: RegistryModel) {
  if (modelBusy.value) return
  if (isActive(provider, item)) {
    modelMenuOpen.value = false
    return
  }
  if (!provider.configured) {
    modelError.value = `「${provider.name}」还没配好凭据，去「模型配置」里补上就能用`
    return
  }
  modelBusy.value = item.id
  modelError.value = ''
  try {
    registry.value = await api<Registry>('/system/model-registry/active', {
      method: 'POST',
      body: JSON.stringify({ provider_id: provider.id, model_id: item.id }),
    })
    await loadModel()
    modelMenuOpen.value = false
  } catch (e) {
    modelError.value = e instanceof Error ? e.message : String(e)
  } finally {
    modelBusy.value = ''
  }
}

async function toggleMenu() {
  menuOpen.value = !menuOpen.value
  if (!menuOpen.value || menuLoaded.value || menuLoading.value) return
  menuLoading.value = true
  try {
    const payload = await api<{
      mcp: { servers: { name: string; tools: { name: string }[]; error: string }[] }
      skills: { name: string; description: string }[]
    }>('/workspace/overview?probe=false')
    skills.value = payload.skills.slice(0, 12)
    servers.value = payload.mcp.servers.filter((server) => !server.error).slice(0, 6)
    menuLoaded.value = true
  } catch {
    skills.value = []
    servers.value = []
  } finally {
    menuLoading.value = false
  }
}

function insertToken(token: string) {
  draft.value = draft.value ? `${draft.value} ${token}` : token
  menuOpen.value = false
  box.value?.focus()
}

async function pickFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  menuOpen.value = false
  if (!files.length || !props.courseId) return
  uploading.value = true
  notice.value = ''
  try {
    for (const file of files) {
      const form = new FormData()
      form.append('file', file)
      form.append('kind', 'lecture')
      const created = await api<{ id: string; filename: string }>(
        `/courses/${props.courseId}/documents`,
        { method: 'POST', body: form as unknown as BodyInit },
      )
      attached.value.push({ id: created.id, filename: created.filename || file.name })
      draft.value = draft.value
        ? `${draft.value}（参考资料：${created.filename || file.name}）`
        : `参考刚上传的《${created.filename || file.name}》，把这一章做成笔记`
    }
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  } finally {
    uploading.value = false
  }
}

async function exportDoc(kind: 'docx' | 'pptx') {
  if (!props.courseId) return
  exporting.value = kind
  menuOpen.value = false
  try {
    const payload = await api<{ url: string; filename: string }>(
      `/courses/${props.courseId}/exports`,
      { method: 'POST', body: JSON.stringify({ kind, title: props.courseName ? `${props.courseName} 复习笔记` : '复习笔记' }) },
    )
    window.open(`${API_BASE}${payload.url}`, '_blank')
    notice.value = `已生成 ${payload.filename}`
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  } finally {
    exporting.value = ''
  }
}

const STEP_LABEL: Record<string, string> = {
  tool: '调用工具',
  skill_use: '使用技能',
  skill_list: '查看技能',
  final: '给出结论',
}

function stepTitle(step: Step) {
  if (step.kind === 'tool' && step.tool) return step.tool
  if (step.kind === 'skill_use' && step.tool) return `技能 · ${step.tool}`
  return STEP_LABEL[step.kind] || step.kind
}

function stepLine(step: Step) {
  const text = (step.output || step.detail || '').replace(/\s+/g, ' ').trim()
  return text.length > 120 ? `${text.slice(0, 120)}…` : text
}

function startTicker(message: AssistantMessage) {
  message.seconds = 0
  if (ticker) clearInterval(ticker)
  ticker = setInterval(() => {
    const last = current.value
    if (last && last.status === 'running') last.seconds += 1
    else if (ticker) { clearInterval(ticker); ticker = null }
  }, 1000)
}

function handleEvent(event: { type: string; [key: string]: unknown }) {
  const message = current.value
  if (!message) return
  if (event.type === 'run') {
    if (event.conversation_id) conversationId.value = String(event.conversation_id)
  } else if (event.type === 'step') {
    message.steps.push(event.step as Step)
  } else if (event.type === 'blocked') {
    message.pending = event.pending as Pending
    message.status = 'blocked'
  } else if (event.type === 'final') {
    message.text = String(event.answer || '')
    message.status = 'done'
  } else if (event.type === 'error') {
    message.status = 'failed'
    message.error = String(event.message || '出错了')
  }
}

function scrollToEnd() {
  void nextTick(() => {
    if (thread.value) thread.value.scrollTop = thread.value.scrollHeight
  })
}

async function send() {
  if (!canSend.value) return
  const goal = draft.value.trim()
  menuOpen.value = false
  notice.value = ''
  oldTask.value = null
  draft.value = ''
  history.value.push({ role: 'user', text: goal })
  const reply: AssistantMessage = { role: 'assistant', text: '', steps: [], status: 'running', seconds: 0, error: '', pending: null }
  history.value.push(reply)
  busy.value = true
  startTicker(reply)
  scrollToEnd()

  try {
    controller = new AbortController()
    const response = await fetch(`${API_BASE}/agent/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal, course_id: props.courseId || '', max_turns: 6, conversation_id: conversationId.value }),
      signal: controller.signal,
    })
    if (!response.ok) throw new Error(`请求失败（${response.status}）`)
    if (!response.body) throw new Error('浏览器不支持流式响应')

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const chunks = buffer.split(String.fromCharCode(10, 10))
      buffer = chunks.pop() || ''
      for (const chunk of chunks) {
        const line = chunk.split(String.fromCharCode(10)).find((item) => item.startsWith('data: '))
        if (!line) continue
        try {
          handleEvent(JSON.parse(line.slice(6)))
        } catch {
          /* 半截 JSON，下一段会补上 */
        }
      }
      scrollToEnd()
    }
  } catch (e) {
    const message = current.value
    if (e instanceof DOMException && e.name === 'AbortError') {
      if (message) { message.status = 'failed'; message.error = message.error || '已停止' }
    } else if (message) {
      message.status = 'failed'
      message.error = e instanceof Error ? e.message : String(e)
    }
  } finally {
    controller = null
    busy.value = false
    if (ticker) { clearInterval(ticker); ticker = null }
    scrollToEnd()
  }
}

function stop() {
  controller?.abort()
  controller = null
  busy.value = false
  if (ticker) { clearInterval(ticker); ticker = null }
  const message = current.value
  if (message && message.status === 'running') {
    message.status = 'failed'
    message.error = message.error || '已停止'
  }
}

async function resume(approve: boolean) {
  const message = current.value
  if (!message) return
  try {
    await api('/agent/resume', { method: 'POST', body: JSON.stringify({ run_id: '', approve }) })
    message.pending = null
    message.status = approve ? 'done' : 'failed'
    if (!approve) message.error = '已取消这一步'
  } catch (e) {
    message.error = e instanceof Error ? e.message : String(e)
  }
}

async function copy(index: number, text: string) {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = index
    setTimeout(() => { if (copied.value === index) copied.value = -1 }, 1600)
  } catch {
    copied.value = -1
  }
}

function onKeydown(event: KeyboardEvent) {
  // 和常见聊天框一致：Enter 发送，Shift+Enter 换行
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    void send()
  }
}

async function loadOldTask(id: string) {
  oldTask.value = null
  try {
    oldTask.value = await api<OldTask>(`/tasks/${id}`)
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  }
}

function reuseOldTask() {
  if (!oldTask.value) return
  draft.value = oldTask.value.message
  oldTask.value = null
  emit('select-task', '')
  void nextTick(() => box.value?.focus())
}

function reset() {
  stop()
  history.value = []
  conversationId.value = ''
  draft.value = ''
  notice.value = ''
  oldTask.value = null
  emit('select-task', '')
  void nextTick(() => box.value?.focus())
}

watch(() => props.taskId, (id) => {
  if (id) void loadOldTask(id)
  else oldTask.value = null
}, { immediate: true })
</script>

<template>
  <div class="chat-page">
    <!-- 历史任务：旧版「按固定步骤执行」的记录，可以交给 Agent 重做 -->
    <template v-if="oldTask">
      <div class="chat-scroll">
        <div class="old-task">
          <p class="muted small">{{ oldTask.status }} · {{ oldTask.title }}</p>
          <p class="old-quote">「{{ oldTask.message }}」</p>
          <p class="muted small">这是旧版按固定步骤执行的记录。交给 Agent 重做，它会自己决定查什么、怎么做。</p>
          <button class="button primary small" @click="reuseOldTask">交给 Agent 重做</button>
        </div>
      </div>
    </template>

    <!-- 对话 -->
    <template v-else>
      <div class="chat-bar">
        <button class="bar-button" @click="toggleSessions">
          对话历史<span v-if="sessions.length"> · {{ sessions.length }}</span>
        </button>
        <button class="bar-button" @click="reset">新对话</button>
        <div v-if="sessionsOpen" class="sessions">
          <p class="sessions-title">最近的对话</p>
          <div v-for="item in sessions" :key="item.id" class="session-row">
            <button class="session-open" @click="openSession(item.id)">
              <span class="session-title">{{ item.title }}</span>
              <span class="session-meta">{{ item.messages }} 条 · {{ timeLabel(item.updated_at) }}</span>
            </button>
            <button class="session-del" title="删除这段对话" @click.stop="removeSession(item.id)">×</button>
          </div>
          <p v-if="sessionsLoading" class="model-empty-row">读取中…</p>
          <p v-else-if="!sessions.length" class="model-empty-row">还没有历史对话</p>
        </div>
      </div>

      <div ref="thread" class="chat-scroll">
        <div v-if="!history.length" class="chat-empty">
          <h1>今天要做什么？</h1>
          <p class="muted">
            {{ courseName ? `当前课程：${courseName}` : '先在左下角选一门课程' }} · 它会自己查资料、调工具
          </p>
          <div class="chat-suggestions">
            <button v-for="item in suggestions" :key="item" class="chip" @click="draft = item">{{ item }}</button>
          </div>
        </div>

        <template v-for="(item, index) in history" :key="index">
          <!-- 用户 -->
          <div v-if="item.role === 'user'" class="row right">
            <div class="bubble">{{ item.text }}</div>
          </div>

          <!-- Agent -->
          <div v-else class="row">
            <div class="avatar">脉</div>
            <div class="answer-col">
              <p class="who">课脉</p>
              <p class="status">
                <span v-if="item.status === 'running'">正在做 · {{ item.seconds }}s</span>
                <span v-else-if="item.status === 'failed'">没有完成 · {{ item.seconds }}s</span>
                <span v-else-if="item.status === 'blocked'">等你确认 · {{ item.seconds }}s</span>
                <span v-else>已完成 · {{ item.seconds }}s</span>
              </p>

              <details v-if="item.steps.length" class="trace">
                <summary>用了 {{ item.steps.length }} 个工具</summary>
                <div v-for="(step, stepIndex) in item.steps" :key="stepIndex" class="trace-row">
                  <span class="trace-tool">{{ stepTitle(step) }}</span>
                  <span class="trace-text">{{ stepLine(step) }}</span>
                </div>
              </details>

              <p v-if="item.status === 'running' && !item.text && !item.steps.length" class="thinking">正在读资料、挑工具…</p>

              <MarkdownContent v-if="item.text" :content="item.text" />

              <p v-if="item.error" class="chat-error">{{ item.error }}</p>

              <div v-if="item.pending" class="approve">
                <p class="approve-title">这一步会改动内容，需要你点头</p>
                <p class="muted small">{{ item.pending.tool }} — {{ item.pending.reason }}</p>
                <div class="approve-actions">
                  <button class="button primary small" @click="resume(true)">允许</button>
                  <button class="text-button" @click="resume(false)">不做了</button>
                </div>
              </div>

              <div v-if="item.status !== 'running' && item.text" class="meta">
                <button class="meta-button" @click="copy(index, item.text)">{{ copied === index ? '已复制' : '复制' }}</button>
                <span class="dot">·</span>
                <span>{{ item.seconds }}s</span>
                <span class="dot">·</span>
                <span>{{ modelLabel }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- 输入区 -->
      <div class="composer-wrap">
        <div v-if="notice" class="notice-line">{{ notice }}</div>
        <div class="composer">
          <div ref="menuWrap" class="plus-wrap">
            <button class="plus" :class="{ open: menuOpen }" aria-label="添加内容" :disabled="!courseId" @click="toggleMenu">＋</button>
            <div v-if="menuOpen" class="menu" role="menu">
              <div class="menu-group">
                <button class="menu-item" role="menuitem" :disabled="uploading" @click="fileInput?.click()">
                  <span>添加文件</span><small>{{ uploading ? '上传中…' : '课件、Word、PPT、PDF' }}</small>
                </button>
              </div>
              <div class="menu-group">
                <p class="menu-title">技能 · {{ menuLoading ? '读取中…' : `${skills.length} 个` }}</p>
                <button v-for="skill in skills" :key="skill.name" class="menu-item" role="menuitem" @click="insertToken(`@skill:${skill.name}`)">
                  <span>{{ skill.name }}</span><small>{{ skill.description.slice(0, 22) }}</small>
                </button>
                <p v-if="!menuLoading && !skills.length" class="menu-empty">还没有装技能 · 去「工作区」导入</p>
              </div>
              <div class="menu-group">
                <p class="menu-title">MCP · {{ menuLoading ? '读取中…' : `${servers.length} 个` }}</p>
                <button v-for="server in servers" :key="server.name" class="menu-item" role="menuitem" @click="insertToken(`@mcp:${server.name}`)">
                  <span>{{ server.name }}</span><small>{{ server.tools.length }} 个工具</small>
                </button>
                <p v-if="!menuLoading && !servers.length" class="menu-empty">还没有连上 MCP · 去「工作区」添加</p>
              </div>
              <div class="menu-group">
                <button class="menu-item" role="menuitem" :disabled="!!exporting" @click="exportDoc('docx')">
                  <span>导出 Word</span><small>{{ exporting === 'docx' ? '生成中…' : '把当前笔记存成 docx' }}</small>
                </button>
                <button class="menu-item" role="menuitem" :disabled="!!exporting" @click="exportDoc('pptx')">
                  <span>导出 PPT</span><small>{{ exporting === 'pptx' ? '生成中…' : '一节一页，要点成行' }}</small>
                </button>
              </div>
            </div>
          </div>

          <textarea
            ref="box"
            v-model="draft"
            rows="1"
            :disabled="!courseId"
            placeholder="今天帮你做些什么？@ 添加资料，/ 调用技能"
            @keydown="onKeydown"
          ></textarea>

          <div class="composer-foot">
            <span class="foot-hint">{{ attached.length ? `已挂 ${attached.length} 份资料` : 'Enter 发送 · Shift+Enter 换行' }}</span>
            <div class="foot-right">
              <div ref="modelWrap" class="model-wrap">
                <button class="model-chip" :class="{ open: modelMenuOpen }" title="切换模型" @click="toggleModelMenu">
                  <span class="dot-live" :class="{ ok: model?.configured }"></span>
                  <span class="model-chip-label">{{ modelLabel }}</span>
                  <span class="caret">▾</span>
                </button>
                <div v-if="modelMenuOpen" class="model-menu" role="listbox">
                  <p class="model-menu-title">选择模型</p>
                  <template v-for="provider in registry?.providers || []" :key="provider.id">
                    <p class="model-group">{{ provider.name }}</p>
                    <button
                      v-for="item in provider.models"
                      :key="item.id"
                      role="option"
                      class="model-row"
                      :class="{ current: isActive(provider, item), muted: !provider.configured }"
                      :disabled="modelBusy === item.id"
                      @click="pickModel(provider, item)"
                    >
                      <span class="model-name">{{ item.name || item.id }}</span>
                      <span v-if="isActive(provider, item)" class="tick">✓</span>
                      <span v-else-if="!provider.configured" class="model-note">未配凭据</span>
                      <span v-else-if="modelBusy === item.id" class="model-note">切换中…</span>
                    </button>
                  </template>
                  <p v-if="registry && !registry.providers.length" class="model-empty-row">还没添加服务商</p>
                  <p v-if="modelError" class="model-error">{{ modelError }}</p>
                  <div class="model-menu-foot">
                    <button class="model-config" @click="modelMenuOpen = false; emit('open-models')">配置模型与服务商</button>
                  </div>
                </div>
              </div>
              <button v-if="busy" class="text-button" @click="stop">停止</button>
              <button class="send" :disabled="!canSend" aria-label="发送" @click="send">↑</button>
            </div>
          </div>
        </div>
        <p class="disclaimer">内容由 AI 生成，请核实重要信息</p>
      </div>

      <input ref="fileInput" type="file" multiple hidden accept=".pdf,.docx,.pptx,.md,.txt" @change="pickFiles" />
    </template>
  </div>
</template>

<style scoped>
.chat-page { display: flex; flex-direction: column; height: calc(100vh - 96px); max-width: 860px; margin: 0 auto; }

.chat-bar { position: relative; display: flex; align-items: center; gap: 8px; padding: 0 4px 6px; flex: none; }
.bar-button { border: 1px solid transparent; background: transparent; color: var(--muted); font-size: 12px; padding: 4px 9px; border-radius: 8px; cursor: pointer; font-family: inherit; }
.bar-button:hover { color: var(--accent-dark); background: var(--soft); border-color: var(--line); }

.sessions { position: absolute; top: 30px; left: 4px; width: 300px; max-height: 340px; overflow-y: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; box-shadow: 0 14px 36px rgba(24, 34, 54, .16); padding: 6px; z-index: 20; }
.sessions-title { margin: 6px 8px 4px; font-size: 10.5px; color: var(--muted); }
.session-row { display: flex; align-items: stretch; gap: 2px; }
.session-open { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; border: 0; background: transparent; border-radius: 7px; padding: 7px 9px; cursor: pointer; text-align: left; font-family: inherit; }
.session-open:hover { background: var(--soft); }
.session-title { font-size: 12.5px; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-meta { font-size: 10.5px; color: var(--muted); }
.session-del { flex: none; width: 24px; border: 0; background: transparent; color: var(--muted); cursor: pointer; font-size: 14px; border-radius: 6px; font-family: inherit; }
.session-del:hover { color: var(--danger); background: var(--danger-soft); }

.chat-scroll { flex: 1; overflow-y: auto; padding: 8px 4px 24px; display: flex; flex-direction: column; gap: 22px; }

.chat-empty { margin-top: 16vh; text-align: center; }
.chat-empty h1 { margin: 0 0 8px; font-size: 22px; font-weight: 600; }
.chat-empty p { margin: 0 0 18px; font-size: 12.5px; }
.chat-suggestions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
.chip { border: 1px solid var(--line); background: var(--surface); border-radius: 16px; padding: 6px 13px; font-size: 12.5px; color: var(--ink); cursor: pointer; font-family: inherit; }
.chip:hover { border-color: var(--accent-line); color: var(--accent-dark); background: var(--soft); }

.row { display: flex; gap: 10px; align-items: flex-start; }
.row.right { justify-content: flex-end; }
.bubble { max-width: 78%; background: var(--surface-alt, #eef1f6); color: var(--ink); border-radius: 14px; padding: 9px 14px; font-size: 13.5px; line-height: 1.7; white-space: pre-wrap; word-break: break-word; }
.avatar { width: 26px; height: 26px; border-radius: 50%; background: var(--accent); color: #fff; font-size: 12px; display: flex; align-items: center; justify-content: center; flex: none; margin-top: 2px; }
.answer-col { flex: 1; min-width: 0; }
.who { margin: 0; font-size: 13px; font-weight: 600; }
.status { margin: 2px 0 10px; font-size: 11.5px; color: var(--muted); }
.thinking { margin: 0; font-size: 12.5px; color: var(--muted); }

.trace { margin: 0 0 10px; font-size: 12px; color: var(--muted); }
.trace summary { cursor: pointer; font-size: 12px; color: var(--muted); padding: 2px 0; }
.trace summary:hover { color: var(--accent-dark); }
.trace-row { display: flex; gap: 8px; padding: 5px 0 5px 12px; border-left: 2px solid var(--line); margin-top: 4px; }
.trace-tool { flex: none; color: var(--ink); font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: 11.5px; }
.trace-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.chat-error { margin: 8px 0 0; font-size: 12.5px; color: var(--danger); }

.meta { display: flex; align-items: center; gap: 7px; margin-top: 10px; font-size: 11.5px; color: var(--muted); }
.meta-button { border: 0; background: transparent; color: var(--muted); font-size: 11.5px; cursor: pointer; padding: 0; font-family: inherit; }
.meta-button:hover { color: var(--accent-dark); }
.dot { color: var(--line); }

.approve { margin: 10px 0 0; padding: 12px 14px; border-left: 3px solid var(--warning); border-radius: 8px; background: var(--warning-soft); }
.approve-title { margin: 0 0 4px; font-size: 12.5px; font-weight: 600; }
.approve-actions { display: flex; gap: 12px; margin-top: 10px; }

.composer-wrap { flex: none; padding: 8px 0 10px; }
.notice-line { margin-bottom: 6px; font-size: 12px; color: var(--accent-dark); }
.composer { position: relative; border: 1px solid var(--line); border-radius: 14px; background: var(--surface); box-shadow: 0 4px 18px rgba(24, 34, 54, .06); padding: 10px 12px 8px; }
.composer textarea { width: 100%; border: 0; outline: 0; resize: none; font: inherit; font-size: 13.5px; line-height: 1.7; background: transparent; padding: 2px 0 6px 34px; max-height: 160px; }
.composer-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.foot-hint { font-size: 11px; color: var(--muted); margin-left: 30px; }
.foot-right { display: flex; align-items: center; gap: 10px; }
.model-wrap { position: relative; }
.model-chip { display: inline-flex; align-items: center; gap: 6px; border: 0; background: transparent; color: var(--muted); font-size: 11.5px; cursor: pointer; font-family: inherit; padding: 2px 4px; border-radius: 6px; max-width: 200px; }
.model-chip:hover, .model-chip.open { color: var(--accent-dark); background: var(--soft); }
.model-chip-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.caret { font-size: 9px; color: var(--muted); }
.dot-live { width: 6px; height: 6px; border-radius: 50%; background: var(--muted); flex: none; }
.dot-live.ok { background: var(--success); }

.model-menu { position: absolute; bottom: 30px; right: 0; width: 268px; max-height: 380px; overflow-y: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; box-shadow: 0 14px 36px rgba(24, 34, 54, .16); padding: 6px; z-index: 20; }
.model-menu-title { margin: 6px 8px 4px; font-size: 10.5px; color: var(--muted); }
.model-group { margin: 8px 8px 2px; font-size: 10.5px; color: var(--muted); }
.model-row { display: flex; align-items: center; gap: 8px; width: 100%; border: 0; background: transparent; border-radius: 7px; padding: 7px 9px; cursor: pointer; text-align: left; font-family: inherit; }
.model-row:hover { background: var(--soft); }
.model-row.current { background: var(--soft); }
.model-row.muted { opacity: .55; }
.model-name { flex: 1; min-width: 0; font-size: 12.5px; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tick { color: var(--accent); font-size: 12px; flex: none; }
.model-note { font-size: 10.5px; color: var(--muted); flex: none; }
.model-empty-row { margin: 8px 9px; font-size: 11.5px; color: var(--muted); }
.model-error { margin: 6px 9px; font-size: 11px; color: var(--danger); }
.model-menu-foot { border-top: 1px solid var(--line); margin-top: 6px; padding-top: 6px; }
.model-config { width: 100%; border: 0; background: transparent; color: var(--accent-dark); font-size: 12px; padding: 7px 9px; text-align: left; cursor: pointer; border-radius: 7px; font-family: inherit; }
.model-config:hover { background: var(--soft); }
.send { width: 30px; height: 30px; border-radius: 50%; border: 0; background: var(--accent); color: #fff; font-size: 15px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
.send:disabled { background: var(--line); color: var(--muted); cursor: not-allowed; }
.disclaimer { margin: 6px 0 0; text-align: center; font-size: 10.5px; color: var(--muted); }

.plus-wrap { position: absolute; left: 10px; bottom: 8px; z-index: 5; }
.plus { width: 26px; height: 26px; border-radius: 50%; border: 1px solid var(--line); background: var(--surface); color: var(--muted); font-size: 15px; line-height: 1; cursor: pointer; display: flex; align-items: center; justify-content: center; }
.plus:hover, .plus.open { border-color: var(--accent); color: var(--accent); background: var(--soft); }
.menu { position: absolute; bottom: 34px; left: 0; width: 268px; max-height: 360px; overflow-y: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; box-shadow: 0 12px 32px rgba(24, 34, 54, .14); padding: 6px; }
.menu-group { padding: 4px 0; border-bottom: 1px solid var(--line); }
.menu-group:last-child { border-bottom: 0; }
.menu-title { margin: 6px 8px 3px; font-size: 10.5px; color: var(--muted); }
.menu-item { display: flex; flex-direction: column; gap: 1px; width: 100%; border: 0; background: transparent; border-radius: 7px; padding: 6px 9px; cursor: pointer; text-align: left; font-family: inherit; }
.menu-item:hover { background: var(--soft); }
.menu-item:disabled { opacity: .55; cursor: not-allowed; }
.menu-item span { font-size: 12.5px; color: var(--ink); }
.menu-item small { font-size: 10.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.menu-empty { margin: 4px 9px 6px; font-size: 11px; color: var(--muted); }

.old-task { margin: 16vh auto 0; max-width: 520px; text-align: left; }
.old-quote { margin: 8px 0; font-size: 13.5px; color: var(--ink); }
</style>
