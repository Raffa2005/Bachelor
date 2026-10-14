'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

for (const managed of [true, false]) {
  test(`pair change invalidates ${managed ? 'hosted' : 'local'} analysis cache`, () => {
    let pair = 'pair-a';
    const values = {modelSelect: 'moe1-step00064000.pb.gz',
      weightsInput: 'moe1-step00064000.pb.gz'};
    const context = {runtimeLocked: false, runtimeManaged: managed,
      el: id => ({value: values[id] || '', selectedOptions: [{dataset: {identity: pair}}]})};
    vm.createContext(context);
    const start = html.indexOf('function engineIdentityKey()');
    const end = html.indexOf('\nfunction ', start + 1);
    vm.runInContext(html.slice(start, end), context);
    const original = context.engineIdentityKey();
    pair = 'pair-b';
    assert.notEqual(context.engineIdentityKey(), original);
    if (!managed) {
      values.weightsInput = 'custom.pb.gz';
      assert.equal(JSON.parse(context.engineIdentityKey()).modelIdentity, undefined);
    }
  });
}

test('analysis menu retains server-verified identity', () => {
  assert.match(html, /opt\.dataset\.identity = model\.identity \|\| ''/);
});
