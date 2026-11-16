import { requestJson } from './startup.js'
import { PollTimer } from './polling.js'

export const validLearningMode = value => value?.version === 1 && typeof value.available === 'boolean' && typeof value.parked === 'boolean' &&
  (!value.available || ['parked', 'live'].includes(value.mode) && typeof value.enabled === 'boolean' &&
    typeof value.vehicle === 'string' && typeof value.revision === 'string' && /^[0-9a-f]{64}$/.test(value.revision))

export const FlmLearningMode = {
  name: 'FlmLearningMode',
  props: { mode: { type: String, required: true }, unauthorized: { type: Function, required: true } },
  data: () => ({ state: null, choice: 'live', dirty: false, request: null, error: '', saved: false, alive: false }),
  mounted() {
    this.alive = true
    this.poller = new PollTimer({ read: () => this.load(), interval: 5000 })
    if (this.mode === 'local') { this.poller.start(); this.load() }
  },
  beforeUnmount() { this.alive = false; this.poller?.stop(); this.request?.abort() },
  methods: {
    async load(save = false) {
      if (!this.alive || this.mode !== 'local' || this.request || document.hidden) return
      const request = new AbortController()
      this.request = request
      try {
        const value = await requestJson('./api/flm/mode', { request: { signal: request.signal,
          ...(save ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: this.choice, revision: this.state.revision }) } : {}),
        } })
        if (!this.alive || this.request !== request || request.signal.aborted) return
        if (!validLearningMode(value)) throw new Error('The learning-mode response is unavailable.')
        this.state = value; this.error = ''
        if (save) { this.dirty = false; this.saved = true }
        if (!this.dirty && value.available) this.choice = value.mode
      } catch (error) {
        if (!this.alive || this.request !== request || request.signal.aborted) return
        if (error.status === 401 || ['access_unavailable', 'setup_required'].includes(error.code)) {
          this.state = null; this.alive = false; this.poller?.stop(); this.unauthorized()
        } else this.error = error.message || 'Learning mode could not be updated.'
      } finally { if (this.request === request) this.request = null }
    },
    save() { if (this.state?.available && this.state.parked) return this.load(true) },
  },
  template: `<section class="gx-card gx-tuner__section gx-route-reviews">
    <h3>When Auto Learn runs</h3>
    <p>Live mode is the default: it qualifies small updates during a drive, then finishes fitting while parked. Offroad only reviews completed drives for the next drive.</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="!state?.available">Identify the vehicle to choose its learning mode.</p>
    <template v-else>
      <form @submit.prevent="save" class="gx-route-reviews__form">
        <label>Learning mode<select v-model="choice" @change="dirty = true; saved = false" :disabled="!state.parked || !!request"><option value="live">Live + parked finishing · default</option><option value="parked">Offroad only · next-drive tune</option></select></label>
        <button type="submit" class="gx-btn gx-btn--tonal" :disabled="!state.parked || !!request || !dirty">Save for next drive</button>
      </form>
      <p v-if="saved" role="status">Saved. This mode starts on the next drive.</p>
      <p v-if="!state.enabled" class="gx-tuner__caption">Auto Learn is off for this vehicle. Choosing a mode does not turn it on.</p>
      <p class="gx-tuner__caption">{{ state.identitySource === 'original_vin' ? 'Vehicle binding uses the original discovered VIN.' : 'Original VIN discovery returned unknown; the device-and-firmware fallback is used when available.' }}</p>
    </template>
    <p class="gx-tuner__caption">Parked results guide a new qualified live trial on the next drive. They do not confirm an interrupted experiment. Driver input excludes the affected interval and settling buffer; clean sections remain usable. Live updates require fresh controller/native evidence and still need validation on your car.</p>
  </section>`,
}
