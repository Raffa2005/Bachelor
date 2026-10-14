'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { Chess } = require('../vendor/chess.js/chess-0.12.0.js');

const webRoot = path.resolve(__dirname, '..');
const analysisHtml = fs.readFileSync(path.join(webRoot, 'index.html'), 'utf8');

function streamFunctions(fetchImpl, overrides = {}) {
  const start = analysisHtml.indexOf('async function postJson(');
  const end = analysisHtml.indexOf('\nfunction evalKey()', start);
  assert.ok(start >= 0 && end > start, 'stream helper functions should be present');
  const context = {
    fetch: fetchImpl,
    ReadableStream,
    TextDecoder,
    AbortController,
    ...overrides,
  };
  vm.createContext(context);
  vm.runInContext(
    analysisHtml.slice(start, end) + '\nglobalThis.streamEvalForTest = streamEval;',
    context,
  );
  return context.streamEvalForTest;
}

function runInterimProjection(analysisUi) {
  const start = analysisHtml.indexOf(
    'function interimEvaluationForCurrentNode()',
  );
  const end = analysisHtml.indexOf(
    '\nfunction presentInterimEvaluation(',
    start,
  );
  assert.ok(start >= 0 && end > start);
  const context = {
    analysisUi,
    analysisRevision: 4,
    engineContextId: 'context',
    variationTree: {
      currentId: 'child',
      nodes: {
        parent: { id: 'parent', parentId: null, raw: null },
        child: { id: 'child', parentId: 'parent', raw: 'e2e4' },
      },
    },
    evalDataByNode: new Map([
      ['parent', { identity: 'engine', data: { lines: [] } }],
    ]),
    el: () => ({ checked: true }),
    engineIdentityKey: () => 'engine',
    configuredNodes: () => 800,
    verboseMoveStats: () => [],
  };
  vm.createContext(context);
  vm.runInContext(
    analysisHtml.slice(start, end) +
      '\nglobalThis.interimResult = interimEvaluationForCurrentNode();',
    context,
  );
  return context.interimResult;
}

function evalPaintScheduler(overrides = {}) {
  const start = analysisHtml.indexOf('function boardInteractionActive()');
  const end = analysisHtml.indexOf('\nfunction renderEval(', start);
  assert.ok(start >= 0 && end > start);
  const frames = [];
  const paints = [];
  const context = {
    board: null,
    hoverMove: null,
    pendingEvalPaint: false,
    evalPaintFrame: null,
    pendingEvalArrowPaint: false,
    evalArrowTimer: null,
    clearTimeout: () => {},
    latestEvalData: { id: 'latest' },
    latestEvalPaintOptions: {},
    evaluationPresentation: () => ({ parsed: {}, stats: [] }),
    el: () => ({ value: 'regular' }),
    renderRegularEval: data => paints.push(['regular', data.id]),
    renderVerboseEval: data => paints.push(['verbose', data.id]),
    syncEvalDisplayMode: () => {},
    drawHoverArrow: () => {},
    requestAnimationFrame(callback) {
      frames.push(callback);
      return frames.length;
    },
    cancelAnimationFrame: () => {},
    ...overrides,
  };
  vm.createContext(context);
  vm.runInContext(
    analysisHtml.slice(start, end) +
      '\nglobalThis.scheduleForTest = scheduleEvalPaint;' +
      '\nglobalThis.cancelForTest = cancelPendingEvalPaint;',
    context,
  );
  return { context, frames, paints };
}

function evalArrowScheduler() {
  const start = analysisHtml.indexOf('function evalArrowClock()');
  const end = analysisHtml.indexOf('\nfunction renderEval(', start);
  assert.ok(start >= 0 && end > start);
  const timers = [];
  const draws = [];
  let now = 1000;
  const context = {
    window: { performance: { now: () => now } },
    Date,
    board: {
      state: {
        draggable: { current: { orig: 'e2' } },
        drawable: {},
        animation: {},
      },
    },
    hoverMove: null,
    pendingEvalArrowPaint: false,
    evalArrowTimer: null,
    lastEvalArrowPaintAt: 0,
    ACTIVE_DRAG_ARROW_REFRESH_MS: 240,
    BLOCKED_ARROW_RETRY_MS: 48,
    setTimeout(callback, delay) {
      const timer = { callback, delay };
      timers.push(timer);
      return timer;
    },
    drawHoverArrow: () => draws.push(now),
  };
  vm.createContext(context);
  vm.runInContext(
    analysisHtml.slice(start, end) +
      '\nglobalThis.scheduleArrowForTest = scheduleEvalArrowPaint;',
    context,
  );
  return {
    context,
    draws,
    timers,
    setNow(value) {
      now = value;
    },
  };
}

function editedPositionError(fen, pieces) {
  const start = analysisHtml.indexOf('function editedPositionError(');
  const end = analysisHtml.indexOf('\nfunction useEditedPosition(', start);
  assert.ok(start >= 0 && end > start);
  const context = { Chess, result: null };
  vm.createContext(context);
  vm.runInContext(
    analysisHtml.slice(start, end) +
      `\nglobalThis.result = editedPositionError(${JSON.stringify(fen)}, piecesForTest);`,
    Object.assign(context, { piecesForTest: pieces }),
  );
  return context.result;
}

test('analysis consumes split NDJSON progress and completion events', async () => {
  const encoder = new TextEncoder();
  const chunks = [
    '{"type":"progress","nodes":12,"target_nodes":80,',
    '"verbose":["info string e2e4 (1) N: 12"]}\n',
    '{"type":"complete","actual_nodes":82,"target_nodes":80,',
    '"bestmove":"bestmove e2e4","verbose":[]}\n',
  ];
  let request = null;
  const fetchImpl = async (url, options) => {
    request = { url, options };
    return new Response(new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/x-ndjson' },
    });
  };
  const streamEval = streamFunctions(fetchImpl);
  const controller = new AbortController();
  const events = [];

  const result = await streamEval(
    { nodes: 80, engine_context_id: 'context' },
    controller.signal,
    event => events.push(event),
  );

  assert.equal(request.url, '/api/engine/eval/stream');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.signal, controller.signal);
  assert.equal(events.length, 2);
  assert.equal(events[0].nodes, 12);
  assert.equal(result.actual_nodes, 82);
  assert.equal(result.bestmove, 'bestmove e2e4');
});

test('analysis uses the completed JSON endpoint when streaming APIs are unavailable', async () => {
  const requests = [];
  const fetchImpl = async (url, options) => {
    requests.push({ url, options });
    return {
      ok: true,
      status: 200,
      async json() {
        return {
          nodes: 20,
          bestmove: 'bestmove e2e4',
          verbose: [],
        };
      },
    };
  };
  const streamEval = streamFunctions(fetchImpl, {
    ReadableStream: undefined,
  });
  const events = [];

  const result = await streamEval(
    { nodes: 20 },
    new AbortController().signal,
    event => events.push(event),
  );

  assert.deepEqual(requests.map(request => request.url), ['/api/engine/eval']);
  assert.equal(events.length, 1);
  assert.equal(events[0].type, 'complete');
  assert.equal(result.bestmove, 'bestmove e2e4');
});

test('analysis surfaces explicit stream errors and rejects truncated streams', async () => {
  const encoder = new TextEncoder();
  const responseFor = line => new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(line));
      controller.close();
    },
  }), { status: 200 });

  const explicitError = streamFunctions(
    async () => responseFor('{"type":"error","error":"search failed"}\n'),
  );
  await assert.rejects(
    explicitError({}, new AbortController().signal, () => {}),
    /search failed/,
  );

  const recoverableError = streamFunctions(
    async () => responseFor(
      '{"type":"error","error":"engine reset required","retriable":true}\n',
    ),
  );
  await assert.rejects(
    recoverableError({}, new AbortController().signal, () => {}),
    error => (
      error.message === 'engine reset required' &&
      error.retriable === true
    ),
  );

  const truncated = streamFunctions(
    async () => responseFor('{"type":"progress","nodes":3}\n'),
  );
  await assert.rejects(
    truncated({}, new AbortController().signal, () => {}),
    /before evaluation completed/,
  );
});

test('analysis presents live visit progress and SAN-only move labels', () => {
  assert.match(
    analysisHtml,
    /<progress[\s\S]*?id="evalProgressBar"[\s\S]*?aria-label="Engine evaluation visits"[\s\S]*?aria-describedby="evalProgressLabel"/,
  );
  assert.match(
    analysisHtml,
    /function setEvalProgress\(actualVisits, targetVisits\)[\s\S]*?actual \+ ' \/ ' \+ target \+ ' visits'/,
  );
  assert.match(
    analysisHtml,
    /new AbortController\(\)[\s\S]*?streamEval\([\s\S]*?controller\.signal/,
  );
  assert.match(
    analysisHtml,
    /function syncActiveLine\(opts = \{\}\) \{[\s\S]*?abortActiveEval\(\);/,
  );
  assert.match(
    analysisHtml,
    /function onEngineToggle\(\)[\s\S]*?abortActiveEval\(\);[\s\S]*?hideEvalProgress\(\);/,
  );
  assert.match(
    analysisHtml,
    /'<span class="eval-move">' \+ escapeHtml\(san\)/,
  );
  assert.match(
    analysisHtml,
    /\(opts\.searching \? 'Leading: ' : ''\) \+ best\.san/,
  );
  assert.match(
    analysisHtml,
    /id="nodesInput"[^>]*max="2147483647"/,
  );
  assert.match(
    analysisHtml,
    /const playedSans = sanSequenceForEngineMoves\(moves\);[\s\S]*?'Engine played ' \+ playedText/,
  );
  assert.doesNotMatch(analysisHtml, /white-move|black-move|escapeHtml\(displayMove\)|escapeHtml\(cand\.raw\)/);
});

test('position changes render inherited analysis and supersede immediately', () => {
  assert.match(
    analysisHtml,
    /<script src="\/analysis-ui\.js\?v=20260805c"><\/script>/,
  );
  assert.match(
    analysisHtml,
    /function syncActiveLine\(opts = \{\}\)[\s\S]*?interimEvaluationForCurrentNode\(\)[\s\S]*?presentInterimEvaluation\(interimEvaluation\)[\s\S]*?scheduleEval\(\{ immediate: true \}\)/,
  );
  assert.match(
    analysisHtml,
    /const projector = analysisUi\?\.projectAnalysisToChild;[\s\S]*?typeof projector === 'function'[\s\S]*?projected = null;/,
  );
  assert.match(
    analysisHtml,
    /enginePayload\(\{ analysis_request_id: seq \}\)/,
  );
  assert.match(
    analysisHtml,
    /function abortActiveEval\(\)[\s\S]*?requestEvalCancellation\(requestId, contextId, revision\)/,
  );
  assert.match(
    analysisHtml,
    /fetch\('\/api\/engine\/eval\/cancel'[\s\S]*?keepalive: true/,
  );
  assert.match(
    analysisHtml,
    /window\.addEventListener\('pagehide', abortActiveEval\)/,
  );
});

test('new roots keep one stable tab context so replacement requests supersede', () => {
  const start = analysisHtml.indexOf('function resetEngineAnalysis()');
  const end = analysisHtml.indexOf('\nfunction escapeHtml(', start);
  const source = analysisHtml.slice(start, end);
  assert.match(source, /abortActiveEval\(\);[\s\S]*?evalDataByNode\.clear\(\);/);
  assert.doesNotMatch(source, /engineContextId\s*=|createEngineContextId/);
  assert.match(
    analysisHtml,
    /if \(opts\.resetEngine\) resetEngineAnalysis\(\);/,
  );
});

test('analysis retries one server-classified engine recovery', () => {
  assert.match(
    analysisHtml,
    /if \(e\?\.retriable === true && \(opts\.retryAttempt \|\| 0\) < 1\)[\s\S]*?lastEvalKey = '';[\s\S]*?setTimeout\(\(\) => \{[\s\S]*?runEvalNow\(\{ retryAttempt: 1 \}\);/,
  );
  assert.match(
    analysisHtml,
    /catch \(e\) \{[\s\S]*?cancelPendingEvalPaint\(\);[\s\S]*?Engine restarting/,
  );
});

test('a rate-limited settled evaluation retries without another board move', () => {
  assert.match(
    analysisHtml,
    /const ENGINE_RATE_LIMIT_RETRY_MS = 2100;/,
  );
  assert.match(
    analysisHtml,
    /if \(e\?\.status === 429 && \(opts\.rateLimitRetryAttempt \|\| 0\) < 1\)[\s\S]*?lastEvalKey = '';[\s\S]*?retryRevision === analysisRevision[\s\S]*?runEvalNow\(\{[\s\S]*?rateLimitRetryAttempt:/,
  );
});

test('terminal completed games stay analyzable when navigating backward', () => {
  assert.match(
    analysisHtml,
    /function renderTerminalEval\(data\)[\s\S]*?Navigate to an earlier position to continue analysis\./,
  );
  assert.match(
    analysisHtml,
    /function paintLatestEvaluation\(\)[\s\S]*?latestEvalData\.terminal === true[\s\S]*?renderTerminalEval\(latestEvalData\)/,
  );
  assert.match(
    analysisHtml,
    /if \(data\.terminal === true\) \{[\s\S]*?hideEvalProgress\(\);[\s\S]*?setTopStatus\(terminal\.label\);[\s\S]*?return;/,
  );
});

test('a stale or faulty analysis helper cannot block making a move', () => {
  assert.equal(runInterimProjection({}), null);
  assert.equal(
    runInterimProjection({
      projectAnalysisToChild() {
        throw new Error('stale helper contract');
      },
    }),
    null,
  );
});

test('engine toggle is the sole evaluation control and settings stay collapsed', () => {
  assert.doesNotMatch(analysisHtml, />\s*Controls\s*<\/h2>/);
  assert.doesNotMatch(analysisHtml, /onclick="runEvalNow\(\)"/);
  assert.doesNotMatch(analysisHtml, /id="playWhiteToggle"|id="playBlackToggle"|> Engine Move</);
  assert.equal((analysisHtml.match(/> Forget Analysis<\/button>/g) || []).length, 1);
  assert.match(
    analysisHtml,
    /<details class="settings-disclosure">\s*<summary><i data-lucide="settings-2"><\/i> Display settings<\/summary>/,
  );
  assert.match(
    analysisHtml,
    /<details class="panel settings-disclosure engine-settings-panel">/,
  );
  assert.match(
    analysisHtml,
    /function onEngineToggle\(\) \{\s*if \(el\('engineToggle'\)\.checked\) runEvalNow\(\);/,
  );
  assert.match(
    analysisHtml,
    /async function runEvalNow\(opts = \{\}\) \{\s*if \(!el\('engineToggle'\)\?\.checked\) return;/,
  );
});

test('regular analysis displays native WDL_mu scores and colors Double Moves without tags', () => {
  assert.match(
    analysisHtml,
    /function whiteEvaluationText\(line\)[\s\S]*?line\?\.white_cp[\s\S]*?pawnText\(cp \/ 100\)/,
  );
  assert.doesNotMatch(analysisHtml, /qToPawns|centipawn_with_drawscore/);
  assert.match(
    analysisHtml,
    /regular-engine-line' \+ \(ability \? ' is-double' : ''\)/,
  );
  assert.doesNotMatch(analysisHtml, /regular-dm-badge|pv-double-marker|mini-dm|dm-badge|eval-kind/);
  assert.match(
    analysisHtml,
    /const isDoubleLeg = startsDoubleMove \|\| completesDoubleMove;[\s\S]*?class="pv-san' \+ \(isDoubleLeg \? ' is-double' : ''\)/,
  );
});

test('live engine snapshots are coalesced away from board interaction', () => {
  assert.match(
    analysisHtml,
    /function boardInteractionActive\(\)[\s\S]*?draggable\?\.current[\s\S]*?drawable\?\.current[\s\S]*?animation\?\.current/,
  );
  assert.match(
    analysisHtml,
    /function flushPendingEvalPaint\(\)[\s\S]*?if \(boardInteractionActive\(\)\)[\s\S]*?requestAnimationFrame\(flushPendingEvalPaint\)[\s\S]*?paintLatestEvaluation\(\)/,
  );
  assert.match(
    analysisHtml,
    /function paintLatestEvaluation\(\)[\s\S]*?evalModeInput'\)\.value === 'verbose'[\s\S]*?renderVerboseEval\([\s\S]*?else \{[\s\S]*?renderRegularEval\(/,
  );
  const renderEvalStart = analysisHtml.indexOf('function renderEval(data, opts = {})');
  const renderEvalEnd = analysisHtml.indexOf('\nfunction evalEventIsCurrent(', renderEvalStart);
  const renderEvalSource = analysisHtml.slice(renderEvalStart, renderEvalEnd);
  assert.match(renderEvalSource, /scheduleEvalPaint\(\);/);
  assert.doesNotMatch(renderEvalSource, /renderRegularEval|renderVerboseEval|drawHoverArrow/);
  assert.match(
    analysisHtml,
    /function renderPvSan\(line\)[\s\S]*?pvSanCache\.get\(cacheKey\)[\s\S]*?MAX_CACHED_PV_FORMATS/,
  );
  assert.match(
    analysisHtml,
    /function drawHoverArrow\(\)[\s\S]*?signature === lastAutoShapeSignature[\s\S]*?board\.setAutoShapes\(shapes\)/,
  );
});

test('eval paint scheduler keeps only the latest frame and waits for Chessground', () => {
  const { context, frames, paints } = evalPaintScheduler();
  context.scheduleForTest();
  context.scheduleForTest();
  context.scheduleForTest();
  assert.equal(frames.length, 1);

  context.board = {
    state: {
      draggable: { current: { orig: 'e2' } },
      drawable: {},
      animation: {},
    },
  };
  frames.shift()();
  assert.deepEqual(paints, []);
  assert.equal(frames.length, 1);

  context.latestEvalData = { id: 'newest' };
  context.board.state.draggable.current = undefined;
  frames.shift()();
  assert.deepEqual(paints, [['regular', 'newest']]);
  assert.equal(frames.length, 0);
});

test('eval paint scheduler cancels stale queued work', () => {
  const { context, frames, paints } = evalPaintScheduler();
  context.scheduleForTest();
  assert.equal(frames.length, 1);
  context.cancelForTest();
  frames.shift()();
  assert.deepEqual(paints, []);
});

test('eval paint scheduler preserves row hover and paints only the selected mode', () => {
  const { context, frames, paints } = evalPaintScheduler({
    hoverMove: { move: 'e2e4' },
    el: () => ({ value: 'verbose' }),
  });
  context.scheduleForTest();
  frames.shift()();
  assert.deepEqual(paints, []);
  assert.equal(frames.length, 1);

  context.hoverMove = null;
  frames.shift()();
  assert.deepEqual(paints, [['verbose', 'latest']]);
});

test('engine arrows refresh at most every 240ms during a sustained drag', () => {
  const scheduler = evalArrowScheduler();
  scheduler.context.scheduleArrowForTest();
  scheduler.timers.shift().callback();
  assert.deepEqual(scheduler.draws, [1000]);

  scheduler.setNow(1100);
  scheduler.context.scheduleArrowForTest();
  scheduler.timers.shift().callback();
  assert.deepEqual(scheduler.draws, [1000]);
  assert.equal(scheduler.timers[0].delay, 140);

  scheduler.setNow(1240);
  scheduler.timers.shift().callback();
  assert.deepEqual(scheduler.draws, [1000, 1240]);
});

test('board editor rejects engine-hostile geometry before applying it', () => {
  assert.match(
    editedPositionError(
      '4k3/8/8/8/8/8/4P3/4K3 w - - 0 1 WB',
      new Map([
        ['e8', { color: 'black', role: 'king' }],
        ['e2', { color: 'white', role: 'pawn' }],
        ['e1', { color: 'white', role: 'king' }],
      ]),
    ) || '',
    /^$/,
  );
  assert.match(
    editedPositionError(
      '4k3/8/8/8/8/8/8/4K2P w - - 0 1 WB',
      new Map([
        ['e8', { color: 'black', role: 'king' }],
        ['e1', { color: 'white', role: 'king' }],
        ['h1', { color: 'white', role: 'pawn' }],
      ]),
    ),
    /Pawns cannot/,
  );
  assert.match(
    editedPositionError(
      '8/8/8/8/8/8/4k3/4K3 w - - 0 1 WB',
      new Map([
        ['e2', { color: 'black', role: 'king' }],
        ['e1', { color: 'white', role: 'king' }],
      ]),
    ),
    /side not to move/,
  );
});

test('position changes discard stale engine hover arrows', () => {
  assert.match(
    analysisHtml,
    /function syncActiveLine\(opts = \{\}\)[\s\S]*?hoverMove = null;[\s\S]*?renderBoard\(/,
  );
  assert.match(
    analysisHtml,
    /function commitUserMove\(token\)[\s\S]*?hoverMove = null;[\s\S]*?clearSelection\(\)/,
  );
});

test('managed analysis selects models by server-owned id', () => {
  assert.match(
    analysisHtml,
    /identity\.modelId = el\('modelSelect'\)\.value/,
  );
  assert.match(
    analysisHtml,
    /payload\.model_id = el\('modelSelect'\)\.value/,
  );
  assert.match(
    analysisHtml,
    /if \(data\.default_model\) select\.value = data\.default_model/,
  );
});
