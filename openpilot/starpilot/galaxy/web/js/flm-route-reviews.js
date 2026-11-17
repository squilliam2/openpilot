import { requestJson } from './startup.js'
import { PollTimer } from './polling.js'

const text = v => typeof v === 'string' && v.length <= 512
const count = v => Number.isSafeInteger(v) && v >= 0
const finite = v => typeof v === 'number' && Number.isFinite(v) && v >= 0
const object = v => v !== null && typeof v === 'object' && !Array.isArray(v)
const digest = v => typeof v === 'string' && /^[0-9a-f]{64}$/.test(v)
const vote = v => object(v) && count(v.support) && count(v.total) && v.support <= v.total &&
  finite(v.lowerBound) && v.lowerBound <= 1
const pooled = v => object(v) && count(v.sections) && count(v.windows) && v.sections <= v.windows
const direction = v => object(v) && text(v.parameter) && digest(v.configurationHash) && text(v.cohort) &&
  (v.pooledComparison === true ? pooled(v.training) && pooled(v.heldout) : vote(v.training) && vote(v.heldout))
const applicationStates = ['waiting_for_vehicle', 'queued', 'native_trial_offered', 'confirmed', 'rolled_back', 'rejected', 'stale_scope']
const tuning = v => object(v) && v.version === 1 && digest(v.sourceSha256) &&
  v.automaticApplicationAllowed === false && v.requiresFreshNativeQualification === true &&
  (v.intentId === undefined || digest(v.intentId)) &&
  (v.selected === null || direction(v.selected) && typeof v.selected.delta === 'number' &&
    Number.isFinite(v.selected.delta) && Math.abs(v.selected.delta) > 0 && Math.abs(v.selected.delta) <= .005 &&
    ['standard', 'faster'].includes(v.selected.convergence) && typeof v.selected.fasterEvidenceSupported === 'boolean' &&
    v.selected.nativeQualified === false && v.selected.requiresFreshNativeQualification === true)
const application = v => object(v) && digest(v.intentId) && text(v.routeId) && applicationStates.includes(v.state) &&
  text(v.reason) && text(v.parameter) && typeof v.delta === 'number' && Number.isFinite(v.delta) &&
  Math.abs(v.delta) > 0 && Math.abs(v.delta) <= .005 &&
  v.requiresFreshNativeQualification === true && v.acceptedMapWrittenByImport === false
export function validRouteReviews(v) {
  return v?.version === 1 && typeof v.parked === 'boolean' && v.automaticApplicationAllowed === false &&
    Array.isArray(v.localRoutes) && v.localRoutes.length <= 100 && v.localRoutes.every(r => object(r) && text(r.routeId) && count(r.segments)) &&
    Array.isArray(v.reviews) && v.reviews.length <= 100 && v.reviews.every(r =>
      object(r) && ['local', 'connect'].includes(r.source) && text(r.routeId) && text(r.reason) && finite(r.updatedAt) &&
      ['queued', 'analyzing', 'ready', 'failed'].includes(r.status) && (r.summary == null || object(r.summary) &&
        (r.summary.version === undefined || [1, 2].includes(r.summary.version)) &&
        typeof r.summary.analysisOnly === 'boolean' && r.summary.automaticApplicationAllowed === false && r.summary.tuneChanged === false &&
        count(r.summary.availableSegments) && Array.isArray(r.summary.reviewedSegments) && r.summary.reviewedSegments.length <= 32 &&
        (r.summary.version !== 2 || count(r.summary.attemptedSegments) && count(r.summary.reviewedCount) && count(r.summary.omittedCount) &&
          r.summary.reviewedCount + r.summary.omittedCount === r.summary.attemptedSegments &&
          r.summary.attemptedSegments <= r.summary.availableSegments && r.summary.reviewedCount >= r.summary.reviewedSegments.length &&
          typeof r.summary.allSegmentsAttempted === 'boolean' &&
          r.summary.allSegmentsAttempted === (r.summary.attemptedSegments === r.summary.availableSegments && r.summary.availableSegments > 0)) &&
        r.summary.reviewedSegments.every(count) && Array.isArray(r.summary.vehicles) && r.summary.vehicles.length <= 32 && r.summary.vehicles.every(text) &&
        (r.summary.counts === undefined || object(r.summary.counts) && Object.values(r.summary.counts).every(count)) &&
        (r.summary.tuning === undefined || tuning(r.summary.tuning)) &&
        (r.summary.application === undefined || application(r.summary.application) && r.summary.tuning?.intentId === r.summary.application.intentId) &&
        Array.isArray(r.summary.metrics) && r.summary.metrics.length <= 24 && r.summary.metrics.every(m =>
          object(m) && ['lateral_acceleration', 'steering_angle'].includes(m.domain) && text(m.speedRange) && count(m.windows) && count(m.samples) &&
          [m.observedSeconds, m.rmse, m.meanAbsoluteError, m.worstAbsoluteError, m.errorReversalHz].every(finite))))
}
export const metricNumber = v => finite(v) ? v.toFixed(3) : 'Unavailable'
export const metricUnits = domain => domain === 'lateral_acceleration' ? 'm/s²' : 'degrees'

export const FlmRouteReviews = {
  name: 'FlmRouteReviews',
  props: { mode: { type: String, required: true }, unauthorized: { type: Function, required: true } },
  data: () => ({ state: null, local: '', connect: '', purpose: 'tune', error: '', request: null, busy: false, alive: false, showAll: false }),
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
    reviewLocal() { if (this.local) return this.load({ source: 'local', routeId: this.local, purpose: this.purpose }) },
    reviewConnect() { if (this.connect.trim()) return this.load({ source: 'connect', routeId: this.connect.trim(), purpose: this.purpose }) },
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
    <p class="gx-tuner__caption">Every available segment is attempted in resumable batches. Consistent route evidence can select an automatic next-drive trial; the normal controller checks and measured keep-or-rollback decision still apply.</p>
    <label>Review purpose<select v-model="purpose" :disabled="busy"><option value="tune">Analyze and tune through qualified trials</option><option value="review">Analysis only</option></select></label>
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
    <p class="gx-tuner__caption">Connect fetches only the requested route from this comma's account. There is no route-wide segment-count limit. One original file is downloaded and decoded at a time, then removed; checkpoints preserve progress when driving or power loss pauses the review.</p>
    <article v-for="row in visible" :key="row.source + row.routeId" class="gx-route-reviews__result">
      <h4>{{ row.source === 'connect' ? 'Connect' : 'Local' }} · {{ row.routeId }}</h4><p role="status">{{ row.reason }}</p>
      <template v-if="row.summary">
        <p>{{ row.summary.vehicles.join(', ') }} · {{ row.summary.reviewedCount ?? row.summary.reviewedSegments.length }} of {{ row.summary.availableSegments }} available segments reviewed</p>
        <p v-if="row.summary.version === 2" class="gx-tuner__caption">{{ row.summary.attemptedSegments }} attempted · {{ row.summary.omittedCount }} unusable · {{ row.summary.allSegmentsAttempted ? 'Every available segment attempted.' : 'Progress saved; processing resumes while parked.' }}</p>
        <p class="gx-tuner__caption">{{ row.summary.wholeRouteReviewed ? 'All available segments reviewed.' : 'Only intact original sections contribute; missing sections remain excluded.' }} {{ row.summary.counts?.routeScopeFrames ? 'Recorded route metadata is present; automatic qualification is separate.' : 'Missing original configuration is never recreated. Legacy tracking can guide a new qualified trial.' }}</p>
        <template v-if="row.summary.tuning">
          <template v-if="row.summary.tuning.selected">
            <p>Route comparison supports {{ row.summary.tuning.selected.parameter }} · proposed coordinate change {{ row.summary.tuning.selected.delta }}.</p>
            <p v-if="row.summary.tuning.selected.pooledComparison">Pooled training: {{ row.summary.tuning.selected.training.windows }} windows across {{ row.summary.tuning.selected.training.sections }} sections; independent held-out: {{ row.summary.tuning.selected.heldout.windows }} windows across {{ row.summary.tuning.selected.heldout.sections }} sections.</p>
            <p v-else>Training: {{ row.summary.tuning.selected.training.support }}/{{ row.summary.tuning.selected.training.total }} supporting sections; held-out: {{ row.summary.tuning.selected.heldout.support }}/{{ row.summary.tuning.selected.heldout.total }}.</p>
            <p v-if="row.summary.tuning.selected.pooledComparison && row.summary.tuning.selected.training.fitWindows !== undefined" class="gx-tuner__caption">Numerical fit uses {{ row.summary.tuning.selected.training.fitWindows }} training and {{ row.summary.tuning.selected.heldout.fitWindows }} held-out windows selected across the full source order before fitting.</p>
          </template>
          <p v-else>No consistent route-wide candidate qualified. Existing automatic learning continues.</p>
          <p v-if="row.summary.tuning.pooledWindowCount !== undefined" class="gx-tuner__caption">{{ row.summary.tuning.pooledWindowCount }} original windows available for matching across sections. A completed analysis does not establish that the current tuning is optimal.</p>
          <p v-if="row.summary.application" role="status">Import state: {{ row.summary.application.state.replaceAll('_', ' ') }}. {{ row.summary.application.reason.replaceAll('_', ' ') }}</p>
          <p class="gx-tuner__caption">A queued trial is separate from an applied or confirmed tune. The current vehicle, settings and accepted profile must match; driver input still excludes only its affected windows.</p>
        </template>
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
