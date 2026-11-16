import assert from 'node:assert/strict'
import { validLearningMode, FlmLearningMode } from '../web/js/flm-learning-mode.js'
import { FlmPage } from '../web/js/flm.js'

const fixture = { version: 1, available: true, parked: true, vehicle: 'Declared car', mode: 'live', enabled: true, revision: 'a'.repeat(64) }
assert.equal(validLearningMode(fixture), true)
assert.equal(validLearningMode({ ...fixture, mode: 'unrecognized' }), false)
assert.equal(validLearningMode({ ...fixture, revision: 'invalid' }), false)
assert.match(FlmLearningMode.template, /Live \+ parked finishing · default/)
assert.match(FlmLearningMode.template, /Offroad only/)
assert.equal(FlmLearningMode.data().choice, 'live')
assert.match(FlmLearningMode.template, /original discovered VIN/)
assert.match(FlmPage.template, /<FlmLearningMode/)
assert.match(FlmLearningMode.template, /does not turn it on/)
console.log('FLM live mode schema and presentation checks passed')
