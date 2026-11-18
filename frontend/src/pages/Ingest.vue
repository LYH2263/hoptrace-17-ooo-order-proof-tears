<script setup>
import { ref } from 'vue'
import { api } from '../api.js'

const result = ref(null)
const busy = ref('')
const err = ref('')

async function run(fixtureName, label) {
  busy.value = label
  err.value = ''
  try {
    result.value = await api.ingest(fixtureName)
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <div class="page-card">
    <h1>装载</h1>
    <p class="lead">
      从本地夹具跑轨迹点装载。正式点在同一事务内按 <code>(event_ts, id)</code> 物化进持久时间线点表，
      投影、状态证明与序证明同成同败；刷新后顺序不回退。
    </p>
    <div class="filter-row">
      <button class="btn" :disabled="!!busy" @click="run(null, 'base')">
        {{ busy === 'base' ? '装载中…' : '跑基础批次 (ingest_batch.json)' }}
      </button>
      <button class="btn amber" :disabled="!!busy" @click="run('ingest_batch_ooo.json', 'ooo')">
        {{ busy === 'ooo' ? '装载中…' : '跑乱序到达批次 (ingest_batch_ooo.json)' }}
      </button>
    </div>
    <p class="muted" style="margin-top:8px">
      乱序夹具：文件内到达顺序与 event_ts 相反，并含同时间戳按 id 决胜的点（H-9 与 H-3）。
    </p>
    <p v-if="err" class="muted" style="margin-top:12px">{{ err }}</p>
    <pre v-if="result" class="result-box">{{ JSON.stringify(result, null, 2) }}</pre>
  </div>
</template>
