<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'

const waybills = ref([])
const selected = ref(null)
const detail = ref(null)
const overview = ref(null)
const err = ref('')

async function loadList() {
  overview.value = await api.overview()
  waybills.value = await api.waybills()
  if (!selected.value && waybills.value.length) {
    selected.value = waybills.value[0].id
  }
}

async function loadDetail(id) {
  if (!id) return
  detail.value = await api.timeline(id)
}

onMounted(async () => {
  try {
    await loadList()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch(selected, (id) => {
  loadDetail(id).catch((e) => { err.value = String(e.message || e) })
})

const hops = computed(() => detail.value?.hops || [])
const proof = computed(() => detail.value?.proof)
const orderProof = computed(() => detail.value?.order_proof)

// 竖轴按 event_ts 的单调性检查（同时间戳允许并列）
const monotonic = computed(() => {
  const ts = hops.value.map((h) => h.event_ts)
  return ts.every((v, i) => i === 0 || ts[i - 1] <= v)
})
const lateCount = computed(() => hops.value.filter((h) => h.arrival_ord).length)
const hashShort = computed(() => {
  const h = orderProof.value?.content_hash
  return h ? `${h.slice(0, 12)}…` : ''
})
</script>

<template>
  <!-- plant-ui -->

  <div>
    <p v-if="err" class="muted">{{ err }}</p>
    <div class="stat-row" v-if="overview">
      <div class="stat"><b>{{ overview.waybills }}</b><span>运单</span></div>
      <div class="stat"><b>{{ overview.hops_primary }}</b><span>正式 hop</span></div>
      <div class="stat"><b>{{ overview.hops_sidecar }}</b><span>旁路</span></div>
      <div class="stat"><b>{{ overview.quarantine }}</b><span>隔离</span></div>
    </div>

    <div class="tri">
      <aside class="pane waybill-pane">
        <h2>运单索引</h2>
        <button
          v-for="w in waybills"
          :key="w.id"
          class="wb-item"
          :class="{ active: selected === w.id }"
          @click="selected = w.id"
        >
          <span class="id">{{ w.id }}</span>
          <span class="title">{{ w.title }}</span>
          <span class="st">{{ w.status_proj }}</span>
        </button>
      </aside>

      <section class="pane axis-pane">
        <div class="axis-head">
          <h1>{{ detail?.waybill?.title || '时间线' }}</h1>
          <span>{{ selected }}</span>
        </div>

        <div class="order-banner" :class="{ ok: monotonic, bad: !monotonic }">
          <span class="dot-pulse" aria-hidden="true"></span>
          event_ts 序检查：{{ monotonic ? '单调不减 ✓（按 (event_ts, id) 物化）' : '存在逆序 ✗' }}
          <em v-if="lateCount"> · 迟到插入 {{ lateCount }} 点（已按序归位，未追加末尾）</em>
        </div>

        <div class="v-axis" v-if="hops.length">
          <div
            class="hop-node"
            v-for="h in hops"
            :key="h.id"
            :class="{ late: h.late_insert }"
          >
            <div class="ts">
              <span class="ord">#{{ h.ord_position }}</span>
              {{ h.event_ts }}
              <span class="arrival">到达序 {{ h.arrival_ord }}</span>
            </div>
            <div class="code">
              {{ h.event_code }}
              <span v-if="h.late_insert" class="late-tag">后到先插</span>
            </div>
            <div class="loc">{{ h.location_code }} · {{ h.id }}</div>
          </div>
        </div>
        <p v-else class="muted">无正式轨迹点</p>
      </section>

      <aside class="pane inspect-pane">
        <h2>检查器</h2>
        <dl class="kv" v-if="detail?.waybill">
          <dt>运单</dt><dd>{{ detail.waybill.id }}</dd>
          <dt>投影状态</dt><dd>{{ detail.waybill.status_proj }}</dd>
          <dt>创建</dt><dd>{{ detail.waybill.created_at }}</dd>
        </dl>

        <h2>状态证明</h2>
        <dl class="kv" v-if="proof">
          <dt>末 hop</dt><dd>{{ proof.last_hop_id }}</dd>
          <dt>映射码</dt><dd>{{ proof.mapped_code }}</dd>
          <dt>投影值</dt><dd>{{ proof.proj_value }}</dd>
          <dt>写入</dt><dd>{{ proof.created_at }}</dd>
        </dl>
        <p v-else class="muted">暂无证明</p>

        <h2>序证明</h2>
        <dl class="kv" v-if="orderProof">
          <dt>批次写入键</dt><dd>{{ orderProof.batch_id }}</dd>
          <dt>有序 hop</dt><dd>{{ orderProof.hop_count }} 点</dd>
          <dt>hop id 序</dt>
          <dd class="id-chain">
            <span v-for="(hid, i) in orderProof.ordered_hop_ids" :key="hid" class="chain-item">
              {{ hid }}<i v-if="i < orderProof.ordered_hop_ids.length - 1">→</i>
            </span>
          </dd>
          <dt>摘要码</dt><dd :title="orderProof.content_hash" class="hash">{{ hashShort }}</dd>
          <dt>末点/投影</dt><dd>{{ orderProof.last_hop_id }} · {{ orderProof.proj_value }}</dd>
          <dt>证明时间</dt><dd>{{ orderProof.created_at }}</dd>
        </dl>
        <p v-else class="muted">暂无序证明</p>
      </aside>

      <footer class="pane dot-rail">
        <span class="label">HOP</span>
        <template v-for="(h, i) in hops" :key="h.id">
          <span class="dot" :class="{ on: true, late: h.late_insert }"></span>
          <span v-if="i < hops.length - 1" class="dot-line"></span>
        </template>
        <template v-if="!hops.length">
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
        </template>
      </footer>
    </div>
  </div>
</template>
