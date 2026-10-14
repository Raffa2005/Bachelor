'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const invite = require('../invite.js');

test('invitation fragments accept only one exact high-entropy token', () => {
  const token = 'A'.repeat(43);
  assert.equal(invite.invitationToken('#' + token), token);
  assert.equal(invite.invitationToken(token), token);
  assert.equal(invite.invitationToken('#short'), null);
  assert.equal(invite.invitationToken('#' + token + '?leak=1'), null);
  assert.equal(invite.invitationToken('#' + '<script>'.repeat(6)), null);
});

test('legacy-host transfer fragments are explicit and exact', () => {
  const token = 'T'.repeat(43);
  assert.equal(invite.transferToken('#transfer=' + token), token);
  assert.equal(invite.transferToken('#' + token), null);
  assert.equal(invite.transferToken('#transfer=short'), null);
  assert.equal(invite.invitationToken('#transfer=' + token), null);
});

test('the invitation fragment is removed before asynchronous redemption', () => {
  const calls = [];
  const location = {
    pathname: '/invite',
    search: '?next=%2Fanalysis',
    hash: '#' + 'B'.repeat(43),
  };
  const token = invite.takeTokenAndCleanLocation(location, {
    replaceState(state, title, target) {
      calls.push([state, title, target]);
    },
  });

  assert.equal(token, 'B'.repeat(43));
  assert.deepEqual(calls, [[{}, '', '/invite?next=%2Fanalysis']]);
  assert.equal(calls[0][2].includes(token), false);
});

test('a transfer fragment is classified and removed before exchange', () => {
  const calls = [];
  const token = 'D'.repeat(43);
  const credential = invite.takeCredentialAndCleanLocation({
    pathname: '/invite',
    search: '?next=%2Fplay',
    hash: '#transfer=' + token,
  }, {
    replaceState(state, title, target) {
      calls.push([state, title, target]);
    },
  });

  assert.deepEqual(credential, { kind: 'transfer', token });
  assert.deepEqual(calls, [[{}, '', '/invite?next=%2Fplay']]);
  assert.equal(calls[0][2].includes(token), false);
});

test('post-redemption destinations remain local and limited to site tools', () => {
  assert.equal(invite.safeNextPath(''), '/');
  assert.equal(invite.safeNextPath('?next=%2Fplay'), '/play');
  assert.equal(
    invite.safeNextPath('?next=%2Fgames%2F' + 'a'.repeat(32)),
    '/games/' + 'a'.repeat(32),
  );
  assert.equal(
    invite.safeNextPath('?next=%2Fanalysis%3Ffen%3Dstartpos'),
    '/analysis?fen=startpos',
  );
  assert.equal(invite.safeNextPath('?next=https%3A%2F%2Fevil.test'), '/');
  assert.equal(invite.safeNextPath('?next=%2F%2Fevil.test'), '/');
  assert.equal(invite.safeNextPath('?next=%2Finvite'), '/');
  assert.equal(invite.safeNextPath('?next=%2Frules'), '/rules');
});

test('invitation code sends secrets only in a same-origin JSON body', async () => {
  const calls = [];
  const token = 'C'.repeat(43);
  const payload = await invite.redeem(async (url, options) => {
    calls.push({ url, options });
    return {
      ok: true,
      async json() {
        return { authorized: true, remaining_uses: 4 };
      },
    };
  }, token);

  assert.deepEqual(payload, { authorized: true, remaining_uses: 4 });
  assert.equal(calls[0].url, '/api/access/redeem');
  assert.equal(calls[0].url.includes(token), false);
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(calls[0].options.referrerPolicy, 'no-referrer');
  assert.deepEqual(JSON.parse(calls[0].options.body), { token });
});

test('session transfer also sends its secret only in a same-origin JSON body', async () => {
  const calls = [];
  const token = 'E'.repeat(43);
  const payload = await invite.redeemTransfer(async (url, options) => {
    calls.push({ url, options });
    return {
      ok: true,
      async json() {
        return { authorized: true };
      },
    };
  }, token);

  assert.deepEqual(payload, { authorized: true });
  assert.equal(calls[0].url, '/api/access/transfer');
  assert.equal(calls[0].url.includes(token), false);
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(calls[0].options.referrerPolicy, 'no-referrer');
  assert.deepEqual(JSON.parse(calls[0].options.body), { token });
});

test('the invitation page never persists or renders its raw secret', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'invite.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '..', 'invite.html'), 'utf8');
  assert.equal(/\blocalStorage\b|\bsessionStorage\b/.test(source), false);
  assert.equal(/innerHTML|insertAdjacentHTML|document\.write/.test(source), false);
  assert.match(html, /id="inviteMessage"/);
  assert.match(html, /src="\/invite\.js\?v=[A-Za-z0-9_-]+"/);
});
