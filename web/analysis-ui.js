(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.DoubleMoveAnalysisUi = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const ENGINE_STREAM_MULTIPV = 8;
  const MAX_LEELA_WIN_CHANCE_DROP = 0.16;
  const MOVE_RE = /^[a-h][1-8][a-h][1-8][qrbn]?\+?$/;

  function finiteMetric(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function normalizeVisibleLineCount(value, fallback = 5) {
    const parsed = Number.parseInt(String(value), 10);
    const safeFallback = Math.max(
      1,
      Math.min(ENGINE_STREAM_MULTIPV, Math.round(Number(fallback) || 1)),
    );
    if (!Number.isFinite(parsed)) return safeFallback;
    return Math.max(1, Math.min(ENGINE_STREAM_MULTIPV, parsed));
  }

  function pawnText(value) {
    const pawns = finiteMetric(value);
    if (pawns === null) return 'N/A';
    const rounded = Math.abs(pawns) < 0.005 ? 0 : pawns;
    return (rounded > 0 ? '+' : '') + rounded.toFixed(2);
  }

  function projectAnalysisToChild({
    data,
    move,
    moveStats = [],
    targetNodes,
  } = {}) {
    const selectedMove = String(move || '');
    if (!data || !MOVE_RE.test(selectedMove)) return null;
    const sourceLines = Array.isArray(data.lines) ? data.lines : [];
    const sourceLine = sourceLines.find(
      line => Array.isArray(line?.pv) && line.pv[0] === selectedMove,
    );
    const sourceStat = (moveStats || []).find(
      stat => String(stat?.move || stat?.uci || '') === selectedMove,
    );
    if (!sourceLine && !sourceStat) return null;

    const visits = finiteMetric(sourceStat?.visits);
    const continuation = sourceLine?.pv?.slice(1) || [];
    const projectedLine = sourceLine
      ? {
          ...sourceLine,
          multipv: 1,
          nodes: visits === null ? sourceLine.nodes : visits,
          pv: continuation,
        }
      : null;
    return {
      type: 'inherited',
      inherited: true,
      actual_nodes: visits,
      target_nodes: finiteMetric(targetNodes),
      bestmove: continuation[0] || null,
      lines: continuation.length ? [projectedLine] : [],
      position_score: sourceLine
        ? {
            white_cp: sourceLine.white_cp,
            white_mate: sourceLine.white_mate,
          }
        : null,
      verbose: [],
    };
  }

  function leelaArrowWidthFromMetrics({
    bestVisits,
    alternativeVisits,
    referenceQ,
    alternativeQ,
    fallbackIndex = 0,
  }) {
    const bestVisitCount = finiteMetric(bestVisits);
    const alternativeVisitCount = finiteMetric(alternativeVisits);
    const reference = finiteMetric(referenceQ);
    const alternative = finiteMetric(alternativeQ);
    let qualityStrength = 1;

    if (alternativeVisitCount !== null && alternativeVisitCount <= 0) {
      return null;
    }
    if (reference !== null && alternative !== null) {
      const winChanceDrop = Math.max(0, (reference - alternative) / 2);
      if (winChanceDrop >= MAX_LEELA_WIN_CHANCE_DROP) return null;
      qualityStrength = 1 - winChanceDrop / MAX_LEELA_WIN_CHANCE_DROP;
    }

    if (
      bestVisitCount !== null &&
      bestVisitCount >= 0 &&
      alternativeVisitCount !== null
    ) {
      const visitStrength = Math.sqrt(
        Math.min(
          1,
          (Math.max(0, alternativeVisitCount) + 1) /
            (bestVisitCount + 1),
        ),
      );
      return Math.max(
        2,
        Math.min(
          12,
          Math.round(2 + 10 * visitStrength * qualityStrength),
        ),
      );
    }
    return Math.max(2, 10 - Math.max(0, Number(fallbackIndex) || 0) * 2);
  }

  function statForMove(statsByMove, move) {
    return statsByMove.get(move) || statsByMove.get(String(move).replace(/\+$/, ''));
  }

  function rankedAlternativeLines(lines, moveStats) {
    const stats = new Map(
      (moveStats || []).map(stat => [String(stat.move || stat.uci || ''), stat]),
    );
    const metric = value => finiteMetric(value);
    return [...(lines || [])]
      .map((line, index) => ({
        line,
        index,
        stat: statForMove(stats, line && line.pv && line.pv[0]),
      }))
      .sort((a, b) => {
        for (const key of ['visits', 'q', 'prior']) {
          const aValue = metric(a.stat && a.stat[key]);
          const bValue = metric(b.stat && b.stat[key]);
          if (aValue === null && bValue !== null) return 1;
          if (aValue !== null && bValue === null) return -1;
          if (aValue !== null && bValue !== null && aValue !== bValue) {
            return bValue - aValue;
          }
        }
        return (
          Number(a.line && a.line.multipv || a.index + 1) -
            Number(b.line && b.line.multipv || b.index + 1) ||
          a.index - b.index
        );
      })
      .map(item => item.line);
  }

  function credibleReferenceQ(moveStats, bestStat) {
    const bestVisits = Math.max(0, finiteMetric(bestStat && bestStat.visits) || 0);
    const minimumVisits = Math.max(1, Math.floor(bestVisits * 0.02));
    const values = (moveStats || [])
      .filter(stat => (finiteMetric(stat && stat.visits) || 0) >= minimumVisits)
      .map(stat => finiteMetric(stat && stat.q))
      .filter(value => value !== null);
    const bestQ = finiteMetric(bestStat && bestStat.q);
    if (bestQ !== null) values.push(bestQ);
    return values.length ? Math.max(...values) : null;
  }

  function moveArrowShape(move, brush, lineWidth) {
    if (!MOVE_RE.test(String(move || ''))) return null;
    const shape = {
      orig: move.slice(0, 2),
      dest: move.slice(2, 4),
      brush,
    };
    if (Number.isFinite(lineWidth)) shape.modifiers = { lineWidth };
    return shape;
  }

  function squareCoordinates(square) {
    return [square.charCodeAt(0) - 97, Number(square[1]) - 1];
  }

  function arrowPathInterferes(from, to, occupied) {
    if (from === to) return true;
    const [fromFile, fromRank] = squareCoordinates(from);
    const [toFile, toRank] = squareCoordinates(to);
    const fileDelta = toFile - fromFile;
    const rankDelta = toRank - fromRank;
    occupied.add(from);
    if ([Math.abs(fileDelta), Math.abs(rankDelta)].sort().join('') === '12') {
      if (occupied.has(to)) return true;
      occupied.add(to);
      return false;
    }
    if (
      !(
        fileDelta === 0 ||
        rankDelta === 0 ||
        Math.abs(fileDelta) === Math.abs(rankDelta)
      )
    ) return true;
    const fileStep = Math.sign(fileDelta);
    const rankStep = Math.sign(rankDelta);
    let file = fromFile + fileStep;
    let rank = fromRank + rankStep;
    while (file !== toFile || rank !== toRank) {
      const square = String.fromCharCode(97 + file) + String(rank + 1);
      if (occupied.has(square)) return true;
      occupied.add(square);
      file += fileStep;
      rank += rankStep;
    }
    if (occupied.has(to)) return true;
    occupied.add(to);
    return false;
  }

  function rootSideMoves(pv, rootTurn) {
    const moves = [];
    let turn = rootTurn === 'b' ? 'b' : 'w';
    let completingDouble = false;
    for (const rawMove of pv || []) {
      const move = String(rawMove || '');
      if (!MOVE_RE.test(move)) break;
      if (turn === rootTurn) {
        moves.push({ move, ability: move.endsWith('+') || completingDouble });
      }
      if (move.endsWith('+')) {
        completingDouble = true;
      } else {
        turn = turn === 'w' ? 'b' : 'w';
        completingDouble = false;
      }
    }
    return moves;
  }

  function maneuverArrowShapes(pv, rootTurn) {
    const shapes = [];
    const occupied = new Set();
    const ownMoves = rootSideMoves(pv, rootTurn).slice(0, 3);
    for (let index = 0; index < ownMoves.length; index++) {
      const item = ownMoves[index];
      const move = item.move;
      if (index > 0 && ownMoves[index - 1].move.slice(2, 4) !== move.slice(0, 2)) {
        break;
      }
      if (arrowPathInterferes(move.slice(0, 2), move.slice(2, 4), occupied)) {
        break;
      }
      const shape = moveArrowShape(
        move,
        item.ability ? 'ability' : 'paleBlue',
      );
      if (shape) shapes.push(shape);
    }
    return shapes;
  }

  function engineArrowShapes({
    mode = 'best',
    visibleLines = 5,
    lines = [],
    moveStats = [],
    rootTurn = 'w',
  } = {}) {
    if (mode === 'off') return [];
    const statsByMove = new Map(
      moveStats.map(stat => [String(stat.move || stat.uci || ''), stat]),
    );
    const eligibleLines = lines.filter(line => {
      const move = line && line.pv && line.pv[0];
      const visits = finiteMetric(statForMove(statsByMove, move)?.visits);
      return visits === null || visits > 0;
    });
    const best = eligibleLines[0];
    const bestMove = best && best.pv && best.pv[0];
    if (!bestMove) return [];
    const limit = normalizeVisibleLineCount(visibleLines, 5);
    const shapes = (
      mode === 'maneuver'
        ? maneuverArrowShapes(best.pv, rootTurn)
        : [
            moveArrowShape(
              bestMove,
              bestMove.endsWith('+') ? 'ability' : 'paleBlue',
            ),
          ].filter(Boolean)
    ).slice(0, limit);
    const seen = new Set([bestMove.replace(/\+$/, '').slice(0, 4)]);
    const bestStat = statForMove(statsByMove, bestMove);
    const referenceQ = credibleReferenceQ(moveStats, bestStat);
    const alternatives = rankedAlternativeLines(eligibleLines.slice(1), moveStats);
    for (const [index, line] of alternatives.entries()) {
      if (shapes.length >= limit) break;
      const move = line && line.pv && line.pv[0];
      const key = String(move || '').replace(/\+$/, '').slice(0, 4);
      if (!move || seen.has(key)) continue;
      const alternative = statForMove(statsByMove, move);
      const lineWidth = leelaArrowWidthFromMetrics({
        bestVisits: bestStat && bestStat.visits,
        alternativeVisits: alternative && alternative.visits,
        referenceQ,
        alternativeQ: alternative && alternative.q,
        fallbackIndex: index,
      });
      if (lineWidth === null) continue;
      const shape = moveArrowShape(
        move,
        move.endsWith('+') ? 'ability' : 'paleGrey',
        lineWidth,
      );
      if (shape) {
        shapes.push(shape);
        seen.add(key);
      }
    }
    return shapes;
  }

  function piecesFromFen(fen) {
    const placement = String(fen || '').trim().split(/\s+/)[0];
    const ranks = placement.split('/');
    if (ranks.length !== 8) return new Map();
    const roleByPiece = {
      p: 'pawn',
      n: 'knight',
      b: 'bishop',
      r: 'rook',
      q: 'queen',
      k: 'king',
    };
    const pieces = new Map();
    for (let rankIndex = 0; rankIndex < 8; rankIndex++) {
      let file = 0;
      for (const character of ranks[rankIndex]) {
        if (/^[1-8]$/.test(character)) {
          file += Number(character);
          continue;
        }
        const role = roleByPiece[character.toLowerCase()];
        if (!role || file > 7) return new Map();
        const key = String.fromCharCode(97 + file) + String(8 - rankIndex);
        pieces.set(key, {
          role,
          color: character === character.toUpperCase() ? 'white' : 'black',
        });
        file += 1;
      }
      if (file !== 8) return new Map();
    }
    return pieces;
  }

  function placementFromPieces(pieces) {
    const pieceByRole = {
      pawn: 'p',
      knight: 'n',
      bishop: 'b',
      rook: 'r',
      queen: 'q',
      king: 'k',
    };
    const ranks = [];
    for (let rank = 8; rank >= 1; rank--) {
      let row = '';
      let empty = 0;
      for (let file = 0; file < 8; file++) {
        const square = String.fromCharCode(97 + file) + String(rank);
        const piece = pieces && typeof pieces.get === 'function'
          ? pieces.get(square)
          : null;
        const symbol = pieceByRole[piece && piece.role];
        if (!symbol) {
          empty += 1;
          continue;
        }
        if (empty) {
          row += String(empty);
          empty = 0;
        }
        row += piece.color === 'white' ? symbol.toUpperCase() : symbol;
      }
      if (empty) row += String(empty);
      ranks.push(row);
    }
    return ranks.join('/');
  }

  function parseEditorPiece(value) {
    const match = /^(white|black):(king|queen|rook|bishop|knight|pawn)$/.exec(
      String(value || ''),
    );
    return match ? { color: match[1], role: match[2] } : null;
  }

  function toggleEditorPieceColor(value) {
    const piece = parseEditorPiece(value);
    if (!piece) return value || null;
    return `${piece.color === 'white' ? 'black' : 'white'}:${piece.role}`;
  }

  function editorPlacement(existing, selected) {
    if (selected === 'erase') return undefined;
    const piece = parseEditorPiece(selected);
    if (!piece) return existing;
    if (existing?.color === piece.color && existing?.role === piece.role) {
      return undefined;
    }
    return piece;
  }

  function editorFen({
    pieces,
    turn = 'w',
    castling = '-',
    whiteAbility = true,
    blackAbility = true,
  } = {}) {
    const requestedCastling = /^[KQkq]+$/.test(String(castling || ''))
      ? [...new Set(String(castling))]
          .sort((a, b) => 'KQkq'.indexOf(a) - 'KQkq'.indexOf(b))
          .join('')
      : '-';
    const hasPiece = (square, color, role) => {
      const piece = pieces && typeof pieces.get === 'function'
        ? pieces.get(square)
        : null;
      return piece?.color === color && piece?.role === role;
    };
    const castlingRequirements = {
      K: [['e1', 'white', 'king'], ['h1', 'white', 'rook']],
      Q: [['e1', 'white', 'king'], ['a1', 'white', 'rook']],
      k: [['e8', 'black', 'king'], ['h8', 'black', 'rook']],
      q: [['e8', 'black', 'king'], ['a8', 'black', 'rook']],
    };
    const safeCastling = [...requestedCastling]
      .filter(right => (
        castlingRequirements[right] &&
        castlingRequirements[right].every(requirement => (
          hasPiece(...requirement)
        ))
      ))
      .join('') || '-';
    const ability =
      (whiteAbility ? 'W' : '-') +
      (blackAbility ? 'B' : '-');
    return [
      placementFromPieces(pieces),
      turn === 'b' ? 'b' : 'w',
      safeCastling,
      '-',
      '0',
      '1',
      ability,
    ].join(' ');
  }

  return {
    ENGINE_STREAM_MULTIPV,
    MAX_LEELA_WIN_CHANCE_DROP,
    finiteMetric,
    normalizeVisibleLineCount,
    pawnText,
    projectAnalysisToChild,
    leelaArrowWidthFromMetrics,
    rankedAlternativeLines,
    credibleReferenceQ,
    maneuverArrowShapes,
    engineArrowShapes,
    piecesFromFen,
    placementFromPieces,
    parseEditorPiece,
    toggleEditorPieceColor,
    editorPlacement,
    editorFen,
  };
});
