import { ref } from 'vue'
import { fetchJson } from '@/api/client'

export function useCrossModule(moduleKey: string) {
  const showCrossPanel = ref(false)
  const selectedRow = ref<any>(null)
  const dependentStatus = ref<{ loaded: boolean; upstream: any[]; downstream: any[] }>({
    loaded: false, upstream: [], downstream: [],
  })
  const depGraph = ref<{ nodes: any[]; edges: any[] }>({ nodes: [], edges: [] })

  function statusClass(s: string): string {
    const done = ['已完成','已通过','已签署','已备案','已核实','已入库','已签认','已汇总','已交接']
    const alert = ['已驳回','异常报警','已过期','需整改','待复检','待补测','已延期','待补充']
    if (done.includes(s)) return 'status-done'
    if (alert.includes(s)) return 'status-alert'
    return 'status-pending'
  }

  function graphNodeClass(mod: string) {
    return mod === moduleKey ? 'graph-node-current' : 'graph-node-other'
  }

  async function selectRow(row: any) {
    selectedRow.value = row
    dependentStatus.value.loaded = false
    try {
      const data = await fetchJson<any>(`/api/cross-module/dependent-status/${moduleKey}/${row.id}`)
      dependentStatus.value = { loaded: true, upstream: data.upstream || [], downstream: data.downstream || [] }
    } catch {
      dependentStatus.value = { loaded: true, upstream: [], downstream: [] }
    }
  }

  async function loadDepGraph() {
    try {
      depGraph.value = await fetchJson<any>('/api/cross-module/dependency-graph')
    } catch {
      depGraph.value = { nodes: [], edges: [] }
    }
  }

  return { showCrossPanel, selectedRow, dependentStatus, depGraph, statusClass, graphNodeClass, selectRow, loadDepGraph }
}