<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api'

type Tool = { name: string; description: string; read_only: boolean; destructive: boolean; schema: Record<string, any> }
type Server = { name: string; disabled: boolean; command: string; tools: Tool[]; error: string; version: string }
type Skill = { name: string; description: string; path: string; chars: number; agent_created: boolean }

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const tab = ref<'mcp' | 'skills'>('mcp')
const loading = ref(false)
const servers = ref<Server[]>([])
const skills = ref<Skill[]>([])
const configPath = ref('')
const error = ref('')

const active = ref<string>('')
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
      </div>

      <p v-if="error" class="ws-error">{{ error }}</p>

      <div v-if="tab === 'mcp'" class="ws-body">
        <div v-for="server in enabled" :key="server.name" class="ws-server">
          <button class="ws-server-head" @click="toggleServer(server)">
            <div>
              <span class="ws-caret">{{ isOpen(server.name) ? '▾' : '▸' }}</span>
              <strong>{{ server.name }}</strong>
              <span v-if="server.version" class="ws-ver">v{{ server.version }}</span>
              <span class="ws-tools-count">{{ server.tools.length }} 个工具</span>
            </div>
            <span v-if="server.error" class="ws-badge bad">连接失败</span>
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

      <div v-else class="ws-body ws-body-skills">
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
          </div>
          <pre>{{ detail.body }}</pre>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
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
