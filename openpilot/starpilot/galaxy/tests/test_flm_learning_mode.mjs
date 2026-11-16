import assert from 'node:assert/strict'
import { validLearningMode, FlmLearningMode } from '../web/js/flm-learning-mode.js'
import { FlmPage } from '../web/js/flm.js'

const fixture = { version: 1, available: true, parked: true, vehicle: 'Declared car', mode: 'live', enabled: true, revision: 'a'.repeat(64) }
assert.equal(validLearningMode(fixture), true)
assert.equal(validLearningMode({ ...fixture, convergence: 'standard' }), true)
assert.equal(validLearningMode({ ...fixture, convergence: 'faster' }), true)
assert.equal(validLearningMode({ ...fixture, convergence: 'aggressive' }), false)
assert.equal(validLearningMode({ ...fixture, convergence: true }), false)
assert.equal(validLearningMode({ ...fixture, mode: 'unrecognized' }), false)
assert.equal(validLearningMode({ ...fixture, revision: 'invalid' }), false)
assert.match(FlmLearningMode.template, /Live \+ parked finishing · default/)
assert.match(FlmLearningMode.template, /Offroad only/)
assert.equal(FlmLearningMode.data().choice, 'live')
assert.equal(FlmLearningMode.data().convergenceChoice, 'standard')
assert.match(FlmLearningMode.template, /Faster convergence \(experimental\)/)
assert.match(FlmLearningMode.template, /theoretical scenarios alone cannot approve a tune/)
assert.match(FlmLearningMode.template, /original discovered VIN/)
assert.match(FlmPage.template, /<FlmLearningMode/)
assert.match(FlmLearningMode.template, /does not turn it on/)

const originalFetch = globalThis.fetch, originalDocument = globalThis.document
try {
  globalThis.document = { hidden: false }
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return { ok: true, json: async () => ({ ...fixture, convergence: options.method === 'POST' ? 'faster' : 'standard' }) }
  }
  const vm = { ...FlmLearningMode.data(), mode: 'local', alive: true, unauthorized() {} }
  for (const [name, method] of Object.entries(FlmLearningMode.methods)) vm[name] = method.bind(vm)
  await vm.load()
  assert.equal(vm.convergenceChoice, 'standard', 'Older or unset preferences stay off')
  vm.choice = 'parked'; vm.convergenceChoice = 'faster'; vm.dirty = true
  await vm.save()
  assert.equal(calls.length, 2)
  assert.deepEqual(JSON.parse(calls[1].options.body), { mode: 'parked', convergence: 'faster', revision: fixture.revision })
  assert.equal(vm.saved, true)
  assert.equal(vm.dirty, false)
  assert.equal(vm.convergenceChoice, 'faster')
  vm.state.parked = false
  await vm.save()
  assert.equal(calls.length, 2, 'Driving cannot send a preference update')
} finally {
  globalThis.fetch = originalFetch; globalThis.document = originalDocument
}
console.log('FLM live mode schema and presentation checks passed')
