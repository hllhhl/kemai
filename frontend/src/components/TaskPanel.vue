<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, API_BASE } from '../api'

type Action = {
  type: string
  label: string
  detail: string
  method: string
  endpoint: string
  body: Record<string, unknown>
  needs_model: boolean
  ready: boolean
  blocked_reason: string
  risk: 'low' | 'moderate' | 'sensitive'
  risk_note: string
  decision: 'allow' | 'ask' | 'deny'
  reason: string
  overwrites: boolean
}
type Plan = {
  intent: string
  actions: Action[]
  hint: string
  blocked: boolean
  model_ready: boolean
  needs_confirm: number
  denied: number
}
type Task = {
  id: string
  title: string
  message: string
  status: 'planned' | 'running' | 'done' | 'failed' | 'cancelled'
  plan: Plan
  result: { label: string; state: string; message: string }[]
  created_at: string
  updated_at: string
}

const props = defineProps<{ courseId: string; courseName: string; taskId: string }>()
const emit = defineEmits<{ 'select-task': [id: string]; navigate: [section: string]; 'open-models': []; changed: [] }>()

const message = ref('')
const busy = ref(false)
const error = ref('')
const task = ref<Task | null>(null)
const loading = ref(false)
// 需要确认的动作必须先勾选：这是"我读过要改什么"，不是走个过场
const confirmed = ref(false)
type Verdict = { label: string; type: string; verdict: string; detail: string }
const verify = ref<{ summary: string; items: Verdict[] } | null>(null)
const verifying = ref(false)

const suggestions = ['把这一章做成笔记', '出 10 道题，难度中等', '这个课件是哪门课的视频']

// 输入框左边的 + ：添附件、挂技能、挂 MCP
const menuOpen = ref(false)
const skills = ref<{ name: string; description: string }[]>([])
const servers = ref<{ name: string; tools: { name: string }[]; error: string }[]>([])
const attached = ref<{ id: string; filename: string }[]>([])
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const exporting = ref('')
const feedbackMessage = ref('')

async function loadMenuData() {
  try {
    // probe=false 只读配置，不启动子进程——菜单要秒开
    const payload = await api<{ mcp: { servers: { name: string; tools: { name: string }[]; error: string }[] }; skills: { name: string; description: string }[] }>(
      '/workspace/overview?probe=false',
    )
    skills.value = payload.skills.slice(0, 12)
    servers.value = payload.mcp.servers.filter((s) => !s.error).slice(0, 6)
  } catch {
    skills.value = []
    servers.value = []
  }
}

function insertToken(token: string) {
  message.value = message.value ? `${message.value} ${token}` : token
  menuOpen.value = false
}

async function pickFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  if (!files.length || !props.courseId) return
  uploading.value = true
  error.value = ''
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
      message.value = message.value
        ? `${message.value}
（参考资料：${created.filename || file.name}）`
        : `参考刚上传的《${created.filename || file.name}》，把这一章做成笔记`
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    uploading.value = false
  }
}

async function exportDoc(kind: 'docx' | 'pptx') {
  if (!props.courseId) return
  exporting.value = kind
  error.value = ''
  try {
    const payload = await api<{ url: string; filename: string }>(
      `/courses/${props.courseId}/exports`,
      { method: 'POST', body: JSON.stringify({ kind, title: props.courseName ? `${props.courseName} 复习笔记` : '复习笔记' }) },
    )
    window.open(`${API_BASE}${payload.url}`, '_blank')
    feedbackMessage.value = `已生成 ${payload.filename}`
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    exporting.value = ''
  }
}

const SECTION: Record<string, string> = { notes: 'outline', practice: 'practice', finder: 'finder' }
const STATUS_TEXT: Record<string, string> = {
  planned: '待执行', running: '执行中', done: '已完成', failed: '失败', cancelled: '已取消',
}
const readable = (action: Action) => action.ready && action.endpoint
const RISK_TEXT: Record<string, string> = { low: '只读', moderate: '会改东西', sensitive: '不可撤销' }
const VERDICT_TEXT: Record<string, string> = {
  verified: '核对通过', pending: '还在跑', contradicted: '对不上', unknown: '查不到',
}
const needConfirm = computed(() => (task.value?.plan.actions || []).filter(
  (a) => a.decision === 'ask' && readable(a),
))
const canRun = computed(() => {
  const current = task.value
  if (!current || busy.value) return false
  if (!current.plan.actions.some((a) => readable(a))) return false
  // 有待确认的动作时，必须显式勾过
  return needConfirm.value.length === 0 || confirmed.value
})

async function loadTask(id: string) {
  loading.value = true
  error.value = ''
  try {
    task.value = await api<Task>(`/tasks/${id}`)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function create() {
  if (!props.courseId || !message.value.trim() || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const created = await api<Task>('/tasks', {
      method: 'POST',
      body: JSON.stringify({ message: message.value.trim(), course_id: props.courseId }),
    })
    task.value = created
    message.value = ''
    emit('select-task', created.id)
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function run() {
  const current = task.value
  if (!current || busy.value) return
  busy.value = true
  error.value = ''
  const results: { label: string; state: string; message: string }[] = []
  try {
    await api(`/tasks/${current.id}`, { method: 'PATCH', body: JSON.stringify({ status: 'running' }) })
    for (const action of current.plan.actions) {
      if (!readable(action)) {
        results.push({ label: action.label, state: 'skipped', message: action.blocked_reason || '这一步现在做不了' })
        continue
      }
      try {
        await api(action.endpoint, { method: action.method, body: JSON.stringify(action.body) })
        const section = SECTION[action.type]
        results.push({
          label: action.label,
          state: 'done',
          message: section ? `已提交，产物在「${{ outline: '复习笔记', practice: '在线练习', finder: 'PPT 找课' }[section]}」` : '已提交',
        })
      } catch (e) {
        results.push({ label: action.label, state: 'failed', message: e instanceof Error ? e.message : String(e) })
      }
    }
    const anyDone = results.some((item) => item.state === 'done')
    task.value = await api<Task>(`/tasks/${current.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status: anyDone ? 'done' : 'failed', result: results }),
    })
    // 202 只说明"已受理"。真正算不算做成，查库核对一次再说。
    if (anyDone) await checkProduct(current.id)
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    try {
      task.value = await api<Task>(`/tasks/${current.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'failed', result: results }),
      })
    } catch {
      /* 状态回写失败就算了，别把原始错误盖掉 */
    }
  } finally {
    busy.value = false
  }
}

async function checkProduct(id: string) {
  verifying.value = true
  verify.value = null
  try {
    // 生成要时间，给两次机会：先看一眼，20 秒后再看
    for (const delay of [0, 20000]) {
      if (delay) await new Promise((r) => setTimeout(r, delay))
      const payload = await api<{ summary: string; items: Verdict[]; task: Task }>(`/tasks/${id}/verify`, { method: 'POST' })
      verify.value = { summary: payload.summary, items: payload.items }
      task.value = payload.task
      if (payload.summary !== 'pending') break
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    verifying.value = false
  }
}

function verifyText(summary: string) {
  const map: Record<string, string> = {
    verified: '都对上了', pending: '还在跑', contradicted: '有对不上的',
    unknown: '有查不到的', nothing: '没有可核对的产物',
  }
  return map[summary] || summary
}

async function remember(scope: 'session' | 'always') {
  const kinds = [...new Set(needConfirm.value.map((a) => a.type))]
  try {
    for (const kind of kinds) {
      await api('/sentinel/grant', { method: 'POST', body: JSON.stringify({ action_type: kind, scope }) })
    }
    confirmed.value = true
    // 授权后重新问一次，让徽章立刻变
    if (task.value) await loadTask(task.value.id)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function restart() {
  task.value = null
  message.value = ''
  emit('select-task', '')
}

watch(() => props.taskId, (id) => {
  confirmed.value = false
  verify.value = null
  if (id) void loadTask(id)
  else { task.value = null; loading.value = false }
}, { immediate: true })
</script>

<template>
  <div class="task-page">
    <!-- 详情 -->
    <template v-if="task">
      <header class="task-head">
        <div>
          <p class="task-eyebrow">
            <span :class="['task-status', task.status]">{{ STATUS_TEXT[task.status] }}</span>
            <span class="task-time">{{ new Date(task.created_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) }}</span>
          </p>
          <h2>{{ task.title }}</h2>
          <p v-if="task.message !== task.title" class="task-quote">「{{ task.message }}」</p>
        </div>
        <button class="button subtle small" @click="restart">新建任务</button>
      </header>

      <p v-if="error" class="task-error">{{ error }}</p>

      <div class="task-steps">
        <article v-for="(action, index) in task.plan.actions" :key="index" class="task-step">
          <div class="task-step-head">
            <span class="task-step-index">{{ index + 1 }}</span>
            <strong>{{ action.label }}</strong>
            <span :class="['task-chip', 'risk', action.risk]" :title="action.risk_note">{{ RISK_TEXT[action.risk] }}</span>
            <span v-if="!action.ready" class="task-chip warn">{{ action.blocked_reason }}</span>
            <span v-else-if="task.result.find((r) => r.label === action.label)" :class="['task-chip', task.result.find((r) => r.label === action.label)!.state]">
              {{ { done: '已完成', failed: '失败', skipped: '跳过' }[task.result.find((r) => r.label === action.label)!.state] || '' }}
            </span>
          </div>
          <p class="task-step-detail">{{ action.detail }}</p>
          <p v-if="action.decision === 'ask' && action.ready" class="task-step-ask">{{ action.reason }}</p>
          <p v-if="task.result.find((r) => r.label === action.label)?.message" class="task-step-msg">
            {{ task.result.find((r) => r.label === action.label)!.message }}
          </p>
          <button
            v-if="SECTION[action.type] && task.result.find((r) => r.label === action.label)?.state === 'done'"
            class="text-button"
            @click="emit('navigate', SECTION[action.type])"
          >
            去看「{{ { outline: '复习笔记', practice: '在线练习', finder: 'PPT 找课' }[SECTION[action.type]] }}」
          </button>
        </article>
      </div>

      <p v-if="task.plan.hint" :class="['task-hint', { blocked: task.plan.blocked }]">{{ task.plan.hint }}</p>

      <div v-if="needConfirm.length" class="task-guard" role="group" aria-label="执行前确认">
        <p class="task-guard-title">这 {{ needConfirm.length }} 步会改动已有内容</p>
        <ul class="task-guard-list">
          <li v-for="action in needConfirm" :key="action.label">{{ action.label }} — {{ action.reason }}</li>
        </ul>
        <label class="task-guard-check">
          <input v-model="confirmed" type="checkbox" />
          <span>我确认按上面的方式执行</span>
        </label>
        <div class="task-guard-actions">
          <button class="text-button" @click="remember('session')">本次不再问</button>
          <button class="text-button" @click="remember('always')">以后都不问</button>
        </div>
      </div>

      <div class="task-actions">
        <button class="button primary" :disabled="busy || !canRun" @click="run">
          {{ busy ? '执行中…' : task.status === 'done' ? '再执行一次' : needConfirm.length ? `确认并执行 ${needConfirm.length} 步` : '开始执行' }}
        </button>
        <button v-if="task.plan.blocked && !task.plan.model_ready" class="text-button" @click="emit('open-models')">去配置模型</button>
        <span v-if="needConfirm.length && !confirmed" class="task-actions-hint">先勾选上面那一行</span>
      </div>

      <div v-if="verifying || verify" class="task-verify">
        <p class="task-verify-title">
          {{ verifying ? '正在核对产物…' : `核对结果：${verifyText(verify!.summary)}` }}
        </p>
        <ul v-if="verify" class="task-verify-list">
          <li v-for="item in verify.items" :key="item.label" :class="['task-verify-row', item.verdict]">
            <span class="task-verify-badge">{{ VERDICT_TEXT[item.verdict] || item.verdict }}</span>
            <span class="task-verify-label">{{ item.label }}</span>
            <span class="task-verify-detail">{{ item.detail }}</span>
          </li>
        </ul>
      </div>
    </template>

    <!-- 新建 -->
    <template v-else>
      <div class="task-new">
        <h1>要做什么？</h1>
        <p class="task-new-sub">{{ courseName ? `当前课程：${courseName}` : '先在左下角选一门课程' }}</p>
        <div class="task-composer">
          <div class="composer-menu-wrap">
            <button class="composer-plus" :class="{ open: menuOpen }" aria-label="添加内容" :disabled="!courseId" @click="menuOpen = !menuOpen; menuOpen && loadMenuData()">＋</button>
            <div v-if="menuOpen" class="composer-menu" role="menu">
              <div class="composer-menu-group">
                <button class="composer-menu-item" role="menuitem" :disabled="uploading" @click="fileInput?.click()">
                  <span>添加文件</span><small>{{ uploading ? '上传中…' : '课件、Word、PPT、PDF' }}</small>
                </button>
              </div>
              <div class="composer-menu-group">
                <p class="composer-menu-title">技能</p>
                <button v-for="skill in skills" :key="skill.name" class="composer-menu-item" role="menuitem" @click="insertToken(`@skill:${skill.name}`)">
                  <span>{{ skill.name }}</span><small>{{ skill.description.slice(0, 22) }}</small>
                </button>
                <p v-if="!skills.length" class="composer-menu-empty">还没有安装技能</p>
              </div>
              <div class="composer-menu-group">
                <p class="composer-menu-title">MCP</p>
                <button v-for="server in servers" :key="server.name" class="composer-menu-item" role="menuitem" @click="insertToken(`@mcp:${server.name}`)">
                  <span>{{ server.name }}</span><small>{{ server.tools.length }} 个工具</small>
                </button>
                <p v-if="!servers.length" class="composer-menu-empty">还没有连上 MCP</p>
              </div>
              <div class="composer-menu-group">
                <button class="composer-menu-item" role="menuitem" :disabled="!!exporting" @click="exportDoc('docx'); menuOpen = false">
                  <span>导出 Word</span><small>{{ exporting === 'docx' ? '生成中…' : '把当前笔记存成 docx' }}</small>
                </button>
                <button class="composer-menu-item" role="menuitem" :disabled="!!exporting" @click="exportDoc('pptx'); menuOpen = false">
                  <span>导出 PPT</span><small>{{ exporting === 'pptx' ? '生成中…' : '一节一页，要点成行' }}</small>
                </button>
              </div>
            </div>
          </div>
          <textarea
            v-model="message"
            rows="3"
            :disabled="busy || !courseId"
            placeholder="把第三章做成笔记，再出 10 道题"
            @keydown.enter.ctrl.prevent="create"
            @keydown.enter.meta.prevent="create"
          ></textarea>
          <div v-if="attached.length" class="composer-attached">
            <span v-for="item in attached" :key="item.id" class="composer-chip">{{ item.filename }}</span>
          </div>
          <input ref="fileInput" type="file" multiple hidden accept=".pdf,.docx,.pptx,.md,.txt" @change="pickFiles" />
          <div class="task-composer-foot">
            <span class="task-tip">Ctrl + Enter 发送</span>
            <button class="button primary small" :disabled="!courseId || !message.trim() || busy" @click="create">
              {{ busy ? '解析中…' : '开始' }}
            </button>
          </div>
        </div>
        <div class="task-suggestions">
          <button v-for="item in suggestions" :key="item" class="task-suggestion" @click="message = item">{{ item }}</button>
        </div>
        <p v-if="loading" class="muted small">正在读取任务…</p>
        <p v-if="error" class="task-error">{{ error }}</p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.composer-menu-wrap { position: absolute; top: 10px; left: 10px; z-index: 5; }
.composer-plus {
  width: 28px; height: 28px; border-radius: 50%; border: 1px solid var(--line);
  background: var(--surface); color: var(--muted); font-size: 16px; line-height: 1;
  cursor: pointer; display: flex; align-items: center; justify-content: center;
}
.composer-plus:hover, .composer-plus.open { border-color: var(--accent); color: var(--accent); background: var(--soft); }
.composer-menu {
  position: absolute; top: 34px; left: 0; width: 268px; max-height: 380px; overflow-y: auto;
  background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
  box-shadow: 0 12px 32px rgba(24, 34, 54, .14); padding: 6px; text-align: left;
}
.composer-menu-group { padding: 4px 0; border-bottom: 1px solid var(--line); }
.composer-menu-group:last-child { border-bottom: 0; }
.composer-menu-title { margin: 6px 8px 3px; font-size: 10.5px; letter-spacing: .04em; color: var(--muted); }
.composer-menu-item {
  display: flex; flex-direction: column; gap: 1px; width: 100%; border: 0; background: transparent;
  border-radius: 7px; padding: 6px 9px; cursor: pointer; text-align: left; font-family: inherit;
}
.composer-menu-item:hover { background: var(--soft); }
.composer-menu-item:disabled { opacity: .55; cursor: not-allowed; }
.composer-menu-item span { font-size: 12.5px; color: var(--ink); }
.composer-menu-item small { font-size: 10.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.composer-menu-empty { margin: 4px 9px 6px; font-size: 11px; color: var(--muted); }
.composer-attached { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 16px 8px 46px; }
.composer-chip { font-size: 11px; padding: 2px 8px; border-radius: 10px; background: var(--soft); color: var(--accent-dark); }

.composer-menu-wrap { position: absolute; top: 10px; left: 10px; z-index: 5; }
.composer-plus {
  width: 28px; height: 28px; border-radius: 50%; border: 1px solid var(--line);
  background: var(--surface); color: var(--muted); font-size: 16px; line-height: 1;
  cursor: pointer; display: flex; align-items: center; justify-content: center;
}
.composer-plus:hover, .composer-plus.open { border-color: var(--accent); color: var(--accent); background: var(--soft); }
.composer-menu {
  position: absolute; top: 34px; left: 0; width: 268px; max-height: 380px; overflow-y: auto;
  background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
  box-shadow: 0 12px 32px rgba(24, 34, 54, .14); padding: 6px; text-align: left;
}
.composer-menu-group { padding: 4px 0; border-bottom: 1px solid var(--line); }
.composer-menu-group:last-child { border-bottom: 0; }
.composer-menu-title { margin: 6px 8px 3px; font-size: 10.5px; letter-spacing: .04em; color: var(--muted); }
.composer-menu-item {
  display: flex; flex-direction: column; gap: 1px; width: 100%; border: 0; background: transparent;
  border-radius: 7px; padding: 6px 9px; cursor: pointer; text-align: left; font-family: inherit;
}
.composer-menu-item:hover { background: var(--soft); }
.composer-menu-item:disabled { opacity: .55; cursor: not-allowed; }
.composer-menu-item span { font-size: 12.5px; color: var(--ink); }
.composer-menu-item small { font-size: 10.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.composer-menu-empty { margin: 4px 9px 6px; font-size: 11px; color: var(--muted); }
.composer-attached { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 16px 8px 46px; }
.composer-chip { font-size: 11px; padding: 2px 8px; border-radius: 10px; background: var(--soft); color: var(--accent-dark); }

.task-page { max-width: 780px; margin: 0 auto; padding: 8px 0 40px; }
.task-new { padding-top: 42px; text-align: center; }
.task-new h1 { margin: 0 0 6px; font-size: 22px; font-weight: 600; }
.task-new-sub { margin: 0 0 22px; font-size: 12.5px; color: var(--muted); }
.task-composer { position: relative; text-align: left; border: 1px solid var(--line); border-radius: 14px; background: var(--surface); box-shadow: 0 6px 22px rgba(24, 34, 54, .06); }
.task-composer textarea { width: 100%; border: none; outline: none; resize: none; padding: 16px 18px 6px 46px; font: inherit; font-size: 14px; line-height: 1.7; background: transparent; }
.task-composer-foot { display: flex; align-items: center; justify-content: space-between; padding: 6px 12px 12px; }
.task-tip { font-size: 11px; color: var(--muted); }
.task-suggestions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 18px; }
.task-suggestion { border: 1px solid var(--line); background: var(--surface); border-radius: 18px; padding: 6px 14px; font-size: 12.5px; color: var(--ink); cursor: pointer; }
.task-suggestion:hover { border-color: var(--accent-line); color: var(--accent-dark); background: var(--soft); }
.task-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; padding-bottom: 14px; border-bottom: 1px solid var(--line); }
.task-eyebrow { display: flex; align-items: center; gap: 10px; margin: 0 0 8px; }
.task-status { font-size: 11px; padding: 2px 8px; border-radius: 10px; background: var(--soft); color: var(--accent-dark); }
.task-status.done { background: var(--success-soft); color: var(--success); }
.task-status.failed { background: var(--danger-soft); color: var(--danger); }
.task-status.running { background: var(--warning-soft); color: var(--warning); }
.task-time { font-size: 11.5px; color: var(--muted); }
.task-head h2 { margin: 0; font-size: 18px; font-weight: 600; line-height: 1.5; }
.task-quote { margin: 6px 0 0; font-size: 12.5px; color: var(--muted); }
.task-steps { display: flex; flex-direction: column; gap: 12px; margin: 18px 0; }
.task-step { border: 1px solid var(--line); border-radius: 12px; padding: 13px 15px; background: var(--surface); }
.task-step-head { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.task-step-index { width: 20px; height: 20px; border-radius: 50%; background: var(--soft); color: var(--accent-dark); font-size: 11px; display: flex; align-items: center; justify-content: center; flex: none; }
.task-step-head strong { font-size: 13.5px; }
.task-step-detail { margin: 6px 0 0 29px; font-size: 12px; color: var(--muted); line-height: 1.6; }
.task-step-msg { margin: 6px 0 0 29px; font-size: 12px; color: var(--ink); }
.task-chip { font-size: 10.5px; padding: 1px 7px; border-radius: 9px; background: var(--soft); color: var(--accent-dark); }
.task-chip.done { background: var(--success-soft); color: var(--success); }
.task-chip.failed { background: var(--danger-soft); color: var(--danger); }
.task-chip.warn { background: var(--warning-soft); color: var(--warning); }
.task-hint { margin: 0 0 16px; font-size: 12.5px; color: var(--muted); }
.task-hint.blocked { color: var(--warning); }
.task-actions { display: flex; align-items: center; gap: 12px; }
.task-error { margin: 10px 0; font-size: 12.5px; color: var(--danger); }
.task-step .text-button { margin: 6px 0 0 29px; }
</style>

<style scoped>
.task-chip.risk { background: var(--surface-alt); color: var(--muted); }
.task-chip.risk.moderate { background: var(--warning-soft); color: var(--warning); }
.task-chip.risk.sensitive { background: var(--danger-soft); color: var(--danger); }
.task-step-ask { margin: 6px 0 0 29px; font-size: 12px; color: var(--warning); }
.task-guard { margin: 0 0 14px; padding: 13px 15px; border: 1px solid var(--warning-soft); border-left: 3px solid var(--warning); border-radius: 10px; background: var(--surface); }
.task-guard-title { margin: 0 0 7px; font-size: 13px; font-weight: 600; color: var(--ink); }
.task-guard-list { margin: 0 0 10px; padding-left: 18px; font-size: 12.5px; color: var(--muted); line-height: 1.7; }
.task-guard-check { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--ink); cursor: pointer; }
.task-guard-check input { width: 15px; height: 15px; accent-color: var(--accent); cursor: pointer; }
.task-guard-actions { display: flex; gap: 14px; margin-top: 8px; }
.task-actions-hint { font-size: 12px; color: var(--muted); }
.task-verify { margin-top: 16px; padding: 12px 14px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); }
.task-verify-title { margin: 0 0 8px; font-size: 12.5px; font-weight: 600; color: var(--ink); }
.task-verify-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.task-verify-row { display: flex; align-items: baseline; gap: 8px; font-size: 12px; color: var(--muted); }
.task-verify-badge { flex: none; padding: 1px 7px; border-radius: 9px; font-size: 10.5px; background: var(--surface-alt); color: var(--muted); }
.task-verify-row.verified .task-verify-badge { background: var(--success-soft); color: var(--success); }
.task-verify-row.contradicted .task-verify-badge { background: var(--danger-soft); color: var(--danger); }
.task-verify-row.pending .task-verify-badge { background: var(--warning-soft); color: var(--warning); }
.task-verify-label { color: var(--ink); flex: none; }
.task-verify-detail { flex: 1; min-width: 0; }
</style>

<style scoped>
.task-chip.risk { background: var(--surface-alt); color: var(--muted); }
.task-chip.risk.moderate { background: var(--warning-soft); color: var(--warning); }
.task-chip.risk.sensitive { background: var(--danger-soft); color: var(--danger); }
.task-step-ask { margin: 6px 0 0 29px; font-size: 12px; color: var(--warning); }
.task-guard { margin: 0 0 14px; padding: 13px 15px; border: 1px solid var(--warning-soft); border-left: 3px solid var(--warning); border-radius: 10px; background: var(--surface); }
.task-guard-title { margin: 0 0 7px; font-size: 13px; font-weight: 600; color: var(--ink); }
.task-guard-list { margin: 0 0 10px; padding-left: 18px; font-size: 12.5px; color: var(--muted); line-height: 1.7; }
.task-guard-check { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--ink); cursor: pointer; }
.task-guard-check input { width: 15px; height: 15px; accent-color: var(--accent); cursor: pointer; }
.task-guard-actions { display: flex; gap: 14px; margin-top: 8px; }
.task-actions-hint { font-size: 12px; color: var(--muted); }
.task-verify { margin-top: 16px; padding: 12px 14px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); }
.task-verify-title { margin: 0 0 8px; font-size: 12.5px; font-weight: 600; color: var(--ink); }
.task-verify-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.task-verify-row { display: flex; align-items: baseline; gap: 8px; font-size: 12px; color: var(--muted); }
.task-verify-badge { flex: none; padding: 1px 7px; border-radius: 9px; font-size: 10.5px; background: var(--surface-alt); color: var(--muted); }
.task-verify-row.verified .task-verify-badge { background: var(--success-soft); color: var(--success); }
.task-verify-row.contradicted .task-verify-badge { background: var(--danger-soft); color: var(--danger); }
.task-verify-row.pending .task-verify-badge { background: var(--warning-soft); color: var(--warning); }
.task-verify-label { color: var(--ink); flex: none; }
.task-verify-detail { flex: 1; min-width: 0; }
</style>
