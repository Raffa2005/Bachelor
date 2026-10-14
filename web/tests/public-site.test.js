'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const publicSite = require('../play.js');
const ruleExamples = require('../rules.js');
const analysisState = require('../analysis-state.js');
const { Chess } = require('../vendor/chess.js/chess-0.12.0.js');

test('route parsing only accepts one opaque game id segment', () => {
  assert.equal(publicSite.gameIdFromPath('/play/abc-123', 'play'), 'abc-123');
  assert.equal(publicSite.gameIdFromPath('/games/a%20b', 'games'), 'a b');
  assert.equal(publicSite.gameIdFromPath('/play', 'play'), null);
  assert.equal(publicSite.gameIdFromPath('/play/a/more', 'play'), null);
  assert.equal(publicSite.gameIdFromPath('/games/abc', 'play'), null);
});

test('bare play resumes only a valid server-provided active game', () => {
  const id = '0123456789abcdef0123456789abcdef';
  assert.deepEqual(publicSite.resumeTarget({ active_game_id: id }, null), {
    id,
    path: '/play/' + id,
  });
  assert.equal(publicSite.resumeTarget({ active_game_id: id }, 'existing-route-id'), null);
  assert.equal(publicSite.resumeTarget({ active_game_id: '../analysis' }, null), null);
  assert.equal(publicSite.resumeTarget({ active_game_id: 'ABCDEF' }, null), null);
  assert.equal(publicSite.resumeTarget({}, null), null);
});

test('setup persistence strips names, consent, and unrelated fields', () => {
  assert.deepEqual(publicSite.sanitizeSetupPreferences({
    display_name: 'Private name',
    publish_name: true,
    color: 'black',
    preset_id: 'friendly',
    clock_id: '5+3',
    csrf_token: 'secret',
  }), {
    color: 'black',
    preset_id: 'friendly',
    clock_id: '5+3',
  });
  assert.deepEqual(publicSite.sanitizeSetupPreferences({
    display_name: 'Private name',
    publish_name: true,
    color: 'invalid',
  }), {
    color: 'random',
    preset_id: '',
    clock_id: '',
  });
});

test('game response guard rejects GETs invalidated by mutation, animation, promotion, or newer GETs', () => {
  const current = {
    requestMutationGeneration: 4,
    currentMutationGeneration: 4,
    requestId: 9,
    latestAppliedRequestId: 8,
    pending: false,
    boardAnimating: false,
    promotionPending: false,
    allowBusy: false,
    allowedMutationGeneration: null,
  };
  assert.equal(publicSite.shouldApplyGameResponse(current), true);
  assert.equal(publicSite.shouldApplyGameResponse({
    ...current,
    currentMutationGeneration: 5,
  }), false);
  assert.equal(publicSite.shouldApplyGameResponse({
    ...current,
    pending: true,
  }), false);
  assert.equal(publicSite.shouldApplyGameResponse({
    ...current,
    boardAnimating: true,
  }), false);
  assert.equal(publicSite.shouldApplyGameResponse({
    ...current,
    promotionPending: true,
  }), false);
  assert.equal(publicSite.shouldApplyGameResponse({
    ...current,
    requestId: 7,
  }), false);
  assert.equal(publicSite.shouldApplyGameResponse({
    ...current,
    pending: true,
    allowBusy: true,
    allowedMutationGeneration: 4,
  }), true);
  assert.equal(publicSite.shouldApplyGameResponse({
    ...current,
    pending: true,
    allowBusy: true,
    allowedMutationGeneration: 3,
  }), false);
});

test('an unchanged game revision can refresh clocks without resetting the board selection', () => {
  assert.equal(
    publicSite.hasSameGameRevision({ revision: 7 }, { revision: 7 }),
    true,
  );
  assert.equal(
    publicSite.hasSameGameRevision({ revision: 7 }, { revision: 8 }),
    false,
  );
  assert.equal(
    publicSite.hasSameGameRevision({ revision: '7' }, { revision: 7 }),
    true,
  );
  assert.equal(
    publicSite.hasSameGameRevision({}, {}),
    false,
  );
});

test('server move fields are normalized and invalid tokens are discarded', () => {
  const state = {
    moves: ['e2e4+', { uci: 'e4e5' }, { move: 'bad' }],
    legal_moves: [{ move: 'g1f3' }, 'g1h3+', '<script>'],
  };
  assert.deepEqual(publicSite.moveTokens(state), ['e2e4+', 'e4e5']);
  assert.deepEqual(publicSite.legalMoves(state), ['g1f3', 'g1h3+']);
});

test('Chessground destinations preserve double-move pawn legs and promotion tokens', () => {
  const firstLeg = publicSite.chessgroundDests(['e2e3+', 'e2e4+', 'g1f3', 'bad']);
  assert.deepEqual(firstLeg.get('e2'), ['e3', 'e4']);
  assert.deepEqual(firstLeg.get('g1'), ['f3']);

  const secondLeg = publicSite.chessgroundDests(['e3e4', 'e3d4']);
  assert.deepEqual(secondLeg.get('e3'), ['e4', 'd4']);

  assert.deepEqual(
    publicSite.matchingMoveTokens(
      ['a7a8q+', 'a7a8r+', 'a7a8b+', 'a7a8n+', 'a7a6'],
      'a7',
      'a8',
    ),
    ['a7a8q+', 'a7a8r+', 'a7a8b+', 'a7a8n+'],
  );
  assert.deepEqual(publicSite.lastMoveSquares({ moves: ['e2e4+', 'e4e5'] }), ['e4', 'e5']);
  assert.equal(publicSite.chessgroundColor('b'), 'black');
  assert.equal(publicSite.chessgroundColor('white'), 'white');
});

test('newly observed legs require an exact history prefix', () => {
  assert.deepEqual(
    publicSite.newlyObservedMoves(
      { moves: ['e2e4', 'e7e5'] },
      { moves: ['e2e4', 'e7e5', 'g1f3+', 'f3e5'] },
    ),
    ['g1f3+', 'f3e5'],
  );
  assert.deepEqual(
    publicSite.newlyObservedMoves(
      { moves: ['e2e4', 'e7e5'] },
      { moves: ['e2e4', 'c7c5'] },
    ),
    [],
  );
});

test('every nonoptimistic observed leg queues, while only a placed drag leg is skipped', () => {
  assert.deepEqual(
    publicSite.observedMoveAnimationPlan(
      { moves: [] },
      { moves: ['e2e4+'] },
      0,
    ),
    { startMoveCount: 0, moveCount: 1, skippedMoveCount: 0 },
  );
  assert.deepEqual(
    publicSite.observedMoveAnimationPlan(
      { moves: [] },
      { moves: ['e2e4+'] },
      1,
    ),
    { startMoveCount: 1, moveCount: 0, skippedMoveCount: 1 },
  );
  assert.deepEqual(
    publicSite.observedMoveAnimationPlan(
      { moves: ['e2e4'] },
      { moves: ['e2e4', 'e7e5+', 'e5e4'] },
      1,
    ),
    { startMoveCount: 2, moveCount: 1, skippedMoveCount: 1 },
  );
});

test('board placement comparison detects a takeback even when no moves append', () => {
  const before = {
    initial_fen: 'startpos',
    moves: ['e2e4', 'e7e5'],
  };
  const after = {
    initial_fen: 'startpos',
    moves: ['e2e4'],
  };
  assert.notEqual(
    publicSite.boardPlacement(before, analysisState, Chess),
    publicSite.boardPlacement(after, analysisState, Chess),
  );
  assert.deepEqual(
    publicSite.observedMoveAnimationPlan(before, after, 0),
    { startMoveCount: 2, moveCount: 0, skippedMoveCount: 0 },
  );
});

test('double-move state supports object and compact ability representations', () => {
  assert.equal(publicSite.isMidDouble({ ability: { mid: true } }), true);
  assert.equal(publicSite.isMidDouble({ ability: { phase: 'second_move' } }), true);
  assert.equal(publicSite.isMidDouble({ ability: 'WBm' }), true);
  assert.equal(publicSite.abilityAvailable({ ability: 'W-' }, 'white'), true);
  assert.equal(publicSite.abilityAvailable({ ability: 'W-' }, 'black'), false);
  assert.equal(publicSite.abilityAvailable({ ability: { white: false, black: true } }, 'b'), true);
});

test('game completion and engine-thinking detection handle active state variants', () => {
  assert.equal(publicSite.isGameComplete({ status: 'active', result: '*' }), false);
  assert.equal(publicSite.isGameComplete({ status: 'completed', result: '1-0' }), true);
  assert.equal(publicSite.engineIsThinking({
    status: 'active',
    result: '*',
    human_color: 'white',
    to_move: 'black',
  }), true);
  assert.equal(publicSite.engineIsThinking({
    status: 'active',
    result: '*',
    human_color: 'white',
    to_move: 'white',
  }), false);
});

test('owner pending-command state suppresses engine and takeback actions', () => {
  const pending = {
    status: 'active',
    result: '*',
    pending_command: true,
    human_color: 'white',
    to_move: 'white',
    initial_fen: 'startpos',
    moves: ['e2e4', 'e7e5'],
  };
  assert.equal(publicSite.hasPendingCommand(pending), true);
  assert.equal(publicSite.hasPendingCommand({ pending_command: false }), false);
  assert.equal(publicSite.hasPendingCommand({ pending_command: 'true' }), false);
  assert.equal(publicSite.canStartGameMutation(pending, false, false), false);
  assert.equal(publicSite.canStartGameMutation({
    ...pending,
    pending_command: false,
  }, false, false), true);
  assert.equal(publicSite.canStartGameMutation({
    ...pending,
    pending_command: false,
  }, true, false), false);
  assert.equal(publicSite.canStartGameMutation({
    ...pending,
    pending_command: false,
  }, false, true), false);
  assert.equal(publicSite.engineIsThinking({
    ...pending,
    to_move: 'black',
  }), false);
  assert.equal(
    publicSite.canRequestTakeback(pending, analysisState, Chess),
    false,
  );
  assert.deepEqual(publicSite.PENDING_COMMAND_POLL_DELAYS_MS, [
    500,
    750,
    1000,
    1500,
    2000,
    3000,
    5000,
  ]);
});

test('takeback is offered only at a human decision point with a prior human move', () => {
  assert.equal(publicSite.canRequestTakeback({
    status: 'active',
    human_color: 'black',
    to_move: 'black',
    initial_fen: 'startpos',
    moves: ['e2e4'],
  }, analysisState, Chess), false);
  assert.equal(publicSite.canRequestTakeback({
    status: 'active',
    human_color: 'white',
    to_move: 'black',
    initial_fen: 'startpos',
    moves: ['e2e4'],
  }, analysisState, Chess), false);
  assert.equal(publicSite.canRequestTakeback({
    status: 'active',
    human_color: 'white',
    to_move: 'white',
    initial_fen: 'startpos',
    moves: ['e2e4', 'e7e5'],
  }, analysisState, Chess), true);
  assert.equal(publicSite.canRequestTakeback({
    status: 'active',
    human_color: 'white',
    to_move: 'white',
    initial_fen: 'startpos',
    moves: ['e2e4+'],
    ability: { mid: true },
  }, analysisState, Chess), true);
});

test('only an unshown engine opening is seeded for first-load animation', () => {
  assert.equal(publicSite.isInitialEngineOpening({
    human_color: 'black',
    initial_fen: 'startpos',
    moves: ['e2e4'],
  }, analysisState, Chess), true);
  assert.equal(publicSite.isInitialEngineOpening({
    human_color: 'black',
    initial_fen: 'startpos',
    moves: ['e2e4+', 'e4e5'],
  }, analysisState, Chess), true);
  assert.equal(publicSite.isInitialEngineOpening({
    human_color: 'black',
    initial_fen: 'startpos',
    moves: ['e2e4', 'e7e5'],
  }, analysisState, Chess), false);
  assert.equal(publicSite.isInitialEngineOpening({
    human_color: 'white',
    initial_fen: 'startpos',
    moves: ['e2e4'],
  }, analysisState, Chess), false);
});

test('replay derivation preserves a two-leg pawn double move', () => {
  const line = publicSite.deriveReplay(
    { initial_fen: 'startpos', moves: ['e2e4+', 'e4e5'] },
    analysisState,
    Chess,
  );
  assert.equal(line.valid, true);
  assert.deepEqual(line.colors, ['w', 'w']);
  assert.equal(line.fens.length, 3);
  assert.match(line.fens[1], / w /);
  assert.match(line.fens[2], / b /);
  assert.equal(line.fens[2].split(' ')[0].split('/')[3], '4P3');
});

test('move records group both double-move legs into uncolored White and Black columns', () => {
  const line = publicSite.deriveReplay(
    {
      initial_fen: 'startpos',
      moves: ['e2e3+', 'e3e4', 'd7d6+', 'd6d5', 'g1f3'],
    },
    analysisState,
    Chess,
  );
  const groups = publicSite.groupMovePairs(line);
  assert.equal(groups.length, 2);
  assert.deepEqual(
    groups[0].white.map(move => ({
      san: move.san,
      first: move.isDoubleStart,
      second: move.isSecondLeg,
    })),
    [
      { san: 'e3', first: true, second: false },
      { san: 'e4', first: false, second: true },
    ],
  );
  assert.deepEqual(
    groups[0].black.map(move => move.san),
    ['d6', 'd5'],
  );
  assert.deepEqual(groups[1].white.map(move => move.san), ['Nf3']);
});

test('advanced options build the exact nested engine and clock contract', () => {
  assert.deepEqual(publicSite.buildAdvancedOptions({
    model_id: 'network-v1',
    nodes: '137',
    temperature: '0.65',
    temp_decay_moves: '12',
    start_fen: '8/8/8/8/8/8/4K3/7k w - - 0 1 W-',
    clock_mode: 'timed',
    initial_minutes: '7.5',
    increment_seconds: '2.25',
  }), {
    model_id: 'network-v1',
    nodes: 137,
    temperature: 0.65,
    temp_decay_moves: 12,
    clock: {
      mode: 'timed',
      initial_ms: 450_000,
      increment_ms: 2_250,
    },
    start_fen: '8/8/8/8/8/8/4K3/7k w - - 0 1 W-',
  });
  assert.deepEqual(publicSite.buildAdvancedOptions({
    model_id: 'network-v1',
    nodes: 1,
    temperature: 0,
    temp_decay_moves: 0,
    clock_mode: 'unlimited',
  }).clock, { mode: 'unlimited' });
  assert.throws(
    () => publicSite.buildAdvancedOptions({
      model_id: 'network-v1',
      nodes: '',
      temperature: 0,
      temp_decay_moves: 0,
      clock_mode: 'unlimited',
    }),
    /Visits must be/,
  );
  assert.throws(
    () => publicSite.buildAdvancedOptions({
      model_id: 'network-v1',
      nodes: 10,
      temperature: 0,
      temp_decay_moves: 641,
      clock_mode: 'unlimited',
    }),
    /Temperature decay moves/,
  );
});

test('clock extraction and ticking use the server snapshot', () => {
  const state = {
    status: 'active',
    to_move: 'white',
    clocks: {
      white_ms: 60_000,
      black: { remaining_ms: 30_000 },
      running_color: 'white',
    },
  };
  assert.equal(publicSite.clockRemaining(state, 'w'), 60_000);
  assert.equal(publicSite.clockRemaining(state, 'black'), 30_000);
  assert.equal(publicSite.adjustedClockMs(state, 'white', 1_000, 3_500), 57_500);
  assert.equal(publicSite.adjustedClockMs(state, 'black', 1_000, 3_500), 30_000);
});

test('clock ticking ignores client/server wall-clock skew', () => {
  const receivedAt = 10_000;
  const baseState = {
    status: 'active',
    to_move: 'b',
    clocks: { b_ms: 5_000, running_color: 'b' },
  };
  assert.equal(publicSite.adjustedClockMs({
    ...baseState,
    server_time_ms: 1_700_000_000_000 - 240_000,
  }, 'b', receivedAt, receivedAt + 1_100), 3_900);
  assert.equal(publicSite.adjustedClockMs({
    ...baseState,
    server_time_ms: 1_700_000_000_000 + 240_000,
  }, 'b', receivedAt, receivedAt + 1_100), 3_900);
});

test('clock formatting covers unlimited, regular, and low-time display', () => {
  assert.equal(publicSite.clockIsUnlimited({ clocks: { enabled: false } }), true);
  assert.equal(publicSite.formatClock(null), '∞');
  assert.equal(publicSite.formatClock(65_000), '1:05');
  assert.equal(publicSite.formatClock(12_340), '0:12.3');
  assert.equal(publicSite.formatClock(-1), '0:00.0');
});

test('player labels support object, string, and anonymous records', () => {
  assert.equal(publicSite.playerName({ players: { white: { display_name: 'Ada' } } }, 'w'), 'Ada');
  assert.equal(publicSite.playerName({ players: { black: 'Engine 10' } }, 'b'), 'Engine 10');
  assert.equal(publicSite.playerName({ human_color: 'w' }, 'white'), 'Anonymous challenger');
  assert.equal(publicSite.playerName({ human_color: 'w' }, 'black'), 'Double Move Engine');
  assert.equal(publicSite.playerMeta({
    human_color: 'white',
    preset_label: 'Advanced',
    nodes: 137,
    players: { black: { type: 'engine', preset_label: 'Advanced' } },
  }, 'black'), 'Advanced · 137 visits');
});

test('completed game outcomes are stated from the human perspective', () => {
  assert.equal(publicSite.humanOutcomeDisplay({
    human_color: 'white',
    result: '1-0',
  }), 'Human won');
  assert.equal(publicSite.humanOutcomeDisplay({
    human_color: 'white',
    result: '0-1',
  }), 'Engine won');
  assert.equal(publicSite.humanOutcomeDisplay({
    human_color: 'black',
    result: '0-1',
  }), 'Human won');
  assert.equal(publicSite.humanOutcomeDisplay({
    human_color: 'b',
    result: '1-0',
  }), 'Engine won');
  assert.equal(publicSite.humanOutcomeDisplay({
    human_color: 'white',
    result: '1/2-1/2',
  }), 'Draw');
  assert.equal(publicSite.humanOutcomeDisplay({
    human_color: 'black',
    result: 'draw',
  }), 'Draw');
  assert.equal(publicSite.humanOutcomeDisplay({ result: '1-0' }), '1–0');
});

test('completed games display exact temperature and decay settings', () => {
  assert.equal(publicSite.temperatureDisplay({
    temperature: 0.5,
    temp_decay_moves: 10,
  }), 'Temp 0.5 · decay 10 moves');
  assert.equal(publicSite.temperatureDisplay({
    temperature: 0,
    temp_decay_moves: 0,
  }), 'Temp 0 · decay 0 moves');
  assert.equal(publicSite.temperatureDisplay({
    temperature: '0.25',
    temp_decay_moves: '1',
  }), 'Temp 0.25 · decay 1 move');
  assert.equal(publicSite.temperatureDisplay({ temperature: 0.5 }), '');
  assert.equal(publicSite.temperatureDisplay({
    temperature: 0.5,
    temp_decay_moves: 1.5,
  }), '');
});

test('API option collections normalize arrays and keyed objects', () => {
  assert.deepEqual(publicSite.normalizedOptions(['Unlimited']), [
    { id: 'Unlimited', label: 'Unlimited', description: '' },
  ]);
  assert.deepEqual(publicSite.normalizedOptions({
    fast: {
      label: 'Fast',
      description: 'Ten visits',
      nodes: 10,
      temperature: 0.5,
      temp_decay_moves: 3,
    },
  }), [
    {
      id: 'fast',
      label: 'Fast',
      description: 'Ten visits',
      initial_ms: null,
      increment_ms: null,
      nodes: 10,
      temperature: 0.5,
      temp_decay_moves: 3,
    },
  ]);
});

test('public scripts never use HTML string injection for dynamic values', () => {
  const root = path.resolve(__dirname, '..');
  for (const filename of ['play.js', 'games.js', 'rules.js']) {
    const source = fs.readFileSync(path.join(root, filename), 'utf8');
    assert.doesNotMatch(source, /\binnerHTML\b|insertAdjacentHTML|document\.write|\.html\s*\(/);
  }
});

test('setup publishes a nonblank name automatically and keeps Advanced explicitly opt-in', () => {
  const html = fs.readFileSync(path.resolve(__dirname, '..', 'play.html'), 'utf8');
  const source = fs.readFileSync(path.resolve(__dirname, '..', 'play.js'), 'utf8');
  assert.match(html, /A name is shown with the completed game\. Leave it blank to play anonymously\./);
  assert.doesNotMatch(html, /id="publishName"/);
  assert.match(source, /publish_name:\s*Boolean\(displayName\)/);
  const details = html.match(/<details class="advanced-settings" id="advancedSettings"([^>]*)>/);
  assert.ok(details);
  assert.doesNotMatch(details[1], /\bopen\b/);
  const enable = html.match(/<input id="advancedEnabled"[^>]*>/);
  assert.ok(enable);
  assert.doesNotMatch(enable[0], /\bchecked\b/);
  assert.match(source, /if \(byId\('advancedEnabled'\)\.checked\) \{/);
  assert.match(html, /not PolicyTemperature/);
  assert.match(html, /A decay of 0 keeps it constant/);
});

test('Play and replay expose SAN-only two-column move records', () => {
  const root = path.resolve(__dirname, '..');
  const playHtml = fs.readFileSync(path.join(root, 'play.html'), 'utf8');
  const gamesHtml = fs.readFileSync(path.join(root, 'games.html'), 'utf8');
  const playSource = fs.readFileSync(path.join(root, 'play.js'), 'utf8');
  const gamesSource = fs.readFileSync(path.join(root, 'games.js'), 'utf8');
  for (const html of [playHtml, gamesHtml]) {
    assert.match(html, /class="move-table-head"[^>]*>[\s\S]*?<span>White<\/span><span>Black<\/span>/);
  }
  assert.match(playSource, /notation\.textContent = move\.san/);
  assert.match(gamesSource, /'Show position after ' \+ move\.san/);
  assert.match(gamesSource, /publicApi\.groupMovePairs\(replay\)/);
  assert.doesNotMatch(playSource, /uci\.textContent\s*=\s*move/);
  assert.doesNotMatch(gamesSource, /uci\.textContent\s*=\s*move/);
  assert.doesNotMatch(gamesSource, /'Show position after ' \+ move(?:[;,)]|\s)/);
});

test('Play and replay color Double Move legs without textual badges', () => {
  const playSource = fs.readFileSync(path.resolve(__dirname, '..', 'play.js'), 'utf8');
  const gamesSource = fs.readFileSync(path.resolve(__dirname, '..', 'games.js'), 'utf8');
  const css = fs.readFileSync(path.resolve(__dirname, '..', 'public-site.css'), 'utf8');
  for (const source of [playSource, gamesSource]) {
    assert.match(source, /move\.isDoubleStart \|\| move\.isSecondLeg \? ' is-double' : ''/);
    assert.doesNotMatch(source, /badge\.textContent = 'DM'|badge\.textContent = '2nd'/);
  }
  assert.match(css, /\.move-token\.is-double\s*\{[^}]*color:\s*var\(--ability\)/);
  assert.doesNotMatch(css, /\.ability-badge|\.meta-badge/);
});

test('Games archive and detail show human outcomes and sampling settings', () => {
  const html = fs.readFileSync(path.resolve(__dirname, '..', 'games.html'), 'utf8');
  const source = fs.readFileSync(path.resolve(__dirname, '..', 'games.js'), 'utf8');
  const css = fs.readFileSync(path.resolve(__dirname, '..', 'public-site.css'), 'utf8');
  assert.match(source, /result\.textContent = publicApi\.humanOutcomeDisplay\(game\)/);
  assert.match(source, /detailResult'\)\.textContent = publicApi\.humanOutcomeDisplay\(detailGame\)/);
  assert.match(source, /gameStrength\(game\),\s*publicApi\.temperatureDisplay\(game\)/);
  assert.match(source, /gameStrength\(detailGame\),\s*publicApi\.temperatureDisplay\(detailGame\)/);
  assert.match(source, /hour: '2-digit',[\s\S]*?minute: '2-digit'/);
  assert.match(source, /board\.className = 'archive-final-board cg-wrap'/);
  assert.match(source, /mountArchiveBoard\(board, game\.final_fen \|\| game\.current_fen\)/);
  assert.match(css, /\.archive-final-board\s*\{[^}]*border-radius:\s*5px/);
  assert.match(
    css,
    /\.archive-final-board cg-board\s*\{[^}]*background-image:\s*conic-gradient/s,
  );
  assert.match(
    html,
    /class="detail-heading-actions"[\s\S]*?id="detailAnalyzeLink"[\s\S]*?Analyze this game/,
  );
  assert.match(
    source,
    /analyzeLink\.href = '\/analysis\?game='[\s\S]*?analyzeLink\.hidden = false;/,
  );
});

test('saved dark theme is applied before styles load on every page', () => {
  const root = path.resolve(__dirname, '..');
  for (const name of [
    'index.html',
    'landing.html',
    'play.html',
    'games.html',
    'rules.html',
    'invite.html',
  ]) {
    const html = fs.readFileSync(path.join(root, name), 'utf8');
    const bootstrap = html.indexOf("localStorage.getItem('dm-theme')");
    const firstStyle = Math.min(
      ...[html.indexOf('<style'), html.indexOf('rel="stylesheet"')]
        .filter(index => index >= 0),
    );
    assert.ok(bootstrap >= 0, name + ' has no theme bootstrap');
    assert.ok(bootstrap < firstStyle, name + ' applies theme after CSS');
  }
  const css = fs.readFileSync(path.join(root, 'public-site.css'), 'utf8');
  assert.match(css, /:root\.dark,\s*body\.dark\s*\{/);
});

test('first-party assets use one release key so cached pages cannot mix releases', () => {
  const root = path.resolve(__dirname, '..');
  const release = '20260805c';
  const pages = [
    'index.html',
    'landing.html',
    'play.html',
    'games.html',
    'rules.html',
    'invite.html',
  ];
  for (const name of pages) {
    const html = fs.readFileSync(path.join(root, name), 'utf8');
    const references = [...html.matchAll(
      /(?:src|href)="\/(?!vendor\/)([^"?]+\.(?:css|js))(?:\?v=([^"&]+))?"/g,
    )];
    for (const reference of references) {
      assert.equal(
        reference[2],
        release,
        name + ' does not version /' + reference[1] + ' with the release key',
      );
    }
  }
});

test('mobile interactions neither select the board nor scroll page ancestors', () => {
  const root = path.resolve(__dirname, '..');
  const source = fs.readFileSync(path.join(root, 'play.js'), 'utf8');
  const css = fs.readFileSync(path.join(root, 'public-site.css'), 'utf8');
  assert.doesNotMatch(source, /scrollIntoView\s*\(/);
  assert.match(source, /current\.getBoundingClientRect\(\)/);
  assert.match(css, /-webkit-tap-highlight-color:\s*transparent/);
  assert.match(css, /\.cg-wrap cg-board[\s\S]*?user-select:\s*none/);
  assert.match(css, /\.move-record\s*\{\s*overflow-anchor:\s*none;/);
});

test('analysis keeps its public header on mobile and exposes lower engine settings', () => {
  const html = fs.readFileSync(path.resolve(__dirname, '..', 'index.html'), 'utf8');
  assert.match(
    html,
    /<a class="brand" href="\/" aria-label="Double Move Chess home">/,
  );
  const navigation = html.match(
    /<nav class="analysis-site-nav" aria-label="Primary navigation">([\s\S]*?)<\/nav>/,
  );
  assert.ok(navigation);
  assert.match(navigation[1], /<a href="\/play">Play<\/a>/);
  assert.match(navigation[1], /<a href="\/games">Games<\/a>/);
  assert.match(navigation[1], /<a href="\/rules">Rules<\/a>/);
  assert.match(
    navigation[1],
    /<a href="\/analysis" aria-current="page">Analysis<\/a>/,
  );
  assert.match(
    navigation[1],
    /<a href="\/SOURCE_AND_LICENSES\.md">Source<\/a>/,
  );
  assert.match(
    html,
    /@media \(max-width: 680px\)[\s\S]*?\.topbar\s*\{[\s\S]*?min-height:\s*60px;[\s\S]*?\.side-column \.panel\.engine-settings-panel\s*\{[\s\S]*?display:\s*block;[\s\S]*?order:\s*3;/,
  );
  assert.doesNotMatch(html, /mobile-analysis-menu/);
  assert.match(
    html,
    /\.brand:focus-visible,\s*\.analysis-site-nav a:focus-visible\s*\{/,
  );
});

test('landing page offers Analysis as a primary hero action', () => {
  const html = fs.readFileSync(path.resolve(__dirname, '..', 'landing.html'), 'utf8');
  assert.match(
    html,
    /<div class="hero-actions">[\s\S]*?href="\/play"[\s\S]*?href="\/analysis">Open analysis<\/a>[\s\S]*?href="\/rules"/,
  );
});

test('every delivered page exposes the browser source and license notice', () => {
  const root = path.resolve(__dirname, '..');
  const pages = [
    'landing.html',
    'play.html',
    'games.html',
    'rules.html',
    'index.html',
    'invite.html',
  ];
  for (const filename of pages) {
    const html = fs.readFileSync(path.join(root, filename), 'utf8');
    assert.match(
      html,
      /href="\/SOURCE_AND_LICENSES\.md"/,
      `${filename} must link to the corresponding-source notice`,
    );
  }

  const notice = fs.readFileSync(
    path.join(root, 'SOURCE_AND_LICENSES.md'),
    'utf8',
  );
  assert.match(notice, /combined browser frontend is distributed under\s+GPL-3\.0-or-later/);
  assert.match(notice, /vendor\/chessground\/10\.1\.1\//);
});

test('Chessground board styling uses square-semantic coordinates and mobile-safe input', () => {
  const css = fs.readFileSync(path.resolve(__dirname, '..', 'public-site.css'), 'utf8');
  const source = fs.readFileSync(path.resolve(__dirname, '..', 'play.js'), 'utf8');
  assert.match(css, /#playBoard coords \.coord-light,\s*#replayBoard coords \.coord-light\s*\{[^}]*color:\s*var\(--board-light-ink\)/s);
  assert.match(css, /#playBoard coords \.coord-dark,\s*#replayBoard coords \.coord-dark\s*\{[^}]*color:\s*var\(--board-dark-ink\)/s);
  assert.match(css, /#replayBoard,\s*#replayBoard cg-board\s*\{\s*touch-action:\s*pan-y;/s);
  assert.match(css, /#playBoard cg-board square\.move-dest/);
  assert.match(
    css,
    /#playBoard cg-board square\.double-highlight,\s*#replayBoard cg-board square\.double-highlight/,
  );
  assert.match(source, /doubleMove:\s*line\.doubleFlags\[index - 1\] === true/);
  assert.match(source, /lastMove:\s*Boolean\(lastMove && lastMove\.length\) && !doubleMove/);
  assert.doesNotMatch(css, /\.square-55d63|\.piece-417db|body\.board-dragging/);
  assert.match(css, /@media \(orientation: landscape\) and \(max-height: 700px\)/);
});

test('Play uses a width-based centered board and a compact unboxed sidebar', () => {
  const css = fs.readFileSync(path.resolve(__dirname, '..', 'public-site.css'), 'utf8');
  const html = fs.readFileSync(path.resolve(__dirname, '..', 'play.html'), 'utf8');
  assert.match(
    css,
    /\.game-layout\s*\{[^}]*--play-board-size:\s*min\(820px,[^}]*grid-template-columns:\s*minmax\(280px,\s*1fr\)\s+minmax\(0,\s*var\(--play-board-size\)\)\s+minmax\(280px,\s*1fr\)/s,
  );
  assert.doesNotMatch(
    css,
    /\.game-layout\s*\{[^}]*calc\(100d?vh - 250px\)/s,
  );
  assert.match(
    css,
    /\.board-wrap\s*\{[^}]*width:\s*min\(100%,\s*var\(--play-board-size,\s*820px\)\)[^}]*max-width:\s*820px/s,
  );
  assert.match(css, /\.game-column\s*\{\s*display:\s*contents;/s);
  assert.match(
    css,
    /\.player-strip\s*\{[^}]*width:\s*min\(320px,\s*100%\)[^}]*border:\s*0;/s,
  );
  assert.match(html, /<section class="game-section">/);
  assert.doesNotMatch(html, /Server-checked play|Only legal moves for the current position/);
});

test('public boards load the pinned Chessground module and use its native interaction path', () => {
  const root = path.resolve(__dirname, '..');
  const playHtml = fs.readFileSync(path.join(root, 'play.html'), 'utf8');
  const gamesHtml = fs.readFileSync(path.join(root, 'games.html'), 'utf8');
  const playSource = fs.readFileSync(path.join(root, 'play.js'), 'utf8');
  const gamesSource = fs.readFileSync(path.join(root, 'games.js'), 'utf8');

  for (const html of [playHtml, gamesHtml]) {
    assert.match(html, /\/vendor\/chessground\/10\.1\.1\/assets\/chessground\.base\.css/);
    assert.match(html, /\/vendor\/chessground\/10\.1\.1\/assets\/chessground\.cburnett\.css/);
    assert.match(html, /<script type="module" src="\/chessground-loader\.js\?v=20260805c"><\/script>/);
    assert.match(html, /class="cg-wrap"/);
    assert.doesNotMatch(html, /chessboardjs|jquery/);
  }

  assert.match(playHtml, /<section class="game-page" id="gameView" hidden>\s*<h1[^>]*>Play Double Move Chess<\/h1>/);
  assert.match(playSource, /root\.Chessground\(byId\('playBoard'\), config\)/);
  assert.match(playSource, /movable:\s*\{[\s\S]*?free:\s*false,[\s\S]*?dests:\s*interactive \? chessgroundDests\(moves\)/);
  assert.match(playSource, /events:\s*\{\s*after:\s*handleGroundMove\s*\}/);
  assert.match(playSource, /animation:\s*\{[\s\S]*?duration:\s*BOARD_ANIMATION_MS/);
  assert.match(
    playSource,
    /drawable:\s*\{[\s\S]*?enabled:\s*true,[\s\S]*?visible:\s*true,[\s\S]*?shapes:\s*sanitizeBoardShapes\(manualShapesByPly\.get\(ply\) \|\| \[\]\),[\s\S]*?onChange:\s*handleBoardShapesChanged/,
  );
  assert.match(
    playSource,
    /if \(hasSameGameRevision\(previousGame, nextGame\)\) \{[\s\S]*?renderGame\(\{ skipBoard: true \}\);[\s\S]*?return true;[\s\S]*?\}[\s\S]*?manualShapesByPly = new Map\(\);/,
  );
  assert.match(playSource, /addEventListener\('chessground-ready'/);
  assert.match(playSource, /applyGame\(loaded,\s*requestOptions\.applyOptions\)/);
  assert.doesNotMatch(playSource, /\.square-55d63|onMoveEnd|board\.position\(|addEventListener\('click', handleBoardClick/);

  assert.match(gamesSource, /root\.Chessground\(byId\('replayBoard'\)/);
  assert.match(gamesSource, /viewOnly:\s*true/);
  assert.match(gamesSource, /function replayHighlight\(move, index\)/);
  assert.match(gamesSource, /custom\.set\(move\.slice\(0, 2\), 'double-highlight'\)/);
  assert.match(gamesSource, /replayBoard\.set\(\{/);
  assert.match(gamesSource, /addEventListener\('chessground-ready'/);
  assert.doesNotMatch(gamesSource, /\.square-55d63|board\.position\(|replayBoard\.resize/);
});

test('promotion choice suspends polling and rejects an already in-flight poll response', () => {
  const source = fs.readFileSync(path.resolve(__dirname, '..', 'play.js'), 'utf8');
  assert.match(
    source,
    /function suspendPollingForPromotion\(\)[\s\S]*?root\.clearTimeout\(pollTimer\)[\s\S]*?pollGeneration \+= 1;/,
  );
  assert.match(
    source,
    /function settlePromotion\(dialog, value\)[\s\S]*?promotionResolver = null;[\s\S]*?resolver\(value\);[\s\S]*?schedulePoll\(\);/,
  );
  assert.match(
    source,
    /if \(\(pending \|\| boardAnimating \|\| promotionResolver\) && !allowBusy\) return false;/,
  );
  assert.equal(
    (source.match(/promotionPending:\s*!!promotionResolver/g) || []).length,
    2,
  );
  assert.match(
    source,
    /generation !== pollGeneration \|\| pending \|\| boardAnimating \|\| promotionResolver/,
  );
});

test('ordered engine-leg animation waits for Chessground with a bounded hidden-tab fallback', () => {
  const source = fs.readFileSync(path.resolve(__dirname, '..', 'play.js'), 'utf8');
  assert.match(
    source,
    /function waitForBoardAnimation\(generation\)[\s\S]*?board\.state\.animation\.current/,
  );
  assert.match(
    source,
    /const maxWaitMs = Math\.max\(animationSpeed \* 3, animationSpeed \+ 250\);/,
  );
  assert.match(source, /document\.addEventListener\('visibilitychange', handleVisibilityChange\);/);
  assert.match(source, /fallbackTimer = root\.setTimeout\(finish, maxWaitMs\);/);
  assert.equal(
    (source.match(/await waitForBoardAnimation\(generation\);/g) || []).length,
    2,
  );
  assert.doesNotMatch(source, /await sleep\(animationSpeed\);/);
});

test('replay keyboard shortcuts ignore modifiers and prevent browser navigation', () => {
  const source = fs.readFileSync(path.resolve(__dirname, '..', 'games.js'), 'utf8');
  assert.match(
    source,
    /if \(event\.altKey \|\| event\.ctrlKey \|\| event\.metaKey \|\| event\.shiftKey\) return;/,
  );
  assert.match(
    source,
    /if \(!\['ArrowLeft', 'ArrowRight', 'Home', 'End'\]\.includes\(event\.key\)\) return;\s*event\.preventDefault\(\);/,
  );
});

test('Play history arrows work globally without stealing text entry or modal keys', () => {
  const source = fs.readFileSync(path.resolve(__dirname, '..', 'play.js'), 'utf8');
  const start = source.indexOf('    function isTextEditingTarget(');
  const end = source.indexOf('\n    function handleBoardKey(', start);
  assert.ok(start >= 0 && end > start);
  const calls = [];
  const context = {
    game: { id: 'game' },
    document: { querySelector: () => null },
    stepViewPly: delta => calls.push(delta),
  };
  vm.createContext(context);
  vm.runInContext(
    source.slice(start, end) +
      '\nglobalThis.navigateForTest = handleMoveNavigationKey;',
    context,
  );
  const target = kind => ({
    closest(selector) {
      if (selector === 'input') {
        return kind === 'text' ? { type: 'text' } : null;
      }
      if (selector.startsWith('textarea')) {
        return kind === 'textarea' ? {} : null;
      }
      return null;
    },
  });
  const event = (key, kind = 'button', extra = {}) => ({
    key,
    target: target(kind),
    defaultPrevented: false,
    isComposing: false,
    ctrlKey: false,
    metaKey: false,
    altKey: false,
    shiftKey: false,
    prevented: false,
    stopped: false,
    preventDefault() {
      this.defaultPrevented = true;
      this.prevented = true;
    },
    stopPropagation() {
      this.stopped = true;
    },
    ...extra,
  });

  const right = event('ArrowRight');
  context.navigateForTest(right);
  const left = event('ArrowLeft');
  context.navigateForTest(left);
  assert.deepEqual(calls, [1, -1]);
  assert.equal(right.prevented && right.stopped, true);
  assert.equal(left.prevented && left.stopped, true);

  context.navigateForTest(event('ArrowRight', 'text'));
  context.navigateForTest(event('ArrowLeft', 'button', { ctrlKey: true }));
  assert.deepEqual(calls, [1, -1]);

  context.document.querySelector = () => ({ open: true });
  context.navigateForTest(event('ArrowRight'));
  assert.deepEqual(calls, [1, -1]);

  assert.match(
    source,
    /document\.addEventListener\('keydown', handleMoveNavigationKey, true\);/,
  );
  const boardHandler = source.slice(
    source.indexOf('    function handleBoardKey('),
    source.indexOf('\n    function handleGroundMove(', source.indexOf('    function handleBoardKey(')),
  );
  assert.doesNotMatch(boardHandler, /stepViewPly\(/);
});

test('active games retain navigation links instead of trapping the player', () => {
  const css = fs.readFileSync(path.resolve(__dirname, '..', 'public-site.css'), 'utf8');
  const html = fs.readFileSync(path.resolve(__dirname, '..', 'play.html'), 'utf8');
  assert.match(html, /<nav class="site-nav"[^>]*>[\s\S]*?href="\/play"[\s\S]*?href="\/games"[\s\S]*?href="\/analysis"/);
  assert.doesNotMatch(css, /body\.active-game \.site-nav a\s*\{[^}]*display:\s*none/);
  assert.doesNotMatch(css, /\.site-nav \.nav-analysis\s*\{[^}]*display:\s*none/);
});

test('pending-command recovery locks play controls and polls with a bounded backoff', () => {
  const source = fs.readFileSync(
    path.resolve(__dirname, '..', 'play.js'),
    'utf8',
  );
  assert.match(
    source,
    /function humanTurn\(\)[\s\S]*?hasPendingCommand\(game\)[\s\S]*?return false;/,
  );
  assert.match(
    source,
    /function interactionMoves\(\)[\s\S]*?hasPendingCommand\(game\)[\s\S]*?return \[\];/,
  );
  assert.match(
    source,
    /async function mutateGame\([\s\S]*?!canStartGameMutation\(game, pending, boardAnimating\)/,
  );
  assert.match(
    source,
    /takebackButton'\)\.disabled[\s\S]*?hasPendingCommand\(game\)/,
  );
  assert.match(
    source,
    /const resignButton = byId\('resignButton'\);[\s\S]*?resignButton\.disabled[\s\S]*?hasPendingCommand\(game\)/,
  );
  assert.match(
    source,
    /hasPendingCommand\(game\)\s*\?\s*pendingCommandDelay/,
  );
  assert.match(source, /Restoring game…/);
});

test('light-theme ability text meets normal-text contrast', () => {
  const css = fs.readFileSync(path.resolve(__dirname, '..', 'public-site.css'), 'utf8');
  const color = css.match(/:root\s*\{[^}]*--ability:\s*(#[0-9a-f]{6})/i);
  assert.ok(color);
  const luminance = value => {
    const channels = value.slice(1).match(/.{2}/g).map(part => parseInt(part, 16) / 255);
    const linear = channels.map(channel =>
      channel <= 0.04045
        ? channel / 12.92
        : ((channel + 0.055) / 1.055) ** 2.4,
    );
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
  };
  const ratio = (luminance('#ffffff') + 0.05) / (luminance(color[1]) + 0.05);
  assert.ok(ratio >= 4.5, 'expected >= 4.5:1, got ' + ratio.toFixed(2) + ':1');
});

test('Play keeps ability, history review, and resignation confirmation in the page UI', () => {
  const root = path.resolve(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'play.html'), 'utf8');
  const source = fs.readFileSync(path.join(root, 'play.js'), 'utf8');
  const css = fs.readFileSync(path.join(root, 'public-site.css'), 'utf8');

  assert.match(
    html,
    /<aside class="game-console"[\s\S]*?<section class="ability-panel">[\s\S]*?<div class="side-stack">/,
  );
  assert.match(html, /id="playStartButton"[\s\S]*?id="playEndButton"/);
  assert.match(html, /first move may activate the ability only if at least one legal second move will remain/);
  assert.match(source, /function setViewPly\(ply\)[\s\S]*?viewPly = next >= total \? null : next;/);
  assert.match(source, /function boardCanMove\(\) \{\s*return atLivePosition\(\) && humanTurn\(\)/);
  assert.match(source, /manualShapesByPly\.set\(viewedPly\(\), sanitizeBoardShapes\(shapes\)\)/);
  assert.match(source, /function requestResignation\(\)[\s\S]*?if \(!resignArmed\)[\s\S]*?mutateGame\('\/resign'/);
  assert.doesNotMatch(source, /root\.confirm|window\.confirm/);
  assert.match(css, /\.ability-control::before[\s\S]*?transform:\s*scale\(\.35\)/);
  assert.match(css, /\.ability-control\[aria-pressed="true"\]::before[\s\S]*?scale\(1\)/);
  assert.match(css, /#resignButton\.is-confirming::before[\s\S]*?animation:\s*resign-window/);
});

test('Analysis offers a main-line-first keyboard variation chooser and a fast native move path', () => {
  const html = fs.readFileSync(path.resolve(__dirname, '..', 'index.html'), 'utf8');
  assert.match(html, /function chooseVariation\(children\)[\s\S]*?selected = 0;/);
  assert.match(html, /ArrowUp[\s\S]*?ArrowDown[\s\S]*?ArrowRight[\s\S]*?enter\(\)/);
  assert.match(html, /if \(children\.length > 1\) \{\s*chooseVariation\(children\);/);
  assert.match(
    html,
    /preservePieces[\s\S]*?board\.getFen\(\) === fen\.split\(\/\\s\+\/\)\[0\][\s\S]*?\.\.\.\(preservePieces \? \{\} : \{ fen \}\)/,
  );
  assert.match(html, /requestAnimationFrame\(\(\) => \{[\s\S]*?refreshChrome\(\);/);
});

test('the complete rules page states the defining legality and king rules', () => {
  const html = fs.readFileSync(path.resolve(__dirname, '..', 'rules.html'), 'utf8');
  assert.match(html, /Every half of a Double Move must be legal/);
  assert.match(html, /A Double Move must be completed/);
  assert.match(html, /no legal second move[\s\S]*?activated first half is not allowed/);
  assert.match(html, /not stalemate because the turn has not been completed/);
  assert.match(html, /You may not capture the king/);
  assert.match(html, /goal remains checkmate/);
  assert.match(html, /The opponent does not reply between the two halves/);
  assert.match(html, /In checkmate there is no legal move at all[\s\S]*?the second half can never be reached/);
  assert.match(html, /Qg8\+ Qh7#/);
  assert.match(html, /Kxh7, Kh8[\s\S]*?Qg8, Qxh7[\s\S]*?Bxg6, Bxh7/);
  assert.match(html, /precisely why the position is checkmate even though Black’s Double Move remains unused/);
  assert.match(html, /Double Move can bypass en passant/);
  assert.match(html, /pawn move first, then complete the turn/);
  assert.match(html, /increment is added once, after the entire turn is complete/);
  assert.match(html, /<code>WB<\/code>[\s\S]*?<code>--<\/code>/);
  assert.match(html, /at least one legal second half must exist/);
});

test('first-party website source contains no em dashes', () => {
  const pending = [path.resolve(__dirname, '..')];
  const emDash = new RegExp(String.fromCodePoint(0x2014));
  while (pending.length) {
    const current = pending.pop();
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      if (entry.name === 'vendor') continue;
      const resolved = path.join(current, entry.name);
      if (entry.isDirectory()) {
        pending.push(resolved);
      } else if (/\.(?:css|html|js|md)$/.test(entry.name)) {
        assert.doesNotMatch(
          fs.readFileSync(resolved, 'utf8'),
          emDash,
          path.relative(path.resolve(__dirname, '..'), resolved),
        );
      }
    }
  }
});

test('rule-board examples are read-only, responsive, and mechanically valid', () => {
  const html = fs.readFileSync(path.resolve(__dirname, '..', 'rules.html'), 'utf8');
  const css = fs.readFileSync(path.resolve(__dirname, '..', 'public-site.css'), 'utf8');
  assert.match(
    html,
    /chessground\.base\.css[\s\S]*?chessground\.cburnett\.css[\s\S]*?id="ruleMateStart"[\s\S]*?id="ruleMateFinish"[\s\S]*?id="ruleEpAvailable"[\s\S]*?id="ruleEpBypassed"[\s\S]*?rules\.js\?v=20260805c[\s\S]*?chessground-loader\.js/,
  );
  assert.match(
    css,
    /\.rule-board-grid\s*\{[^}]*repeat\(auto-fit,\s*minmax\(min\(230px,\s*100%\),\s*1fr\)\)/s,
  );
  for (const example of ruleExamples.RULE_EXAMPLES) {
    const config = ruleExamples.boardConfig(example);
    assert.equal(config.viewOnly, true);
    assert.equal(config.draggable.enabled, false);
    assert.equal(config.drawable.enabled, false);
    assert.ok(config.drawable.autoShapes.length > 0);
  }

  const mateFen =
    'r2q3k/ppp3p1/2np2Pp/5b2/3P1B2/1Q4P1/PPP2P2/3R2K1 w - - 0 1 WB';
  const mate = analysisState.deriveLine(
    mateFen,
    ['b3g8+', 'g8h7'],
    Chess,
  );
  assert.equal(mate.valid, true);
  assert.deepEqual(mate.moveSans, ['Qg8+', 'Qh7#']);
  const finalPosition = new Chess(mate.fens.at(-1));
  assert.equal(finalPosition.in_checkmate(), true);

  const epFen = '7k/8/8/8/3p4/8/4P3/7K w - - 0 1 WB';
  const epAvailable = analysisState.deriveLine(
    epFen,
    ['h1g1+', 'e2e4', 'd4e3'],
    Chess,
  );
  assert.equal(epAvailable.valid, true);
  assert.equal(epAvailable.fens[2].split(/\s+/)[3], 'e3');

  const epBypassed = analysisState.deriveLine(
    epFen,
    ['e2e4+', 'h1g1', 'd4e3'],
    Chess,
  );
  assert.equal(epBypassed.valid, false);
  assert.deepEqual(epBypassed.rawMoves, ['e2e4+', 'h1g1']);
  assert.equal(epBypassed.fens[2].split(/\s+/)[3], '-');
});
