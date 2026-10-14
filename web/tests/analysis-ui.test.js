'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../analysis-ui.js');

test('native WDL_mu centipawns are formatted as pawn values', () => {
  assert.equal(ui.pawnText(0), '0.00');
  assert.equal(ui.pawnText(0.5), '+0.50');
  assert.equal(ui.pawnText(-0.5), '-0.50');
});

test('parent analysis projects the selected child immediately', () => {
  const projected = ui.projectAnalysisToChild({
    data: {
      lines: [
        {
          multipv: 1,
          white_cp: 37,
          white_mate: null,
          nodes: 800,
          pv: ['e2e4', 'c7c5', 'g1f3'],
        },
        {
          multipv: 2,
          white_cp: 12,
          white_mate: null,
          nodes: 800,
          pv: ['d2d4', 'd7d5'],
        },
      ],
    },
    move: 'e2e4',
    moveStats: [
      { move: 'e2e4', visits: 521 },
      { move: 'd2d4', visits: 177 },
    ],
    targetNodes: 800,
  });

  assert.equal(projected.inherited, true);
  assert.equal(projected.actual_nodes, 521);
  assert.equal(projected.target_nodes, 800);
  assert.equal(projected.bestmove, 'c7c5');
  assert.deepEqual(projected.lines, [{
    multipv: 1,
    white_cp: 37,
    white_mate: null,
    nodes: 521,
    pv: ['c7c5', 'g1f3'],
  }]);
  assert.deepEqual(projected.position_score, {
    white_cp: 37,
    white_mate: null,
  });
});

test('child projection keeps normal and Double Move edges distinct', () => {
  const source = {
    lines: [
      { multipv: 1, white_cp: 20, pv: ['e2e4+', 'e4e5', 'e7e6'] },
      { multipv: 2, white_cp: -5, pv: ['e2e4', 'e7e5'] },
    ],
  };

  const ability = ui.projectAnalysisToChild({
    data: source,
    move: 'e2e4+',
    moveStats: [{ move: 'e2e4+', visits: 90 }],
    targetNodes: 800,
  });
  const normal = ui.projectAnalysisToChild({
    data: source,
    move: 'e2e4',
    moveStats: [{ move: 'e2e4', visits: 30 }],
    targetNodes: 800,
  });

  assert.deepEqual(ability.lines[0].pv, ['e4e5', 'e7e6']);
  assert.equal(ability.actual_nodes, 90);
  assert.deepEqual(normal.lines[0].pv, ['e7e5']);
  assert.equal(normal.actual_nodes, 30);
});

test('Leela alternative width combines visits and Q drop', () => {
  const popular = ui.leelaArrowWidthFromMetrics({
    bestVisits: 100,
    alternativeVisits: 64,
    referenceQ: 0.4,
    alternativeQ: 0.38,
  });
  const rare = ui.leelaArrowWidthFromMetrics({
    bestVisits: 100,
    alternativeVisits: 4,
    referenceQ: 0.4,
    alternativeQ: 0.38,
  });
  const losing = ui.leelaArrowWidthFromMetrics({
    bestVisits: 100,
    alternativeVisits: 100,
    referenceQ: 0.4,
    alternativeQ: 0.08,
  });
  assert.ok(popular > rare);
  assert.equal(losing, null);
  assert.equal(ui.leelaArrowWidthFromMetrics({
    bestVisits: 100,
    alternativeVisits: 0,
    referenceQ: 0.4,
    alternativeQ: 0.4,
  }), null);
});

test('maneuver arrows understand both legs of a Double Move', () => {
  assert.deepEqual(
    ui.maneuverArrowShapes(
      ['e2e4+', 'e4e5', 'c7c6', 'e5d6', 'g8f6'],
      'w',
    ).map(shape => [shape.orig, shape.dest, shape.brush]),
    [
      ['e2', 'e4', 'ability'],
      ['e4', 'e5', 'ability'],
      ['e5', 'd6', 'paleBlue'],
    ],
  );
});

test('engine arrows keep Double Moves orange and scale alternatives', () => {
  const shapes = ui.engineArrowShapes({
    mode: 'best',
    visibleLines: 3,
    rootTurn: 'w',
    lines: [
      { multipv: 1, pv: ['e2e4+', 'e4e5'] },
      { multipv: 2, pv: ['g1f3'] },
      { multipv: 3, pv: ['d2d4'] },
    ],
    moveStats: [
      { move: 'e2e4+', visits: 100, q: 0.4, prior: 0.4 },
      { move: 'g1f3', visits: 40, q: 0.36, prior: 0.2 },
      { move: 'd2d4', visits: 10, q: 0.2, prior: 0.2 },
    ],
  });
  assert.equal(shapes[0].brush, 'ability');
  assert.equal(shapes[1].brush, 'paleGrey');
  assert.ok(shapes[1].modifiers.lineWidth >= 2);
});

test('engine arrows omit explicit zero-visit moves but retain missing-stat PVs', () => {
  const shapes = ui.engineArrowShapes({
    mode: 'best',
    visibleLines: 4,
    rootTurn: 'w',
    lines: [
      { multipv: 1, pv: ['e2e4'] },
      { multipv: 2, pv: ['g1f3'] },
      { multipv: 3, pv: ['d2d4'] },
    ],
    moveStats: [
      { move: 'e2e4', visits: 0, q: 0.5, prior: 0.6 },
      { move: 'g1f3', visits: 12, q: 0.45, prior: 0.2 },
    ],
  });
  assert.deepEqual(
    shapes.map(shape => shape.orig + shape.dest),
    ['g1f3', 'd2d4'],
  );
  assert.equal(shapes[0].brush, 'paleBlue');

  const inherited = ui.engineArrowShapes({
    lines: [{ multipv: 1, pv: ['c7c5'] }],
    moveStats: [],
  });
  assert.equal(inherited.length, 1);
  assert.equal(inherited[0].orig + inherited[0].dest, 'c7c5');
});

test('board editor round-trips placement and emits both ability flags', () => {
  const fen = 'r3k3/8/8/8/8/8/4P3/4K2R b - - 0 1 W-';
  const pieces = ui.piecesFromFen(fen);
  assert.equal(ui.placementFromPieces(pieces), fen.split(' ')[0]);
  assert.equal(
    ui.editorFen({
      pieces,
      turn: 'b',
      castling: 'qKq',
      whiteAbility: true,
      blackAbility: false,
    }),
    'r3k3/8/8/8/8/8/4P3/4K2R b Kq - 0 1 W-',
  );
});

test('board editor removes castling rights without their king and rook', () => {
  const pieces = ui.piecesFromFen(
    '4k3/8/8/8/8/8/8/4K3 w - - 0 1',
  );
  assert.equal(
    ui.editorFen({ pieces, castling: 'KQkq' }).split(' ')[2],
    '-',
  );
});

test('board editor palette parses pieces and flips only their color', () => {
  assert.deepEqual(ui.parseEditorPiece('white:knight'), {
    color: 'white',
    role: 'knight',
  });
  assert.equal(ui.parseEditorPiece('erase'), null);
  assert.equal(ui.toggleEditorPieceColor('white:rook'), 'black:rook');
  assert.equal(ui.toggleEditorPieceColor('black:pawn'), 'white:pawn');
  assert.equal(ui.toggleEditorPieceColor('erase'), 'erase');
});

test('placing an identical held piece clears it while other pieces replace', () => {
  const blackRook = { color: 'black', role: 'rook' };
  assert.equal(ui.editorPlacement(blackRook, 'black:rook'), undefined);
  assert.deepEqual(ui.editorPlacement(blackRook, 'white:rook'), {
    color: 'white',
    role: 'rook',
  });
  assert.equal(ui.editorPlacement(blackRook, 'erase'), undefined);
  assert.equal(ui.editorPlacement(blackRook, null), blackRook);
});
