import assert from 'node:assert/strict'
import { FlmRouteReviews, validRouteReviews, metricNumber, metricUnits } from '../web/js/flm-route-reviews.js'
import { FlmPage } from '../web/js/flm.js'

const summary = { analysisOnly: true, automaticApplicationAllowed: false, tuneChanged: false,
  availableSegments: 355, reviewedSegments: [0, 27], vehicles: ['Declared car'], metrics: [
    { domain: 'lateral_acceleration', speedRange: 'at_least_15_mps', windows: 4, samples: 804,
      observedSeconds: 8, rmse: .25, meanAbsoluteError: .2, worstAbsoluteError: .4, errorReversalHz: .5 }],
}
const fixture = { version: 1, parked: true, automaticApplicationAllowed: false, localRoutes: [{ routeId: 'declared', segments: 355 }],
  reviews: [{ source: 'local', routeId: 'declared', status: 'ready', reason: 'Recorded tracking reviewed; tune unchanged', updatedAt: 100, summary }] }
assert.equal(validRouteReviews(fixture), true)
assert.equal(validRouteReviews({ ...fixture, automaticApplicationAllowed: true }), false)
assert.equal(validRouteReviews({ ...fixture, reviews: [{ ...fixture.reviews[0], summary: { ...summary, tuneChanged: true } }] }), false)
assert.equal(validRouteReviews({ ...fixture, reviews: [{ ...fixture.reviews[0], summary: { ...summary, metrics: [{ ...summary.metrics[0], rmse: NaN }] } }] }), false)
assert.equal(metricUnits('lateral_acceleration'), 'm/s²')
assert.equal(metricUnits('steering_angle'), 'degrees')
assert.equal(metricNumber(.25), '0.250')
assert.match(FlmPage.template, /<FlmRouteReviews/)
assert.match(FlmRouteReviews.template, /Review Connect route/)
assert.match(FlmRouteReviews.template, /14 evenly spaced/)

const priorFetch = globalThis.fetch, priorDocument = globalThis.document
globalThis.document = { hidden: false }
let resolve, revoked = 0
globalThis.fetch = () => new Promise(r => { resolve = r })
const page = { ...FlmRouteReviews.data(), mode: 'local', alive: true, unauthorized() { revoked++ }, poller: { stop() {} } }
for (const [name, method] of Object.entries(FlmRouteReviews.methods)) page[name] = method.bind(page)
const pending = page.load()
page.alive = false
page.request.abort()
resolve({ ok: true, json: async () => fixture })
await pending
assert.equal(page.state, null, 'An unmounted page must not accept an older response')
page.alive = true
globalThis.fetch = async () => ({ ok: false, status: 401, json: async () => ({ error: 'Sign in' }) })
await page.load()
assert.equal(revoked, 1)
assert.equal(page.state, null)
globalThis.fetch = priorFetch
globalThis.document = priorDocument
console.log('Historical route review schema, units, auth and stale response checks passed')
