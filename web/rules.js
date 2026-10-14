(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (!root || !root.document) return;

  const initialize = function () {
    api.initRuleBoards();
  };
  if (root.Chessground) initialize();
  else root.addEventListener('chessground-ready', initialize, { once: true });
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  const ABILITY_BRUSH = Object.freeze({
    key: 'ability',
    color: '#f05a28',
    opacity: 0.78,
    lineWidth: 12,
  });

  const RULE_EXAMPLES = Object.freeze([
    Object.freeze({
      id: 'ruleMateStart',
      fen: 'r2q3k/ppp3p1/2np2Pp/5b2/3P1B2/1Q4P1/PPP2P2/3R2K1 w - - 0 1 WB',
      shapes: Object.freeze([
        Object.freeze({
          orig: 'b3',
          dest: 'g8',
          brush: 'ability',
          label: Object.freeze({ text: '1', fill: '#f05a28' }),
        }),
        Object.freeze({
          orig: 'g8',
          dest: 'h7',
          brush: 'ability',
          label: Object.freeze({ text: '2', fill: '#f05a28' }),
        }),
      ]),
    }),
    Object.freeze({
      id: 'ruleMateFinish',
      fen: 'r2q3k/ppp3pQ/2np2Pp/5b2/3P1B2/6P1/PPP2P2/3R2K1 b - - 2 1 -B',
      lastMove: Object.freeze(['g8', 'h7']),
      check: 'black',
      shapes: Object.freeze([
        Object.freeze({
          orig: 'h7',
          dest: 'h8',
          brush: 'red',
        }),
      ]),
    }),
    Object.freeze({
      id: 'ruleEpAvailable',
      fen: '7k/8/8/8/3pP3/8/8/6K1 b - e3 0 1 -B',
      lastMove: Object.freeze(['e2', 'e4']),
      shapes: Object.freeze([
        Object.freeze({
          orig: 'd4',
          dest: 'e3',
          brush: 'green',
          label: Object.freeze({ text: 'e.p.', fill: '#2e7d32' }),
        }),
      ]),
    }),
    Object.freeze({
      id: 'ruleEpBypassed',
      fen: '7k/8/8/8/3pP3/8/8/6K1 b - - 1 1 -B',
      lastMove: Object.freeze(['h1', 'g1']),
      shapes: Object.freeze([
        Object.freeze({
          orig: 'e3',
          brush: 'red',
          label: Object.freeze({ text: '×', fill: '#b42318' }),
        }),
      ]),
    }),
  ]);

  let initialized = false;

  function boardConfig(example) {
    return {
      fen: example.fen,
      orientation: 'white',
      coordinates: true,
      coordinatesOnSquares: false,
      ranksPosition: 'left',
      viewOnly: true,
      check: example.check || false,
      lastMove: example.lastMove || undefined,
      animation: { enabled: false },
      movable: {
        free: false,
        color: undefined,
        dests: new Map(),
      },
      premovable: { enabled: false },
      draggable: { enabled: false },
      selectable: { enabled: false },
      drawable: {
        enabled: false,
        visible: true,
        autoShapes: example.shapes,
        brushes: { ability: ABILITY_BRUSH },
      },
    };
  }

  function initRuleBoards() {
    if (initialized || !root?.Chessground || !root.document) return false;
    let rendered = 0;
    for (const example of RULE_EXAMPLES) {
      const element = root.document.getElementById(example.id);
      if (!element) continue;
      root.Chessground(element, boardConfig(example));
      rendered++;
    }
    initialized = rendered > 0;
    return initialized;
  }

  return {
    ABILITY_BRUSH,
    RULE_EXAMPLES,
    boardConfig,
    initRuleBoards,
  };
});
