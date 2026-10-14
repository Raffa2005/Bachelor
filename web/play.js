(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.DoubleMovePublic = api;
  if (root && root.document) {
    const start = function () {
      api.initPublicChrome();
      if (root.document.body && root.document.body.dataset.page === 'play') {
        api.initPlayPage();
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

  const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
  const MOVE_RE = /^[a-h][1-8][a-h][1-8][qrbn]?\+?$/;
  const PUBLIC_GAME_ID_RE = /^[0-9a-f]{32}$/;
  const PLAY_SETUP_STORAGE_KEY = 'dm-play-setup-v1';
  const BOARD_ANIMATION_MS = 190;
  const PENDING_COMMAND_POLL_DELAYS_MS = Object.freeze([
    500,
    750,
    1000,
    1500,
    2000,
    3000,
    5000,
  ]);
  const ACTIVE_STATUSES = new Set([
    'active',
    'created',
    'in_progress',
    'playing',
    'thinking',
    'engine_thinking',
    'waiting',
  ]);

  class ApiError extends Error {
    constructor(message, status, payload) {
      super(message);
      this.name = 'ApiError';
      this.status = status || 0;
      this.payload = payload || null;
      this.code = payload && (payload.code || payload.error_code) || '';
    }
  }

  function normalizeColor(value) {
    const color = String(value == null ? '' : value).toLowerCase();
    if (color === 'w' || color === 'white') return 'w';
    if (color === 'b' || color === 'black') return 'b';
    return null;
  }

  function colorName(color) {
    return normalizeColor(color) === 'b' ? 'Black' : 'White';
  }

  function normalizeMoveToken(value) {
    if (typeof value === 'string') return value.trim();
    if (!value || typeof value !== 'object') return '';
    return String(value.uci || value.move || value.token || '').trim();
  }

  function moveTokens(state) {
    const values = state && Array.isArray(state.moves) ? state.moves : [];
    return values.map(normalizeMoveToken).filter(move => MOVE_RE.test(move));
  }

  function legalMoves(state) {
    const nested = state && state.position && state.position.legal_moves;
    const values = state && Array.isArray(state.legal_moves)
      ? state.legal_moves
      : Array.isArray(nested)
        ? nested
        : [];
    return values.map(normalizeMoveToken).filter(move => MOVE_RE.test(move));
  }

  function chessgroundDests(moves) {
    const destinations = new Map();
    for (const value of moves || []) {
      const move = normalizeMoveToken(value);
      if (!MOVE_RE.test(move)) continue;
      const source = move.slice(0, 2);
      const target = move.slice(2, 4);
      const targets = destinations.get(source) || [];
      if (!targets.includes(target)) targets.push(target);
      destinations.set(source, targets);
    }
    return destinations;
  }

  function matchingMoveTokens(moves, source, target) {
    const prefix = String(source || '') + String(target || '');
    if (!/^[a-h][1-8][a-h][1-8]$/.test(prefix)) return [];
    return (moves || [])
      .map(normalizeMoveToken)
      .filter(move => MOVE_RE.test(move) && move.slice(0, 4) === prefix);
  }

  function chessgroundColor(value) {
    return normalizeColor(value) === 'b' ? 'black' : 'white';
  }

  function lastMoveSquares(state) {
    const moves = moveTokens(state);
    const move = moves[moves.length - 1];
    return move ? [move.slice(0, 2), move.slice(2, 4)] : [];
  }

  function newlyObservedMoves(previousState, nextState) {
    const previous = moveTokens(previousState);
    const next = moveTokens(nextState);
    if (previous.length > next.length) return [];
    for (let index = 0; index < previous.length; index++) {
      if (previous[index] !== next[index]) return [];
    }
    return next.slice(previous.length);
  }

  function observedMoveAnimationPlan(previousState, nextState, optimisticMoveCount) {
    const observedMoves = newlyObservedMoves(previousState, nextState);
    const requestedOptimisticMoves = Math.max(
      0,
      Math.floor(Number(optimisticMoveCount) || 0),
    );
    const skippedMoveCount = Math.min(requestedOptimisticMoves, observedMoves.length);
    return {
      startMoveCount: moveTokens(previousState).length + skippedMoveCount,
      moveCount: observedMoves.length - skippedMoveCount,
      skippedMoveCount,
    };
  }

  function unwrapGame(payload) {
    if (!payload || typeof payload !== 'object') return payload;
    return payload.game || payload.state || payload.data && payload.data.game || payload;
  }

  function gameIdFromPath(pathname, routeName) {
    const route = routeName || 'play';
    const parts = String(pathname || '')
      .split('/')
      .filter(Boolean);
    if (parts.length !== 2 || parts[0] !== route) return null;
    try {
      const value = decodeURIComponent(parts[1]);
      return value && value !== 'new' ? value : null;
    } catch (error) {
      return null;
    }
  }

  function resumeTarget(config, currentGameId) {
    if (currentGameId || !config || typeof config !== 'object') return null;
    const id = config.active_game_id;
    if (typeof id !== 'string' || !PUBLIC_GAME_ID_RE.test(id)) return null;
    return {
      id,
      path: '/play/' + encodeURIComponent(id),
    };
  }

  function sanitizeSetupPreferences(value) {
    const source = value && typeof value === 'object' ? value : {};
    const color = ['white', 'black', 'random'].includes(source.color)
      ? source.color
      : 'random';
    return {
      color,
      preset_id: typeof source.preset_id === 'string' ? source.preset_id : '',
      clock_id: typeof source.clock_id === 'string' ? source.clock_id : '',
    };
  }

  function groupMovePairs(line) {
    const source = line && typeof line === 'object' ? line : {};
    const rawMoves = Array.isArray(source.rawMoves) ? source.rawMoves : [];
    const sans = Array.isArray(source.moveSans) ? source.moveSans : [];
    const colors = Array.isArray(source.colors) ? source.colors : [];
    const numbers = Array.isArray(source.moveNumbers) ? source.moveNumbers : [];
    const doubleFlags = Array.isArray(source.doubleFlags) ? source.doubleFlags : [];
    const groups = [];
    const byNumber = new Map();

    rawMoves.forEach((rawMove, index) => {
      const fallbackNumber = Math.floor(index / 2) + 1;
      const parsedNumber = Number(numbers[index]);
      const moveNumber = Number.isFinite(parsedNumber) && parsedNumber > 0
        ? Math.floor(parsedNumber)
        : fallbackNumber;
      let group = byNumber.get(moveNumber);
      if (!group) {
        group = { moveNumber, white: [], black: [] };
        byNumber.set(moveNumber, group);
        groups.push(group);
      }
      const color = normalizeColor(colors[index]) || (index % 2 === 0 ? 'w' : 'b');
      const isDouble = doubleFlags[index] === true;
      const isDoubleStart = String(rawMove).endsWith('+');
      group[color === 'b' ? 'black' : 'white'].push({
        index,
        san: typeof sans[index] === 'string' && sans[index] ? sans[index] : '?',
        isDouble,
        isDoubleStart,
        isSecondLeg: isDouble && !isDoubleStart,
      });
    });
    return groups;
  }

  function boundedNumber(value, label, minimum, maximum, integer) {
    const text = typeof value === 'number' ? String(value) : String(value == null ? '' : value).trim();
    const parsed = text ? Number(text) : NaN;
    if (
      !Number.isFinite(parsed) ||
      parsed < minimum ||
      parsed > maximum ||
      (integer && !Number.isInteger(parsed))
    ) {
      throw new RangeError(
        label + ' must be ' +
          (integer ? 'a whole number' : 'a number') +
          ' from ' + minimum + ' to ' + maximum + '.',
      );
    }
    return parsed;
  }

  function buildAdvancedOptions(values) {
    const source = values && typeof values === 'object' ? values : {};
    const modelId = String(source.model_id || '').trim();
    if (!modelId) throw new RangeError('Choose a model for the advanced game.');
    const nodes = boundedNumber(source.nodes, 'Visits', 1, 1_000_000, true);
    const temperature = boundedNumber(
      source.temperature,
      'Temperature',
      0,
      10,
      false,
    );
    const tempDecayMoves = boundedNumber(
      source.temp_decay_moves,
      'Temperature decay moves',
      0,
      640,
      true,
    );
    const clockMode = String(source.clock_mode || 'unlimited');
    let clock;
    if (clockMode === 'unlimited') {
      clock = { mode: 'unlimited' };
    } else if (clockMode === 'timed') {
      const initialMinutes = boundedNumber(
        source.initial_minutes,
        'Initial minutes',
        1 / 60,
        1440,
        false,
      );
      const incrementSeconds = boundedNumber(
        source.increment_seconds,
        'Increment seconds',
        0,
        3600,
        false,
      );
      clock = {
        mode: 'timed',
        initial_ms: Math.round(initialMinutes * 60_000),
        increment_ms: Math.round(incrementSeconds * 1000),
      };
    } else {
      throw new RangeError('Choose unlimited or a custom time control.');
    }
    const startFen = String(source.start_fen || 'startpos').trim();
    if (!startFen || startFen.length > 256 || /[\u0000-\u001f\u007f]/.test(startFen)) {
      throw new RangeError('Start FEN is invalid.');
    }
    return {
      model_id: modelId,
      nodes,
      temperature,
      temp_decay_moves: tempDecayMoves,
      clock,
      start_fen: startFen,
    };
  }

  function shouldApplyGameResponse(context) {
    const state = context || {};
    if (state.requestMutationGeneration !== state.currentMutationGeneration) return false;
    if (
      Number.isFinite(state.requestId) &&
      Number.isFinite(state.latestAppliedRequestId) &&
      state.requestId < state.latestAppliedRequestId
    ) return false;
    if (state.promotionPending) return false;
    if (state.allowBusy) {
      return state.allowedMutationGeneration === state.currentMutationGeneration;
    }
    return !state.pending && !state.boardAnimating;
  }

  function hasSameGameRevision(previousState, nextState) {
    if (!previousState || !nextState) return false;
    const previousRevision = Number(previousState.revision);
    const nextRevision = Number(nextState.revision);
    return Number.isSafeInteger(previousRevision) &&
      Number.isSafeInteger(nextRevision) &&
      previousRevision === nextRevision;
  }

  function isGameComplete(state) {
    if (!state || typeof state !== 'object') return false;
    const status = String(state.status || '').toLowerCase();
    const result = String(state.result || '');
    if (status && !ACTIVE_STATUSES.has(status)) return true;
    return !!result && result !== '*' && result !== 'ongoing';
  }

  function hasPendingCommand(state) {
    return !!state && state.pending_command === true;
  }

  function canStartGameMutation(state, requestPending, animating) {
    return !!state &&
      requestPending !== true &&
      animating !== true &&
      !isGameComplete(state) &&
      !hasPendingCommand(state);
  }

  function isMidDouble(state) {
    if (!state || typeof state !== 'object') return false;
    const ability = state.ability;
    if (state.mid_double === true || state.double_move_pending === true) return true;
    if (ability && typeof ability === 'object') {
      if (
        ability.mid === true ||
        ability.mid_double === true ||
        ability.active === true ||
        ability.pending_second === true
      ) return true;
      const phase = String(ability.phase || ability.state || '').toLowerCase();
      if (phase === 'second' || phase === 'second_move' || phase === 'mid') return true;
    }
    if (typeof ability === 'string' && /m$/i.test(ability)) return true;
    return false;
  }

  function abilityAvailable(state, color) {
    const side = normalizeColor(color);
    const ability = state && state.ability;
    const plusExists = legalMoves(state).some(move => move.endsWith('+'));
    if (!side) return plusExists;
    if (typeof ability === 'string') {
      if (/^(?:W|-)?(?:B|-)?m?$/i.test(ability)) {
        const token = ability.toUpperCase();
        return side === 'w' ? token.includes('W') : token.includes('B');
      }
    }
    if (ability && typeof ability === 'object') {
      const candidates = side === 'w'
        ? [ability.w, ability.white, ability.white_available]
        : [ability.b, ability.black, ability.black_available];
      if (ability.available && typeof ability.available === 'object') {
        candidates.push(
          ability.available[side],
          ability.available[side === 'w' ? 'white' : 'black'],
        );
      }
      const explicit = candidates.find(value => typeof value === 'boolean');
      if (explicit !== undefined) return explicit;
    }
    return plusExists;
  }

  function playerFor(state, color) {
    const side = normalizeColor(color) || 'w';
    const long = side === 'w' ? 'white' : 'black';
    const players = state && state.players;
    if (Array.isArray(players)) {
      return players.find(player => normalizeColor(player && player.color) === side) || {};
    }
    if (players && typeof players === 'object') {
      return players[long] || players[side] || {};
    }
    const direct = state && (state[long + '_player'] || state[side + '_player']);
    return direct || {};
  }

  function playerName(state, color) {
    const side = normalizeColor(color) || 'w';
    const player = playerFor(state, side);
    if (typeof player === 'string') return player;
    if (player && typeof player === 'object') {
      const value = player.display_name || player.name || player.label;
      if (value) return String(value);
      const type = String(player.type || player.kind || '').toLowerCase();
      if (type === 'engine' || type === 'computer') return 'Double Move Engine';
      if (type === 'human') return 'Anonymous challenger';
    }
    const human = normalizeColor(state && state.human_color);
    return side === human ? 'Anonymous challenger' : 'Double Move Engine';
  }

  function playerMeta(state, color) {
    const side = normalizeColor(color) || 'w';
    const human = normalizeColor(state && state.human_color);
    if (side === human) return 'You';
    const player = playerFor(state, side);
    let label = '';
    if (player && typeof player === 'object') {
      const value = player.description || player.preset_label || player.strength || player.type;
      if (value && !/^(?:engine|computer)$/i.test(String(value))) label = String(value);
    }
    const preset = state && (state.preset_label || state.strength_label);
    if (!label && preset) label = String(preset);
    if (!label) label = 'Engine';
    const nodes = numericValue(state && state.nodes);
    if (
      nodes !== null &&
      nodes >= 1 &&
      !new RegExp('\\b' + String(nodes).replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s+visits?\\b', 'i').test(label)
    ) {
      label += ' · ' + nodes + ' visit' + (nodes === 1 ? '' : 's');
    }
    return label;
  }

  function engineIsThinking(state) {
    if (!state || isGameComplete(state) || hasPendingCommand(state)) return false;
    if (state.engine_thinking === true) return true;
    const status = String(state.status || '').toLowerCase();
    if (status === 'engine_thinking' || status === 'thinking') return true;
    const human = normalizeColor(state.human_color);
    const turn = normalizeColor(state.to_move);
    return !!human && !!turn && human !== turn;
  }

  function canRequestTakeback(state, analysisState, ChessConstructor) {
    if (
      !state ||
      isGameComplete(state) ||
      hasPendingCommand(state) ||
      engineIsThinking(state)
    ) return false;
    const human = normalizeColor(state.human_color);
    const turn = normalizeColor(state.to_move);
    if (!human || turn !== human) return false;
    const replay = deriveReplay(state, analysisState, ChessConstructor);
    return replay.colors.some(color => normalizeColor(color) === human);
  }

  function isInitialEngineOpening(state, analysisState, ChessConstructor) {
    const tokens = moveTokens(state);
    if (normalizeColor(state && state.human_color) !== 'b') return false;
    if (tokens.length < 1 || tokens.length > 2) return false;
    const replay = deriveReplay(state, analysisState, ChessConstructor);
    if (!replay.valid || replay.colors.length !== tokens.length) return false;
    if (!replay.colors.every(color => normalizeColor(color) === 'w')) return false;
    return tokens.length === 1 || tokens[0].endsWith('+');
  }

  function normalizeFen(value) {
    const fen = String(value || 'startpos').trim();
    if (!fen || fen === 'startpos') return START_FEN;
    return fen.split(/\s+/).slice(0, 6).join(' ');
  }

  function deriveReplay(state, analysisState, ChessConstructor) {
    const initialFen = state && state.initial_fen || 'startpos';
    const tokens = moveTokens(state);
    if (analysisState && typeof analysisState.deriveLine === 'function' && ChessConstructor) {
      try {
        const line = analysisState.deriveLine(initialFen, tokens, ChessConstructor);
        if (line && line.valid && line.rawMoves.length === tokens.length) return line;
      } catch (error) {}
    }

    const fens = [normalizeFen(initialFen)];
    const colors = [];
    const moveSans = [];
    const moveNumbers = [];
    let fen = fens[0];
    for (let index = 0; index < tokens.length; index++) {
      const token = tokens[index];
      const parts = fen.split(/\s+/);
      const color = normalizeColor(parts[1]) || (index % 2 === 0 ? 'w' : 'b');
      colors.push(color);
      moveNumbers.push(Number(parts[5]) || Math.floor(index / 2) + 1);
      moveSans.push('?');
      if (analysisState && typeof analysisState.applyUciToFen === 'function') {
        fen = analysisState.applyUciToFen(fen, token, !token.endsWith('+'));
      }
      fens.push(normalizeFen(fen));
    }
    return {
      valid: true,
      rawMoves: tokens,
      fens,
      colors,
      moveSans,
      moveNumbers,
      doubleFlags: tokens.map(token => token.endsWith('+')),
    };
  }

  function currentFen(state, analysisState, ChessConstructor) {
    const explicit = state && (
      state.current_fen ||
      state.fen ||
      state.position && state.position.fen
    );
    if (explicit) return normalizeFen(explicit);
    const replay = deriveReplay(state || {}, analysisState, ChessConstructor);
    return replay.fens[replay.fens.length - 1] || START_FEN;
  }

  function boardPlacement(state, analysisState, ChessConstructor) {
    return currentFen(state, analysisState, ChessConstructor).split(/\s+/)[0];
  }

  function clockObject(state) {
    return state && state.clocks && typeof state.clocks === 'object' ? state.clocks : {};
  }

  function numericValue(value) {
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) return Number(value);
    return null;
  }

  function clockRemaining(state, color) {
    const clocks = clockObject(state);
    const side = normalizeColor(color) || 'w';
    const long = side === 'w' ? 'white' : 'black';
    const candidates = [
      clocks[side + '_ms'],
      clocks[long + '_ms'],
      clocks[side],
      clocks[long],
      clocks.remaining_ms && clocks.remaining_ms[side],
      clocks.remaining_ms && clocks.remaining_ms[long],
      clocks.remaining && clocks.remaining[side],
      clocks.remaining && clocks.remaining[long],
    ];
    for (const candidate of candidates) {
      const direct = numericValue(candidate);
      if (direct !== null) return Math.max(0, direct);
      if (candidate && typeof candidate === 'object') {
        const nested = numericValue(
          candidate.remaining_ms != null ? candidate.remaining_ms
            : candidate.time_ms != null ? candidate.time_ms
              : candidate.ms,
        );
        if (nested !== null) return Math.max(0, nested);
      }
    }
    return null;
  }

  function runningClockColor(state) {
    const clocks = clockObject(state);
    if (clocks.running === false || clocks.paused === true) return null;
    return normalizeColor(
      clocks.running_color ||
      clocks.active_color ||
      clocks.turn ||
      state && state.to_move,
    );
  }

  function clockIsUnlimited(state) {
    const clocks = state && state.clocks;
    if (clocks == null) return true;
    if (clocks === 'unlimited') return true;
    if (typeof clocks === 'object') {
      if (clocks.unlimited === true || clocks.enabled === false) return true;
      const id = String(clocks.id || clocks.clock_id || '').toLowerCase();
      if (id === 'unlimited' || id === 'none') return true;
    }
    return false;
  }

  function adjustedClockMs(state, color, receivedAtMs, nowMs) {
    const remaining = clockRemaining(state, color);
    if (remaining === null) return null;
    if (isGameComplete(state) || runningClockColor(state) !== normalizeColor(color)) return remaining;
    const receivedValue = numericValue(receivedAtMs);
    const nowValue = numericValue(nowMs);
    const received = receivedValue === null ? Date.now() : receivedValue;
    const now = nowValue === null ? Date.now() : nowValue;
    const elapsed = Math.max(0, now - received);
    return Math.max(0, remaining - elapsed);
  }

  function formatClock(milliseconds) {
    if (milliseconds === null || milliseconds === undefined) return '∞';
    const ms = Math.max(0, Number(milliseconds) || 0);
    if (ms < 20_000) {
      const seconds = Math.floor(ms / 1000);
      const tenths = Math.floor((ms % 1000) / 100);
      return '0:' + String(seconds).padStart(2, '0') + '.' + tenths;
    }
    const totalSeconds = Math.ceil(ms / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return minutes + ':' + String(seconds).padStart(2, '0');
  }

  function resultDisplay(result) {
    const value = String(result || '*');
    if (value === '1/2-1/2' || value === '0.5-0.5' || value.toLowerCase() === 'draw') return '½–½';
    return value.replace('-', '–');
  }

  function humanOutcomeDisplay(game) {
    const result = String(game && game.result || '');
    if (
      result === '1/2-1/2' ||
      result === '0.5-0.5' ||
      result.toLowerCase() === 'draw'
    ) {
      return 'Draw';
    }
    const winner = result === '1-0' ? 'w' : result === '0-1' ? 'b' : '';
    const human = normalizeColor(game && game.human_color);
    if (!winner || !human) return resultDisplay(result);
    return winner === human ? 'Human won' : 'Engine won';
  }

  function temperatureDisplay(game) {
    const temperature = numericValue(game && game.temperature);
    const decay = numericValue(game && game.temp_decay_moves);
    if (
      temperature === null ||
      temperature < 0 ||
      decay === null ||
      decay < 0 ||
      !Number.isInteger(decay)
    ) {
      return '';
    }
    return 'Temp ' + String(temperature) + ' · decay ' + String(decay) +
      ' move' + (decay === 1 ? '' : 's');
  }

  function terminationDisplay(value) {
    const text = String(value || 'Game complete').replace(/[_-]+/g, ' ').trim();
    if (!text) return 'Game complete';
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function idempotencyKey() {
    if (root.crypto && typeof root.crypto.randomUUID === 'function') {
      return root.crypto.randomUUID();
    }
    const random = Math.random().toString(36).slice(2);
    return Date.now().toString(36) + '-' + random + '-' + random.split('').reverse().join('');
  }

  async function requestJson(url, options, fetchImplementation) {
    const fetcher = fetchImplementation || root.fetch;
    if (typeof fetcher !== 'function') throw new ApiError('Network access is unavailable.', 0, null);
    const opts = Object.assign({
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {},
    }, options || {});
    opts.headers = Object.assign({ Accept: 'application/json' }, opts.headers || {});
    if (Object.prototype.hasOwnProperty.call(opts, 'json')) {
      opts.body = JSON.stringify(opts.json);
      opts.headers['Content-Type'] = 'application/json';
      delete opts.json;
    }
    let response;
    try {
      response = await fetcher(url, opts);
    } catch (error) {
      const networkError = new ApiError('Could not reach the game server.', 0, null);
      networkError.cause = error;
      throw networkError;
    }
    const contentType = response.headers && response.headers.get
      ? response.headers.get('content-type') || ''
      : '';
    let payload = null;
    try {
      payload = contentType.includes('application/json')
        ? await response.json()
        : await response.text();
    } catch (error) {}
    if (!response.ok) {
      const message = payload && typeof payload === 'object'
        ? payload.message || payload.error || payload.detail
        : typeof payload === 'string' && payload.trim()
          ? payload.trim()
          : 'The server rejected this request.';
      throw new ApiError(String(message), response.status, payload);
    }
    return payload;
  }

  function sleep(milliseconds) {
    return new Promise(resolve => root.setTimeout(resolve, milliseconds));
  }

  async function requestWithNetworkRetry(url, options, fetchImplementation) {
    try {
      return await requestJson(url, options, fetchImplementation);
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 0) throw error;
      await sleep(300);
      return requestJson(url, options, fetchImplementation);
    }
  }

  function normalizedOptions(values) {
    let list = values;
    if (!Array.isArray(list) && list && typeof list === 'object') {
      list = Object.keys(list).map(key => {
        const entry = list[key];
        return entry && typeof entry === 'object'
          ? Object.assign({ id: key }, entry)
          : { id: key, label: entry };
      });
    }
    if (!Array.isArray(list)) return [];
    return list.map((entry, index) => {
      if (typeof entry === 'string') return { id: entry, label: entry, description: '' };
      if (!entry || typeof entry !== 'object') return null;
      const id = entry.id || entry.value || entry.key || String(index);
      const normalized = {
        id: String(id),
        label: String(entry.label || entry.name || entry.title || id),
        description: String(entry.description || entry.help || entry.detail || ''),
        initial_ms: numericValue(entry.initial_ms != null ? entry.initial_ms : entry.time_ms),
        increment_ms: numericValue(entry.increment_ms),
      };
      for (const key of ['nodes', 'temperature', 'temp_decay_moves']) {
        const value = numericValue(entry[key]);
        if (value !== null) normalized[key] = value;
      }
      return normalized;
    }).filter(Boolean);
  }

  function initPublicChrome() {
    const document = root.document;
    if (!document) return;
    let dark = false;
    try {
      dark = root.localStorage.getItem('dm-theme') === 'dark';
    } catch (error) {}
    document.documentElement.classList.toggle('dark', dark);
    document.body.classList.toggle('dark', dark);
    const button = document.getElementById('themeButton');
    const syncIcon = function () {
      if (!button) return;
      const isDark = document.documentElement.classList.contains('dark');
      const icon = button.querySelector('[data-lucide]');
      if (icon) icon.setAttribute('data-lucide', isDark ? 'sun' : 'moon');
      const label = isDark ? 'Use light theme' : 'Use dark theme';
      button.setAttribute('aria-label', label);
      button.setAttribute('title', label);
      if (root.lucide) root.lucide.createIcons();
    };
    if (button) {
      button.addEventListener('click', function () {
        const nextDark = !document.documentElement.classList.contains('dark');
        document.documentElement.classList.toggle('dark', nextDark);
        document.body.classList.toggle('dark', nextDark);
        try {
          root.localStorage.setItem('dm-theme', nextDark ? 'dark' : 'light');
        } catch (error) {}
        syncIcon();
      });
    }
    syncIcon();
  }

  function initPlayPage() {
    const document = root.document;
    if (!document) return;

    const analysisState = root.DoubleMoveAnalysisState;
    const ChessConstructor = root.Chess;
    const reducedMotion = root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const animationSpeed = reducedMotion ? 0 : BOARD_ANIMATION_MS;
    const byId = id => document.getElementById(id);
    const monotonicNow = () => root.performance && typeof root.performance.now === 'function'
      ? root.performance.now()
      : Date.now();

    let config = null;
    let game = null;
    let gameId = gameIdFromPath(root.location && root.location.pathname, 'play');
    let board = null;
    let boardOrientation = null;
    let receivedAt = monotonicNow();
    let pending = false;
    let abilityArmed = false;
    let pollTimer = null;
    let pollGeneration = 0;
    let createAttempt = null;
    let promotionResolver = null;
    let keyboardSquare = null;
    let keyboardFrom = null;
    let boardAnimating = false;
    let boardAnimationGeneration = 0;
    let mutationGeneration = 0;
    let gameRequestGeneration = 0;
    let latestAppliedGameRequest = 0;
    let pendingCommandPollAttempt = 0;
    let availablePresets = [];
    let availableClocks = [];
    let availableModels = [];
    let advancedInitialized = false;
    let viewPly = null;
    let manualShapesByPly = new Map();
    let resignArmed = false;
    let resignTimer = null;

    const setupView = byId('setupView');
    const gameView = byId('gameView');
    const errorView = byId('gameErrorView');
    const setupMessage = byId('setupMessage');
    const gameMessage = byId('gameMessage');

    function setMessage(node, text, kind) {
      if (!node) return;
      node.className = 'message' + (kind ? ' ' + kind : '');
      node.textContent = text || '';
    }

    function sanitizeBoardShapes(shapes) {
      if (!Array.isArray(shapes)) return [];
      return shapes
        .filter(shape => /^[a-h][1-8]$/.test(String(shape && shape.orig || '')))
        .map(shape => ({
          orig: shape.orig,
          ...(shape.dest && /^[a-h][1-8]$/.test(String(shape.dest))
            ? { dest: shape.dest }
            : {}),
          brush: ['green', 'red', 'blue', 'yellow'].includes(shape.brush)
            ? shape.brush
            : 'green',
        }));
    }

    function handleBoardShapesChanged(shapes) {
      manualShapesByPly.set(viewedPly(), sanitizeBoardShapes(shapes));
    }

    function showDialog(dialog) {
      if (!dialog) return;
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    }

    function closeDialog(dialog) {
      if (!dialog) return;
      if (typeof dialog.close === 'function') dialog.close();
      else dialog.removeAttribute('open');
    }

    function suspendPollingForPromotion() {
      if (pollTimer) root.clearTimeout(pollTimer);
      pollTimer = null;
      pollGeneration += 1;
    }

    function settlePromotion(dialog, value) {
      const resolver = promotionResolver;
      promotionResolver = null;
      closeDialog(dialog);
      if (!resolver) return;
      resolver(value);
      schedulePoll();
    }

    function configureRulesDialog() {
      const dialog = byId('rulesDialog');
      const open = () => showDialog(dialog);
      byId('setupRulesButton').addEventListener('click', open);
      byId('rulesButton').addEventListener('click', open);
      byId('closeRulesButton').addEventListener('click', () => closeDialog(dialog));
      dialog.addEventListener('click', event => {
        if (event.target === dialog) closeDialog(dialog);
      });
    }

    function configurePromotionDialog() {
      const dialog = byId('promotionDialog');
      const cancel = function () {
        settlePromotion(dialog, null);
      };
      byId('cancelPromotionButton').addEventListener('click', cancel);
      dialog.addEventListener('cancel', event => {
        event.preventDefault();
        cancel();
      });
      dialog.querySelectorAll('[data-promotion]').forEach(button => {
        button.addEventListener('click', function () {
          settlePromotion(dialog, button.dataset.promotion);
        });
      });
    }

    function choosePromotion() {
      if (promotionResolver) return Promise.resolve(null);
      suspendPollingForPromotion();
      return new Promise(resolve => {
        promotionResolver = resolve;
        showDialog(byId('promotionDialog'));
      });
    }

    function readStoredSetupPreferences() {
      try {
        const raw = root.localStorage.getItem(PLAY_SETUP_STORAGE_KEY);
        if (!raw) return sanitizeSetupPreferences(null);
        const sanitized = sanitizeSetupPreferences(JSON.parse(raw));
        root.localStorage.setItem(PLAY_SETUP_STORAGE_KEY, JSON.stringify(sanitized));
        return sanitized;
      } catch (error) {
        try {
          root.localStorage.removeItem(PLAY_SETUP_STORAGE_KEY);
        } catch (removeError) {}
        return sanitizeSetupPreferences(null);
      }
    }

    function restoreSetup() {
      byId('displayName').value = '';
      const stored = readStoredSetupPreferences();
      const color = document.querySelector('input[name="color"][value="' + stored.color + '"]');
      if (color) color.checked = true;
    }

    function saveSetup(body) {
      try {
        root.localStorage.setItem(
          PLAY_SETUP_STORAGE_KEY,
          JSON.stringify(sanitizeSetupPreferences(body)),
        );
      } catch (error) {}
    }

    async function loadConfig() {
      const payload = await requestJson('/api/play/config');
      config = payload && payload.config
        ? Object.assign({}, payload.config, {
            csrf_token: payload.config.csrf_token || payload.csrf_token,
            active_game_id: payload.config.active_game_id || payload.active_game_id,
            models: payload.config.models || payload.models,
            default_model_id:
              payload.config.default_model_id || payload.default_model_id,
          })
        : payload || {};
      const presets = normalizedOptions(config.presets);
      const clocks = normalizedOptions(config.clocks);
      const models = normalizedOptions(config.models);
      if (!config.csrf_token) throw new ApiError('The game server did not provide a session token.', 0, null);
      if (!presets.length) throw new ApiError('No engine strengths are available.', 0, null);
      if (!clocks.length) throw new ApiError('No clock choices are available.', 0, null);
      availablePresets = presets;
      availableClocks = clocks;
      availableModels = models;
      renderPresets(presets);
      renderClocks(clocks);
      renderModels(models, config.default_model_id);
      syncAdvancedDefaults();
      syncAdvancedAvailability();
      byId('createGameButton').disabled = false;
      return config;
    }

    function renderPresets(presets) {
      const select = byId('presetSelect');
      select.replaceChildren();
      presets.forEach((preset, index) => {
        const option = document.createElement('option');
        option.value = preset.id;
        option.textContent = preset.label;
        option.dataset.description = preset.description;
        if (preset.nodes !== undefined) option.dataset.nodes = String(preset.nodes);
        if (preset.temperature !== undefined) {
          option.dataset.temperature = String(preset.temperature);
        }
        if (preset.temp_decay_moves !== undefined) {
          option.dataset.tempDecayMoves = String(preset.temp_decay_moves);
        }
        select.appendChild(option);
        if (index === 0) option.selected = true;
      });
      select.disabled = false;
      syncPresetHelp();
      const stored = readStoredSetupPreferences();
      if (presets.some(preset => preset.id === stored.preset_id)) {
        select.value = stored.preset_id;
        syncPresetHelp();
      }
    }

    function syncPresetHelp() {
      const selected = byId('presetSelect').selectedOptions[0];
      if (!selected) {
        byId('presetHelp').textContent = 'Visits limit how much the model searches before moving.';
        return;
      }
      const details = [];
      if (selected.dataset.nodes) details.push(selected.dataset.nodes + ' visits');
      if (selected.dataset.temperature) {
        details.push('temperature ' + selected.dataset.temperature);
      }
      if (selected.dataset.tempDecayMoves) {
        details.push('decay ' + selected.dataset.tempDecayMoves + ' moves');
      }
      const description = selected.dataset.description || '';
      byId('presetHelp').textContent = [description, details.join(' · ')]
        .filter(Boolean)
        .join(' · ') || 'Visits limit how much the model searches before moving.';
    }

    function clockSubLabel(clock) {
      if (clock.description) return clock.description;
      if (clock.initial_ms === null) return '';
      const minutes = Math.round(clock.initial_ms / 60_000);
      const increment = Math.round((clock.increment_ms || 0) / 1000);
      return increment ? minutes + ' min · +' + increment + ' sec' : minutes + ' minutes';
    }

    function renderClocks(clocks) {
      const wrap = byId('clockChoices');
      wrap.replaceChildren();
      const storedClock = readStoredSetupPreferences().clock_id;
      clocks.forEach((clock, index) => {
        const item = document.createElement('div');
        item.className = 'choice';
        const input = document.createElement('input');
        input.type = 'radio';
        input.name = 'clock_id';
        input.id = 'clockChoice' + index;
        input.value = clock.id;
        input.checked = storedClock ? storedClock === clock.id : index === 0;
        const label = document.createElement('label');
        label.htmlFor = input.id;
        label.append(document.createTextNode(clock.label));
        const subLabel = clockSubLabel(clock);
        if (subLabel) {
          const small = document.createElement('small');
          small.textContent = subLabel;
          label.appendChild(small);
        }
        item.append(input, label);
        wrap.appendChild(item);
      });
      if (!wrap.querySelector('input:checked') && wrap.querySelector('input')) {
        wrap.querySelector('input').checked = true;
      }
    }

    function renderModels(models, defaultModelId) {
      const select = byId('advancedModelSelect');
      select.replaceChildren();
      models.forEach(model => {
        const option = document.createElement('option');
        option.value = model.id;
        option.textContent = model.label;
        select.appendChild(option);
      });
      const requestedDefault = String(defaultModelId || '');
      if (models.some(model => model.id === requestedDefault)) {
        select.value = requestedDefault;
      }
      byId('advancedEnabled').disabled = models.length === 0;
    }

    function selectedClockOption() {
      const selected = document.querySelector('input[name="clock_id"]:checked');
      return availableClocks.find(clock => clock.id === (selected && selected.value)) ||
        availableClocks[0] ||
        null;
    }

    function selectedPresetOption() {
      return availablePresets.find(preset => preset.id === byId('presetSelect').value) ||
        availablePresets[0] ||
        null;
    }

    function displayDecimal(value) {
      const rounded = Math.round(Number(value) * 10_000) / 10_000;
      return Number.isFinite(rounded) ? String(rounded) : '';
    }

    function syncAdvancedDefaults() {
      const preset = selectedPresetOption();
      const clock = selectedClockOption();
      if (preset) {
        byId('advancedNodes').value = String(
          Number.isFinite(preset.nodes) ? preset.nodes : 1,
        );
        byId('advancedTemperature').value = String(
          Number.isFinite(preset.temperature) ? preset.temperature : 0,
        );
        byId('advancedTempDecay').value = String(
          Number.isFinite(preset.temp_decay_moves) ? preset.temp_decay_moves : 0,
        );
      }
      byId('advancedStartFen').value = 'startpos';
      if (
        clock &&
        clock.initial_ms !== null &&
        clock.initial_ms !== undefined
      ) {
        byId('advancedClockMode').value = 'timed';
        byId('advancedInitialMinutes').value = displayDecimal(clock.initial_ms / 60_000);
        byId('advancedIncrementSeconds').value = displayDecimal(
          (clock.increment_ms || 0) / 1000,
        );
      } else {
        byId('advancedClockMode').value = 'unlimited';
      }
      advancedInitialized = true;
      syncAdvancedAvailability();
    }

    function syncAdvancedAvailability() {
      const enabled = byId('advancedEnabled').checked && availableModels.length > 0;
      const timed = byId('advancedClockMode').value === 'timed';
      byId('advancedSettings').classList.toggle('is-enabled', enabled);
      for (const id of [
        'advancedModelSelect',
        'advancedNodes',
        'advancedTemperature',
        'advancedTempDecay',
        'advancedStartFen',
        'advancedClockMode',
      ]) {
        byId(id).disabled = !enabled;
      }
      byId('advancedClockFields').hidden = !timed;
      byId('advancedInitialMinutes').disabled = !enabled || !timed;
      byId('advancedIncrementSeconds').disabled = !enabled || !timed;
    }

    function setupBody() {
      const form = byId('setupForm');
      const formData = new root.FormData(form);
      const displayName = String(formData.get('display_name') || '').trim();
      const body = {
        display_name: displayName,
        publish_name: Boolean(displayName),
        color: String(formData.get('color') || 'random'),
        preset_id: String(formData.get('preset_id') || ''),
        clock_id: String(formData.get('clock_id') || ''),
      };
      if (byId('advancedEnabled').checked) {
        if (!advancedInitialized) syncAdvancedDefaults();
        body.advanced = buildAdvancedOptions({
          model_id: byId('advancedModelSelect').value,
          nodes: byId('advancedNodes').value,
          temperature: byId('advancedTemperature').value,
          temp_decay_moves: byId('advancedTempDecay').value,
          start_fen: byId('advancedStartFen').value,
          clock_mode: byId('advancedClockMode').value,
          initial_minutes: byId('advancedInitialMinutes').value,
          increment_seconds: byId('advancedIncrementSeconds').value,
        });
        if (!availableModels.some(model => model.id === body.advanced.model_id)) {
          throw new RangeError('Choose an available model.');
        }
      }
      return body;
    }

    async function createGame(event) {
      event.preventDefault();
      let body;
      try {
        body = setupBody();
      } catch (error) {
        setMessage(setupMessage, error.message || 'Check the advanced settings.', 'error');
        return;
      }
      if (!body.preset_id || !body.clock_id) {
        setMessage(setupMessage, 'Choose an engine strength and clock.', 'error');
        return;
      }
      const signature = JSON.stringify(body);
      if (!createAttempt || createAttempt.signature !== signature) {
        createAttempt = { signature, key: idempotencyKey() };
      }
      const button = byId('createGameButton');
      button.disabled = true;
      setMessage(setupMessage, 'Creating your game…');
      try {
        const payload = await requestWithNetworkRetry('/api/play/games', {
          method: 'POST',
          headers: {
            'X-CSRF-Token': config.csrf_token,
            'Idempotency-Key': createAttempt.key,
          },
          json: body,
        });
        const created = unwrapGame(payload);
        if (!created || !created.id) throw new ApiError('The server created no playable game.', 0, payload);
        saveSetup(body);
        createAttempt = null;
        gameId = String(created.id);
        if (root.history && root.history.replaceState) {
          root.history.replaceState({}, '', '/play/' + encodeURIComponent(gameId));
        }
        applyGame(created);
      } catch (error) {
        setMessage(setupMessage, error.message || 'Could not create the game.', 'error');
      } finally {
        button.disabled = !config;
      }
    }

    async function loadGame(options) {
      if (!gameId) return false;
      const requestOptions = options || {};
      const silent = !!requestOptions.silent;
      const allowBusy = !!requestOptions.allowBusy;
      if ((pending || boardAnimating || promotionResolver) && !allowBusy) return false;
      const requestId = ++gameRequestGeneration;
      const requestMutationGeneration = mutationGeneration;
      const allowedMutationGeneration = allowBusy
        ? requestOptions.mutationGeneration
        : null;
      if (allowBusy && allowedMutationGeneration !== requestMutationGeneration) return false;
      if (!silent) {
        setupView.hidden = true;
        gameView.hidden = true;
        errorView.hidden = true;
      }
      try {
        const payload = await requestJson('/api/play/games/' + encodeURIComponent(gameId));
        const loaded = unwrapGame(payload);
        if (!loaded || !loaded.id) throw new ApiError('The game response was incomplete.', 0, payload);
        if (!shouldApplyGameResponse({
          requestMutationGeneration,
          currentMutationGeneration: mutationGeneration,
          requestId,
          latestAppliedRequestId: latestAppliedGameRequest,
          pending,
          boardAnimating,
          promotionPending: !!promotionResolver,
          allowBusy,
          allowedMutationGeneration,
        })) return false;
        latestAppliedGameRequest = Math.max(latestAppliedGameRequest, requestId);
        return applyGame(loaded, requestOptions.applyOptions) !== false;
      } catch (error) {
        if (!shouldApplyGameResponse({
          requestMutationGeneration,
          currentMutationGeneration: mutationGeneration,
          requestId,
          latestAppliedRequestId: latestAppliedGameRequest,
          pending,
          boardAnimating,
          promotionPending: !!promotionResolver,
          allowBusy,
          allowedMutationGeneration,
        })) return false;
        latestAppliedGameRequest = Math.max(latestAppliedGameRequest, requestId);
        if (silent) {
          if ([401, 403, 404, 410].includes(error.status)) {
            if (pollTimer) root.clearTimeout(pollTimer);
            pollTimer = null;
            document.body.classList.remove('active-game');
            gameView.hidden = true;
            errorView.hidden = false;
            byId('gameErrorText').textContent = error.message || 'This game is no longer available.';
            return false;
          }
          if (!pending && !boardAnimating) renderGame();
          schedulePoll(1800);
          return false;
        }
        document.body.classList.remove('active-game');
        setupView.hidden = true;
        gameView.hidden = true;
        errorView.hidden = false;
        byId('gameErrorText').textContent = error.message || 'The link may be invalid or no longer available.';
        return false;
      }
    }

    function invalidateBoardAnimation() {
      boardAnimationGeneration++;
    }

    function applyGame(nextGame, options) {
      const previousGame = game;
      const previousCommandPending = hasPendingCommand(previousGame);
      const currentRevision = numericValue(game && game.revision);
      const nextRevision = numericValue(nextGame && nextGame.revision);
      if (currentRevision !== null && nextRevision !== null && nextRevision < currentRevision) return false;
      if (hasSameGameRevision(previousGame, nextGame)) {
        game = nextGame;
        gameId = String(game.id || gameId);
        receivedAt = monotonicNow();
        pending = false;
        renderGame({ skipBoard: true });
        schedulePoll();
        return true;
      }
      if (!previousGame || String(previousGame.id || '') !== String(nextGame.id || '')) {
        viewPly = null;
        manualShapesByPly = new Map();
      }
      disarmResign(false);
      const animationPlan = observedMoveAnimationPlan(
        previousGame,
        nextGame,
        options && options.optimisticMoveCount,
      );
      const seedInitialBoard =
        !previousGame &&
        !board &&
        animationSpeed > 0 &&
        isInitialEngineOpening(nextGame, analysisState, ChessConstructor);

      document.body.classList.add('active-game');
      setupView.hidden = true;
      errorView.hidden = true;
      gameView.hidden = false;

      if (seedInitialBoard) {
        const initialFen = normalizeFen(nextGame.initial_fen || 'startpos');
        const seedGame = Object.assign({}, nextGame, {
          moves: [],
          current_fen: initialFen,
        });
        delete seedGame.fen;
        if (seedGame.position && typeof seedGame.position === 'object') {
          seedGame.position = Object.assign({}, seedGame.position);
          delete seedGame.position.fen;
        }
        game = seedGame;
        gameView.getBoundingClientRect();
        ensureBoard();
      }
      const animateObservedMovesInOrder =
        !!board &&
        animationSpeed > 0 &&
        animationPlan.moveCount > 0;
      const animateDirectBoardChange =
        !!previousGame &&
        !!board &&
        animationSpeed > 0 &&
        animationPlan.moveCount === 0 &&
        animationPlan.skippedMoveCount === 0 &&
        boardPlacement(previousGame, analysisState, ChessConstructor) !==
          boardPlacement(nextGame, analysisState, ChessConstructor);
      const shouldAnimate = animateObservedMovesInOrder || animateDirectBoardChange;
      const shouldAnimateLivePosition = viewPly === null && shouldAnimate;
      if (!shouldAnimateLivePosition) invalidateBoardAnimation();
      game = nextGame;
      gameId = String(game.id || gameId);
      if (viewPly !== null && viewPly >= replayLine().rawMoves.length) {
        viewPly = null;
      }
      receivedAt = monotonicNow();
      pending = false;
      if (hasPendingCommand(game)) {
        pendingCommandPollAttempt = previousCommandPending
          ? Math.min(
              pendingCommandPollAttempt + 1,
              PENDING_COMMAND_POLL_DELAYS_MS.length - 1,
            )
          : 0;
      } else {
        pendingCommandPollAttempt = 0;
      }
      if (isMidDouble(game)) abilityArmed = false;
      keyboardSquare = null;
      keyboardFrom = null;
      if (board) board.selectSquare(null);
      boardAnimating = shouldAnimateLivePosition;
      renderGame({ skipBoard: shouldAnimateLivePosition });
      if (shouldAnimateLivePosition && animateObservedMovesInOrder) {
        animateObservedMoves(animationPlan.startMoveCount);
      } else if (shouldAnimateLivePosition && animateDirectBoardChange) {
        animateCurrentPosition();
      }
      schedulePoll();
      return true;
    }

    function interactionMoves() {
      if (
        !game ||
        pending ||
        boardAnimating ||
        promotionResolver ||
        hasPendingCommand(game)
      ) return [];
      const moves = legalMoves(game);
      if (isMidDouble(game)) return moves;
      if (abilityArmed) return moves.filter(move => move.endsWith('+'));
      return moves.filter(move => !move.endsWith('+'));
    }

    function humanTurn() {
      if (
        !game ||
        isGameComplete(game) ||
        pending ||
        boardAnimating ||
        promotionResolver ||
        hasPendingCommand(game)
      ) return false;
      const human = normalizeColor(game.human_color);
      const turn = normalizeColor(game.to_move);
      return !!human && human === turn;
    }

    function boardCanMove() {
      return atLivePosition() && humanTurn() && !!interactionMoves().length;
    }

    function replayLine() {
      return deriveReplay(game, analysisState, ChessConstructor);
    }

    function livePly() {
      return replayLine().rawMoves.length;
    }

    function viewedPly(line) {
      const replay = line || replayLine();
      const total = replay.rawMoves.length;
      if (viewPly === null) return total;
      return Math.max(0, Math.min(total, Number(viewPly) || 0));
    }

    function atLivePosition() {
      return viewPly === null;
    }

    function setViewPly(ply) {
      if (!game || boardAnimating || promotionResolver) return;
      const line = replayLine();
      const total = line.rawMoves.length;
      const next = Math.max(0, Math.min(total, Number(ply) || 0));
      viewPly = next >= total ? null : next;
      clearKeyboardSelection();
      renderGame();
    }

    function stepViewPly(delta) {
      setViewPly(viewedPly() + delta);
    }

    function currentDoubleMoveFlag() {
      if (!game) return false;
      const line = replayLine();
      const index = viewedPly(line) - 1;
      return index >= 0 && line.doubleFlags && line.doubleFlags[index] === true;
    }

    function boardHighlightMap(lastMove, doubleMove) {
      const highlights = new Map();
      const line = replayLine();
      const index = viewedPly(line) - 1;
      const rawMove = index >= 0 ? line.rawMoves[index] : '';
      const squares = Array.isArray(lastMove)
        ? lastMove
        : rawMove
          ? [rawMove.slice(0, 2), rawMove.slice(2, 4)]
          : [];
      const isDouble = typeof doubleMove === 'boolean'
        ? doubleMove
        : currentDoubleMoveFlag();
      if (isDouble && squares.length === 2) {
        highlights.set(squares[0], 'double-highlight');
        highlights.set(squares[1], 'double-highlight');
      }
      if (keyboardSquare) highlights.set(keyboardSquare, 'keyboard-cursor');
      if (keyboardFrom) highlights.set(keyboardFrom, 'keyboard-origin');
      return highlights;
    }

    function boardConfig(options) {
      const requested = options || {};
      const line = replayLine();
      const ply = viewedPly(line);
      const fen = requested.fen || line.fens[ply] ||
        currentFen(game, analysisState, ChessConstructor);
      const orientation = normalizeColor(game && game.human_color) === 'b' ? 'black' : 'white';
      const interactive = requested.interactive !== false && boardCanMove();
      const moves = interactive ? interactionMoves() : [];
      const lastMove = Object.prototype.hasOwnProperty.call(requested, 'lastMove')
        ? requested.lastMove
        : ply > 0
          ? [line.rawMoves[ply - 1].slice(0, 2), line.rawMoves[ply - 1].slice(2, 4)]
          : [];
      const doubleMove = Object.prototype.hasOwnProperty.call(requested, 'doubleMove')
        ? requested.doubleMove === true
        : currentDoubleMoveFlag();
      const turn = normalizeColor(fen.split(/\s+/)[1]) || normalizeColor(game && game.to_move) || 'w';
      return {
        fen,
        orientation,
        turnColor: chessgroundColor(turn),
        coordinates: true,
        coordinatesOnSquares: false,
        ranksPosition: 'left',
        viewOnly: false,
        autoCastle: true,
        lastMove: lastMove && lastMove.length ? lastMove : null,
        highlight: {
          lastMove: Boolean(lastMove && lastMove.length) && !doubleMove,
          check: false,
          custom: boardHighlightMap(lastMove, doubleMove),
        },
        animation: {
          enabled: requested.animate !== false && animationSpeed > 0,
          duration: BOARD_ANIMATION_MS,
        },
        movable: {
          free: false,
          color: interactive ? chessgroundColor(game.human_color) : undefined,
          dests: interactive ? chessgroundDests(moves) : new Map(),
          showDests: true,
          events: { after: handleGroundMove },
        },
        premovable: { enabled: false },
        draggable: {
          enabled: interactive,
          distance: 3,
          autoDistance: true,
          showGhost: true,
          deleteOnDropOff: false,
        },
        selectable: { enabled: interactive },
        drawable: {
          enabled: true,
          visible: true,
          defaultSnapToValidMove: true,
          eraseOnMovablePieceClick: false,
          shapes: sanitizeBoardShapes(manualShapesByPly.get(ply) || []),
          onChange: handleBoardShapesChanged,
        },
      };
    }

    function ensureBoard() {
      if (board || !root.Chessground) return;
      const config = boardConfig({ animate: false });
      boardOrientation = config.orientation;
      board = root.Chessground(byId('playBoard'), config);
      const boardElement = byId('playBoard');
      boardElement.addEventListener('keydown', handleBoardKey);
      syncBoardAccessibility();
    }

    function updateBoard(animate) {
      ensureBoard();
      if (!board) {
        setMessage(gameMessage, 'The interactive board could not be loaded.', 'error');
        return;
      }
      const config = boardConfig({ animate: animate !== false });
      if (
        typeof board.getFen === 'function' &&
        board.getFen() === String(config.fen || '').split(/\s+/)[0]
      ) {
        delete config.fen;
      }
      boardOrientation = config.orientation;
      board.set(config);
      syncBoardAccessibility();
    }

    function syncBoardAccessibility() {
      const boardElement = byId('playBoard');
      if (!boardElement || !game) return;
      const line = replayLine();
      const ply = viewedPly(line);
      const turn = colorName((line.fens[ply] || '').split(/\s+/)[1] || game.to_move);
      const phase = atLivePosition() && isMidDouble(game)
        ? ' Complete the second move.'
        : '';
      const review = atLivePosition()
        ? ''
        : ' Reviewing move ' + ply + ' of ' + line.rawMoves.length + '.';
      const cursor = keyboardSquare ? ' Keyboard cursor ' + keyboardSquare + '.' : '';
      const selected = keyboardFrom ? ' ' + keyboardFrom + ' selected as the move origin.' : '';
      boardElement.setAttribute(
        'aria-label',
        'Double Move Chess board. ' + turn + ' to move.' + phase + review + cursor + selected,
      );
    }

    function initialKeyboardSquare() {
      const moves = interactionMoves();
      return moves.length ? moves[0].slice(0, 2) : (boardOrientation === 'black' ? 'h8' : 'a1');
    }

    function setKeyboardSquare(square) {
      keyboardSquare = square;
      if (board) board.set({ highlight: { custom: boardHighlightMap() } });
      syncBoardAccessibility();
    }

    function clearKeyboardSelection() {
      keyboardFrom = null;
      keyboardSquare = null;
      if (board) {
        board.selectSquare(null);
        board.set({ highlight: { custom: boardHighlightMap() } });
      }
      syncBoardAccessibility();
    }

    function isTextEditingTarget(target) {
      if (!target || typeof target.closest !== 'function') return false;
      if (
        target.closest(
          'textarea, [contenteditable]:not([contenteditable="false"])',
        )
      ) {
        return true;
      }
      const input = target.closest('input');
      if (!input) return false;
      return ![
        'button',
        'checkbox',
        'color',
        'file',
        'hidden',
        'image',
        'radio',
        'range',
        'reset',
        'submit',
      ].includes(String(input.type || 'text').toLowerCase());
    }

    function handleMoveNavigationKey(event) {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      if (
        !game ||
        event.defaultPrevented ||
        event.isComposing ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        event.shiftKey ||
        document.querySelector('dialog[open]') ||
        isTextEditingTarget(event.target)
      ) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      stepViewPly(event.key === 'ArrowLeft' ? -1 : 1);
    }

    function handleBoardKey(event) {
      if (!event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
        if (event.key === 'Home') {
          event.preventDefault();
          setViewPly(0);
          return;
        }
        if (event.key === 'End') {
          event.preventDefault();
          setViewPly(livePly());
          return;
        }
      }
      const navigation = {
        ArrowLeft: [-1, 0],
        ArrowRight: [1, 0],
        ArrowUp: [0, 1],
        ArrowDown: [0, -1],
      };
      if (navigation[event.key] && event.shiftKey && boardCanMove()) {
        event.preventDefault();
        event.stopPropagation();
        const current = keyboardSquare || initialKeyboardSquare();
        const direction = boardOrientation === 'black' ? -1 : 1;
        const file = Math.max(
          0,
          Math.min(7, 'abcdefgh'.indexOf(current[0]) + navigation[event.key][0] * direction),
        );
        const rank = Math.max(
          1,
          Math.min(8, Number(current[1]) + navigation[event.key][1] * direction),
        );
        setKeyboardSquare('abcdefgh'[file] + String(rank));
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        clearKeyboardSelection();
        setMessage(gameMessage, 'Keyboard move cancelled.');
        return;
      }
      if ((event.key !== 'Enter' && event.key !== ' ') || !boardCanMove()) return;
      event.preventDefault();
      const square = keyboardSquare || initialKeyboardSquare();
      setKeyboardSquare(square);
      const moves = interactionMoves();
      if (!keyboardFrom) {
        if (!moves.some(move => move.slice(0, 2) === square)) {
          setMessage(gameMessage, square + ' has no available move.', 'error');
          return;
        }
        keyboardFrom = square;
        board.selectSquare(square);
        board.set({ highlight: { custom: boardHighlightMap() } });
        syncBoardAccessibility();
        setMessage(gameMessage, square + ' selected.');
        return;
      }
      if (!matchingMoveTokens(moves, keyboardFrom, square).length) {
        setMessage(gameMessage, keyboardFrom + ' to ' + square + ' is not available.', 'error');
        return;
      }
      keyboardFrom = null;
      keyboardSquare = null;
      board.selectSquare(square);
      syncBoardAccessibility();
    }

    function handleGroundMove(source, target) {
      keyboardFrom = null;
      keyboardSquare = null;
      syncBoardAccessibility();
      if (!boardCanMove()) {
        updateBoard(false);
        return;
      }
      submitMoveIntent(source, target);
    }

    async function submitMoveIntent(source, target) {
      if (!boardCanMove()) return;
      let candidates = matchingMoveTokens(interactionMoves(), source, target);
      if (!candidates.length) {
        setMessage(gameMessage, 'That move is not available in this position.', 'error');
        updateBoard(false);
        return;
      }
      const intentGameId = gameId;
      const intentRevision = game.revision;
      const promotions = Array.from(new Set(
        candidates
          .map(move => move.replace(/\+$/, ''))
          .filter(move => move.length === 5)
          .map(move => move.charAt(4)),
      ));
      if (promotions.length > 1) {
        const choice = await choosePromotion();
        if (!choice) {
          updateBoard(false);
          renderAbility();
          return;
        }
        if (
          intentGameId !== gameId ||
          String(intentRevision) !== String(game && game.revision)
        ) {
          updateBoard(false);
          setMessage(gameMessage, 'The position changed before promotion was chosen.', 'error');
          return;
        }
        candidates = candidates.filter(move => move.replace(/\+$/, '').charAt(4) === choice);
      }
      const move = candidates[0];
      if (!move) {
        updateBoard(false);
        return;
      }
      await mutateGame(
        '/moves',
        { move, expected_revision: game.revision },
        'Sending move…',
        { preserveBoard: true },
      );
    }

    async function mutationRequest(path, body, key) {
      if (!config || !config.csrf_token) await loadConfig();
      return requestWithNetworkRetry(
        '/api/play/games/' + encodeURIComponent(gameId) + path,
        {
          method: 'POST',
          headers: {
            'X-CSRF-Token': config.csrf_token,
            'Idempotency-Key': key,
          },
          json: body,
        },
      );
    }

    async function mutateGame(path, body, pendingText, options) {
      if (!canStartGameMutation(game, pending, boardAnimating)) return;
      const generation = ++mutationGeneration;
      invalidateBoardAnimation();
      pending = true;
      clearKeyboardSelection();
      setMessage(gameMessage, pendingText || 'Updating game…');
      renderGame({ skipBoard: !!(options && options.preserveBoard) });
      const key = idempotencyKey();
      try {
        const payload = await mutationRequest(path, body, key);
        if (generation !== mutationGeneration) return;
        const updated = unwrapGame(payload);
        const applyOptions = {
          optimisticMoveCount: options && options.preserveBoard ? 1 : 0,
        };
        if (updated && updated.id) {
          if (applyGame(updated, applyOptions) === false) {
            pending = false;
            renderGame();
            setMessage(gameMessage, 'The game response was older than the current position.', 'error');
            return;
          }
        }
        else {
          const refreshed = await loadGame({
            silent: true,
            allowBusy: true,
            mutationGeneration: generation,
            applyOptions,
          });
          if (!refreshed && generation === mutationGeneration) {
            pending = false;
            renderGame();
            setMessage(gameMessage, 'The action completed, but the game could not be refreshed.', 'error');
            return;
          }
        }
        setMessage(gameMessage, '');
      } catch (error) {
        if (generation !== mutationGeneration) return;
        pending = false;
        if (error.status === 409 || error.status === 412) {
          setMessage(gameMessage, 'The game changed in another tab. Refreshing the board…');
          await loadGame({ silent: true });
        } else {
          setMessage(gameMessage, error.message || 'The action could not be completed.', 'error');
          renderGame();
        }
      }
    }

    function toggleAbility() {
      if (!atLivePosition() || !humanTurn() || isMidDouble(game)) return;
      const plusMoves = legalMoves(game).filter(move => move.endsWith('+'));
      if (!plusMoves.length) return;
      abilityArmed = !abilityArmed;
      clearKeyboardSelection();
      renderAbility();
      updateBoard(false);
      setMessage(
        gameMessage,
        abilityArmed ? 'Double move armed. Choose the first move.' : '',
        abilityArmed ? 'ok' : '',
      );
    }

    function renderStatus() {
      const title = byId('turnTitle');
      const detail = byId('turnDetail');
      const thinking = byId('thinkingIndicator');
      thinking.hidden = true;
      if (!atLivePosition()) {
        const line = replayLine();
        title.textContent = 'Reviewing move ' + viewedPly(line) + ' of ' + line.rawMoves.length;
        detail.textContent = 'Return to the live position to continue playing.';
        return;
      }
      if (isGameComplete(game)) {
        title.textContent = 'Game complete';
        detail.textContent = terminationDisplay(game.termination);
        return;
      }
      if (pending) {
        title.textContent = 'Saving move…';
        detail.textContent = '';
        thinking.hidden = false;
        return;
      }
      if (hasPendingCommand(game)) {
        title.textContent = 'Restoring game…';
        detail.textContent = '';
        thinking.hidden = false;
        return;
      }
      if (boardAnimating) {
        title.textContent = 'Playing move…';
        detail.textContent = '';
        thinking.hidden = false;
        return;
      }
      if (engineIsThinking(game)) {
        title.textContent = isMidDouble(game) ? 'Engine’s second move' : 'Engine is thinking';
        detail.textContent = '';
        thinking.hidden = false;
        return;
      }
      if (humanTurn()) {
        if (isMidDouble(game)) {
          title.textContent = 'Make your second move';
          detail.textContent = 'Complete the double move before your turn ends.';
        } else {
          title.textContent = 'Your move';
          detail.textContent = '';
        }
        return;
      }
      title.textContent = colorName(game.to_move) + ' to move';
      detail.textContent = '';
    }

    function renderPlayer(slot, color) {
      const strip = byId(slot + 'Player');
      byId(slot + 'PlayerName').textContent = playerName(game, color);
      byId(slot + 'PlayerMeta').textContent = playerMeta(game, color);
      strip.dataset.color = color;
      strip.classList.toggle('active', !isGameComplete(game) && runningClockColor(game) === color);
    }

    function renderPlayers() {
      const human = normalizeColor(game.human_color) || 'w';
      const bottom = human;
      const top = human === 'w' ? 'b' : 'w';
      renderPlayer('top', top);
      renderPlayer('bottom', bottom);
      updateClocks();
    }

    function updateClockNode(slot, color) {
      const node = byId(slot + 'Clock');
      let display;
      let milliseconds = null;
      if (clockIsUnlimited(game)) {
        display = '∞';
        node.removeAttribute('datetime');
      } else {
        milliseconds = adjustedClockMs(game, color, receivedAt, monotonicNow());
        display = formatClock(milliseconds);
        node.setAttribute('datetime', 'PT' + Math.max(0, Math.round((milliseconds || 0) / 1000)) + 'S');
      }
      node.textContent = display;
      const low = milliseconds !== null && milliseconds < 20_000 && !isGameComplete(game);
      node.classList.toggle('low', low);
      node.setAttribute(
        'aria-label',
        colorName(color) + ' clock, ' +
          (display === '∞' ? 'unlimited' : display) +
          (low ? ', low time' : ''),
      );
    }

    function updateClocks() {
      if (!game) return;
      const human = normalizeColor(game.human_color) || 'w';
      updateClockNode('bottom', human);
      updateClockNode('top', human === 'w' ? 'b' : 'w');
    }

    function renderAbility() {
      const button = byId('abilityButton');
      const detail = byId('abilityDetail');
      const badge = byId('abilityBadge');
      const available = abilityAvailable(game, game.human_color);
      const mid = isMidDouble(game);
      const canArm =
        atLivePosition() &&
        humanTurn() &&
        !mid &&
        legalMoves(game).some(move => move.endsWith('+'));
      if (!available || mid) abilityArmed = false;
      button.disabled = !canArm || pending;
      button.setAttribute('aria-pressed', abilityArmed ? 'true' : 'false');
      if (mid) {
        detail.textContent = 'Finish the second move now';
        badge.textContent = 'In use';
      } else if (!available) {
        detail.textContent = 'Already used in this game';
        badge.textContent = 'Spent';
      } else if (abilityArmed) {
        detail.textContent = 'Your next move starts the pair';
        badge.textContent = 'Armed';
      } else {
        detail.textContent = canArm ? 'Available once per game' : 'Available on your turn';
        badge.textContent = 'Ready';
      }
    }

    function appendMoveTokens(cell, moves) {
      moves.forEach(move => {
        const token = document.createElement('button');
        token.type = 'button';
        token.className = 'move-token' +
          (move.isDoubleStart || move.isSecondLeg ? ' is-double' : '');
        token.dataset.ply = String(move.index + 1);
        token.title = 'Review this position';
        token.setAttribute(
          'aria-label',
          'Review position after ' + move.san +
            (move.isDoubleStart
              ? ', first leg of a double move'
              : move.isSecondLeg
                ? ', second leg of a double move'
                : ''),
        );
        token.addEventListener('click', () => setViewPly(move.index + 1));
        const notation = document.createElement('strong');
        notation.textContent = move.san;
        token.appendChild(notation);
        cell.appendChild(token);
      });
    }

    function renderMoveRecord() {
      const list = byId('moveList');
      const line = replayLine();
      const activePly = viewedPly(line);
      list.replaceChildren();
      if (!line.rawMoves.length) {
        const row = document.createElement('li');
        row.className = 'move-pair empty';
        const index = document.createElement('span');
        index.className = 'move-index';
        index.textContent = '';
        const text = document.createElement('span');
        text.className = 'move-cell';
        text.textContent = 'No moves yet';
        row.append(index, text);
        list.appendChild(row);
        return;
      }
      groupMovePairs(line).forEach(group => {
        const row = document.createElement('li');
        row.className = 'move-pair';
        const number = document.createElement('span');
        number.className = 'move-index';
        number.textContent = group.moveNumber + '.';
        const white = document.createElement('span');
        white.className = 'move-cell white';
        white.setAttribute('aria-label', 'White moves');
        const black = document.createElement('span');
        black.className = 'move-cell black';
        black.setAttribute('aria-label', 'Black moves');
        appendMoveTokens(white, group.white);
        appendMoveTokens(black, group.black);
        row.append(number, white, black);
        list.appendChild(row);
      });
      const current = list.querySelector('[data-ply="' + activePly + '"]');
      if (current) current.classList.add('current');
      const record = byId('moveRecord');
      root.setTimeout(() => {
        if (viewPly === null) record.scrollTop = record.scrollHeight;
        else if (current) {
          const item = current.getBoundingClientRect();
          const viewport = record.getBoundingClientRect();
          if (item.top < viewport.top) {
            record.scrollTop -= viewport.top - item.top;
          } else if (item.bottom > viewport.bottom) {
            record.scrollTop += item.bottom - viewport.bottom;
          }
        }
      }, 0);
    }

    function renderHistoryNavigation() {
      const line = replayLine();
      const ply = viewedPly(line);
      const total = line.rawMoves.length;
      byId('playReplayCounter').textContent =
        ply + ' / ' + total + (viewPly === null ? ' · live' : '');
      byId('playStartButton').disabled = ply <= 0 || boardAnimating;
      byId('playBackButton').disabled = ply <= 0 || boardAnimating;
      byId('playForwardButton').disabled = ply >= total || boardAnimating;
      byId('playEndButton').disabled = viewPly === null || boardAnimating;
    }

    function outcomeTitle() {
      const result = String(game.result || '');
      const human = normalizeColor(game.human_color);
      if (result === '1/2-1/2' || result === '0.5-0.5' || result.toLowerCase() === 'draw') return 'Draw';
      if (result === '1-0') return human === 'w' ? 'You won' : 'Engine won';
      if (result === '0-1') return human === 'b' ? 'You won' : 'Engine won';
      return 'Game complete';
    }

    function renderResult() {
      const complete = isGameComplete(game);
      const card = byId('resultCard');
      card.hidden = !complete;
      if (!complete) return;
      byId('resultMark').textContent = resultDisplay(game.result);
      byId('resultTitle').textContent = outcomeTitle();
      const takebacks = Math.max(0, Number(game.takeback_count) || 0);
      byId('resultDetail').textContent = terminationDisplay(game.termination) +
        ' · ' + takebacks + ' takeback' + (takebacks === 1 ? '' : 's');
      const encoded = encodeURIComponent(gameId);
      byId('analyzeLink').href = '/analysis?game=' + encoded;
      byId('viewGameLink').href = '/games/' + encoded;
      byId('againLink').href = '/play';
    }

    function renderActions() {
      const active = !isGameComplete(game);
      byId('takebackButton').disabled =
        !active ||
        pending ||
        hasPendingCommand(game) ||
        boardAnimating ||
        !canRequestTakeback(game, analysisState, ChessConstructor);
      const resignButton = byId('resignButton');
      resignButton.disabled =
        !active ||
        pending ||
        hasPendingCommand(game) ||
        boardAnimating;
      if (resignButton.disabled) disarmResign(false);
      resignButton.classList.toggle('is-confirming', resignArmed);
      resignButton.setAttribute(
        'aria-label',
        resignArmed ? 'Press again to confirm resignation' : 'Resign game',
      );
      byId('resignLabel').textContent = resignArmed ? 'Press again' : 'Resign';
    }

    function disarmResign(refresh) {
      if (resignTimer !== null) root.clearTimeout(resignTimer);
      resignTimer = null;
      const changed = resignArmed;
      resignArmed = false;
      if (refresh !== false && changed && game) renderActions();
    }

    function requestResignation() {
      if (!game || isGameComplete(game) || pending || boardAnimating) return;
      if (!resignArmed) {
        resignArmed = true;
        resignTimer = root.setTimeout(() => disarmResign(true), 3000);
        renderActions();
        return;
      }
      disarmResign(false);
      mutateGame('/resign', { expected_revision: game.revision }, 'Resigning game…');
    }

    function renderGame(options) {
      if (!game) return;
      renderStatus();
      renderPlayers();
      renderAbility();
      renderMoveRecord();
      renderHistoryNavigation();
      renderActions();
      renderResult();
      if (!(options && options.skipBoard)) updateBoard();
    }

    function finishBoardAnimation(generation) {
      if (generation !== boardAnimationGeneration) return;
      boardAnimating = false;
      renderGame();
      schedulePoll();
    }

    function waitForBoardAnimation(generation) {
      if (
        generation !== boardAnimationGeneration ||
        !board ||
        animationSpeed <= 0 ||
        !board.state ||
        !board.state.animation ||
        !board.state.animation.current ||
        document.hidden
      ) return Promise.resolve();

      const maxWaitMs = Math.max(animationSpeed * 3, animationSpeed + 250);
      return new Promise(resolve => {
        let settled = false;
        let frameId = null;
        let observerTimer = null;
        let fallbackTimer = null;

        const finish = function () {
          if (settled) return;
          settled = true;
          if (frameId !== null && typeof root.cancelAnimationFrame === 'function') {
            root.cancelAnimationFrame(frameId);
          }
          if (observerTimer !== null) root.clearTimeout(observerTimer);
          if (fallbackTimer !== null) root.clearTimeout(fallbackTimer);
          document.removeEventListener('visibilitychange', handleVisibilityChange);
          resolve();
        };

        const observe = function () {
          if (
            generation !== boardAnimationGeneration ||
            document.hidden ||
            !board ||
            !board.state ||
            !board.state.animation ||
            !board.state.animation.current
          ) {
            finish();
            return;
          }
          if (typeof root.requestAnimationFrame === 'function') {
            frameId = root.requestAnimationFrame(observe);
          } else {
            observerTimer = root.setTimeout(observe, 16);
          }
        };

        const handleVisibilityChange = function () {
          if (document.hidden) finish();
        };

        document.addEventListener('visibilitychange', handleVisibilityChange);
        fallbackTimer = root.setTimeout(finish, maxWaitMs);
        observe();
      });
    }

    async function animateCurrentPosition(existingGeneration) {
      const generation = existingGeneration || ++boardAnimationGeneration;
      ensureBoard();
      if (!board) {
        finishBoardAnimation(generation);
        return;
      }
      board.set(boardConfig({
        fen: currentFen(game, analysisState, ChessConstructor),
        lastMove: lastMoveSquares(game),
        animate: true,
        interactive: false,
      }));
      await waitForBoardAnimation(generation);
      finishBoardAnimation(generation);
    }

    async function animateObservedMoves(previousMoveCount) {
      const generation = ++boardAnimationGeneration;
      const line = deriveReplay(game, analysisState, ChessConstructor);
      ensureBoard();
      if (!board) {
        finishBoardAnimation(generation);
        return;
      }
      if (!line.valid) {
        await animateCurrentPosition(generation);
        return;
      }
      const anchorIndex = Math.max(0, Math.min(previousMoveCount, line.fens.length - 1));
      const anchorMove = anchorIndex > 0 ? line.rawMoves[anchorIndex - 1] : null;
      board.set(boardConfig({
        fen: line.fens[anchorIndex],
        lastMove: anchorMove ? [anchorMove.slice(0, 2), anchorMove.slice(2, 4)] : [],
        doubleMove: anchorIndex > 0 && line.doubleFlags[anchorIndex - 1] === true,
        animate: false,
        interactive: false,
      }));
      for (let index = previousMoveCount + 1; index < line.fens.length; index++) {
        if (generation !== boardAnimationGeneration) return;
        const move = line.rawMoves[index - 1];
        board.set(boardConfig({
          fen: line.fens[index],
          lastMove: [move.slice(0, 2), move.slice(2, 4)],
          doubleMove: line.doubleFlags[index - 1] === true,
          animate: true,
          interactive: false,
        }));
        await waitForBoardAnimation(generation);
        if (generation !== boardAnimationGeneration) return;
      }
      finishBoardAnimation(generation);
    }

    function schedulePoll(delay) {
      if (pollTimer) root.clearTimeout(pollTimer);
      pollTimer = null;
      if (!game || isGameComplete(game) || promotionResolver) return;
      const generation = ++pollGeneration;
      const pendingCommandDelay = PENDING_COMMAND_POLL_DELAYS_MS[
        Math.min(
          pendingCommandPollAttempt,
          PENDING_COMMAND_POLL_DELAYS_MS.length - 1,
        )
      ];
      const interval = delay || (
        hasPendingCommand(game)
          ? pendingCommandDelay
          : engineIsThinking(game)
            ? 850
            : 5000
      );
      pollTimer = root.setTimeout(async function () {
        if (generation !== pollGeneration || pending || boardAnimating || promotionResolver) {
          schedulePoll(600);
          return;
        }
        await loadGame({ silent: true });
      }, interval);
    }

    byId('setupForm').addEventListener('submit', createGame);
    byId('presetSelect').addEventListener('change', function () {
      syncPresetHelp();
      if (!byId('advancedEnabled').checked) syncAdvancedDefaults();
    });
    byId('clockChoices').addEventListener('change', function () {
      if (!byId('advancedEnabled').checked) syncAdvancedDefaults();
    });
    byId('advancedSettings').addEventListener('toggle', function () {
      if (this.open && !advancedInitialized) syncAdvancedDefaults();
    });
    byId('advancedEnabled').addEventListener('change', function () {
      if (this.checked && !advancedInitialized) syncAdvancedDefaults();
      syncAdvancedAvailability();
    });
    byId('advancedClockMode').addEventListener('change', syncAdvancedAvailability);
    byId('abilityButton').addEventListener('click', toggleAbility);
    byId('playStartButton').addEventListener('click', () => setViewPly(0));
    byId('playBackButton').addEventListener('click', () => stepViewPly(-1));
    byId('playForwardButton').addEventListener('click', () => stepViewPly(1));
    byId('playEndButton').addEventListener('click', () => setViewPly(livePly()));
    document.addEventListener('keydown', handleMoveNavigationKey, true);
    byId('takebackButton').addEventListener('click', function () {
      mutateGame('/takeback', { expected_revision: game.revision }, 'Requesting takeback…');
    });
    byId('resignButton').addEventListener('click', requestResignation);
    byId('retryGameButton').addEventListener('click', function () {
      loadGame();
    });
    if (!root.Chessground) {
      root.addEventListener('chessground-ready', function () {
        if (game) updateBoard(false);
      }, { once: true });
    }
    document.addEventListener('visibilitychange', function () {
      if (
        !document.hidden &&
        game &&
        !isGameComplete(game) &&
        !pending &&
        !boardAnimating &&
        !promotionResolver
      ) loadGame({ silent: true });
    });

    configureRulesDialog();
    configurePromotionDialog();
    restoreSetup();
    root.setInterval(updateClocks, 100);

    const configPromise = loadConfig().catch(error => {
      setMessage(setupMessage, error.message || 'Could not load game choices.', 'error');
      byId('createGameButton').disabled = true;
      return null;
    });
    if (gameId) {
      setupView.hidden = true;
      Promise.allSettled([configPromise, loadGame()]);
    } else {
      configPromise.then(loadedConfig => {
        const target = resumeTarget(loadedConfig, gameId);
        if (!target || game) return;
        gameId = target.id;
        setupView.hidden = true;
        if (root.history && typeof root.history.replaceState === 'function') {
          root.history.replaceState({}, '', target.path);
        }
        return loadGame();
      });
    }
  }

  return {
    ApiError,
    START_FEN,
    MOVE_RE,
    PUBLIC_GAME_ID_RE,
    PLAY_SETUP_STORAGE_KEY,
    BOARD_ANIMATION_MS,
    PENDING_COMMAND_POLL_DELAYS_MS,
    normalizeColor,
    colorName,
    normalizeMoveToken,
    moveTokens,
    legalMoves,
    chessgroundDests,
    matchingMoveTokens,
    chessgroundColor,
    lastMoveSquares,
    newlyObservedMoves,
    observedMoveAnimationPlan,
    unwrapGame,
    gameIdFromPath,
    resumeTarget,
    sanitizeSetupPreferences,
    groupMovePairs,
    buildAdvancedOptions,
    shouldApplyGameResponse,
    hasSameGameRevision,
    isGameComplete,
    hasPendingCommand,
    canStartGameMutation,
    isMidDouble,
    abilityAvailable,
    playerFor,
    playerName,
    playerMeta,
    engineIsThinking,
    canRequestTakeback,
    isInitialEngineOpening,
    normalizeFen,
    deriveReplay,
    currentFen,
    boardPlacement,
    clockRemaining,
    runningClockColor,
    clockIsUnlimited,
    adjustedClockMs,
    formatClock,
    resultDisplay,
    humanOutcomeDisplay,
    temperatureDisplay,
    terminationDisplay,
    idempotencyKey,
    requestJson,
    requestWithNetworkRetry,
    normalizedOptions,
    initPublicChrome,
    initPlayPage,
  };
});
