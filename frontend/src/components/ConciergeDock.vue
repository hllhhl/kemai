<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { api } from '../api'

type Action = {
  type: string
  label: string
  detail: string
  method: string
  endpoint: string
  body: Record<string, unknown>
  needs_model: boolean
  seconds: number
  ready: boolean
  blocked_reason: string
}
type Plan = {
  intent: string
  actions: Action[]
  needs_model: boolean
  model_ready: boolean
  hint: string
  blocked: boolean
  reasons: string[]
}
type Step = { state: 'idle' | 'running' | 'done' | 'failed'; message: string }

const props = defineProps<{ courseId: string; courseName: string; chapterId: string; modelConfigured: boolean }>()
const emit = defineEmits<{ navigate: [section: string]; openModels: [] }>()

const open = ref(false)
const text = ref('')
const busy = ref(false)
const plan = ref<Plan | null>(null)
const error = ref('')
const steps = ref<Record<number, Step>>({})
const input = ref<HTMLInputElement | null>(null)

const canAsk = computed(() => !!props.courseId && text.value.trim().length > 0 && !busy.value)
const finished = computed(() => plan.value?.actions.every((_, i) => steps.value[i]?.state === 'done'))
const blocked = computed(() => !!plan.value?.blocked)

const SECTION: Record<string, string> = { notes: 'outline', practice: 'practice', finder: 'finder', answer: '' }

async function ask() {
  if (!canAsk.value) return
  busy.value = true
  error.value = ''
  plan.value = null
  steps.value = {}
  try {
    plan.value = await api<Plan>('/concierge/plan', {
      method: 'POST',
      body: JSON.stringify({ message: text.value.trim(), course_id: props.courseId, chapter_id: props.chapterId }),
    })
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function run() {
  if (!plan.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    for (const [index, action] of plan.value.actions.entries()) {
      steps.value = { ...steps.value, [index]: { state: 'running', message: '正在提交…' } }
      if (!action.ready) {
        steps.value = { ...steps.value, [index]: { state: 'failed', message: action.blocked_reason } }
        continue
      }
      if (action.type === 'answer') {
        steps.value = { ...steps.value, [index]: { state: 'failed', message: '这类问题请用右侧助手，那边能带上你选中的段落' } }
        continue
      }
      try {
        await api(action.endpoint, { method: action.method, body: JSON.stringify(action.body) })
        const section = SECTION[action.type]
        steps.value = {
          ...steps.value,
          [index]: { state: 'done', message: section ? `已提交，去「${sectionLabel(section)}」看进度` : '已提交' },
        }
      } catch (e) {
        steps.value = {
          ...steps.value,
          [index]: { state: 'failed', message: e instanceof Error ? e.message : String(e) },
        }
      }
    }
  } finally {
    busy.value = false
  }
}

function sectionLabel(section: string) {
  return { outline: '复习笔记', practice: '在线练习', finder: 'PPT 找课' }[section] || section
}

function reset() {
  plan.value = null
  steps.value = {}
  error.value = ''
}

watch(open, async (value) => {
  if (value) {
    await nextTick()
    input.value?.focus()
  }
})
</script>

<template>
  <div class="dock">
    <button v-if="!open" class="dock-bubble" aria-label="对课脉说一句话" @click="open = true">
      <svg viewBox="0 0 24 24" width="22" height="22" fill="none" aria-hidden="true">
        <path d="M4 12h4l2.5-6 3 12 2.5-6h4" stroke="currentColor" stroke-width="2"
              stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      <span class="dock-bubble-text">说一句</span>
    </button>

    <section v-else class="dock-panel" aria-label="对课脉说一句话">
      <header class="dock-head">
        <div>
          <strong>要做什么？</strong>
          <p class="dock-sub">
            {{ courseName ? `当前课程：${courseName}` : '先选一门课程' }}
          </p>
        </div>
        <button class="icon-button small" aria-label="收起" @click="open = false">×</button>
      </header>

      <div class="dock-input">
        <input
          ref="input"
          v-model="text"
          :disabled="busy"
          placeholder="把第三章做成笔记，再出 10 道题"
          @keydown.enter.prevent="ask"
        />
        <button class="button primary small" :disabled="!canAsk" @click="ask">{{ busy ? '…' : '发送' }}</button>
      </div>

      <p v-if="!courseId" class="dock-hint">左边先选一门课程，我才知道在哪门课上做事。</p>
      <p v-if="error" class="dock-error">{{ error }}</p>

      <div v-if="plan" class="dock-plan">
        <p class="dock-plan-hint">{{ plan.hint }}</p>

        <div v-for="(action, index) in plan.actions" :key="index" class="dock-action">
          <div class="dock-action-main">
            <span class="dock-action-label">{{ action.label }}</span>
            <span v-if="!action.ready" class="dock-badge warn">{{ action.blocked_reason }}</span>
          </div>
          <p class="dock-action-detail">{{ action.detail }}</p>
          <p v-if="steps[index]" :class="['dock-step', steps[index].state]">{{ steps[index].message }}</p>
          <button
            v-if="steps[index]?.state === 'done' && SECTION[action.type]"
            class="text-button"
            @click="emit('navigate', SECTION[action.type])"
          >
            打开「{{ sectionLabel(SECTION[action.type]) }}」
          </button>
        </div>

        <div class="dock-actions">
          <button class="button primary small" :disabled="busy || blocked" @click="run">
            {{ finished ? '全部完成' : busy ? '执行中…' : '按这个方案做' }}
          </button>
          <button class="text-button" :disabled="busy" @click="reset">换个说法</button>
          <button v-if="blocked" class="text-button" @click="emit('openModels')">去配置模型</button>
        </div>
      </div>

      <footer class="dock-foot">
        我只做计划，动手要你点头；生成的东西会落到对应页面，不会覆盖你没有确认的内容。
      </footer>
    </section>
  </div>
</template>

<style scoped>
.dock { position: fixed; right: 22px; bottom: 22px; z-index: 55; }
.dock-bubble {
  display: flex; align-items: center; gap: 8px; padding: 11px 16px; border: none; border-radius: 24px;
  background: var(--accent); color: #fff; box-shadow: 0 8px 24px rgba(52, 85, 219, .3); cursor: pointer;
  font-size: 13px; font-weight: 600;
}
.dock-bubble:hover { background: var(--accent-dark); }
.dock-panel {
  width: 400px; max-height: 76vh; display: flex; flex-direction: column; gap: 10px;
  background: var(--surface); border: 1px solid var(--line); border-radius: 14px;
  padding: 14px 16px; box-shadow: 0 18px 44px rgba(24, 34, 54, .18);
}
.dock-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
.dock-head strong { font-size: 14px; }
.dock-sub { margin: 2px 0 0; font-size: 11.5px; color: var(--muted); }
.dock-input { display: flex; gap: 8px; }
.dock-input input { flex: 1; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; font-size: 13px; }
.dock-input input:focus { outline: 2px solid var(--accent-line); outline-offset: -1px; }
.dock-hint, .dock-plan-hint { margin: 0; font-size: 11.5px; color: var(--muted); }
.dock-error { margin: 0; font-size: 12px; color: var(--danger); }
.dock-plan { display: flex; flex-direction: column; gap: 8px; overflow-y: auto; }
.dock-action { border: 1px solid var(--line); border-radius: 9px; padding: 9px 11px; background: var(--surface-alt); }
.dock-action-main { display: flex; align-items: center; gap: 8px; }
.dock-action-label { font-size: 13px; font-weight: 600; }
.dock-action-detail { margin: 3px 0 0; font-size: 11.5px; color: var(--muted); line-height: 1.55; }
.dock-badge { font-size: 10.5px; padding: 1px 5px; border-radius: 4px; background: var(--soft); color: var(--accent-dark); }
.dock-badge.warn { background: var(--warning-soft); color: var(--warning); }
.dock-step { margin: 6px 0 0; font-size: 11.5px; }
.dock-step.running { color: var(--accent); }
.dock-step.done { color: var(--success); }
.dock-step.failed { color: var(--danger); }
.dock-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.dock-foot { font-size: 10.5px; color: var(--muted); line-height: 1.6; border-top: 1px solid var(--line); padding-top: 8px; }
</style>
