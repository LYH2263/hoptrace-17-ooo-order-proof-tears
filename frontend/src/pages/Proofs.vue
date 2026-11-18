<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const atts = ref([])
const waybillId = ref('')
const batchId = ref('')
const err = ref('')

async function load() {
  err.value = ''
  try {
    const q = {
      waybillId: waybillId.value || undefined,
      batchId: batchId.value || undefined,
    }
    const [proofData, attData] = await Promise.all([
      api.orderProofs(q),
      api.attestations(q),
    ])
    rows.value = proofData
    atts.value = attData
  } catch (e) {
    err.value = String(e.message || e)
  }
}

onMounted(load)
</script>

<template>
  <!-- plant-ui -->

  <div class="page-card">
    <h1>序证明与撕裂核证</h1>
    <p class="lead">
      装载成功时竖轴点序、投影、序证明在同一事务内核证：竖轴序 = 证明序 = 投影派生源，
      缺一回滚。核证实体记录运单、有序 hop id、新旧内容哈希、投影、批次键与撕裂结论，
      与测绘板竖轴读同一张 <code>order_tear_attestations</code> 表。
    </p>

    <div class="filter-row">
      <label>运单 <input v-model="waybillId" placeholder="如 WB-1001" /></label>
      <label>批次写入键 <input v-model="batchId" placeholder="如 B-OOO-02" /></label>
      <button class="btn" @click="load">查询</button>
    </div>
    <p v-if="err" class="muted">{{ err }}</p>

    <h2 style="margin-top:18px">序撕裂核证</h2>
    <table class="table">
      <thead>
        <tr>
          <th>id</th><th>运单</th><th>批次</th><th>撕裂结论</th>
          <th>竖轴 hop 序</th><th>新序哈希</th><th>旧序哈希</th><th>末点</th><th>投影</th><th>时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in atts" :key="`att-${r.id}`">
          <td>{{ r.id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.batch_id }}</td>
          <td>
            <span class="verdict" :class="r.aligned ? 'ok' : 'bad'">
              {{ r.tear_verdict }}{{ r.aligned ? ' ✓' : ' ✗' }}
            </span>
          </td>
          <td class="id-chain">
            <span v-for="(hid, i) in r.axis_hop_ids" :key="`ax-${r.id}-${hid}`" class="chain-item">
              {{ hid }}<i v-if="i < r.axis_hop_ids.length - 1">→</i>
            </span>
          </td>
          <td class="hash" :title="r.axis_content_hash">{{ r.axis_content_hash.slice(0, 10) }}…</td>
          <td class="hash" :title="r.prior_content_hash || ''">
            {{ r.prior_content_hash ? `${r.prior_content_hash.slice(0, 10)}…` : '—' }}
          </td>
          <td>{{ r.last_hop_id }}</td>
          <td>{{ r.proj_value }}</td>
          <td>{{ r.created_at }}</td>
        </tr>
        <tr v-if="!atts.length"><td colspan="10" class="muted">暂无核证</td></tr>
      </tbody>
    </table>

    <h2 style="margin-top:18px">序证明</h2>
    <table class="table">
      <thead>
        <tr><th>id</th><th>运单</th><th>批次</th><th>成员列表</th><th>摘要码</th><th>末点</th><th>投影</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="`op-${r.id}`">
          <td>{{ r.id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.batch_id }}</td>
          <td class="id-chain">
            <span v-for="(hid, i) in r.ordered_hop_ids" :key="`opm-${r.id}-${hid}`" class="chain-item">
              {{ hid }}<i v-if="i < r.ordered_hop_ids.length - 1">→</i>
            </span>
          </td>
          <td class="hash" :title="r.content_hash">{{ r.content_hash.slice(0, 10) }}…</td>
          <td>{{ r.last_hop_id }}</td>
          <td>{{ r.proj_value }}</td>
          <td>{{ r.created_at }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="8" class="muted">暂无序证明</td></tr>
      </tbody>
    </table>
  </div>
</template>
