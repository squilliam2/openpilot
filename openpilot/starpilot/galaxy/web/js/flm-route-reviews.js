import { requestJson } from './startup.js'
import { PollTimer } from './polling.js'

const text = v => typeof v === 'string' && v.length <= 512
const count = v => Number.isSafeInteger(v) && v >= 0
const finite = v => typeof v === 'number' && Number.isFinite(v) && v >= 0
export function validRouteReviews(v) {
  return v?.version === 1 && typeof v.parked === 'boolean' && v.automaticApplicationAllowed === false &&
    Array.isArray(v.localRoutes) && v.localRoutes.length <= 100 && v.localRoutes.every(r => text(r.routeId) && count(r.segments)) &&
    Array.isArray(v.reviews) && v.reviews.length <= 100 && v.reviews.every(r =>
      ['local', 'connect'].includes(r.source) && text(r.routeId) && text(r.reason) && finite(r.updatedAt) &&
      ['queued', 'analyzing', 'ready', 'failed'].includes(r.status) && (r.summary == null ||
        r.summary.analysisOnly === true && r.summary.automaticApplicationAllowed === false && r.summary.tuneChanged === false &&
        count(r.summary.availableSegments) && Array.isArray(r.summary.reviewedSegments) && r.summary.reviewedSegments.length <= 14 &&
        r.summary.reviewedSegments.every(count) && Array.isArray(r.summary.vehicles) && r.summary.vehicles.every(text) &&
        Array.isArray(r.summary.metrics) && r.summary.metrics.length <= 24 && r.summary.metrics.every(m =>
          ['lateral_acceleration', 'steering_angle'].includes(m.domain) && text(m.speedRange) && count(m.windows) && count(m.samples) &&
          [m.observedSeconds, m.rmse, m.meanAbsoluteError, m.worstAbsoluteError, m.errorReversalHz].every(finite))))
}
export const metricNumber = v => finite(v) ? v.toFixed(3) : 'Unavailable'
export const metricUnits = domain => domain === 'lateral_acceleration' ? 'm/s²' : 'degrees'

export const FlmRouteReviews = {
  name: 'FlmRouteReviews',
  props: { mode: { type: String, required: true }, unauthorized: { type: Function, required: true } },
  data: () => ({ state: null, local: '', connect: '', error: '', request: null, busy: false, alive: false, showAll: false }),
  computed: { visible() { return this.state?.reviews.slice(0, this.showAll ? 100 : 6) || [] } },
  mounted() {
    this.alive = true
    this.poller = new PollTimer({ read: () => this.load(), interval: 5000 })
    this.visibility = () => { if (document.hidden) this.request?.abort(); else this.load() }
    document.addEventListener('visibilitychange', this.visibility)
    if (this.mode === 'local') { this.poller.start(); this.load() }
  },
  beforeUnmount() {
    this.alive = false
    this.poller?.stop()
    this.request?.abort()
    document.removeEventListener('visibilitychange', this.visibility)
  },
  methods: {
    metricNumber, metricUnits,
    async load(body = null) {
      if (!this.alive || this.mode !== 'local' || this.request || document.hidden) return
      const request = new AbortController()
      this.request = request
      this.busy = body !== null
      try {
        const v = await requestJson('./api/flm/route-reviews', { request: {
          signal: request.signal, ...(body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
        } })
        if (!this.alive || this.request !== request || request.signal.aborted) return
        if (!validRouteReviews(v)) throw new Error('The historical review response is unavailable.')
        this.state = v; this.error = ''
        if (!this.local && v.localRoutes.length) this.local = v.localRoutes[0].routeId
      } catch (error) {
        if (!this.alive || this.request !== request || request.signal.aborted) return
        if (error.status === 401 || ['access_unavailable', 'setup_required'].includes(error.code)) {
          this.state = null; this.alive = false; this.poller?.stop(); this.unauthorized()
        } else this.error = error.message || 'Historical reviews are unavailable.'
      } finally {
        if (this.request === request) { this.request = null; this.busy = false }
      }
    },
    reviewLocal() { if (this.local) return this.load({ source: 'local', routeId: this.local }) },
    reviewConnect() { if (this.connect.trim()) return this.load({ source: 'connect', routeId: this.connect.trim() }) },
    download(row) {
      if (!row.summary) return
      const url = URL.createObjectURL(new Blob([JSON.stringify(row.summary, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a')
      link.href = url; link.download = 'starpilot-historical-route-review.json'; link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    },
  },
  template: `<section class="gx-card gx-tuner__section gx-route-reviews">
    <h3>Existing routes</h3>
    <p>Retained local drives are reviewed automatically while parked. You can also request a specific Connect route.</p>
    <p class="gx-tuner__caption">Historical reports measure recorded steering response. They do not apply a tune or recreate missing tuning evidence.</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="state && !state.parked" role="status">Reviews and downloads will resume when parked.</p>
    <p v-if="state?.scanIncomplete" class="gx-tuner__caption">The bounded local inventory is incomplete; some older drives may not be listed.</p>
    <form @submit.prevent="reviewLocal" class="gx-route-reviews__form">
      <label>Recording on this comma<select v-model="local" :disabled="busy || !state?.localRoutes.length"><option value="">Choose a local drive</option><option v-for="route in state?.localRoutes || []" :key="route.routeId" :value="route.routeId">{{ route.routeId }} · {{ route.segments }} segments</option></select></label>
      <button class="gx-btn gx-btn--tonal" type="submit" :disabled="busy || !local">Review local drive</button>
    </form>
    <form @submit.prevent="reviewConnect" class="gx-route-reviews__form">
      <label>Connect route URL or route ID<input v-model="connect" maxlength="512" placeholder="https://connect.comma.ai/device/route" :disabled="busy" autocomplete="off" spellcheck="false" /></label>
      <button class="gx-btn gx-btn--tonal" type="submit" :disabled="busy || !connect.trim()">Review Connect route</button>
    </form>
    <p class="gx-tuner__caption">Connect fetches only the requested route from this comma's account. Long drives use up to 14 evenly spaced original segments, with a maximum 448 MiB download. Temporary logs are removed after review.</p>
    <article v-for="row in visible" :key="row.source + row.routeId" class="gx-route-reviews__result">
      <h4>{{ row.source === 'connect' ? 'Connect' : 'Local' }} · {{ row.routeId }}</h4><p role="status">{{ row.reason }}</p>
      <template v-if="row.summary">
        <p>{{ row.summary.vehicles.join(', ') }} · {{ row.summary.reviewedSegments.length }} of {{ row.summary.availableSegments }} available segments reviewed</p>
        <p class="gx-tuner__caption">{{ row.summary.wholeRouteReviewed ? 'All available segments reviewed.' : 'Selected intact segments only; this is not a whole-drive assessment.' }} {{ row.summary.counts?.routeScopeFrames ? 'Recorded route metadata is present; automatic qualification is separate.' : 'Original FLM route-scope metadata was not recorded.' }}</p>
        <div class="gx-route-reviews__metrics" v-for="metric in row.summary.metrics" :key="metric.configurationHash + metric.domain + metric.speedRange">
          <strong>{{ metric.domain === 'lateral_acceleration' ? 'Lateral acceleration' : 'Steering angle' }} · {{ metric.speedRange === 'below_15_mps' ? 'Below 34 mph / 54 km/h' : '34 mph / 54 km/h and above' }}</strong>
          <span>{{ metric.windows }} uninterrupted windows · {{ metricNumber(metric.observedSeconds) }} s observed</span>
          <span>RMS error: {{ metricNumber(metric.rmse) }} {{ metricUnits(metric.domain) }} · Mean absolute error: {{ metricNumber(metric.meanAbsoluteError) }} {{ metricUnits(metric.domain) }}</span>
          <span>Error reversals: {{ metricNumber(metric.errorReversalHz) }} Hz. This measures recorded error changes, not their cause.</span>
        </div>
        <p v-if="!row.summary.metrics.length">No uninterrupted active-steering windows qualified for tracking measurements.</p>
        <button type="button" class="gx-btn gx-btn--tonal" @click="download(row)">Download historical report</button>
      </template>
    </article>
    <button v-if="state?.reviews.length > 6" type="button" class="gx-btn gx-btn--text" @click="showAll = !showAll">{{ showAll ? 'Show fewer reviews' : 'Show all saved reviews' }}</button>
  </section>`,
}
