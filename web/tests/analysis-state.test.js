'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { Chess } = require('../vendor/chess.js/chess-0.12.0.js');
const state = require('../analysis-state.js');

function fenField(fen, index) {
  return fen.split(/\s+/)[index];
}

function pieceOn(fen, square) {
  const chess = state.loadChess(Chess, fen);
  assert.ok(chess, 'expected a chess.js-loadable FEN');
  const piece = chess.get(square);
  return piece ? piece.color + piece.type : null;
}

test('White can move the same pawn twice after activating Double Move', () => {
  const line = state.deriveLine('startpos', ['e2e4+', 'e4e5'], Chess);

  assert.equal(fenField(line.fens[1], 1), 'w');
  assert.equal(fenField(line.fens[1], 3), '-');
  assert.equal(pieceOn(line.fens[1], 'e4'), 'wp');
  const midpoint = state.loadChess(Chess, line.fens[1]);
  assert.ok(midpoint.moves({ verbose: true }).some(move => move.from === 'e4' && move.to === 'e5'));

  assert.equal(fenField(line.fens[2], 1), 'b');
  assert.equal(pieceOn(line.fens[2], 'e5'), 'wp');
  assert.deepEqual(line.colors, ['w', 'w']);
  assert.deepEqual(line.doubleFlags, [true, true]);
  assert.deepEqual(line.moveNumbers, [1, 1]);
});

test('Black can move the same pawn twice after activating Double Move', () => {
  const line = state.deriveLine('startpos', ['g1f3', 'e7e5+', 'e5e4'], Chess);

  assert.equal(fenField(line.fens[2], 1), 'b');
  assert.equal(fenField(line.fens[2], 3), '-');
  assert.equal(pieceOn(line.fens[2], 'e5'), 'bp');
  const midpoint = state.loadChess(Chess, line.fens[2]);
  assert.ok(midpoint.moves({ verbose: true }).some(move => move.from === 'e5' && move.to === 'e4'));

  assert.equal(fenField(line.fens[3], 1), 'w');
  assert.equal(fenField(line.fens[3], 5), '2');
  assert.equal(pieceOn(line.fens[3], 'e4'), 'bp');
  assert.deepEqual(line.colors, ['w', 'b', 'b']);
  assert.deepEqual(line.doubleFlags, [false, true, true]);
  assert.deepEqual(line.moveNumbers, [1, 1, 1]);
});

test('Double Move first legs must leave a legal second half', () => {
  const trapped = '7k/8/8/8/p7/2nn4/P7/K7 w - - 0 1 WB';

  const ordinary = state.deriveLine(trapped, ['a2a3'], Chess);
  assert.equal(ordinary.valid, true);
  assert.equal(state.canCompleteDoubleMove(Chess, trapped, 'a2a3'), false);

  const activated = state.deriveLine(trapped, ['a2a3+'], Chess);
  assert.equal(activated.valid, false);
  assert.match(activated.error, /leaves no legal second half/);
  assert.deepEqual(activated.rawMoves, []);

  const impossibleMidpoint =
    '7k/8/8/8/p7/P1nn4/8/K7 w - - 0 1 -Bm';
  const imported = state.deriveLine(impossibleMidpoint, [], Chess);
  assert.equal(imported.valid, false);
  assert.match(imported.error, /Mid-double FEN has no legal second half/);
});

test('a checking first half remains legal when its Double Move can finish', () => {
  const fen =
    'r2q3k/ppp3p1/2np2Pp/5b2/3P1B2/1Q4P1/PPP2P2/3R2K1 w - - 0 1 WB';

  assert.equal(state.canCompleteDoubleMove(Chess, fen, 'b3g8'), true);
  const line = state.deriveLine(fen, ['b3g8+', 'g8h7'], Chess);
  assert.equal(line.valid, true);
  assert.equal(line.midDouble, false);
});

test('en-passant targets exist only after a pawn double-step that flips the turn', () => {
  const whiteNormal = state.applyUciToFen(state.START_FEN, 'e2e4', true);
  assert.equal(fenField(whiteNormal, 3), 'e3');

  const blackStart = '8/4p3/8/8/8/8/8/4K2k b - - 0 9';
  const blackNormal = state.applyUciToFen(blackStart, 'e7e5', true);
  assert.equal(fenField(blackNormal, 3), 'e6');
  assert.equal(fenField(blackNormal, 5), '10');

  const whiteFirstHalf = state.applyUciToFen(state.START_FEN, 'e2e4', false);
  assert.equal(fenField(whiteFirstHalf, 1), 'w');
  assert.equal(fenField(whiteFirstHalf, 3), '-');

  const secondLegDoubleStep = state.deriveLine('startpos', ['g1f3+', 'e2e4'], Chess);
  assert.equal(fenField(secondLegDoubleStep.fens[2], 1), 'b');
  assert.equal(fenField(secondLegDoubleStep.fens[2], 3), 'e3');
});

test('chess.js loading rejects invalid FEN instead of silently changing it', () => {
  const staleMidpoint = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e3 0 1';
  assert.equal(state.loadChess(Chess, staleMidpoint), null);
  assert.equal(state.loadChess(Chess, 'not a fen'), null);
});

test('regex-valid illegal and wrong-color histories are rejected explicitly', () => {
  const illegalPawn = state.deriveLine('startpos', ['e2e5'], Chess);
  assert.equal(illegalPawn.valid, false);
  assert.match(illegalPawn.error, /Illegal move/);
  assert.deepEqual(illegalPawn.rawMoves, []);
  assert.equal(illegalPawn.fens.length, 1);

  const unmarkedSameColor = state.deriveLine('startpos', ['e2e4', 'g1f3'], Chess);
  assert.equal(unmarkedSameColor.valid, false);
  assert.match(unmarkedSameColor.error, /g1f3/);
  assert.deepEqual(unmarkedSameColor.rawMoves, ['e2e4']);
  assert.equal(unmarkedSameColor.fens.length, 2);
});

test('the second half of Double Move cannot capture the opponent king', () => {
  const fen = 'k7/8/8/8/8/8/8/R3K3 w - - 0 1';
  const line = state.deriveLine(fen, ['a1a7+', 'a7a8'], Chess);
  assert.equal(line.valid, false);
  assert.match(line.error, /cannot capture the opponent king/);
  assert.deepEqual(line.rawMoves, ['a1a7+']);
  assert.equal(line.midDouble, true);
});

test('the optional seventh FEN field is preserved and drives ability state', () => {
  assert.deepEqual(
    ['WB', 'W-', '-B', '--', '-Bm', 'W-m'].map(token => state.parseAbilityState(state.START_FEN + ' ' + token)),
    [
      { valid: true, w: true, b: true, midDouble: false, token: 'WB' },
      { valid: true, w: true, b: false, midDouble: false, token: 'W-' },
      { valid: true, w: false, b: true, midDouble: false, token: '-B' },
      { valid: true, w: false, b: false, midDouble: false, token: '--' },
      { valid: true, w: false, b: true, midDouble: true, token: '-Bm' },
      { valid: true, w: true, b: false, midDouble: true, token: 'W-m' },
    ]
  );

  const noWhiteAbility = state.START_FEN + ' -B';
  const initial = state.deriveLine(noWhiteAbility, [], Chess);
  assert.equal(initial.valid, true);
  assert.deepEqual(initial.abilityAvailable, { w: false, b: true });
  assert.equal(initial.midDouble, false);

  const invalidBurn = state.deriveLine(noWhiteAbility, ['e2e4+'], Chess);
  assert.equal(invalidBurn.valid, false);
  assert.match(invalidBurn.error, /White has already used/);

  const existingMidDouble = state.deriveLine(state.START_FEN + ' -Bm', ['e2e3'], Chess);
  assert.equal(existingMidDouble.valid, true);
  assert.deepEqual(existingMidDouble.doubleFlags, [true]);
  assert.deepEqual(existingMidDouble.abilityAvailable, { w: false, b: true });
  assert.equal(existingMidDouble.midDouble, false);

  const inconsistentMidDouble = state.deriveLine(state.START_FEN + ' W-m', [], Chess);
  assert.equal(inconsistentMidDouble.valid, false);
  assert.match(inconsistentMidDouble.error, /moving side ability available/);

  const firstHalfFen = state.applyUciToFen(state.START_FEN + ' WB', 'e2e4', false);
  assert.equal(fenField(firstHalfFen, 6), '-Bm');
  const completedFen = state.applyUciToFen(firstHalfFen, 'e4e5', true);
  assert.equal(fenField(completedFen, 6), '-B');

  const copiedMidpoint = state.canonicalVariantFen(
    state.deriveLine('startpos', ['e2e4+'], Chess).fens[1],
    { w: false, b: true },
    true
  );
  assert.equal(fenField(copiedMidpoint, 6), '-Bm');
  assert.equal(
    state.canonicalVariantFen(state.START_FEN, { w: true, b: true }, false),
    state.START_FEN
  );

  const tree = state.createTree(noWhiteAbility);
  assert.equal(state.activePayload(tree).fen, noWhiteAbility);
});

test('ability availability is isolated to the selected variation path', () => {
  const tree = state.createTree('startpos');
  state.addMoves(tree, ['e2e4+', 'e4e5']);
  const burnedLeaf = tree.currentId;
  state.goToNode(tree, tree.rootId);
  state.addMoves(tree, ['d2d4']);
  const unburnedLeaf = tree.currentId;

  const burned = state.deriveLine(tree.startFen, state.activeTokens(tree, burnedLeaf), Chess);
  const unburned = state.deriveLine(tree.startFen, state.activeTokens(tree, unburnedLeaf), Chess);
  assert.deepEqual(burned.abilityAvailable, { w: false, b: true });
  assert.deepEqual(unburned.abilityAvailable, { w: true, b: true });
});

test('branching preserves the original main line and reuses an identical child', () => {
  const tree = state.treeFromMoves('startpos', ['e2e4', 'e7e5', 'g1f3'], true);
  const originalCount = state.countNodes(tree);
  const e4Id = state.pathIds(tree)[1];
  const originalReplyId = tree.nodes[e4Id].children[0];

  state.goToNode(tree, e4Id);
  const firstAdd = state.addMoves(tree, ['c7c5']);
  const sicilianId = tree.currentId;

  assert.equal(firstAdd.createdIds.length, 1);
  assert.deepEqual(tree.nodes[e4Id].children, [originalReplyId, sicilianId]);
  assert.equal(tree.nodes[originalReplyId].raw, 'e7e5');
  assert.equal(state.countNodes(tree), originalCount + 1);

  state.goToNode(tree, e4Id);
  const secondAdd = state.addMoves(tree, ['c7c5']);
  assert.deepEqual(secondAdd.createdIds, []);
  assert.deepEqual(secondAdd.reusedIds, [sicilianId]);
  assert.equal(tree.currentId, sicilianId);
  assert.equal(state.countNodes(tree), originalCount + 1);
});

test('tree metadata derives branched SAN, FEN, and Double Move state once per node', () => {
  const tree = state.treeFromMoves(
    'startpos',
    ['e2e4', 'e7e5', 'g1f3'],
    true,
  );
  const e4Id = state.pathIds(tree)[1];
  state.goToNode(tree, tree.rootId);
  state.addMoves(tree, ['d2d4+', 'd4d5']);
  state.goToNode(tree, e4Id);
  state.addMoves(tree, ['c7c5+', 'c5c4']);

  const metadata = state.deriveTreeMetadata(tree, Chess);

  assert.equal(metadata.valid, true);
  assert.equal(metadata.error, null);
  assert.equal(metadata.order.length, state.countNodes(tree) + 1);
  assert.equal(metadata.nodes[tree.rootId].fen, state.START_FEN);
  assert.deepEqual(
    metadata.nodes[tree.rootId].abilityAvailable,
    { w: true, b: true },
  );

  for (const nodeId of Object.keys(tree.nodes)) {
    if (nodeId === tree.rootId) continue;
    const line = state.deriveLine(
      tree.startFen,
      state.activeTokens(tree, nodeId),
      Chess,
    );
    const index = line.rawMoves.length - 1;
    const actual = metadata.nodes[nodeId];
    assert.equal(actual.valid, true, nodeId);
    assert.equal(actual.fen, line.fens[line.fens.length - 1], nodeId);
    assert.equal(actual.san, line.moveSans[index], nodeId);
    assert.equal(actual.color, line.colors[index], nodeId);
    assert.equal(actual.moveNumber, line.moveNumbers[index], nodeId);
    assert.equal(actual.isDouble, line.doubleFlags[index], nodeId);
    assert.deepEqual(actual.moveSquares, line.moveSquares[index], nodeId);
    assert.deepEqual(actual.abilityAvailable, line.abilityAvailable, nodeId);
    assert.equal(actual.midDouble, line.midDouble, nodeId);
  }
});

test('cached tree metadata reconstructs every active branch exactly', () => {
  const tree = state.treeFromMoves(
    'startpos',
    ['e2e4', 'e7e5', 'g1f3'],
    true,
  );
  const e4Id = state.pathIds(tree)[1];
  state.goToNode(tree, tree.rootId);
  state.addMoves(tree, ['d2d4+', 'd4d5']);
  state.goToNode(tree, e4Id);
  state.addMoves(tree, ['c7c5+', 'c5c4']);
  const metadata = state.deriveTreeMetadata(tree, Chess);

  for (const nodeId of Object.keys(tree.nodes)) {
    const expected = state.deriveLine(
      tree.startFen,
      state.activeTokens(tree, nodeId),
      Chess,
    );
    const actual = state.activeLineFromTreeMetadata(
      tree,
      metadata,
      nodeId,
    );
    assert.ok(actual, nodeId);
    assert.deepEqual(actual, expected, nodeId);
  }
});

test('tree metadata construction scales linearly with long variation depth', () => {
  const moves = Array.from(
    { length: 160 },
    (_, index) => ['g1f3', 'g8f6', 'f3g1', 'f6g8'][index % 4],
  );
  const tree = state.treeFromMoves('startpos', moves, true);
  let chessConstructions = 0;
  function CountingChess(...args) {
    chessConstructions++;
    return new Chess(...args);
  }

  const metadata = state.deriveTreeMetadata(tree, CountingChess);

  assert.equal(metadata.valid, true);
  assert.equal(metadata.order.length, moves.length + 1);
  assert.equal(
    chessConstructions,
    moves.length + 1,
    'one root validation plus one parent-to-child derivation per move',
  );
  assert.ok(
    chessConstructions < moves.length * (moves.length + 3) / 2,
    'must not replay every root-to-node prefix',
  );
});

test('tree metadata reports illegal nodes and descendants without throwing', () => {
  const tree = state.createTree('startpos');
  state.addMoves(tree, ['e2e4', 'g1f3', 'h7h6']);
  const path = state.pathIds(tree);

  const metadata = state.deriveTreeMetadata(tree, Chess);

  assert.equal(metadata.valid, false);
  assert.equal(metadata.invalidNodeId, path[2]);
  assert.match(metadata.nodes[path[2]].error, /Illegal move/);
  assert.match(metadata.nodes[path[3]].error, /invalid parent/);
  assert.deepEqual(metadata.invalidNodeIds, [path[2], path[3]]);
});

test('the plus marker is part of variation child identity', () => {
  const tree = state.createTree('startpos');
  state.addMoves(tree, ['e2e4']);
  state.goToNode(tree, tree.rootId);
  state.addMoves(tree, ['e2e4+']);

  const children = tree.nodes[tree.rootId].children;
  assert.equal(children.length, 2);
  assert.deepEqual(children.map(id => tree.nodes[id].raw), ['e2e4', 'e2e4+']);
});

test('preferred-line and sibling navigation follow ordered children and wrap', () => {
  const tree = state.createTree('startpos');
  state.addMoves(tree, ['e2e4', 'e7e5']);
  const e4Id = state.pathIds(tree)[1];
  const e5Id = tree.currentId;
  state.goToNode(tree, e4Id);
  state.addMoves(tree, ['c7c5']);
  const c5Id = tree.currentId;

  state.navigateSibling(tree, 1);
  assert.equal(tree.currentId, e5Id);
  state.navigateSibling(tree, -1);
  assert.equal(tree.currentId, c5Id);

  state.promotePath(tree);
  assert.equal(tree.nodes[e4Id].children[0], c5Id);
  state.goToNode(tree, e4Id);
  state.navigatePreferredChild(tree);
  assert.equal(tree.currentId, c5Id);
  state.navigateParent(tree);
  assert.equal(tree.currentId, e4Id);
  state.goToEnd(tree);
  assert.equal(tree.currentId, c5Id);
});

test('deleting a variation removes only its subtree and selects its parent', () => {
  const tree = state.treeFromMoves('startpos', ['e2e4', 'e7e5', 'g1f3'], true);
  const e4Id = state.pathIds(tree)[1];
  const mainReplyId = tree.nodes[e4Id].children[0];
  state.goToNode(tree, e4Id);
  state.addMoves(tree, ['c7c5', 'g1f3']);
  const branchRootId = tree.nodes[e4Id].children[1];
  const branchLeafId = tree.currentId;

  assert.equal(state.deleteSubtree(tree, branchRootId), true);
  assert.equal(tree.currentId, e4Id);
  assert.ok(tree.nodes[mainReplyId]);
  assert.equal(tree.nodes[branchRootId], undefined);
  assert.equal(tree.nodes[branchLeafId], undefined);
  assert.deepEqual(tree.nodes[e4Id].children, [mainReplyId]);
  assert.equal(state.deleteSubtree(tree, tree.rootId), false);
});

test('tree serialization preserves branch order, current node, and next identity', () => {
  const tree = state.createTree('startpos');
  state.addMoves(tree, ['e2e4', 'e7e5']);
  const e4Id = state.pathIds(tree)[1];
  state.goToNode(tree, e4Id);
  state.addMoves(tree, ['c7c5']);
  const currentId = tree.currentId;

  const restored = state.deserializeTree(state.serializeTree(tree), 'startpos');
  assert.deepEqual(restored, tree);
  assert.equal(restored.currentId, currentId);
  state.addMoves(restored, ['g1f3']);
  assert.notEqual(restored.currentId, currentId);

  const fallback = state.deserializeTree('{broken', 'startpos');
  assert.equal(fallback.currentId, fallback.rootId);
  assert.equal(state.countNodes(fallback), 0);
});

test('tree deserialization bounds depth, size, and unsafe next identifiers', () => {
  const nodes = {
    root: { id: 'root', parentId: null, raw: null, children: ['n1'] },
  };
  for (let index = 1; index <= 2050; index++) {
    nodes['n' + index] = {
      id: 'n' + index,
      parentId: index === 1 ? 'root' : 'n' + (index - 1),
      raw: index % 2 ? 'e2e3' : 'e7e6',
      children: index === 2050 ? [] : ['n' + (index + 1)],
    };
  }
  const tooDeep = state.deserializeTree(JSON.stringify({
    version: 1,
    startFen: 'startpos',
    rootId: 'root',
    currentId: 'n2050',
    nextId: 2051,
    nodes,
  }), 'startpos');
  assert.equal(state.countNodes(tooDeep), 0);

  const huge = state.deserializeTree(' '.repeat(1_000_001), 'startpos');
  assert.equal(state.countNodes(huge), 0);

  const unsafe = state.createTree('startpos');
  state.addMoves(unsafe, ['e2e4']);
  const serialized = JSON.parse(state.serializeTree(unsafe));
  serialized.nextId = 1e308;
  const restored = state.deserializeTree(serialized, 'startpos');
  state.addMoves(restored, ['e7e5']);
  assert.equal(restored.nodes[restored.currentId].raw, 'e7e5');
  assert.ok(Number.isSafeInteger(restored.nextId));
});

test('tree construction and iterative deserialization accept 2048-move lines', () => {
  const moves = Array.from(
    { length: 2048 },
    (_, index) => index % 2 ? 'e7e6' : 'e2e3',
  );
  const tree = state.createTree('startpos');
  state.addMoves(tree, moves);
  assert.equal(state.countNodes(tree), 2048);
  assert.throws(
    () => state.addMoves(tree, ['g1f3']),
    /2048-move safety limit/,
  );

  const restored = state.deserializeTree(
    state.serializeTree(tree),
    'startpos',
  );
  assert.equal(state.countNodes(restored), 2048);
  assert.equal(state.activeTokens(restored).length, 2048);
});

test('engine payload contains only the active root-to-current path', () => {
  const tree = state.treeFromMoves('startpos', ['e2e4', 'e7e5', 'g1f3'], true);
  const e4Id = state.pathIds(tree)[1];
  state.goToNode(tree, e4Id);
  state.addMoves(tree, ['c7c5+', 'c5c4']);

  assert.deepEqual(state.activePayload(tree, undefined, Chess), {
    fen: 'startpos',
    moves: ['e2e4', 'c7c5+', 'c5c4'],
  });

  state.navigateParent(tree);
  assert.deepEqual(state.activePayload(tree, { nodes: 42 }, Chess), {
    fen: 'startpos',
    moves: ['e2e4', 'c7c5+'],
    nodes: 42,
  });
});

test('validated engine payload cannot diverge from the derived active line', () => {
  const tree = state.createTree('startpos');
  state.addMoves(tree, ['e2e4', 'g1f3']);

  const line = state.deriveLine(tree.startFen, state.activeTokens(tree), Chess);
  assert.equal(line.valid, false);
  assert.deepEqual(line.rawMoves, ['e2e4']);
  assert.throws(
    () => state.activePayload(tree, undefined, Chess),
    /Illegal move/
  );
  assert.equal(state.validateTree(tree, Chess).valid, false);
});

test('archived import validates and preserves a two-leg pawn line', () => {
  const prepared = state.prepareArchivedGame({
    initial_fen: 'startpos',
    moves: ['e2e4+', 'e4e5'],
  }, Chess);

  assert.equal(prepared.startFen, 'startpos');
  assert.deepEqual(prepared.moves, ['e2e4+', 'e4e5']);
  assert.deepEqual(prepared.line.colors, ['w', 'w']);
  assert.match(prepared.line.fens[1], / w /);
  assert.match(prepared.line.fens[2], / b /);
});

test('archived import rejects malformed FEN and illegal variant history', () => {
  assert.throws(
    () => state.prepareArchivedGame({
      initial_fen: 'not a fen',
      moves: ['e2e4'],
    }, Chess),
    /FEN|position|invalid/i,
  );
  assert.throws(
    () => state.prepareArchivedGame({
      initial_fen: 'startpos',
      moves: ['e2e4', 'g1f3'],
    }, Chess),
    /Illegal move/i,
  );
});

test('archived import accepts 2048 legal legs and rejects 2049', () => {
  const cycle = ['g1f3', 'g8f6', 'f3g1', 'f6g8'];
  const moves = Array.from(
    { length: 2048 },
    (_, index) => cycle[index % cycle.length],
  );
  const prepared = state.prepareArchivedGame({
    initial_fen: 'startpos',
    moves,
  }, Chess);
  assert.equal(prepared.moves.length, 2048);
  assert.equal(prepared.line.rawMoves.length, 2048);

  assert.throws(
    () => state.prepareArchivedGame({
      initial_fen: 'startpos',
      moves: moves.concat('g1f3'),
    }, Chess),
    /move list is invalid/i,
  );
});
