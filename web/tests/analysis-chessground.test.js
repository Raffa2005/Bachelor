'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const webRoot = path.resolve(__dirname, '..');
const analysisHtml = fs.readFileSync(path.join(webRoot, 'index.html'), 'utf8');

function sha256(filename) {
  return crypto
    .createHash('sha256')
    .update(fs.readFileSync(path.join(webRoot, filename)))
    .digest('hex');
}

function analysisNavigationHarness(overrides = {}) {
  const start = analysisHtml.indexOf('function isTextEditingTarget(');
  const end = analysisHtml.indexOf('\nfunction promotionName(', start);
  assert.ok(start >= 0 && end > start);
  const calls = [];
  const navigationOptions = [];
  const timers = [];
  const settled = [];
  const context = {
    variationRequest: null,
    promotionRequest: null,
    keyboardNavigationActive: false,
    keyboardNavigationTimer: null,
    KEYBOARD_NAVIGATION_SETTLE_MS: 250,
    document: { querySelector: () => null },
    goToParent: options => {
      calls.push(-1);
      navigationOptions.push(options);
      return true;
    },
    goToPreferredChild: options => {
      calls.push(1);
      navigationOptions.push(options);
      return true;
    },
    setTimeout: (callback, delay) => {
      timers.push({ callback, delay, cleared: false });
      return timers.length;
    },
    clearTimeout: id => {
      if (timers[id - 1]) timers[id - 1].cleared = true;
    },
    rawMoves: ['e2e4'],
    el: () => ({ value: '' }),
    persistAnalysisTree: () => settled.push('persist'),
    syncMoveListSelection: () => settled.push('moves'),
    interimEvaluationForCurrentNode: () => null,
    latestEvalData: null,
    latestEvalIdentity: null,
    latestEvalPresentation: null,
    presentInterimEvaluation: () => settled.push('interim'),
    scheduleEval: options => settled.push(['eval', options]),
    ...overrides,
  };
  vm.createContext(context);
  vm.runInContext(
    analysisHtml.slice(start, end) +
      '\nglobalThis.navigateForTest = handleMoveNavigationKey;' +
      '\nglobalThis.keyupForTest = handleMoveNavigationKeyUp;',
    context,
  );
  return {
    calls,
    context,
    navigate: context.navigateForTest,
    keyup: context.keyupForTest,
    navigationOptions,
    settled,
    timers,
  };
}

function navigationTarget(kind) {
  const input = kind === 'text-input'
    ? { type: 'text' }
    : kind === 'checkbox'
      ? { type: 'checkbox' }
      : null;
  return {
    tagName: kind === 'select' ? 'SELECT' : 'BUTTON',
    closest(selector) {
      if (selector === 'input') return input;
      if (selector.startsWith('textarea')) {
        return kind === 'textarea' ? {} : null;
      }
      return null;
    },
  };
}

function navigationEvent(key, target, extra = {}) {
  return {
    key,
    target,
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
  };
}

test('analysis loads the exact pinned Chessground distribution', () => {
  assert.match(
    analysisHtml,
    /\/vendor\/chessground\/10\.1\.1\/assets\/chessground\.base\.css/,
  );
  assert.match(
    analysisHtml,
    /\/vendor\/chessground\/10\.1\.1\/assets\/chessground\.cburnett\.css/,
  );
  assert.match(
    analysisHtml,
    /<script type="module" src="\/chessground-loader\.js\?v=20260805c"><\/script>/,
  );
  assert.equal(
    sha256('vendor/chessground/10.1.1/dist/chessground.min.js'),
    '878c755e2aa1105d646d149266817fd96ecabe9bbefca65f445306273ab7196b',
  );
  assert.equal(
    sha256('vendor/chessground/10.1.1/assets/chessground.base.css'),
    'fbfa4b0e791c58c9e793161e543ce712d85663318a82e547aee7db8f817770f2',
  );
  assert.equal(
    sha256('vendor/chessground/10.1.1/assets/chessground.cburnett.css'),
    'f9216192b5383a83e62a81de1cf58520f7c251f6f24930f5710a3c797f78ced4',
  );
});

test('board editor reuses Chessground pieces and keeps click placement separate from drag', () => {
  const palette = analysisHtml.match(
    /<div class="piece-palette"[\s\S]*?<\/div>/,
  )?.[0] || '';
  assert.equal(
    (palette.match(/class="editor-piece-icon cg-wrap"/g) || []).length,
    12,
  );
  assert.doesNotMatch(palette, /[♔♕♖♗♘♙♚♛♜♝♞♟]/);
  assert.match(
    analysisHtml,
    /function editorSelectSquare\(square\)[\s\S]*?analysisUi\.editorPlacement\(current, editorPiece\)/,
  );
  assert.match(
    analysisHtml,
    /function editorPointerUp\(event\)[\s\S]*?if \(gesture\.moved[\s\S]*?requestAnimationFrame\(\(\) => editorSelectSquare\(square\)\)/,
  );
  assert.match(
    analysisHtml,
    /editorBoard'\)\.addEventListener\('contextmenu', editorToggleHeldColor\)/,
  );
  assert.doesNotMatch(
    analysisHtml,
    /events:\s*\{\s*select:\s*editorSelectSquare\s*\}/,
  );
});

test('analysis uses one persistent native Chessground interaction path', () => {
  assert.match(
    analysisHtml,
    /board = window\.Chessground\(el\('board'\), config\);[\s\S]*?board\.set\(config\);/,
  );
  assert.match(
    analysisHtml,
    /movable:\s*\{[\s\S]*?free:\s*false,[\s\S]*?dests:\s*interactive \? chessgroundDests\(\)[\s\S]*?events:\s*\{\s*after:\s*onBoardMove\s*\}/,
  );
  assert.match(analysisHtml, /premovable:\s*\{\s*enabled:\s*false\s*\}/);
  assert.match(
    analysisHtml,
    /draggable:\s*\{[\s\S]*?enabled:\s*interactive,[\s\S]*?showGhost:\s*true/,
  );
  assert.match(analysisHtml, /selectable:\s*\{\s*enabled:\s*interactive\s*\}/);
  assert.match(
    analysisHtml,
    /animation:\s*\{[\s\S]*?enabled:\s*Boolean\(animate\)[\s\S]*?duration:\s*BOARD_ANIMATION_MS/,
  );
  assert.match(analysisHtml, /const BOARD_ANIMATION_MS = 190;/);
  assert.match(
    analysisHtml,
    /const autoShapes = boardAutoShapes\(\);[\s\S]*?drawable:\s*\{[\s\S]*?enabled:\s*true,[\s\S]*?visible:\s*true,[\s\S]*?shapes:\s*sanitizeBoardShapes\([\s\S]*?autoShapes,[\s\S]*?onChange:\s*onBoardShapesChanged/,
  );
  assert.match(
    analysisHtml,
    /function drawHoverArrow\(\)[\s\S]*?signature === lastAutoShapeSignature[\s\S]*?board\.setAutoShapes\(shapes\);/,
  );
  assert.doesNotMatch(analysisHtml, /board\?*\.setShapes\(/);
  assert.doesNotMatch(
    analysisHtml,
    /chessboardjs|Chessboard\(|\.square-55d63|piece-417db|board-arrow|createElementNS|bindBoardPointerEvents|squareFromPointerEvent/,
  );
});

test('analysis offers only completable first legs when Double Move is armed', () => {
  assert.match(
    analysisHtml,
    /function completableDoubleFirstMoves\(\)[\s\S]*?analysisState\.canCompleteDoubleMove/,
  );
  assert.match(
    analysisHtml,
    /if \(abilityActive && !midDouble\) return completableDoubleFirstMoves\(\)/,
  );
  assert.match(
    analysisHtml,
    /No legal Double Move can be completed from this position/,
  );
});

test('analysis validates native moves against canonical variant state', () => {
  assert.match(
    analysisHtml,
    /function boardLegalMovesForCurrentPosition\(\)[\s\S]*?chess\.moves\(\{\s*verbose:\s*true\s*\}\)[\s\S]*?isForbiddenMidDoubleKingCapture/,
  );
  assert.match(
    analysisHtml,
    /function completeBoardMove\(source, target\)[\s\S]*?const requestRevision = analysisRevision;[\s\S]*?legalMoveCandidates\(source, target\)[\s\S]*?if \(!candidates\.some\(move => move\.promotion\)\) \{[\s\S]*?commitUserMove\(source \+ target\);[\s\S]*?requestRevision !== analysisRevision[\s\S]*?renderBoard\(false\);/,
  );
  assert.match(
    analysisHtml,
    /function chessgroundDests\(\)[\s\S]*?legalMovesForCurrentPosition\(\)[\s\S]*?dests\.set\(move\.from, targets\)/,
  );
  assert.match(
    analysisHtml,
    /const ordered = \['q', 'r', 'b', 'n'\];/,
  );
  assert.match(
    analysisHtml,
    /if \(promotionRequest\) cancelPromotion\(null\);/,
  );
});

test('analysis promotion dialog traps focus and restores it on every exit', () => {
  assert.match(
    analysisHtml,
    /const previouslyFocused = document\.activeElement;[\s\S]*?document\.body \? el\('board'\) : previouslyFocused;[\s\S]*?role', 'dialog'[\s\S]*?aria-modal', 'true'/,
  );
  assert.match(
    analysisHtml,
    /picker\.addEventListener\('keydown', event => \{[\s\S]*?event\.key === 'Escape'[\s\S]*?event\.key !== 'Tab'[\s\S]*?button:not\(\[disabled\]\)[\s\S]*?last\.focus\(\)[\s\S]*?first\.focus\(\)/,
  );
  assert.match(
    analysisHtml,
    /const focusTarget = restoreFocus\?\.isConnected \? restoreFocus : el\('board'\);[\s\S]*?focusTarget\?\.focus/,
  );
  assert.match(
    analysisHtml,
    /function onBoardKeyDown\(event\) \{\s*if \(promotionRequest\) \{[\s\S]*?cancelPromotion\(null\);[\s\S]*?\}\s*return;\s*\}/,
  );
});

test('analysis waits for native engine-leg animation completion with a bounded fallback', () => {
  assert.match(
    analysisHtml,
    /const BOARD_ANIMATION_FALLBACK_MS = BOARD_ANIMATION_MS \* 3;/,
  );
  assert.match(
    analysisHtml,
    /function boardAnimationActive\(\)[\s\S]*?board\?\.state\?\.animation\?\.current[\s\S]*?piece\.cgAnimating[\s\S]*?piece\.cgFading[\s\S]*?classList\.contains\('anim'\)/,
  );
  assert.match(
    analysisHtml,
    /function waitForBoardAnimationCompletion\(\)[\s\S]*?new MutationObserver[\s\S]*?visibilitychange[\s\S]*?setTimeout\(finish, BOARD_ANIMATION_FALLBACK_MS\)/,
  );
  assert.match(
    analysisHtml,
    /async function appendEngineMovesSequentially\(tokens\)[\s\S]*?await waitForBoardAnimationCompletion\(\);/,
  );
  assert.doesNotMatch(
    analysisHtml,
    /await new Promise\(resolve => setTimeout\(resolve, BOARD_ANIMATION_MS\)\)/,
  );
});

test('analysis board remains navigable and keyboard operable', () => {
  assert.match(
    analysisHtml,
    /id="board"[\s\S]*?class="cg-wrap"[\s\S]*?tabindex="0"[\s\S]*?role="application"/,
  );
  assert.match(analysisHtml, /el\('board'\)\.addEventListener\('keydown', onBoardKeyDown\)/);
  assert.match(analysisHtml, /function flipBoard\(\)[\s\S]*?board\?\.toggleOrientation\(\)/);
  assert.match(
    analysisHtml,
    /<nav class="analysis-site-nav"[\s\S]*?href="\/play"[\s\S]*?href="\/games"[\s\S]*?href="\/analysis"/,
  );
  assert.match(
    analysisHtml,
    /\.switch input\s*\{[^}]*width:\s*1px;[^}]*min-height:\s*0;[^}]*clip:\s*rect\(0 0 0 0\)/s,
  );
  assert.match(
    analysisHtml,
    /@media \(max-width: 680px\)[\s\S]*?\.workspace\s*\{[^}]*align-items:\s*stretch;/,
  );
});

test('analysis history arrows work from focused controls without stealing text editing', () => {
  const harness = analysisNavigationHarness();
  const focusedButton = navigationEvent('ArrowRight', navigationTarget('button'));
  harness.navigate(focusedButton);
  assert.deepEqual(harness.calls, [1]);
  assert.equal(focusedButton.prevented, true);
  assert.equal(focusedButton.stopped, true);

  const focusedSelect = navigationEvent('ArrowLeft', navigationTarget('select'));
  harness.navigate(focusedSelect);
  assert.deepEqual(harness.calls, [1, -1]);

  const textInput = navigationEvent('ArrowRight', navigationTarget('text-input'));
  harness.navigate(textInput);
  assert.deepEqual(harness.calls, [1, -1]);
  assert.equal(textInput.prevented, false);

  const modified = navigationEvent(
    'ArrowRight',
    navigationTarget('button'),
    { shiftKey: true },
  );
  harness.navigate(modified);
  assert.deepEqual(harness.calls, [1, -1]);

  harness.context.document.querySelector = () => ({ open: true });
  harness.navigate(navigationEvent('ArrowLeft', navigationTarget('button')));
  assert.deepEqual(harness.calls, [1, -1]);

  assert.match(
    analysisHtml,
    /document\.addEventListener\('keydown', handleMoveNavigationKey, true\);/,
  );
});

test('held analysis navigation settles once and suppresses repeated animation', () => {
  const harness = analysisNavigationHarness();
  const repeated = navigationEvent(
    'ArrowRight',
    navigationTarget('button'),
    { repeat: true },
  );
  for (let index = 0; index < 12; index++) {
    harness.navigate({ ...repeated });
  }

  assert.equal(harness.navigationOptions.length, 12);
  assert.equal(harness.navigationOptions[0].animate, false);
  assert.equal(harness.navigationOptions[0].keyboardNavigation, true);
  assert.equal(harness.navigationOptions[0].persist, false);
  assert.equal(harness.navigationOptions[0].scheduleEval, false);
  assert.equal(harness.timers[0].delay, 250);
  assert.equal(harness.timers.length, 12);
  assert.ok(harness.timers.slice(0, -1).every(timer => timer.cleared));
  assert.equal(harness.timers.at(-1).cleared, false);
  assert.deepEqual(harness.settled, []);

  harness.keyup(navigationEvent('ArrowRight', navigationTarget('button')));
  assert.deepEqual(harness.settled.slice(0, 3), [
    'persist',
    'moves',
    'interim',
  ]);
  assert.equal(harness.settled[3][0], 'eval');
  assert.equal(harness.settled[3][1].immediate, true);
  assert.equal(harness.context.keyboardNavigationActive, false);
  assert.match(
    analysisHtml,
    /document\.addEventListener\('keyup', handleMoveNavigationKeyUp, true\);/,
  );
  assert.match(
    analysisHtml,
    /window\.addEventListener\('blur', settleKeyboardNavigation\);/,
  );
});

test('analysis caches variation metadata and only changes selection during held navigation', () => {
  const metadataStart = analysisHtml.indexOf('function variationMetadata()');
  const metadataEnd = analysisHtml.indexOf(
    '\nfunction activeLineFromVariationMetadata()',
    metadataStart,
  );
  assert.ok(metadataStart >= 0 && metadataEnd > metadataStart);
  const metadataSource = analysisHtml.slice(metadataStart, metadataEnd);
  assert.match(
    metadataSource,
    /if \(!variationMetadataCache\)[\s\S]*?analysisState\.deriveTreeMetadata\(/,
  );

  const renderStart = analysisHtml.indexOf('function renderMoveList()');
  const renderEnd = analysisHtml.indexOf('\nfunction updateUI()', renderStart);
  assert.ok(renderStart >= 0 && renderEnd > renderStart);
  const renderSource = analysisHtml.slice(renderStart, renderEnd);
  assert.match(renderSource, /const treeMetadata = variationMetadata\(\);/);
  assert.doesNotMatch(renderSource, /analysisState\.deriveLine|analysisState\.activeTokens/);
  assert.match(
    analysisHtml,
    /if \(opts\.keyboardNavigation\) \{\s*syncMoveListSelection\(\);/,
  );
  assert.match(
    analysisHtml,
    /el\('moveList'\)\.addEventListener\('click', event =>/,
  );
});
