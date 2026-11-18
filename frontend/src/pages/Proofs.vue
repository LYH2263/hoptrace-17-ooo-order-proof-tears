<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const waybillId = ref('')
const batchId = ref('')
const err = ref('')

async function load() {
  err.value = ''
  try {
    rows.value = await api.orderProofs({
      waybillId: waybillId.value || undefined,
      batchId: batchId.value || undefined,
    })
  } catch (e) {
    err.value = String(e.message || e)
  }
}

onMounted(load)
</script>

<template>
  <!-- plant-ui -->

  <div class="page-card">
    <h1>序证明</h1>
    <p class="lead">
      装载成功时与点序、投影装载后落库：运单 + 成员列表 + 摘要码 + 投影值 + 本批写入键。
      点序任何漂移都会改变摘要码。支持按运单或批次查询。
    </p>

    <div class="filter-row">
      <label>运单 <input v-model="waybillId" placeholder="如 WB-1001" /></label>
      <label>批次写入键 <input v-model="batchId" placeholder="如 B-OOO-02" /></label>
      <button class="btn" @click="load">查询</button>
    </div>
    <p v-if="err" class="muted">{{ err }}</p>

    <table class="table">
      <thead>
        <tr><th>id</th><th>运单</th><th>批次</th><th>成员列表</th><th>摘要码</th><th>末点</th><th>投影</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.batch_id }}</td>
          <td class="id-chain">
            <span v-for="(hid, i) in r.ordered_hop_ids" :key="hid" class="chain-item">
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
