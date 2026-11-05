<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, API_BASE } from '../api'
import MarkdownContent from './MarkdownContent.vue'

/**
 * 新建任务 = 一个 Agent 会话。
 *
 * 这里不再把一句话翻译成固定动作后逐个调 REST 接口：那个做法只会「一件事
 * 一件事办」，遇到需要先看资料、再决定下一步的问题就傻住了。现在直接走
 * /agent/stream，由 Agent 自己挑工具（课程检索、笔记生成、MCP、技能），
 * 一轮一轮往前推，界面上看得到它每一步在干什么。
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
type Run = {
  id: string
  status: 'running' | 'done' | 'blocked' | 'failed'
  answer: string
  steps: Step[]
  pending: Pending
  turn: number
  max_turns: number
}
type OldTask = {
  id: string
  title: string
  message: string
  status: string
  created_at: string
}

const props = defineProps<{ courseId: string; courseName: string; taskId: string }>()
const emit = defineEmits<{ 'select-task': [id: string]; navigate: [section: string]; 'open-models': []; changed: [] }>()

const message = ref('')
const busy = ref(false)
const error = ref('')
const run = ref<Run | null>(null)
const conversationId = ref('')
const oldTask = ref<OldTask | null>(null)
const promptBox = ref<HTMLTextAreaElement | null>(null)

// 输入框左边的 ＋：添附件、挂技能、挂 MCP
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
const feedbackMessage = ref('')

let controller: AbortController | null = null

const suggestions = [
  '把这一章做成复习笔记',
  '出 10 道题，难度中等',
  '这个课件对应哪门课的视频',
  '把这门课的考点按章整理一遍',
]

const started = computed(() => !!run.value || !!oldTask.value)
const canSend = computed(() => !!props.courseId && !!message.value.trim() && !busy.value)

/** 点空白处收起菜单：之前只能再点一次加号，弹层盖住输入框，看不見自己打了什么。 */
function closeMenuOnOutside(event: MouseEvent) {
  if (!menuOpen.value) return
  const target = event.target as Node | null
  if (menuWrap.value && target && menuWrap.value.contains(target)) return
  menuOpen.value = false
}
function closeMenuOnEscape(event: KeyboardEvent) {
  if (event.key === 'Escape') menuOpen.value = false
}

onMounted(() => {
  // mousedown 比 click 早，点其他控件时菜单先收起来，不会吃掉那一次点击
  document.addEventListener('mousedown', closeMenuOnOutside)
  document.addEventListener('keydown', closeMenuOnEscape)
})
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', closeMenuOnOutside)
  document.removeEventListener('keydown', closeMenuOnEscape)
  stop()
})

async function toggleMenu() {
  menuOpen.value = !menuOpen.value
  if (!menuOpen.value || menuLoaded.value || menuLoading.value) return
  menuLoading.value = true
  try {
    // probe=false 只读配置，不启动 MCP 子进程——菜单要秒开
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
  message.value = message.value ? `${message.value} ${token}` : token
  menuOpen.value = false
  promptBox.value?.focus()
}

async function pickFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  menuOpen.value = false
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
  menuOpen.value = false
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

/** 工具返回往往几百字，时间线里只留开头，点开由 answer 承载全文 */
function stepSummary(step: Step) {
  const text = (step.output || step.detail || '').replace(/\s+/g, ' ').trim()
  return text.length > 160 ? `${text.slice(0, 160)}…` : text
}

function handleEvent(event: { type: string; [key: string]: unknown }) {
  const current = run.value
  if (!current) return
  if (event.type === 'run') {
    current.id = String(event.run_id || '')
    current.max_turns = Number(event.max_turns || current.max_turns)
    if (event.conversation_id) conversationId.value = String(event.conversation_id)
  } else if (event.type === 'step') {
    const step = event.step as Step
    current.steps.push(step)
    current.turn = step.turn
  } else if (event.type === 'blocked') {
    current.pending = event.pending as Pending
    current.status = 'blocked'
  } else if (event.type === 'final') {
    current.answer = String(event.answer || '')
    current.status = 'done'
  } else if (event.type === 'error') {
    current.status = 'failed'
    current.answer = String(event.message || '出错了')
  }
}

async function send() {
  if (!canSend.value) return
  const goal = message.value.trim()
  menuOpen.value = false
  busy.value = true
  error.value = ''
  feedbackMessage.value = ''
  oldTask.value = null
  // 先放空壳，第一轮模型调用要十几秒，界面不能是死的
  run.value = { id: '', status: 'running', answer: '', steps: [], pending: null, turn: 0, max_turns: 6 }
  message.value = ''
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
      // 事件之间空行分隔；末尾可能截断，留在 buffer 等下一段
      const chunks = buffer.split(String.fromCharCode(10, 10))
      buffer = chunks.pop() || ''
      for (const chunk of chunks) {
        const line = chunk.split(String.fromCharCode(10)).find((item) => item.startsWith('data: '))
        if (!line) continue
        try {
          handleEvent(JSON.parse(line.slice(6)))
        } catch {
          /* 半截 JSON，下一段补上 */
        }
      }
    }
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') {
      if (run.value) run.value.status = 'failed'
    } else {
      error.value = e instanceof Error ? e.message : String(e)
      if (run.value) run.value.status = 'failed'
    }
  } finally {
    controller = null
    busy.value = false
  }
}

function stop() {
  controller?.abort()
  controller = null
  busy.value = false
  if (run.value && run.value.status === 'running') {
    run.value.status = 'failed'
    run.value.answer = run.value.answer || '已停止'
  }
}

async function resume(approve: boolean) {
  if (!run.value?.id) return
  try {
    await api('/agent/resume', { method: 'POST', body: JSON.stringify({ run_id: run.value.id, approve }) })
    run.value.pending = null
    run.value.status = approve ? 'done' : 'failed'
    if (!approve) run.value.answer = '已取消这一步'
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

/** 从历史任务进来：把那次的原话留在这儿，一键交给 Agent 重跑 */
async function loadOldTask(id: string) {
  oldTask.value = null
  try {
    oldTask.value = await api<OldTask>(`/tasks/${id}`)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function reuseOldTask() {
  if (!oldTask.value) return
  message.value = oldTask.value.message
  oldTask.value = null
  emit('select-task', '')
  promptBox.value?.focus()
}

function reset() {
  stop()
  run.value = null
  oldTask.value = null
  conversationId.value = ''
  message.value = ''
  error.value = ''
  feedbackMessage.value = ''
  emit('select-task', '')
}

watch(() => props.taskId, (id) => {
  if (id) void loadOldTask(id)
  else oldTask.value = null
}, { immediate: true })
</script>

<template>
  <div class="task-page">
    <!-- 历史任务：保留原样，但可以一句话交给 Agent 重做 -->
    <template v-if="oldTask">
      <header class="task-head">
        <div>
          <p class="task-eyebrow"><span class="task-status">{{ oldTask.status }}</span></p>
          <h2>{{ oldTask.title }}</h2>
          <p class="task-quote">「{{ oldTask.message }}」</p>
        </div>
        <button class="button subtle small" @click="reset">新建任务</button>
      </header>
      <p class="task-hint">这条是旧版「按固定步骤执行」的记录。交给 Agent 重做，它会自己决定查什么、怎么做。</p>
      <div class="task-actions">
        <button class="button primary" @click="reuseOldTask">交给 Agent 重做</button>
      </div>
    </template>

    <!-- Agent 会话 -->
    <template v-else-if="run">
      <div class="agent-thread">
        <article v-for="(step, index) in run.steps" :key="index" class="agent-step">
          <div class="agent-step-head">
            <span class="task-step-index">{{ step.turn || index + 1 }}</span>
            <strong>{{ stepTitle(step) }}</strong>
            <span :class="['task-chip', run.status === 'failed' ? 'failed' : 'done']">{{ run.status === 'running' ? '进行中' : '已结束' }}</span>
          </div>
          <p v-if="stepSummary(step)" class="agent-step-body">{{ stepSummary(step) }}</p>
        </article>

        <p v-if="busy && !run.steps.length" class="agent-waiting">正在读资料、挑工具…</p>

        <section v-if="run.answer" class="agent-answer">
          <p class="agent-answer-label">{{ run.status === 'failed' ? '没有完成' : '回答' }}</p>
          <MarkdownContent :content="run.answer" />
        </section>

        <div v-if="run.pending" class="task-guard">
          <p class="task-guard-title">这一步会改动内容，需要你点头</p>
          <p class="agent-step-body">{{ run.pending.tool }} — {{ run.pending.reason }}</p>
          <div class="task-guard-actions">
            <button class="button primary small" @click="resume(true)">允许</button>
            <button class="text-button" @click="resume(false)">不做了</button>
          </div>
        </div>

        <p v-if="error" class="task-error">{{ error }}</p>

        <!-- 继续追问：同一个 conversation_id 共享上下文 -->
        <div class="task-composer agent-composer">
          <div ref="menuWrap" class="composer-menu-wrap">
            <button class="composer-plus" :class="{ open: menuOpen }" aria-label="添加内容" :disabled="!courseId" @click="toggleMenu">＋</button>
            <div v-if="menuOpen" class="composer-menu" role="menu">
              <div class="composer-menu-group">
                <button class="composer-menu-item" role="menuitem" :disabled="uploading" @click="fileInput?.click()">
                  <span>添加文件</span><small>{{ uploading ? '上传中…' : '课件、Word、PPT、PDF' }}</small>
                </button>
              </div>
              <div class="composer-menu-group">
                <p class="composer-menu-title">技能 · {{ menuLoading ? '读取中…' : `${skills.length} 个` }}</p>
                <button v-for="skill in skills" :key="skill.name" class="composer-menu-item" role="menuitem" @click="insertToken(`@skill:${skill.name}`)">
                  <span>{{ skill.name }}</span><small>{{ skill.description.slice(0, 22) }}</small>
                </button>
                <p v-if="!menuLoading && !skills.length" class="composer-menu-empty">还没有装技能 · 去「工作区」导入</p>
              </div>
              <div class="composer-menu-group">
                <p class="composer-menu-title">MCP · {{ menuLoading ? '读取中…' : `${servers.length} 个` }}</p>
                <button v-for="server in servers" :key="server.name" class="composer-menu-item" role="menuitem" @click="insertToken(`@mcp:${server.name}`)">
                  <span>{{ server.name }}</span><small>{{ server.tools.length }} 个工具</small>
                </button>
                <p v-if="!menuLoading && !servers.length" class="composer-menu-empty">还没有连上 MCP · 去「工作区」添加</p>
              </div>
              <div class="composer-menu-group">
                <button class="composer-menu-item" role="menuitem" :disabled="!!exporting" @click="exportDoc('docx')">
                  <span>导出 Word</span><small>{{ exporting === 'docx' ? '生成中…' : '把当前笔记存成 docx' }}</small>
                </button>
                <button class="composer-menu-item" role="menuitem" :disabled="!!exporting" @click="exportDoc('pptx')">
                  <span>导出 PPT</span><small>{{ exporting === 'pptx' ? '生成中…' : '一节一页，要点成行' }}</small>
                </button>
              </div>
            </div>
          </div>
          <textarea
            ref="promptBox"
            v-model="message"
            rows="2"
            :disabled="busy"
            placeholder="继续追问，或让它接着做下一步"
            @keydown.enter.ctrl.prevent="send"
            @keydown.enter.meta.prevent="send"
          ></textarea>
          <input ref="fileInput" type="file" multiple hidden accept=".pdf,.docx,.pptx,.md,.txt" @change="pickFiles" />
          <div class="task-composer-foot">
            <span class="task-tip">{{ busy ? 'Ctrl + Enter 无效，先停止' : 'Ctrl + Enter 发送 · Esc 关闭菜单' }}</span>
            <div class="agent-composer-actions">
              <button v-if="busy" class="text-button" @click="stop">停止</button>
              <button class="button primary small" :disabled="!canSend" @click="send">发送</button>
            </div>
          </div>
        </div>

        <button class="text-button agent-reset" @click="reset">开始新的任务</button>
      </div>
    </template>

    <!-- 新建 -->
    <template v-else>
      <div class="task-new">
        <h1>要做什么？</h1>
        <p class="task-new-sub">
          {{ courseName ? `当前课程：${courseName} · 它会自己查资料、调工具，把结果交给你` : '先在左下角选一门课程' }}
        </p>
        <div class="task-composer">
          <div ref="menuWrap" class="composer-menu-wrap">
            <button class="composer-plus" :class="{ open: menuOpen }" aria-label="添加内容" :disabled="!courseId" @click="toggleMenu">＋</button>
            <div v-if="menuOpen" class="composer-menu" role="menu">
              <div class="composer-menu-group">
                <button class="composer-menu-item" role="menuitem" :disabled="uploading" @click="fileInput?.click()">
                  <span>添加文件</span><small>{{ uploading ? '上传中…' : '课件、Word、PPT、PDF' }}</small>
                </button>
              </div>
              <div class="composer-menu-group">
                <p class="composer-menu-title">技能 · {{ menuLoading ? '读取中…' : `${skills.length} 个` }}</p>
                <button v-for="skill in skills" :key="skill.name" class="composer-menu-item" role="menuitem" @click="insertToken(`@skill:${skill.name}`)">
                  <span>{{ skill.name }}</span><small>{{ skill.description.slice(0, 22) }}</small>
                </button>
                <p v-if="!menuLoading && !skills.length" class="composer-menu-empty">还没有装技能 · 去「工作区」导入</p>
              </div>
              <div class="composer-menu-group">
                <p class="composer-menu-title">MCP · {{ menuLoading ? '读取中…' : `${servers.length} 个` }}</p>
                <button v-for="server in servers" :key="server.name" class="composer-menu-item" role="menuitem" @click="insertToken(`@mcp:${server.name}`)">
                  <span>{{ server.name }}</span><small>{{ server.tools.length }} 个工具</small>
                </button>
                <p v-if="!menuLoading && !servers.length" class="composer-menu-empty">还没有连上 MCP · 去「工作区」添加</p>
              </div>
              <div class="composer-menu-group">
                <button class="composer-menu-item" role="menuitem" :disabled="!!exporting" @click="exportDoc('docx')">
                  <span>导出 Word</span><small>{{ exporting === 'docx' ? '生成中…' : '把当前笔记存成 docx' }}</small>
                </button>
                <button class="composer-menu-item" role="menuitem" :disabled="!!exporting" @click="exportDoc('pptx')">
                  <span>导出 PPT</span><small>{{ exporting === 'pptx' ? '生成中…' : '一节一页，要点成行' }}</small>
                </button>
              </div>
            </div>
          </div>
          <textarea
            ref="promptBox"
            v-model="message"
            rows="3"
            :disabled="busy || !courseId"
            placeholder="把第三章做成笔记，再出 10 道题；或者问我这份课件讲了什么"
            @keydown.enter.ctrl.prevent="send"
            @keydown.enter.meta.prevent="send"
          ></textarea>
          <div v-if="attached.length" class="composer-attached">
            <span v-for="item in attached" :key="item.id" class="composer-chip">{{ item.filename }}</span>
          </div>
          <input ref="fileInput" type="file" multiple hidden accept=".pdf,.docx,.pptx,.md,.txt" @change="pickFiles" />
          <div class="task-composer-foot">
            <span class="task-tip">Ctrl + Enter 发送 · Esc 关闭菜单</span>
            <button class="button primary small" :disabled="!canSend" @click="send">
              {{ busy ? '执行中…' : '交给 Agent' }}
            </button>
          </div>
        </div>
        <div class="task-suggestions">
          <button v-for="item in suggestions" :key="item" class="task-suggestion" @click="message = item">{{ item }}</button>
        </div>
        <p v-if="feedbackMessage" class="task-hint">{{ feedbackMessage }}</p>
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
.task-head h2 { margin: 0; font-size: 18px; font-weight: 600; line-height: 1.5; }
.task-quote { margin: 6px 0 0; font-size: 12.5px; color: var(--muted); }
.task-chip { font-size: 10.5px; padding: 1px 7px; border-radius: 9px; background: var(--soft); color: var(--accent-dark); }
.task-chip.done { background: var(--success-soft); color: var(--success); }
.task-chip.failed { background: var(--danger-soft); color: var(--danger); }
.task-hint { margin: 12px 0 16px; font-size: 12.5px; color: var(--muted); }
.task-actions { display: flex; align-items: center; gap: 12px; }
.task-error { margin: 10px 0; font-size: 12.5px; color: var(--danger); }

.agent-thread { display: flex; flex-direction: column; gap: 12px; }
.agent-step { border: 1px solid var(--line); border-radius: 12px; padding: 11px 14px; background: var(--surface); }
.agent-step-head { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.agent-step-head strong { font-size: 13px; }
.task-step-index { width: 20px; height: 20px; border-radius: 50%; background: var(--soft); color: var(--accent-dark); font-size: 11px; display: flex; align-items: center; justify-content: center; flex: none; }
.agent-step-body { margin: 6px 0 0 29px; font-size: 12px; color: var(--muted); line-height: 1.65; }
.agent-waiting { margin: 4px 0; font-size: 12.5px; color: var(--muted); }
.agent-answer { border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; background: var(--surface); }
.agent-answer-label { margin: 0 0 8px; font-size: 11px; letter-spacing: .04em; color: var(--muted); }
.agent-composer { margin-top: 4px; }
.agent-composer-actions { display: flex; align-items: center; gap: 12px; }
.agent-reset { align-self: flex-start; margin-top: 2px; }
.task-guard { margin: 4px 0; padding: 13px 15px; border: 1px solid var(--warning-soft); border-left: 3px solid var(--warning); border-radius: 10px; background: var(--surface); }
.task-guard-title { margin: 0 0 7px; font-size: 13px; font-weight: 600; color: var(--ink); }
.task-guard-actions { display: flex; gap: 12px; margin-top: 10px; }
</style>
