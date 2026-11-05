<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, API_BASE } from '../api'

type Tool = { name: string; description: string; read_only: boolean; destructive: boolean; schema: Record<string, any> }
type Server = { name: string; disabled: boolean; command: string; tools: Tool[]; error: string; version: string }
type Skill = { name: string; description: string; path: string; chars: number; agent_created: boolean }

const props = defineProps<{ open: boolean; courseId?: string }>()
const emit = defineEmits<{ close: [] }>()

const tab = ref<'mcp' | 'skills' | 'agent'>('mcp')
const loading = ref(false)
const servers = ref<Server[]>([])
const skills = ref<Skill[]>([])
const configPath = ref('')
const error = ref('')

const active = ref<string>('')

// ── 导入：技能包 / MCP 服务器 ──────────────────────────
const importing = ref<'' | 'skill' | 'mcp'>('')
const skillText = ref('')
const skillPath = ref('')
const skillOverwrite = ref(false)
const mcpForm = ref({ name: '', command: '', args: '', env: '', url: '' })
const mcpPreview = ref<{ command: string; warning: string } | null>(null)
const mcpTest = ref<{ ok: boolean; tools?: { name: string }[]; error?: string } | null>(null)
const mcpConfirm = ref(false)
const importError = ref('')
const importOk = ref('')

function resetImport() {
  importError.value = ''
  importOk.value = ''
  mcpPreview.value = null
  mcpTest.value = null
  mcpConfirm.value = false
}

async function doImportSkill() {
  resetImport()
  importing.value = 'skill'
  try {
    const payload = await api<{ name: string; path: string; overwrote: boolean; files?: number }>(
      '/workspace/skills/import',
      { method: 'POST', body: JSON.stringify({ text: skillText.value, path: skillPath.value, overwrite: skillOverwrite.value }) },
    )
    importOk.value = `已导入 ${payload.name}${payload.files ? `（${payload.files} 个文件）` : ''}${payload.overwrote ? '，覆盖了旧的' : ''}`
    skillText.value = ''
    skillPath.value = ''
    await load(false)
  } catch (e) {
    importError.value = e instanceof Error ? e.message : String(e)
  } finally {
    importing.value = ''
  }
}

function mcpPayload() {
  const form = mcpForm.value
  return {
    name: form.name.trim(),
    command: form.command.trim(),
    args: form.args.split('\n').map((line) => line.trim()).filter(Boolean),
    env: Object.fromEntries(
      form.env.split('\n').map((line) => line.split('=')).filter((kv) => kv.length >= 2)
        .map((kv) => [kv[0].trim(), kv.slice(1).join('=').trim()]),
    ),
    url: form.url.trim(),
  }
}

async function previewMcp() {
  resetImport()
  try {
    mcpPreview.value = await api('/workspace/mcp/preview', { method: 'POST', body: JSON.stringify(mcpPayload()) })
  } catch (e) {
    importError.value = e instanceof Error ? e.message : String(e)
  }
}

async function testMcp() {
  resetImport()
  importing.value = 'mcp'
  try {
    mcpTest.value = await api('/workspace/mcp/test', { method: 'POST', body: JSON.stringify(mcpPayload()) })
  } catch (e) {
    importError.value = e instanceof Error ? e.message : String(e)
  } finally {
    importing.value = ''
  }
}

async function saveMcp() {
  resetImport()
  importing.value = 'mcp'
  try {
    const result = await api<{ name: string; command: string }>('/workspace/mcp/servers', {
      method: 'POST',
      body: JSON.stringify({ server: mcpPayload(), confirm: mcpConfirm.value, overwrite: skillOverwrite.value }),
    })
    importOk.value = `已导入 ${result.name}：${result.command}`
    mcpForm.value = { name: '', command: '', args: '', env: '', url: '' }
    await load(true)
  } catch (e) {
    importError.value = e instanceof Error ? e.message : String(e)
  } finally {
    importing.value = ''
  }
}

// ── Agent：模型自己选工具 ──────────────────────────────
type AgentStep = { turn: number; kind: string; tool?: string; output?: string; detail?: string; reason?: string; decision?: string; ok?: boolean }
type AgentRun = {
  id: string; status: string; answer: string; steps: AgentStep[]
  pending: { tool: string; args: Record<string, unknown>; risk: string; reason: string } | null
  turn: number; max_turns: number
}
const agentGoal = ref('')
const agentRun = ref<AgentRun | null>(null)
const agentBusy = ref(false)
const tools = ref<{ name: string; description: string; read_only: boolean }[]>([])
// 关掉弹窗时要能掐断请求：agent 一次跑几十秒，用户关了它还接着烧额度
let controller: AbortController | null = null

function stopAgent() {
  controller?.abort()
  controller = null
  agentBusy.value = false
  if (agentRun.value && agentRun.value.status === 'running') agentRun.value.status = 'failed'
  if (agentRun.value) agentRun.value.answer = agentRun.value.answer || '已停止'
}

watch(() => props.open, (open) => { if (!open) stopAgent() })

watch(() => props.open, (open) => { if (!open) stopAgent() })

function handleAgentEvent(event: { type: string; [key: string]: unknown }) {
  const run = agentRun.value
  if (!run) return
  if (event.type === 'run') {
    run.id = String(event.run_id || '')
    run.max_turns = Number(event.max_turns || run.max_turns)
  } else if (event.type === 'step') {
    const step = event.step as AgentStep
    run.steps.push(step)
    run.turn = step.turn
  } else if (event.type === 'blocked') {
    run.pending = event.pending as AgentRun['pending']
    run.status = 'blocked'
  } else if (event.type === 'final') {
    run.answer = String(event.answer || '')
    run.status = 'done'
  } else if (event.type === 'error') {
    run.status = 'failed'
    run.answer = String(event.message || '出错了')
  }
}

async function startAgent() {
  if (!agentGoal.value.trim() || agentBusy.value) return
  agentBusy.value = true
  importError.value = ''
  // 先放一个空壳，事件一到就往里填——否则第一轮模型调用那 10 秒界面是空的
  agentRun.value = { id: '', status: 'running', answer: '', steps: [], pending: null, turn: 0, max_turns: 6 }
  try {
    controller = new AbortController()
    const response = await fetch(`${API_BASE}/agent/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal: agentGoal.value.trim(), course_id: props.courseId || '', max_turns: 6 }),
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
      // 事件之间用空行分隔；最后一段可能不完整，留在 buffer 里等下一次
      const chunks = buffer.split(String.fromCharCode(10, 10))
      buffer = chunks.pop() || ''
      for (const chunk of chunks) {
        const line = chunk.split(String.fromCharCode(10)).find((item) => item.startsWith('data: '))
        if (!line) continue
        try {
          handleAgentEvent(JSON.parse(line.slice(6)))
        } catch {
          /* 半截 JSON 就跳过，下一段会补上 */
        }
      }
    }
  } catch (e) {
    // 主动停止不算错误，别弹红字
    if (e instanceof DOMException && e.name === 'AbortError') {
      if (agentRun.value) agentRun.value.status = 'failed'
    } else {
      importError.value = e instanceof Error ? e.message : String(e)
      if (agentRun.value) agentRun.value.status = 'failed'
    }
  } finally {
    controller = null
    agentBusy.value = false
  }
}

async function resumeAgent(approve: boolean) {
  if (!agentRun.value) return
  agentBusy.value = true
  try {
    agentRun.value = await api<AgentRun>('/agent/resume', {
      method: 'POST',
      body: JSON.stringify({ run_id: agentRun.value.id, approve }),
    })
  } catch (e) {
    importError.value = e instanceof Error ? e.message : String(e)
  } finally {
    agentBusy.value = false
  }
}

async function loadTools() {
  try {
    const payload = await api<{ tools: { name: string; description: string; read_only: boolean }[] }>('/agent/tools?probe=false')
    tools.value = payload.tools
  } catch { tools.value = [] }
}

async function removeSkill(name: string) {
  if (!confirm(`删除技能 ${name}？`)) return
  try {
    await api(`/workspace/skills/${encodeURIComponent(name)}`, { method: 'DELETE' })
    await load(false)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function removeServer(name: string) {
  if (!confirm(`移除 MCP 服务器 ${name}？会从 mcp.json 里删掉这一项。`)) return
  try {
    await api(`/workspace/mcp/servers/${encodeURIComponent(name)}`, { method: 'DELETE' })
    await load(true)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

// 工具多的服务器默认折叠：UUMit 有 56 个，全铺出来屏幕装不下
const expanded = ref<Set<string>>(new Set())
function isOpen(name: string) { return expanded.value.has(name) }
function toggleServer(server: Server) {
  const next = new Set(expanded.value)
  if (next.has(server.name)) next.delete(server.name)
  else next.add(server.name)
  expanded.value = next
}
function autoExpand(list: Server[]) {
  const next = new Set(expanded.value)
  for (const server of list) {
    if (!server.disabled && server.tools.length && server.tools.length <= 12) next.add(server.name)
  }
  expanded.value = next
}
// 工具多的服务器默认折叠：UUMit 有 56 个，全铺出来屏幕装不下
const running = ref<string>('')
const args = ref<Record<string, string>>({})
const result = ref<{ text: string; is_error: boolean; truncated: boolean; chars: number } | null>(null)

const query = ref('')
const detail = ref<{ name: string; body: string; path: string } | null>(null)

const enabled = computed(() => servers.value.filter((s) => !s.disabled))
const disabled = computed(() => servers.value.filter((s) => s.disabled))
const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return skills.value
  return skills.value.filter((s) => s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q))
})

// 从 schema 生成一个能直接改的参数骨架，省得用户对着空 {} 猜字段
function skeleton(schema: Record<string, any>): string {
  const props = schema?.properties || {}
  const out: Record<string, any> = {}
  for (const [key, spec] of Object.entries<any>(props)) {
    if (spec?.enum?.length) out[key] = spec.enum[0]
    else if (spec?.type === 'string') out[key] = ''
    else if (spec?.type === 'integer' || spec?.type === 'number') out[key] = 0
    else if (spec?.type === 'boolean') out[key] = false
    else if (spec?.type === 'array') out[key] = []
    else if (spec?.type === 'object' || !spec?.type) out[key] = {}
  }
  return JSON.stringify(out, null, 2)
}

// probe=false 先拿配置秒开，再追一次 probe=true 补工具
async function load(probe = true) {
  loading.value = probe
  error.value = ''
  try {
    const payload = await api<{ mcp: { config_path: string; servers: Server[] }; skills: Skill[] }>(
      `/workspace/overview?probe=${probe}`,
    )
    servers.value = payload.mcp.servers
    autoExpand(payload.mcp.servers)
    autoExpand(payload.mcp.servers)
    configPath.value = payload.mcp.config_path
    skills.value = payload.skills
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function pick(server: Server, tool: Tool) {
  const key = `${server.name}::${tool.name}`
  if (active.value === key) { active.value = ''; return }
  active.value = key
  result.value = null
  args.value = { [key]: skeleton(tool.schema) }
}

async function run(server: Server, tool: Tool) {
  const key = `${server.name}::${tool.name}`
  running.value = key
  result.value = null
  error.value = ''
  try {
    let parsed: Record<string, unknown> = {}
    const raw = (args.value[key] || '').trim()
    if (raw) {
      try { parsed = JSON.parse(raw) } catch { throw new Error('参数不是合法 JSON') }
    }
    result.value = await api<{ text: string; is_error: boolean; truncated: boolean; chars: number }>(
      '/workspace/mcp/call',
      { method: 'POST', body: JSON.stringify({ server: server.name, tool: tool.name, arguments: parsed, timeout: 120 }) },
    )
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    running.value = ''
  }
}

async function openSkill(skill: Skill) {
  detail.value = { name: skill.name, body: '读取中…', path: skill.path }
  try {
    const payload = await api<{ name: string; body: string; path: string }>(`/workspace/skills/${encodeURIComponent(skill.name)}`)
    detail.value = { name: payload.name, body: payload.body, path: payload.path }
  } catch (e) {
    detail.value = { name: skill.name, body: '读取失败：' + (e instanceof Error ? e.message : String(e)), path: skill.path }
  }
}

watch(() => props.open, (value) => {
  if (!value) return
  tab.value = 'mcp'
  active.value = ''
  result.value = null
  detail.value = null
  query.value = ''
  if (!servers.value.length) {
    void load(false).then(() => load(true))
  }
})
</script>

<template>
  <div v-if="open" class="ws-mask" @click.self="emit('close')">
    <section class="ws-panel" role="dialog" aria-label="工作区">
      <header class="ws-head">
        <div>
          <h2>工作区</h2>
          <p class="ws-sub">
            接入本机的 MCP 工具与技能包。配置直接读 <code>{{ configPath || '~/.workbuddy/mcp.json' }}</code>，改那边这边就生效。
          </p>
        </div>
        <div class="ws-head-actions">
          <button class="button subtle small" :disabled="loading" @click="load(true)">{{ loading ? '读取中…' : '刷新' }}</button>
          <button class="icon-button small" aria-label="关闭" @click="emit('close')">×</button>
        </div>
      </header>

      <div class="ws-tabs" role="tablist">
        <button :class="['ws-tab', { on: tab === 'mcp' }]" @click="tab = 'mcp'">MCP 工具 <span class="ws-count">{{ enabled.length }}</span></button>
        <button :class="['ws-tab', { on: tab === 'skills' }]" @click="tab = 'skills'">技能包 <span class="ws-count">{{ skills.length }}</span></button>
        <button :class="['ws-tab', { on: tab === 'agent' }]" @click="tab = 'agent'; loadTools()">让 Agent 自己查</button>
      </div>

      <p v-if="error" class="ws-error">{{ error }}</p>
      <p v-if="importError" class="ws-error">{{ importError }}</p>
      <p v-if="importOk" class="ws-ok">{{ importOk }}</p>

      <div v-if="tab === 'mcp'" class="ws-body">
        <section class="ws-import">
          <div class="ws-import-head">
            <strong>添加 MCP 服务器</strong>
            <small>会写进 mcp.json，导入后立刻生效</small>
          </div>
          <div class="ws-form">
            <label>名称 <input v-model="mcpForm.name" placeholder="filesystem" /></label>
            <label>启动命令 <input v-model="mcpForm.command" placeholder="npx" /></label>
            <label class="wide">参数 <textarea v-model="mcpForm.args" rows="2" placeholder="-y&#10;@modelcontextprotocol/server-filesystem&#10;D:\课程资料"></textarea></label>
            <label class="wide">环境变量 <textarea v-model="mcpForm.env" rows="2" placeholder="API_KEY=xxx（每行一个）"></textarea></label>
            <label>或者远程地址 <input v-model="mcpForm.url" placeholder="http://127.0.0.1:7000/mcp" /></label>
          </div>
          <div class="ws-import-actions">
            <button class="button subtle small" @click="previewMcp">预览命令</button>
            <button class="button subtle small" :disabled="!!importing" @click="testMcp">{{ importing === 'mcp' ? '连接中…' : '试连一次' }}</button>
            <label class="ws-check"><input v-model="skillOverwrite" type="checkbox" />覆盖同名</label>
          </div>
          <div v-if="mcpPreview" class="ws-preview">
            <p class="ws-preview-label">将要长期运行的命令：</p>
            <code>{{ mcpPreview.command }}</code>
            <p class="ws-preview-warn">{{ mcpPreview.warning }}</p>
            <label class="ws-check"><input v-model="mcpConfirm" type="checkbox" />我确认这条命令可以在这台机器上运行</label>
            <button class="button primary small" :disabled="!mcpConfirm || !!importing" @click="saveMcp">导入</button>
          </div>
          <div v-if="mcpTest" :class="['ws-preview', mcpTest.ok ? 'ok' : 'bad']">
            <p v-if="mcpTest.ok" class="ws-preview-label">连上了，{{ mcpTest.tools?.length || 0 }} 个工具：{{ (mcpTest.tools || []).slice(0, 6).map(t => t.name).join('、') }}</p>
            <p v-else class="ws-preview-label">连不上：{{ mcpTest.error }}</p>
          </div>
        </section>

        <div v-for="server in enabled" :key="server.name" class="ws-server">
          <button class="ws-server-head" @click="toggleServer(server)">
            <div>
              <span class="ws-caret">{{ isOpen(server.name) ? '▾' : '▸' }}</span>
              <strong>{{ server.name }}</strong>
              <span v-if="server.version" class="ws-ver">v{{ server.version }}</span>
              <span class="ws-tools-count">{{ server.tools.length }} 个工具</span>
            </div>
            <span v-if="server.error" class="ws-badge bad">连接失败</span>
            <span class="ws-server-remove" role="button" tabindex="0" title="从 mcp.json 移除"
                  @click.stop="removeServer(server.name)" @keydown.enter.stop="removeServer(server.name)">移除</span>
          </button>
          <p v-if="server.error" class="ws-server-error">{{ server.error }}</p>

          <div v-for="tool in server.tools" v-show="isOpen(server.name)" :key="tool.name" class="ws-tool">
            <button class="ws-tool-row" @click="pick(server, tool)">
              <span class="ws-tool-name">{{ tool.name }}</span>
              <span v-if="tool.read_only" class="ws-badge ok">只读</span>
              <span v-if="tool.destructive" class="ws-badge bad">会改东西</span>
              <span class="ws-tool-desc">{{ tool.description }}</span>
              <span class="ws-tool-caret">{{ active === `${server.name}::${tool.name}` ? '收起' : '试调用' }}</span>
            </button>

            <div v-if="active === `${server.name}::${tool.name}`" class="ws-tool-open">
              <label class="ws-label">参数（JSON）</label>
              <textarea v-model="args[`${server.name}::${tool.name}`]" rows="5" spellcheck="false"></textarea>
              <div class="ws-tool-actions">
                <button class="button small" :disabled="running === `${server.name}::${tool.name}`" @click="run(server, tool)">
                  {{ running === `${server.name}::${tool.name}` ? '执行中…' : '运行这个工具' }}
                </button>
                <span class="ws-hint">工具直接跑在这台机器上，结果不会发给模型</span>
              </div>
              <div v-if="result" :class="['ws-result', { bad: result.is_error }]">
                <div class="ws-result-head">
                  <span>{{ result.is_error ? '执行报错' : '返回结果' }}</span>
                  <span class="ws-hint">{{ result.chars }} 字{{ result.truncated ? '（已截断显示）' : '' }}</span>
                </div>
                <pre>{{ result.text || '（空）' }}</pre>
              </div>
            </div>
          </div>
        </div>

        <div v-if="disabled.length" class="ws-disabled">
          <p class="ws-label">已禁用（mcp.json 里 <code>disabled: true</code>）</p>
          <p class="ws-disabled-list">{{ disabled.map((s) => s.name).join(' · ') }}</p>
        </div>
      </div>

      <div v-else-if="tab === 'agent'" class="ws-body">
        <section class="ws-import">
          <div class="ws-import-head">
            <strong>让 agent 自己查</strong>
            <small>它自己挑工具、自己调；会改东西的调用会停下来问你</small>
          </div>
          <textarea v-model="agentGoal" rows="3" class="ws-skill-text" placeholder="例如：用 ltspice 看看现在有哪些电路分组，把名字列出来"></textarea>
          <div class="ws-import-actions">
            <button v-if="!agentBusy" class="button primary small" :disabled="!agentGoal.trim()" @click="startAgent">开始</button>
            <button v-else class="button subtle small danger" @click="stopAgent">停止</button>
            <span class="ws-tools-count">可用工具 {{ tools.length }} 个</span>
          </div>
        </section>

        <div v-if="agentRun" class="ws-agent-run">
          <p class="ws-agent-status">
            状态：{{ { running: '运行中', done: '已完成', blocked: '等你确认', failed: '失败' }[agentRun.status] || agentRun.status }}
            · 第 {{ agentRun.turn }}/{{ agentRun.max_turns }} 轮
          </p>

          <ol class="ws-steps">
            <li v-for="(step, index) in agentRun.steps" :key="index" :class="['ws-step', step.decision === 'deny' ? 'deny' : '']">
              <span class="ws-step-index">{{ step.turn }}</span>
              <div>
                <p class="ws-step-title">
                  {{ step.kind === 'tool' ? (step.tool || '工具') : step.kind === 'final' ? '给出回答' : step.kind }}
                  <span v-if="step.decision === 'ask'" class="ws-badge warn">需确认</span>
                  <span v-else-if="step.decision === 'deny'" class="ws-badge bad">已拒绝</span>
                  <span v-else-if="step.ok === false" class="ws-badge bad">失败</span>
                </p>
                <p v-if="step.output" class="ws-step-out">{{ step.output.slice(0, 220) }}</p>
                <p v-else-if="step.reason" class="ws-step-out">{{ step.reason }}</p>
              </div>
            </li>
          </ol>

          <div v-if="agentRun.pending" class="ws-preview">
            <p class="ws-preview-label">它想调用 {{ agentRun.pending.tool }}（{{ agentRun.pending.risk }}）：{{ agentRun.pending.reason }}</p>
            <code>{{ JSON.stringify(agentRun.pending.args) }}</code>
            <div class="ws-import-actions">
              <button class="button primary small" :disabled="agentBusy" @click="resumeAgent(true)">允许这一步</button>
              <button class="button subtle small" :disabled="agentBusy" @click="resumeAgent(false)">拒绝</button>
            </div>
          </div>

          <div v-if="agentRun.answer" class="ws-agent-answer">{{ agentRun.answer }}</div>
        </div>
      </div>

      <div v-else class="ws-body ws-body-skills">
        <section class="ws-import">
          <div class="ws-import-head">
            <strong>导入技能包</strong>
            <small>粘贴 SKILL.md，或填一个本地目录</small>
          </div>
          <textarea v-model="skillText" rows="5" class="ws-skill-text" placeholder="---&#10;name: my-skill&#10;description: 一句话说明什么时候用它&#10;---&#10;&#10;正文：步骤、产出、注意事项"></textarea>
          <input v-model="skillPath" class="ws-skill-path" placeholder="或者：D:\skills\my-skill（目录里要有 SKILL.md）" />
          <div class="ws-import-actions">
            <label class="ws-check"><input v-model="skillOverwrite" type="checkbox" />覆盖同名技能</label>
            <button class="button primary small" :disabled="importing === 'skill'" @click="doImportSkill">{{ importing === 'skill' ? '导入中…' : '导入' }}</button>
          </div>
        </section>

        <input v-model="query" class="ws-search" placeholder="搜索技能：fpga、docx、仿真…" />
        <div class="ws-skill-list">
          <button v-for="skill in filtered" :key="skill.name" :class="['ws-skill', { on: detail?.name === skill.name }]" @click="openSkill(skill)">
            <span class="ws-skill-name">{{ skill.name }}</span>
            <span v-if="skill.agent_created" class="ws-badge">自建</span>
            <span class="ws-skill-desc">{{ skill.description }}</span>
          </button>
          <p v-if="!filtered.length" class="muted small">没有匹配的技能。</p>
        </div>
        <div v-if="detail" class="ws-skill-detail">
          <div class="ws-result-head">
            <span>{{ detail.name }}</span>
            <span class="ws-hint">{{ detail.path }}</span>
            <button class="text-button danger" @click="removeSkill(detail.name); detail = null">删除这个技能</button>
          </div>
          <pre>{{ detail.body }}</pre>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.ws-server-remove { font-size: 11px; color: var(--danger); padding: 1px 6px; border-radius: 5px; cursor: pointer; }
.ws-server-remove:hover { background: var(--danger-soft); }

.ws-agent-status { margin: 0 0 10px; font-size: 12.5px; color: var(--muted); }
.ws-steps { list-style: none; margin: 0 0 12px; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.ws-step { display: flex; gap: 9px; align-items: flex-start; }
.ws-step-index { width: 20px; height: 20px; border-radius: 50%; background: var(--soft); color: var(--accent-dark); font-size: 11px; display: flex; align-items: center; justify-content: center; flex: none; }
.ws-step.deny .ws-step-index { background: var(--danger-soft); color: var(--danger); }
.ws-step-title { margin: 0; font-size: 12.5px; color: var(--ink); display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.ws-step-out { margin: 3px 0 0; font-size: 11.5px; color: var(--muted); font-family: ui-monospace, Consolas, monospace; word-break: break-all; }
.ws-badge.warn { background: var(--warning-soft); color: var(--warning); }
.ws-badge.bad { background: var(--danger-soft); color: var(--danger); }
.ws-agent-answer { margin-top: 12px; padding: 11px 13px; border-left: 3px solid var(--accent); background: var(--soft); border-radius: 6px; font-size: 13px; line-height: 1.75; white-space: pre-wrap; }

.ws-agent-run { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; background: var(--surface); }
.ws-agent-status { margin: 0 0 10px; font-size: 12.5px; color: var(--muted); }
.ws-steps { list-style: none; margin: 0 0 12px; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.ws-step { display: flex; gap: 9px; align-items: flex-start; }
.ws-step-index { width: 20px; height: 20px; border-radius: 50%; background: var(--soft); color: var(--accent-dark); font-size: 11px; display: flex; align-items: center; justify-content: center; flex: none; }
.ws-step.deny .ws-step-index { background: var(--danger-soft); color: var(--danger); }
.ws-step-title { margin: 0; font-size: 12.5px; color: var(--ink); display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.ws-step-out { margin: 3px 0 0; font-size: 11.5px; color: var(--muted); font-family: ui-monospace, Consolas, monospace; word-break: break-all; }
.ws-badge.warn { background: var(--warning-soft); color: var(--warning); }
.ws-badge.bad { background: var(--danger-soft); color: var(--danger); }
.ws-agent-answer { margin-top: 12px; padding: 11px 13px; border-left: 3px solid var(--accent); background: var(--soft); border-radius: 6px; font-size: 13px; line-height: 1.75; white-space: pre-wrap; }

.ws-ok { margin: 8px 0; font-size: 12.5px; color: var(--success); }
.ws-import { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; background: var(--surface-alt); margin-bottom: 14px; }
.ws-import-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 10px; }
.ws-import-head strong { font-size: 13px; }
.ws-import-head small { font-size: 11.5px; color: var(--muted); }
.ws-form { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; }
.ws-form label { display: flex; flex-direction: column; gap: 3px; font-size: 11.5px; color: var(--muted); }
.ws-form label.wide { grid-column: 1 / -1; }
.ws-form input, .ws-form textarea {
  font: inherit; font-size: 12.5px; padding: 6px 9px; border: 1px solid var(--line);
  border-radius: 6px; background: var(--surface); color: var(--ink); resize: vertical;
}
.ws-skill-text { width: 100%; font: 12px/1.6 ui-monospace, Consolas, monospace; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); color: var(--ink); resize: vertical; }
.ws-skill-path { width: 100%; margin-top: 7px; font: inherit; font-size: 12.5px; padding: 6px 9px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); color: var(--ink); }
.ws-import-actions { display: flex; align-items: center; gap: 12px; margin-top: 10px; }
.ws-check { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink); cursor: pointer; }
.ws-check input { width: 14px; height: 14px; accent-color: var(--accent); }
.ws-preview { margin-top: 10px; padding: 9px 11px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); }
.ws-preview.ok { border-color: var(--success-soft); }
.ws-preview.bad { border-color: var(--danger-soft); }
.ws-preview-label { margin: 0 0 5px; font-size: 12px; color: var(--ink); }
.ws-preview code { display: block; font: 12px/1.6 ui-monospace, Consolas, monospace; padding: 6px 8px; background: #1f2430; color: #e6e9f0; border-radius: 5px; word-break: break-all; }
.ws-preview-warn { margin: 6px 0; font-size: 11.5px; color: var(--warning); }

.ws-mask { position: fixed; inset: 0; background: rgba(24, 34, 54, .35); display: flex; align-items: center; justify-content: center; z-index: 60; }
.ws-panel { width: min(1040px, 94vw); height: min(760px, 88vh); background: var(--panel, var(--surface)); border-radius: 14px; padding: 18px 20px; box-shadow: 0 18px 48px rgba(24, 34, 54, .25); display: flex; flex-direction: column; }
.ws-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.ws-head h2 { margin: 0 0 4px; font-size: 16px; }
.ws-sub { margin: 0; font-size: 12px; color: var(--muted, var(--muted)); line-height: 1.6; }
.ws-sub code { font-size: 11.5px; background: var(--surface-alt); padding: 1px 4px; border-radius: 3px; }
.ws-head-actions { display: flex; gap: 8px; align-items: center; }
.ws-tabs { display: flex; gap: 6px; margin: 14px 0 10px; border-bottom: 1px solid var(--line, var(--line)); }
.ws-tab { background: none; border: none; padding: 8px 4px; margin-right: 14px; font-size: 13px; color: var(--muted, var(--muted)); cursor: pointer; border-bottom: 2px solid transparent; }
.ws-tab.on { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
.ws-count { font-size: 11px; background: var(--surface-alt); border-radius: 8px; padding: 0 5px; margin-left: 3px; }
.ws-body { flex: 1; overflow-y: auto; padding-right: 4px; }
.ws-server { border: 1px solid var(--line, var(--line)); border-radius: 10px; padding: 10px 12px; margin-bottom: 10px; }
.ws-server-head { display: flex; justify-content: space-between; align-items: baseline; width: 100%; background: none; border: none; padding: 0; cursor: pointer; font: inherit; color: inherit; text-align: left; }
.ws-caret { font-size: 10px; color: var(--muted, var(--muted)); margin-right: 5px; }
.ws-ver { font-size: 11px; color: var(--muted, var(--muted)); margin-left: 6px; }
.ws-tools-count { font-size: 11.5px; color: var(--muted, var(--muted)); margin-left: 8px; }
.ws-server-error { margin: 4px 0 0; font-size: 12px; color: var(--danger); }
.ws-tool { border-top: 1px dashed var(--line); margin-top: 8px; padding-top: 6px; }
.ws-tool-row { display: flex; align-items: baseline; gap: 8px; width: 100%; text-align: left; background: none; border: none; padding: 5px 0; cursor: pointer; font: inherit; color: inherit; }
.ws-tool-row:hover .ws-tool-name { color: var(--accent); }
.ws-tool-name { font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; color: var(--ink); flex: none; }
.ws-tool-desc { font-size: 11.5px; color: var(--muted, var(--muted)); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
.ws-badge { font-size: 10.5px; padding: 1px 5px; border-radius: 4px; background: #f0ece2; color: #6b6357; white-space: nowrap; flex: none; }
.ws-tool-caret { font-size: 11px; color: var(--accent); flex: none; white-space: nowrap; }
.ws-tool-caret { font-size: 11px; color: var(--accent); flex: none; white-space: nowrap; }
.ws-badge.ok { background: #dfeee2; color: var(--success); }
.ws-badge.bad { background: var(--danger-soft); color: var(--danger); }
.ws-tool-open { margin: 6px 0 10px; }
.ws-label { display: block; font-size: 11.5px; color: var(--muted, var(--muted)); margin: 6px 0 4px; }
.ws-tool-open textarea { width: 100%; font-family: ui-monospace, Consolas, monospace; font-size: 12px; padding: 8px; border: 1px solid var(--line, var(--line)); border-radius: 6px; background: #fff; resize: vertical; }
.ws-tool-actions { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.ws-hint { font-size: 11px; color: var(--muted, var(--muted)); }
.ws-result { margin-top: 10px; border: 1px solid var(--line, var(--line)); border-radius: 8px; overflow: hidden; }
.ws-result.bad { border-color: #e7c0b8; }
.ws-result-head { display: flex; justify-content: space-between; font-size: 11.5px; padding: 6px 9px; background: var(--surface-alt); color: var(--ink); }
.ws-result pre { margin: 0; padding: 9px; font-size: 12px; line-height: 1.65; max-height: 260px; overflow: auto; white-space: pre-wrap; word-break: break-word; }
.ws-disabled { margin-top: 6px; }
.ws-disabled-list { margin: 0; font-size: 12px; color: var(--muted, var(--muted)); }
.ws-body-skills { display: grid; grid-template-columns: 320px 1fr; gap: 14px; }
.ws-search { grid-column: 1 / -1; padding: 7px 10px; border: 1px solid var(--line, var(--line)); border-radius: 6px; font: inherit; font-size: 12.5px; }
.ws-skill-list { overflow-y: auto; border-right: 1px solid var(--line); padding-right: 10px; }
.ws-skill { display: block; width: 100%; text-align: left; background: none; border: none; border-radius: 6px; padding: 7px 8px; cursor: pointer; font: inherit; color: inherit; }
.ws-skill:hover { background: var(--surface-alt); }
.ws-skill.on { background: var(--soft); }
.ws-skill-name { font-size: 12.5px; font-weight: 600; display: block; }
.ws-skill-desc { display: block; font-size: 11.5px; color: var(--muted, var(--muted)); line-height: 1.5; margin-top: 2px; }
.ws-skill-detail { overflow: hidden; display: flex; flex-direction: column; }
.ws-skill-detail pre { flex: 1; margin: 0; overflow: auto; font-size: 12px; line-height: 1.7; white-space: pre-wrap; word-break: break-word; padding: 10px; background: var(--surface); }
</style>
