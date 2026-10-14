(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.document) {
    const start = function () {
      if (root.document.body && root.document.body.dataset.page === 'invite') {
        api.initInvitationPage();
      }
    };
    if (root.document.readyState === 'loading') {
      root.document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
      start();
    }
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  const TOKEN_RE = /^[A-Za-z0-9_-]{43}$/;
  const TRANSFER_RE = /^#transfer=([A-Za-z0-9_-]{43})$/;

  function invitationToken(hash) {
    const value = String(hash || '').replace(/^#/, '');
    return TOKEN_RE.test(value) ? value : null;
  }

  function transferToken(hash) {
    const match = TRANSFER_RE.exec(String(hash || ''));
    return match ? match[1] : null;
  }

  function safeNextPath(search) {
    let candidate = '';
    try {
      candidate = new URLSearchParams(String(search || '')).get('next') || '';
    } catch (_error) {
      return '/';
    }
    if (!candidate.startsWith('/') || candidate.startsWith('//')) return '/';
    try {
      const parsed = new URL(candidate, 'https://double-move.invalid');
      if (parsed.origin !== 'https://double-move.invalid') return '/';
      if (
        parsed.pathname === '/analysis'
        || parsed.pathname === '/analysis/'
        || parsed.pathname === '/games'
        || parsed.pathname === '/games/'
        || /^\/games\/[0-9a-f]{32}$/.test(parsed.pathname)
        || parsed.pathname === '/play'
        || parsed.pathname === '/play/'
        || /^\/play\/[0-9a-f]{32}$/.test(parsed.pathname)
        || parsed.pathname === '/rules'
        || parsed.pathname === '/rules/'
      ) {
        return parsed.pathname + parsed.search;
      }
    } catch (_error) {
      return '/';
    }
    return '/';
  }

  function takeTokenAndCleanLocation(location, history) {
    const token = invitationToken(location && location.hash);
    if (
      location
      && history
      && typeof history.replaceState === 'function'
      && location.hash
    ) {
      history.replaceState(
        {},
        '',
        String(location.pathname || '/invite') + String(location.search || ''),
      );
    }
    return token;
  }

  function takeCredentialAndCleanLocation(location, history) {
    const transfer = transferToken(location && location.hash);
    const invitation = transfer ? null : invitationToken(location && location.hash);
    if (
      location
      && history
      && typeof history.replaceState === 'function'
      && location.hash
    ) {
      history.replaceState(
        {},
        '',
        String(location.pathname || '/invite') + String(location.search || ''),
      );
    }
    if (transfer) return { kind: 'transfer', token: transfer };
    if (invitation) return { kind: 'invitation', token: invitation };
    return null;
  }

  async function readJsonResponse(response) {
    let payload = null;
    try {
      payload = await response.json();
    } catch (_error) {
      payload = null;
    }
    if (!response.ok) {
      throw new Error(
        payload && payload.error
          ? payload.error
          : 'The invitation could not be checked.',
      );
    }
    return payload || {};
  }

  async function redeem(fetchImpl, token) {
    const response = await fetchImpl('/api/access/redeem', {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      referrerPolicy: 'no-referrer',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
    return readJsonResponse(response);
  }

  async function redeemTransfer(fetchImpl, token) {
    const response = await fetchImpl('/api/access/transfer', {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      referrerPolicy: 'no-referrer',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
    return readJsonResponse(response);
  }

  async function accessStatus(fetchImpl) {
    const response = await fetchImpl('/api/access/status', {
      credentials: 'same-origin',
      cache: 'no-store',
      referrerPolicy: 'no-referrer',
    });
    return readJsonResponse(response);
  }

  function initInvitationPage() {
    const document = root.document;
    const message = document.getElementById('inviteMessage');
    const title = document.getElementById('inviteTitle');
    const continueLink = document.getElementById('inviteContinue');
    const next = safeNextPath(root.location.search);
    const credential = takeCredentialAndCleanLocation(
      root.location,
      root.history,
    );

    const showContinue = function (heading, text) {
      title.textContent = heading;
      message.textContent = text;
      message.classList.remove('error');
      continueLink.href = next;
      continueLink.hidden = false;
    };
    const showError = function (text) {
      title.textContent = 'Invitation needed';
      message.textContent = text;
      message.classList.add('error');
      continueLink.hidden = true;
    };

    if (credential) {
      const exchange = credential.kind === 'transfer'
        ? redeemTransfer
        : redeem;
      exchange(root.fetch.bind(root), credential.token).then(function () {
        root.location.replace(next);
      }).catch(function (error) {
        showError(error.message || 'This access link could not be used.');
      });
      return;
    }

    accessStatus(root.fetch.bind(root)).then(function (status) {
      if (status.authorized) {
        showContinue(
          status.mode === 'open' ? 'The site is open' : 'Access confirmed',
          status.mode === 'open'
            ? 'No invitation is currently required.'
            : 'This browser already has access.',
        );
      } else {
        showError('Open the complete invitation link you received.');
      }
    }).catch(function (error) {
      showError(error.message || 'Access could not be checked.');
    });
  }

  return {
    TOKEN_RE,
    TRANSFER_RE,
    invitationToken,
    transferToken,
    safeNextPath,
    takeTokenAndCleanLocation,
    takeCredentialAndCleanLocation,
    readJsonResponse,
    redeem,
    redeemTransfer,
    accessStatus,
    initInvitationPage,
  };
});
