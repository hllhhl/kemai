<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'

type Chapter = { id: string; title: string; page: number }
type Identity = { course: string; pages: number; chapters: Chapter[]; glossary: string[]; signature: string; keywords: string[] }
type ScanResult = { identity: Identity; deck: { document_id: string; filename: string; pages: number; signature: string }; preview: Array<{ page: number; label: string; lines: number; chapters: Chapter[] }> }
type Mapping = { part: number; cid: number; title: string; duration: number; chapter_id: string | null; pages: number[]; labels: string[]; how: string; confidence: number; evidence: string[] }
type Candidate = { bvid: string; title: string; owner: string; videos: number; score: number; chapter_cover: number; order_ratio: number; reasons: string[]; need_confirm: boolean; mapping: Mapping[]; rough_score?: number }
type CompareResult = { left: { filename: string; pages: number }; right: { filename: string; pages: number }; similarity: number; page_cover: number; order_ratio: number; verdict: string; matched: Array<{ page_a: number; page_b: number; similarity: number }>; note: string }

const props = defineProps<{ courseId: string; documents: Array<{ id: string; filename: string; status: string }> }>()
const emit = defineEmits<{ refresh: [] }>()

const importing = ref<number | null>(null)
const imported = ref<Record<number, string>>({})
const digesting = ref<number | null>(null)
const digests = ref<Record<number, { degraded: boolean; keypoints: Array<{ text: string; at: number }>; gotchas: Array<{ text: string; at: number }>; dropped: number }>>({})

const scan = ref<ScanResult | null>(null)
const courseName = ref('')
const busy = ref(false)
const error = ref('')
const candidates = ref<Candidate[]>([])
const selected = ref<Candidate | null>(null)
const adopted = ref<{ bvid: string; title: string; parts: Mapping[] } | null>(null)
const compareDoc = ref('')
const comparison = ref<CompareResult | null>(null)

const readyDocuments = computed(() => props.documents.filter(doc => doc.status === 'ready'))
const mappedPages = computed(() => {
  const pages = new Set<number>()
  for (const item of selected.value?.mapping || []) for (const page of item.pages) pages.add(page)
  return pages.size
})
const averageConfidence = computed(() => {
  const items = selected.value?.mapping || []
  if (!items.length) return 0
  return Math.round((items.reduce((sum, item) => sum + item.confidence, 0) / items.length) * 100)
})

function fail(e: unknown) { error.value = e instanceof Error ? e.message : String(e) }

async function runScan() {
  busy.value = true; error.value = ''
  try {
    scan.value = await api<ScanResult>(`/courses/${encodeURIComponent(props.courseId)}/finder/scan`)
    courseName.value = scan.value.identity.course
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function search() {
  busy.value = true; error.value = ''
  try {
    const result = await api<{ identity: Identity; candidates: Candidate[] }>(
      `/courses/${encodeURIComponent(props.courseId)}/finder/search`,
      { method: 'POST', body: JSON.stringify({ course_name: courseName.value.trim(), limit: 8 }) },
    )
    candidates.value = result.candidates
    selected.value = result.candidates[0] ?? null
    if (!result.candidates.length) error.value = '没找到候选课程。可以换一个课程名，或直接贴 BV 号。'
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function inspectByBvid(bvid: string) {
  if (!bvid.trim()) return
  busy.value = true; error.value = ''
  try {
    const result = await api<{ video: { bvid: string; title: string; owner: string; videos: number }; score: number; reasons: string[]; need_confirm: boolean; mapping: Mapping[] }>(
      `/courses/${encodeURIComponent(props.courseId)}/finder/inspect`,
      { method: 'POST', body: JSON.stringify({ bvid: bvid.trim() }) },
    )
    const candidate: Candidate = {
      bvid: result.video.bvid, title: result.video.title, owner: result.video.owner,
      videos: result.video.videos, score: result.score, chapter_cover: 0, order_ratio: 0,
      reasons: result.reasons, need_confirm: result.need_confirm, mapping: result.mapping,
    }
    candidates.value = [candidate]
    selected.value = candidate
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function compare() {
  if (!compareDoc.value) return
  busy.value = true; error.value = ''
  try {
    comparison.value = await api<CompareResult>(
      `/courses/${encodeURIComponent(props.courseId)}/finder/compare`,
      { method: 'POST', body: JSON.stringify({ other_document_id: compareDoc.value }) },
    )
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function adopt() {
  if (!selected.value) return
  busy.value = true; error.value = ''
  try {
    adopted.value = await api<{ bvid: string; title: string; parts: Mapping[] }>(
      `/courses/${encodeURIComponent(props.courseId)}/finder/adopt`,
      {
        method: 'POST',
        body: JSON.stringify({
          bvid: selected.value.bvid, title: selected.value.title, owner: selected.value.owner,
          score: selected.value.score, need_confirm: selected.value.need_confirm,
          mapping: selected.value.mapping,
        }),
      },
    )
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function importSubtitle(row: Mapping) {
  if (importing.value !== null || !selected.value) return
  importing.value = row.part
  error.value = ''
  try {
    const result = await api<{ document_id: string; blocks: number; chunks: number; chapter_id: string | null; reused: boolean }>(
      `/courses/${encodeURIComponent(props.courseId)}/finder/import-transcript`,
      {
        method: 'POST',
        body: JSON.stringify({
          bvid: selected.value.bvid, cid: row.cid, part: row.part,
          part_title: row.title, chapter_id: row.chapter_id || null,
        }),
      },
    )
    imported.value = {
      ...imported.value,
      [row.part]: result.reused ? '已在资料库' : `已入库 ${result.blocks} 块`,
    }
    emit('refresh')
  } catch (e) {
    imported.value = { ...imported.value, [row.part]: '失败：' + (e instanceof Error ? e.message : String(e)) }
  } finally {
    importing.value = null
  }
}

async function digestPart(row: Mapping) {
  if (digesting.value !== null || !selected.value) return
  digesting.value = row.part
  error.value = ''
  try {
    const result = await api<{ degraded: boolean; keypoints: Array<{ text: string; at: number }>; gotchas: Array<{ text: string; at: number }>; dropped: number }>(
      `/courses/${encodeURIComponent(props.courseId)}/finder/digest-part`,
      { method: 'POST', body: JSON.stringify({ bvid: selected.value.bvid, part: row.part, part_title: row.title }) },
    )
    digests.value = { ...digests.value, [row.part]: result }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    digesting.value = null
  }
}

function stamp(seconds: number) {
  const total = Math.max(0, Math.round(seconds))
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
}

async function loadAdopted() {
  try { adopted.value = await api<{ bvid: string; title: string; parts: Mapping[] } | null>(`/courses/${encodeURIComponent(props.courseId)}/finder/adopted`) }
  catch { adopted.value = null }
}

onMounted(() => { loadAdopted().then(runScan).catch(() => undefined) })
watch(() => props.courseId, () => { scan.value = null; candidates.value = []; selected.value = null; comparison.value = null; loadAdopted().then(runScan).catch(() => undefined) })
</script>

<template>
  <section class="panel">
    <div class="section-top">
      <div>
        <h2>PPT 反查整门课</h2>
        <p class="muted">给一份课件，找出 B 站上讲同一套课件的课程，并把每一讲对回课件页码。比对的是内容与章节顺序，不是文件哈希；置信度不够会要求你确认。</p>
      </div>
      <span class="badge neutral">本地运行</span>
    </div>

    <div v-if="error" class="notice error" role="alert"><span>{{ error }}</span></div>

    <div v-if="scan" class="finder-grid">
      <div class="finder-block">
        <h3>课件身份</h3>
        <label class="finder-label">课程名（单份课件常常只有章节名，补上更准）
          <input v-model="courseName" maxlength="60" placeholder="例如：数据结构" />
        </label>
        <div class="finder-stats">
          <div><span>页数</span><strong>{{ scan.identity.pages }}</strong></div>
          <div><span>章节</span><strong>{{ scan.identity.chapters.length }}</strong></div>
          <div><span>指纹</span><strong class="mono">{{ scan.identity.signature }}</strong></div>
        </div>
        <p class="muted small">课件：{{ scan.deck.filename }} · 检索词：{{ scan.identity.keywords.join(' / ') || '—' }}</p>
        <div class="finder-actions">
          <button class="button primary" :disabled="busy" @click="search">{{ busy ? '检索中…' : '搜索候选课程' }}</button>
          <button class="button subtle" :disabled="busy" @click="runScan">重新扫描</button>
        </div>
        <div class="finder-inline">
          <input aria-label="已知 BV 号" placeholder="已知 BV 号，直接查" @keyup.enter="inspectByBvid(($event.target as HTMLInputElement).value)" />
        </div>
        <div class="finder-inline">
          <select v-model="compareDoc" aria-label="选择另一份课件">
            <option value="">比对本机另一份课件…</option>
            <option v-for="doc in readyDocuments" :key="doc.id" :value="doc.id">{{ doc.filename }}</option>
          </select>
          <button class="button subtle small" :disabled="busy || !compareDoc" @click="compare">比对</button>
        </div>
        <div v-if="comparison" class="notice info">
          <div>
            <strong>{{ comparison.verdict }}</strong> · 页覆盖 {{ Math.round(comparison.page_cover * 100) }}% · 顺序一致 {{ Math.round(comparison.order_ratio * 100) }}%
            <p class="small">{{ comparison.left.filename }}（{{ comparison.left.pages }} 页）↔ {{ comparison.right.filename }}（{{ comparison.right.pages }} 页）</p>
            <p class="muted small">{{ comparison.note }}</p>
          </div>
        </div>
      </div>

      <div class="finder-block">
        <h3>候选课程</h3>
        <p v-if="!candidates.length" class="muted">点「搜索候选课程」。会先用分 P 标题和课件章节序列做零成本对齐。</p>
        <button v-for="item in candidates" :key="item.bvid" type="button"
                :class="['finder-candidate', { active: selected?.bvid === item.bvid }]"
                @click="selected = item">
          <div class="finder-candidate-head">
            <strong>{{ item.title }}</strong>
            <span class="mono">{{ Math.round(item.score * 100) }}</span>
          </div>
          <div class="finder-bar"><span :style="{ width: `${Math.max(4, item.score * 100)}%` }"></span></div>
          <p class="small">
            {{ item.owner }} · {{ item.videos }} 讲 · {{ item.bvid }}
            <span v-if="item.need_confirm" class="badge warning">需人工确认</span>
          </p>
          <p class="muted small">{{ item.reasons.slice(0, 2).join(' / ') }}</p>
        </button>
      </div>
    </div>

    <div v-if="selected" class="finder-block">
      <div class="section-top">
        <div>
          <h3>逐讲大纲 · {{ selected.title }}</h3>
          <p class="muted small">已映射 {{ selected.mapping.length }} 讲，覆盖 {{ mappedPages }} 页课件，平均置信度 {{ averageConfidence }}%。</p>
        </div>
        <button class="button primary small" :disabled="busy || !!adopted" @click="adopt">{{ adopted ? '已采纳' : '采纳并保存大纲' }}</button>
      </div>
      <table class="finder-table">
        <thead><tr><th>讲</th><th>标题</th><th>课件页</th><th>依据</th><th>置信</th><th>字幕</th></tr></thead>
        <tbody>
          <tr v-for="row in selected.mapping" :key="row.part">
            <td class="mono">P{{ row.part }}</td>
            <td>{{ row.title }}</td>
            <td class="mono">{{ row.labels.join('、') || '—' }}</td>
            <td class="muted small">{{ row.how }}</td>
            <td><span :class="['badge', row.confidence >= 0.7 ? 'neutral' : 'warning']">{{ Math.round(row.confidence * 100) }}%</span></td>
            <td class="finder-cell">
              <button class="button subtle small" :disabled="importing !== null" @click="importSubtitle(row)">
                {{ importing === row.part ? '导入中…' : (imported[row.part] || '导入字幕') }}
              </button>
              <button v-if="imported[row.part] && !imported[row.part].startsWith('失败')"
                      class="button subtle small" :disabled="digesting !== null" @click="digestPart(row)">
                {{ digesting === row.part ? '生成中…' : '要点' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="Object.keys(digests).length" class="finder-block">
      <h3>逐讲要点</h3>
      <p class="muted small">每条要点都带视频时间戳，且已和字幕的真实时间逐一核对；对不上的已被丢掉。</p>
      <div v-for="(digest, part) in digests" :key="part" class="finder-digest">
        <div class="finder-digest-head">
          <strong>第 {{ part }} 讲</strong>
          <span v-if="digest.degraded" class="badge warning">降级摘要（未配模型）</span>
          <span v-if="digest.dropped" class="muted small">丢掉 {{ digest.dropped }} 条编造时间戳</span>
        </div>
        <ul class="finder-points">
          <li v-for="(point, index) in digest.keypoints" :key="'k' + index">
            <span class="mono finder-stamp">{{ stamp(point.at) }}</span>
            {{ point.text }}
          </li>
        </ul>
        <template v-if="digest.gotchas && digest.gotchas.length">
          <p class="muted small" style="margin:8px 0 2px">易错点</p>
          <ul class="finder-points">
            <li v-for="(item, index) in digest.gotchas" :key="'g' + index">
              <span class="mono finder-stamp">{{ stamp(item.at) }}</span>
              {{ item.text }}
            </li>
          </ul>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.finder-grid { display: grid; grid-template-columns: 1fr; gap: 14px; margin-top: 16px; }
.finder-block { border: 1px solid var(--line, #e3ddd1); border-radius: 10px; padding: 14px 16px; background: var(--panel, #fffdf8); margin-bottom: 16px; }
.finder-block h3 { margin: 0 0 10px; font-size: 15px; }
.finder-label { display: block; font-size: 12px; color: var(--muted, #857c70); margin-bottom: 10px; }
.finder-label input { width: 100%; margin-top: 4px; padding: 7px 9px; border: 1px solid var(--line, #e3ddd1); border-radius: 6px; background: #fff; }
.finder-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 8px; }
.finder-stats div { background: #faf7f0; border-radius: 6px; padding: 7px 9px; }
.finder-stats span { display: block; font-size: 11px; color: var(--muted, #857c70); }
.finder-stats strong { font-size: 15px; }
.finder-stats .mono { font-size: 12px; }
.finder-actions { display: flex; gap: 8px; margin: 10px 0; }
.finder-inline { display: flex; gap: 8px; margin-top: 8px; }
.finder-inline input, .finder-inline select { flex: 1; padding: 6px 9px; border: 1px solid var(--line, #e3ddd1); border-radius: 6px; background: #fff; }
.finder-candidate { display: block; width: 100%; text-align: left; border: 1px solid var(--line, #e3ddd1); border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; background: #fff; cursor: pointer; }
.finder-candidate.active { border-color: #a4432a; box-shadow: 0 0 0 2px rgba(164, 67, 42, .1); }
.finder-candidate-head { display: flex; justify-content: space-between; gap: 10px; }
.finder-candidate-head strong { font-size: 13px; line-height: 1.5; }
.finder-bar { height: 4px; background: #efe9dc; border-radius: 3px; margin: 6px 0; overflow: hidden; }
.finder-bar span { display: block; height: 100%; background: #3d7a52; border-radius: 3px; }
.finder-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.finder-table th, .finder-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #efe9dc; }
.finder-table th { font-size: 11px; color: var(--muted, #857c70); font-weight: 500; }
.mono { font-family: ui-monospace, Consolas, monospace; }
.finder-cell { display: flex; gap: 4px; flex-wrap: wrap; }
.finder-digest { border-top: 1px dashed #efe9dc; padding-top: 10px; margin-top: 10px; }
.finder-digest-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px; }
.finder-points { margin: 0; padding-left: 18px; font-size: 12.5px; line-height: 1.8; }
.finder-stamp { color: #a4432a; font-size: 11px; margin-right: 4px; }
.small { font-size: 11.5px; }
.muted { color: var(--muted, #857c70); }

</style>
