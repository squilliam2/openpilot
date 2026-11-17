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
assert.match(FlmRouteReviews.template, /Every available segment is attempted/)
assert.match(FlmRouteReviews.template, /no route-wide segment-count limit/)
assert.match(FlmRouteReviews.template, /Import state:/)
assert.match(FlmRouteReviews.template, /held-out:/)
assert.equal(FlmRouteReviews.data().purpose, 'tune')

const votes = { support: 177, total: 177, lowerBound: .98 }
const intentId = 'd'.repeat(64)
const fullSummary = { ...summary, version: 2, analysisOnly: false,
  availableSegments: 355, attemptedSegments: 355, reviewedCount: 354, omittedCount: 1,
  reviewedSegments: Array.from({ length: 32 }, (_, i) => i), allSegmentsAttempted: true,
  omittedSegments: [[354, 'Incomplete original recording']], segmentPreviewTruncated: true,
  tuning: { version: 1, sourceSha256: 'e'.repeat(64), intentId, automaticApplicationAllowed: false,
    requiresFreshNativeQualification: true, selected: { parameter: 'ff_gain_left', delta: .005,
      configurationHash: 'a'.repeat(64), cohort: 'high:left:steady', training: votes, heldout: votes,
      convergence: 'standard', fasterEvidenceSupported: true, nativeQualified: false, requiresFreshNativeQualification: true } },
  application: { intentId, routeId: 'declared', state: 'waiting_for_vehicle', reason: 'current_vehicle_and_native_qualification_required',
    parameter: 'ff_gain_left', delta: .005, requiresFreshNativeQualification: true, acceptedMapWrittenByImport: false },
}
const withSummary = value => ({ ...fixture, reviews: [{ ...fixture.reviews[0], summary: value }] })
const pooledSummary = structuredClone(fullSummary)
Object.assign(pooledSummary.tuning.selected, { pooledComparison: true, training: { sections: 4, windows: 8 }, heldout: { sections: 3, windows: 5 } })
assert.equal(validRouteReviews(withSummary(pooledSummary)), true)
pooledSummary.tuning.selected.heldout.sections = 6
assert.equal(validRouteReviews(withSummary(pooledSummary)), false)
assert.equal(validRouteReviews(withSummary(fullSummary)), true, '32 preview entries do not limit the 355 attempted sections')
assert.equal(validRouteReviews(withSummary({ ...fullSummary, availableSegments: 1001, attemptedSegments: 1001,
  reviewedCount: 1000 })), true, 'The schema has no 50, 355 or 512 route segment cap')
assert.equal(validRouteReviews(withSummary({ ...fullSummary, attemptedSegments: 33, reviewedCount: 32,
  allSegmentsAttempted: false })), true, 'Partial progress remains available while processing resumes')
assert.equal(validRouteReviews(withSummary({ ...fullSummary, attemptedSegments: 33 })), false)
assert.equal(validRouteReviews(withSummary({ ...fullSummary, reviewedCount: 356 })), false)
assert.equal(validRouteReviews(withSummary({ ...fullSummary, allSegmentsAttempted: false })), false)
assert.equal(validRouteReviews(withSummary({ ...fullSummary, reviewedSegments: Array.from({ length: 33 }, (_, i) => i) })), false)
for (const bad of [null, 'invalid', [], { selected: {} }, { ...fullSummary.tuning, selected: {} },
  { ...fullSummary.tuning, selected: { ...fullSummary.tuning.selected, training: null } },
  { ...fullSummary.tuning, selected: { ...fullSummary.tuning.selected, heldout: { support: 178, total: 177, lowerBound: .9 } } },
  { ...fullSummary.tuning, selected: { ...fullSummary.tuning.selected, delta: Infinity } },
  { ...fullSummary.tuning, selected: { ...fullSummary.tuning.selected, nativeQualified: true } }]) {
  assert.equal(validRouteReviews(withSummary({ ...fullSummary, tuning: bad })), false)
}
for (const bad of [null, {}, { ...fullSummary.application, state: null }, { ...fullSummary.application, reason: [] },
  { ...fullSummary.application, intentId: 'f'.repeat(64) }, { ...fullSummary.application, acceptedMapWrittenByImport: true }]) {
  assert.equal(validRouteReviews(withSummary({ ...fullSummary, application: bad })), false)
}
for (const state of ['waiting_for_vehicle', 'queued', 'native_trial_offered', 'confirmed', 'rolled_back', 'rejected', 'stale_scope']) {
  assert.equal(validRouteReviews(withSummary({ ...fullSummary, application: { ...fullSummary.application, state } })), true)
}
assert.equal(validRouteReviews(withSummary({ ...fullSummary, tuning: { ...fullSummary.tuning, selected: null } })), true)
assert.equal(validRouteReviews({ ...fixture, localRoutes: [null] }), false)
assert.equal(validRouteReviews({ ...fixture, reviews: [null] }), false)
assert.equal(validRouteReviews(withSummary({ ...summary, metrics: [null] })), false)

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
page.alive = true
page.connect = ' declared-route '
page.local = 'declared-local'
const submitted = []
globalThis.fetch = async (_url, options) => {
  submitted.push(JSON.parse(options.body))
  return { ok: true, json: async () => withSummary(fullSummary) }
}
await page.reviewConnect()
assert.deepEqual(submitted[0], { source: 'connect', routeId: 'declared-route', purpose: 'tune' })
page.purpose = 'review'
await page.reviewLocal()
assert.deepEqual(submitted[1], { source: 'local', routeId: 'declared-local', purpose: 'review' })
assert.equal(page.state.reviews[0].summary.reviewedCount, 354)

globalThis.fetch = priorFetch
globalThis.document = priorDocument
console.log('Full-route progress, qualified import schema, purpose selection, auth and stale response checks passed')
