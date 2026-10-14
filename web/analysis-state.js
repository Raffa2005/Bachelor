(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.DoubleMoveAnalysisState = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
  const MOVE_RE = /^[a-h][1-8][a-h][1-8][qrbn]?\+?$/;
  const TREE_VERSION = 1;
  const ROOT_ID = 'root';
  const MAX_SERIALIZED_TREE_LENGTH = 1_000_000;
  const MAX_TREE_NODES = 4_096;
  const MAX_TREE_DEPTH = 2_048;

  function normalizedStartFen(value) {
    const text = String(value || 'startpos').trim();
    return !text || text === 'startpos' ? START_FEN : text.split(/\s+/).slice(0, 6).join(' ');
  }

  function pieceColor(piece) {
    if (!piece || piece === ' ') return null;
    return piece === piece.toUpperCase() ? 'w' : 'b';
  }

  function charAt(rank, file) {
    let currentFile = 0;
    for (let i = 0; i < rank.length; i++) {
      const character = rank[i];
      if (character >= '1' && character <= '8') {
        currentFile += parseInt(character, 10);
      } else {
        if (currentFile === file) return character;
        currentFile++;
      }
    }
    return ' ';
  }

  function setChar(rank, file, piece) {
    let result = '';
    let currentFile = 0;
    for (let i = 0; i < rank.length; i++) {
      const character = rank[i];
      if (character >= '1' && character <= '8') {
        const count = parseInt(character, 10);
        if (file >= currentFile && file < currentFile + count) {
          const before = file - currentFile;
          const after = currentFile + count - file - 1;
          if (before > 0) result += String(before);
          result += piece;
          if (after > 0) result += String(after);
          currentFile += count;
        } else {
          currentFile += count;
          result += character;
        }
      } else {
        result += currentFile === file ? piece : character;
        currentFile++;
      }
    }
    return result;
  }

  function compactRank(rank) {
    let result = '';
    let empty = 0;
    for (const character of rank) {
      if (character >= '0' && character <= '9') empty += parseInt(character, 10);
      else if (character === ' ') empty++;
      else {
        if (empty > 0) {
          result += String(empty);
          empty = 0;
        }
        result += character;
      }
    }
    if (empty > 0) result += String(empty);
    return result;
  }

  function applyUciToFen(fen, rawUci, flipTurn) {
    const shouldFlip = flipTurn === undefined ? true : !!flipTurn;
    const uci = String(rawUci || '').replace(/\+$/, '');
    if (!MOVE_RE.test(uci)) return fen;

    const parts = String(fen || '').trim().split(/\s+/);
    if (parts.length < 2 || parts[0].split('/').length !== 8) return fen;
    while (parts.length < 6) parts.push(parts.length === 2 ? '-' : parts.length === 3 ? '-' : parts.length === 4 ? '0' : '1');

    const ranks = parts[0].split('/');
    const turn = parts[1] === 'b' ? 'b' : 'w';
    let castling = parts[2] || '-';
    const fromFile = uci.charCodeAt(0) - 97;
    const fromRank = 8 - parseInt(uci[1], 10);
    const toFile = uci.charCodeAt(2) - 97;
    const toRank = 8 - parseInt(uci[3], 10);
    const promotion = uci.length > 4 ? uci[4] : null;
    const piece = charAt(ranks[fromRank], fromFile);
    if (!piece || piece === ' ') return fen;

    const movingColor = pieceColor(piece);
    const capturedPiece = charAt(ranks[toRank], toFile);
    let isCastling = false;

    if (piece.toLowerCase() === 'k') {
      if (
        capturedPiece !== ' ' &&
        pieceColor(capturedPiece) === movingColor &&
        capturedPiece.toLowerCase() === 'r'
      ) {
        isCastling = true;
        const kingSide = toFile > fromFile;
        const kingDestination = kingSide ? 6 : 2;
        const rookDestination = kingSide ? 5 : 3;
        const rook = movingColor === 'w' ? 'R' : 'r';
        ranks[fromRank] = setChar(ranks[fromRank], fromFile, ' ');
        ranks[fromRank] = setChar(ranks[fromRank], toFile, ' ');
        ranks[fromRank] = setChar(ranks[fromRank], kingDestination, piece);
        ranks[fromRank] = setChar(ranks[fromRank], rookDestination, rook);
      } else if (Math.abs(toFile - fromFile) === 2 && fromRank === toRank) {
        isCastling = true;
        const rookFile = toFile > fromFile ? 7 : 0;
        const rook = movingColor === 'w' ? 'R' : 'r';
        ranks[fromRank] = setChar(ranks[fromRank], fromFile, ' ');
        ranks[fromRank] = setChar(ranks[fromRank], rookFile, ' ');
        ranks[fromRank] = setChar(ranks[fromRank], toFile, piece);
        ranks[fromRank] = setChar(ranks[fromRank], toFile > fromFile ? toFile - 1 : toFile + 1, rook);
      }
    }

    if (
      !isCastling &&
      piece.toLowerCase() === 'p' &&
      fromFile !== toFile &&
      capturedPiece === ' '
    ) {
      const capturedRank = movingColor === 'w' ? toRank + 1 : toRank - 1;
      if (capturedRank >= 0 && capturedRank < 8) {
        ranks[capturedRank] = setChar(ranks[capturedRank], toFile, ' ');
      }
    }

    if (!isCastling) {
      ranks[fromRank] = setChar(ranks[fromRank], fromFile, ' ');
      const destinationPiece = promotion
        ? movingColor === 'w'
          ? promotion.toUpperCase()
          : promotion.toLowerCase()
        : piece;
      ranks[toRank] = setChar(ranks[toRank], toFile, destinationPiece);
    }

    let nextCastling = castling;
    if (piece === 'K') nextCastling = nextCastling.replace(/[KQ]/g, '');
    if (piece === 'k') nextCastling = nextCastling.replace(/[kq]/g, '');
    if (piece === 'R' && fromFile === 7 && fromRank === 7) nextCastling = nextCastling.replace('K', '');
    if (piece === 'R' && fromFile === 0 && fromRank === 7) nextCastling = nextCastling.replace('Q', '');
    if (piece === 'r' && fromFile === 7 && fromRank === 0) nextCastling = nextCastling.replace('k', '');
    if (piece === 'r' && fromFile === 0 && fromRank === 0) nextCastling = nextCastling.replace('q', '');
    if (capturedPiece === 'R' && toFile === 7 && toRank === 7) nextCastling = nextCastling.replace('K', '');
    if (capturedPiece === 'R' && toFile === 0 && toRank === 7) nextCastling = nextCastling.replace('Q', '');
    if (capturedPiece === 'r' && toFile === 7 && toRank === 0) nextCastling = nextCastling.replace('k', '');
    if (capturedPiece === 'r' && toFile === 0 && toRank === 0) nextCastling = nextCastling.replace('q', '');
    if (!nextCastling) nextCastling = '-';

    let enPassant = '-';
    if (
      shouldFlip &&
      piece.toLowerCase() === 'p' &&
      Math.abs(parseInt(uci[3], 10) - parseInt(uci[1], 10)) === 2
    ) {
      const midpointRank = (parseInt(uci[1], 10) + parseInt(uci[3], 10)) / 2;
      enPassant = uci[2] + String(midpointRank);
    }

    const previousHalfmove = parseInt(parts[4], 10);
    const halfmove = piece.toLowerCase() === 'p' || capturedPiece !== ' '
      ? 0
      : (Number.isFinite(previousHalfmove) ? previousHalfmove : 0) + 1;
    const previousFullmove = parseInt(parts[5], 10);
    const fullmove = (Number.isFinite(previousFullmove) && previousFullmove > 0 ? previousFullmove : 1) +
      (shouldFlip && turn === 'b' ? 1 : 0);
    const nextTurn = shouldFlip ? (turn === 'w' ? 'b' : 'w') : turn;
    const result = [
      ranks.map(compactRank).join('/'),
      nextTurn,
      nextCastling,
      enPassant,
      String(halfmove),
      String(fullmove),
    ];
    if (parts.length >= 7) {
      const ability = parseAbilityState(fen);
      if (ability.valid) {
        if (!shouldFlip) ability[movingColor] = false;
        const abilityToken =
          (ability.w ? 'W' : '-') +
          (ability.b ? 'B' : '-') +
          (!shouldFlip ? 'm' : '');
        result.push(abilityToken);
      }
    }
    return result.join(' ');
  }

  function loadChess(ChessConstructor, fen) {
    if (typeof ChessConstructor !== 'function') return null;
    const requestedFen = normalizedStartFen(fen);
    const chess = new ChessConstructor();
    if (chess.load(requestedFen)) return chess;
    return null;
  }

  function parseAbilityState(fen) {
    const parts = String(fen || '').trim().split(/\s+/);
    const token = parts.length >= 7 ? parts[6] : 'WB';
    if (!/^(?:W|-)?(?:B|-)?m?$/.test(token) || !token) {
      return { valid: false, w: false, b: false, midDouble: false, token };
    }
    return {
      valid: true,
      w: token.includes('W'),
      b: token.includes('B'),
      midDouble: token.includes('m'),
      token,
    };
  }

  function canonicalVariantFen(fen, abilityAvailable, midDouble, omitDefault) {
    const parts = normalizedStartFen(fen).split(/\s+/);
    const ability = abilityAvailable || { w: true, b: true };
    const isDefault = ability.w !== false && ability.b !== false && !midDouble;
    if (omitDefault !== false && isDefault) return parts.join(' ');
    const token =
      (ability.w !== false ? 'W' : '-') +
      (ability.b !== false ? 'B' : '-') +
      (midDouble ? 'm' : '');
    return parts.concat(token).join(' ');
  }

  function normalizedChessMove(chess, rawUci) {
    const uci = String(rawUci || '').replace(/\+$/, '');
    const from = uci.slice(0, 2);
    let to = uci.slice(2, 4);
    const promotion = uci.length > 4 ? uci[4] : undefined;
    const movingPiece = chess.get(from);
    const destinationPiece = chess.get(to);
    if (
      movingPiece &&
      movingPiece.type === 'k' &&
      destinationPiece &&
      destinationPiece.type === 'r' &&
      destinationPiece.color === movingPiece.color
    ) {
      to = to[0] > from[0] ? 'g' + from[1] : 'c' + from[1];
    }
    return { from, to, promotion };
  }

  function hasLegalSecondHalf(ChessConstructor, fen) {
    const chess = loadChess(ChessConstructor, fen);
    if (!chess) return false;
    const movingColor = String(fen || '').split(/\s+/)[1] === 'b' ? 'b' : 'w';
    return chess.moves({ verbose: true }).some(move => {
      const destination = chess.get(move.to);
      return !(
        destination &&
        destination.type === 'k' &&
        destination.color !== movingColor
      );
    });
  }

  function canCompleteDoubleMove(ChessConstructor, fen, rawUci) {
    const ability = parseAbilityState(fen);
    const normalizedFen = normalizedStartFen(fen);
    const movingColor = normalizedFen.split(/\s+/)[1] === 'b' ? 'b' : 'w';
    if (
      !ability.valid ||
      ability.midDouble ||
      !ability[movingColor]
    ) {
      return false;
    }
    const uci = String(rawUci || '').replace(/\+$/, '');
    const derived = deriveMoveMetadata({
      valid: true,
      fen: normalizedFen,
      abilityAvailable: { w: ability.w, b: ability.b },
      midDouble: false,
    }, uci + '+', ChessConstructor, null);
    return derived.valid;
  }

  function uciToSan(ChessConstructor, fen, rawUci) {
    const uci = String(rawUci || '').replace(/\+$/, '');
    const chess = loadChess(ChessConstructor, fen);
    if (chess) {
      for (const move of chess.moves({ verbose: true })) {
        if (move.from === uci.slice(0, 2) && move.to === uci.slice(2, 4)) {
          const promotion = uci.length > 4 ? uci[4] : undefined;
          if (!promotion || move.promotion === promotion) return move.san;
        }
      }
    }

    const fromFile = uci.charCodeAt(0) - 97;
    const fromRank = 8 - parseInt(uci[1], 10);
    const toFile = uci.charCodeAt(2) - 97;
    const toRank = 8 - parseInt(uci[3], 10);
    const ranks = String(fen || '').split(/\s+/)[0].split('/');
    const piece = ranks.length === 8 ? charAt(ranks[fromRank], fromFile) : ' ';
    if (piece && piece.toLowerCase() === 'k' && Math.abs(toFile - fromFile) === 2 && fromRank === toRank) {
      return toFile > fromFile ? 'O-O' : 'O-O-O';
    }
    if (piece && piece.toLowerCase() === 'k') {
      const destination = charAt(ranks[toRank], toFile);
      if (
        destination !== ' ' &&
        pieceColor(destination) === pieceColor(piece) &&
        destination.toLowerCase() === 'r'
      ) {
        return toFile > fromFile ? 'O-O' : 'O-O-O';
      }
    }
    const pieceName = piece ? piece.toUpperCase() : '';
    const promotionText = uci.length > 4 ? '=' + uci[4].toUpperCase() : '';
    if (pieceName === 'P') {
      if (fromFile !== toFile) return uci[0] + 'x' + uci.slice(2, 4) + promotionText;
      return uci.slice(2, 4) + promotionText;
    }
    return (pieceName || '') + uci.slice(2, 4) + promotionText;
  }

  function deriveRootMetadata(startFen, ChessConstructor) {
    const fen = normalizedStartFen(startFen);
    const initialAbility = parseAbilityState(startFen);
    const abilityAvailable = {
      w: initialAbility.w,
      b: initialAbility.b,
    };
    const midDouble = initialAbility.midDouble;
    let error = null;

    if (!initialAbility.valid) {
      error = 'Invalid seventh-field ability state: ' + initialAbility.token;
    } else if (!loadChess(ChessConstructor, fen)) {
      error = 'Invalid starting FEN';
    } else {
      const startingTurn = fen.split(/\s+/)[1] === 'b' ? 'b' : 'w';
      if (midDouble && abilityAvailable[startingTurn]) {
        error = 'Mid-double FEN cannot leave the moving side ability available';
      } else if (midDouble && !hasLegalSecondHalf(ChessConstructor, fen)) {
        error = 'Mid-double FEN has no legal second half';
      }
    }

    return {
      valid: !error,
      error,
      fen,
      san: null,
      color: null,
      moveNumber: null,
      isDouble: false,
      moveSquares: null,
      abilityAvailable,
      midDouble,
    };
  }

  function deriveMoveMetadata(parent, rawUci, ChessConstructor, tokenIndex) {
    const token = String(rawUci || '');
    const previousFen = parent && parent.fen;
    const abilityAvailable = {
      w: !!(parent && parent.abilityAvailable && parent.abilityAvailable.w),
      b: !!(parent && parent.abilityAvailable && parent.abilityAvailable.b),
    };
    const wasMidDouble = !!(parent && parent.midDouble);
    const hasPlus = token.endsWith('+');
    const uci = hasPlus ? token.slice(0, -1) : token;
    const from = uci.slice(0, 2);
    const to = uci.slice(2, 4);
    const tokenNumber = Number.isInteger(tokenIndex) ? tokenIndex + 1 : null;
    const tokenDescription = tokenNumber === null
      ? token
      : 'token ' + tokenNumber + ': ' + token;
    const invalid = error => ({
      valid: false,
      error,
      fen: previousFen,
      san: null,
      color: null,
      moveNumber: null,
      isDouble: hasPlus || wasMidDouble,
      moveSquares: MOVE_RE.test(token) ? [from, to] : null,
      abilityAvailable,
      midDouble: wasMidDouble,
    });

    if (!parent || parent.valid === false) {
      return invalid('Cannot derive a move from an invalid parent position');
    }
    if (!MOVE_RE.test(token)) {
      return invalid('Invalid UCI move: ' + token);
    }

    const fenParts = String(previousFen || '').split(/\s+/);
    const fenTurn = fenParts[1] === 'b' ? 'b' : 'w';
    if (hasPlus && wasMidDouble) {
      return invalid('Move ' + token + ' cannot activate Double Move during its second half');
    }
    if (hasPlus && !abilityAvailable[fenTurn]) {
      return invalid((fenTurn === 'w' ? 'White' : 'Black') + ' has already used Double Move');
    }

    const chess = loadChess(ChessConstructor, previousFen);
    if (!chess) {
      return invalid('Invalid FEN before move ' + token);
    }
    const chessMove = normalizedChessMove(chess, uci);
    const destinationPiece = chess.get(chessMove.to);
    if (
      wasMidDouble &&
      destinationPiece &&
      destinationPiece.type === 'k' &&
      destinationPiece.color !== fenTurn
    ) {
      return invalid('The second half of Double Move cannot capture the opponent king');
    }
    const move = chess.move(chessMove);
    if (!move) {
      return invalid('Illegal move at ' + tokenDescription);
    }

    let nextFen = chess.fen();
    const nextAbilityAvailable = Object.assign({}, abilityAvailable);
    let midDouble = false;
    if (hasPlus) {
      const nextParts = nextFen.split(/\s+/);
      nextParts[1] = fenTurn;
      nextParts[3] = '-';
      nextParts[5] = fenParts[5] || '1';
      nextFen = nextParts.join(' ');
      nextAbilityAvailable[fenTurn] = false;
      midDouble = true;
      if (!hasLegalSecondHalf(ChessConstructor, nextFen)) {
        return invalid(
          'The first half of Double Move leaves no legal second half'
        );
      }
    }

    const parsedFullmove = parseInt(fenParts[5], 10);
    return {
      valid: true,
      error: null,
      fen: nextFen,
      san: move.san,
      color: fenTurn,
      moveNumber: Number.isFinite(parsedFullmove) && parsedFullmove > 0
        ? parsedFullmove
        : 1,
      isDouble: hasPlus || wasMidDouble,
      moveSquares: [from, to],
      abilityAvailable: nextAbilityAvailable,
      midDouble,
    };
  }

  function deriveLine(startFen, rawTokens, ChessConstructor) {
    const tokens = Array.isArray(rawTokens)
      ? rawTokens.map(String).filter(token => MOVE_RE.test(token))
      : [];
    const rootMetadata = deriveRootMetadata(startFen, ChessConstructor);
    const fens = [rootMetadata.fen];
    const sans = [];
    const doubleFlags = [];
    const colors = [];
    const squares = [];
    const moveNumbers = [];
    const abilityStates = [];
    let abilityAvailable = Object.assign({}, rootMetadata.abilityAvailable);
    let pendingDouble = rootMetadata.midDouble;
    let error = rootMetadata.error;
    abilityStates.push({
      w: abilityAvailable.w,
      b: abilityAvailable.b,
      midDouble: pendingDouble,
    });

    for (let tokenIndex = 0; tokenIndex < tokens.length && !error; tokenIndex++) {
      const rawUci = tokens[tokenIndex];
      const derived = deriveMoveMetadata({
        valid: true,
        fen: fens[fens.length - 1],
        abilityAvailable,
        midDouble: pendingDouble,
      }, rawUci, ChessConstructor, tokenIndex);
      if (!derived.valid) {
        error = derived.error;
        break;
      }

      abilityAvailable = Object.assign({}, derived.abilityAvailable);
      pendingDouble = derived.midDouble;
      doubleFlags.push(derived.isDouble);
      colors.push(derived.color);
      sans.push(derived.san);
      squares.push(derived.moveSquares);
      moveNumbers.push(derived.moveNumber);
      fens.push(derived.fen);
      abilityStates.push({
        w: abilityAvailable.w,
        b: abilityAvailable.b,
        midDouble: pendingDouble,
      });
    }

    return {
      valid: !error && fens.length === tokens.length + 1,
      error,
      rawMoves: tokens.slice(0, fens.length - 1),
      fens,
      moveSans: sans,
      doubleFlags,
      colors,
      moveSquares: squares,
      moveNumbers,
      abilityStates,
      abilityAvailable: Object.assign({}, abilityAvailable),
      midDouble: pendingDouble,
    };
  }

  function deriveTreeMetadata(tree, ChessConstructor) {
    const nodes = Object.create(null);
    const order = [];
    const invalidNodeIds = [];
    const invalidNodeSet = new Set();
    const errors = [];
    const rawNodes = tree && tree.nodes && typeof tree.nodes === 'object'
      ? tree.nodes
      : null;
    const rootId = tree && typeof tree.rootId === 'string'
      ? tree.rootId
      : ROOT_ID;

    const reportError = (nodeId, error) => {
      if (nodeId !== null && !invalidNodeSet.has(nodeId)) {
        invalidNodeSet.add(nodeId);
        invalidNodeIds.push(nodeId);
      }
      errors.push({ nodeId, error });
    };

    if (!rawNodes || !rawNodes[rootId]) {
      const error = 'Variation tree has no valid root node';
      reportError(rootId, error);
      return {
        valid: false,
        error,
        invalidNodeId: rootId,
        invalidNodeIds,
        errors,
        nodes,
        order,
      };
    }

    const root = deriveRootMetadata(tree.startFen, ChessConstructor);
    nodes[rootId] = Object.assign({
      nodeId: rootId,
      parentId: null,
      raw: null,
      depth: 0,
    }, root);
    order.push(rootId);
    if (!root.valid) reportError(rootId, root.error);

    const seen = new Set([rootId]);
    const rootChildren = Array.isArray(rawNodes[rootId].children)
      ? rawNodes[rootId].children
      : [];
    const pending = [];
    for (let index = rootChildren.length - 1; index >= 0; index--) {
      pending.push({
        nodeId: rootChildren[index],
        parentId: rootId,
        depth: 1,
      });
    }

    while (pending.length) {
      const item = pending.pop();
      const nodeId = item.nodeId;
      if (typeof nodeId !== 'string' || !rawNodes[nodeId]) {
        reportError(
          typeof nodeId === 'string' ? nodeId : null,
          'Variation tree references a missing child node',
        );
        continue;
      }
      if (seen.has(nodeId)) {
        reportError(nodeId, 'Variation tree contains a cycle or repeated child reference');
        continue;
      }
      seen.add(nodeId);
      order.push(nodeId);

      const node = rawNodes[nodeId];
      const parent = nodes[item.parentId];
      let metadata;
      if (node.parentId !== item.parentId) {
        metadata = {
          valid: false,
          error: 'Variation node parent does not match its containing branch',
          fen: parent && parent.fen,
          san: null,
          color: null,
          moveNumber: null,
          isDouble: false,
          moveSquares: null,
          abilityAvailable: Object.assign(
            { w: false, b: false },
            parent && parent.abilityAvailable,
          ),
          midDouble: !!(parent && parent.midDouble),
        };
      } else if (!parent || !parent.valid) {
        metadata = {
          valid: false,
          error: 'Variation node has an invalid parent position',
          fen: parent && parent.fen,
          san: null,
          color: null,
          moveNumber: null,
          isDouble: false,
          moveSquares: null,
          abilityAvailable: Object.assign(
            { w: false, b: false },
            parent && parent.abilityAvailable,
          ),
          midDouble: !!(parent && parent.midDouble),
        };
      } else {
        metadata = deriveMoveMetadata(
          parent,
          node.raw,
          ChessConstructor,
          item.depth - 1,
        );
      }
      nodes[nodeId] = Object.assign({
        nodeId,
        parentId: item.parentId,
        raw: String(node.raw || ''),
        depth: item.depth,
      }, metadata);
      if (!metadata.valid) reportError(nodeId, metadata.error);

      const children = Array.isArray(node.children) ? node.children : [];
      for (let index = children.length - 1; index >= 0; index--) {
        pending.push({
          nodeId: children[index],
          parentId: nodeId,
          depth: item.depth + 1,
        });
      }
    }

    for (const nodeId of Object.keys(rawNodes)) {
      if (seen.has(nodeId)) continue;
      order.push(nodeId);
      nodes[nodeId] = {
        nodeId,
        parentId: rawNodes[nodeId] && rawNodes[nodeId].parentId || null,
        raw: String(rawNodes[nodeId] && rawNodes[nodeId].raw || ''),
        depth: null,
        valid: false,
        error: 'Variation node is not reachable from the root',
        fen: null,
        san: null,
        color: null,
        moveNumber: null,
        isDouble: false,
        moveSquares: null,
        abilityAvailable: { w: false, b: false },
        midDouble: false,
      };
      reportError(nodeId, nodes[nodeId].error);
    }

    return {
      valid: errors.length === 0,
      error: errors.length ? errors[0].error : null,
      invalidNodeId: errors.length ? errors[0].nodeId : null,
      invalidNodeIds,
      errors,
      nodes,
      order,
    };
  }

  function createTree(startFen) {
    return {
      version: TREE_VERSION,
      startFen: String(startFen || 'startpos').trim() || 'startpos',
      rootId: ROOT_ID,
      currentId: ROOT_ID,
      nextId: 1,
      nodes: {
        [ROOT_ID]: {
          id: ROOT_ID,
          parentId: null,
          raw: null,
          children: [],
        },
      },
    };
  }

  function nextNodeId(tree) {
    if (!Number.isSafeInteger(tree.nextId) || tree.nextId < 1) tree.nextId = 1;
    let id;
    let attempts = 0;
    do {
      id = 'n' + tree.nextId++;
      if (!Number.isSafeInteger(tree.nextId)) tree.nextId = 1;
      attempts++;
      if (attempts > MAX_TREE_NODES + 1) {
        throw new Error('Variation tree has no safe node identifiers remaining');
      }
    } while (tree.nodes[id]);
    return id;
  }

  function pathIds(tree, nodeId) {
    const target = nodeId || tree.currentId;
    if (!tree || !tree.nodes || !tree.nodes[target]) return [ROOT_ID];
    const reversed = [];
    const seen = new Set();
    let current = target;
    while (current && tree.nodes[current] && !seen.has(current)) {
      seen.add(current);
      reversed.push(current);
      current = tree.nodes[current].parentId;
    }
    if (reversed[reversed.length - 1] !== tree.rootId) return [tree.rootId];
    return reversed.reverse();
  }

  function activeTokens(tree, nodeId) {
    return pathIds(tree, nodeId)
      .slice(1)
      .map(id => tree.nodes[id].raw);
  }

  function activeLineFromTreeMetadata(tree, metadata, nodeId) {
    if (!metadata || !metadata.valid || !metadata.nodes) return null;
    const path = pathIds(tree, nodeId);
    const states = path.map(id => metadata.nodes[id]);
    if (
      states.length !== path.length ||
      states.some(state => !state || !state.valid)
    ) {
      return null;
    }
    const root = states[0];
    const moves = states.slice(1);
    const current = states[states.length - 1];
    return {
      valid: true,
      error: null,
      rawMoves: moves.map(state => state.raw),
      fens: states.map(state => state.fen),
      moveSans: moves.map(state => state.san),
      doubleFlags: moves.map(state => state.isDouble),
      colors: moves.map(state => state.color),
      moveSquares: moves.map(state => state.moveSquares),
      moveNumbers: moves.map(state => state.moveNumber),
      abilityStates: states.map(state => ({
        w: state.abilityAvailable.w,
        b: state.abilityAvailable.b,
        midDouble: state.midDouble,
      })),
      abilityAvailable: {
        w: current.abilityAvailable.w,
        b: current.abilityAvailable.b,
      },
      midDouble: current.midDouble,
    };
  }

  function addMoves(tree, rawTokens) {
    const createdIds = [];
    const reusedIds = [];
    for (const rawToken of rawTokens || []) {
      const token = String(rawToken);
      if (!MOVE_RE.test(token)) continue;
      const parent = tree.nodes[tree.currentId];
      if (!parent) break;
      const existingId = parent.children.find(childId => {
        const child = tree.nodes[childId];
        return child && child.raw === token;
      });
      if (existingId) {
        tree.currentId = existingId;
        reusedIds.push(existingId);
        continue;
      }
      if (countNodes(tree) >= MAX_TREE_NODES) {
        throw new Error('Variation tree reached its ' + MAX_TREE_NODES + '-move safety limit');
      }
      if (pathIds(tree).length - 1 >= MAX_TREE_DEPTH) {
        throw new Error('Variation reached its ' + MAX_TREE_DEPTH + '-move safety limit');
      }
      const id = nextNodeId(tree);
      tree.nodes[id] = {
        id,
        parentId: parent.id,
        raw: token,
        children: [],
      };
      parent.children.push(id);
      tree.currentId = id;
      createdIds.push(id);
    }
    return { currentId: tree.currentId, createdIds, reusedIds };
  }

  function treeFromMoves(startFen, rawTokens, gotoEnd) {
    const tree = createTree(startFen);
    addMoves(tree, rawTokens);
    if (!gotoEnd) tree.currentId = tree.rootId;
    return tree;
  }

  function goToNode(tree, nodeId) {
    if (tree.nodes[nodeId]) tree.currentId = nodeId;
    return tree.currentId;
  }

  function goToDepth(tree, depth) {
    const path = pathIds(tree);
    const targetDepth = Math.max(0, Number.isFinite(depth) ? Math.floor(depth) : 0);
    if (targetDepth < path.length) {
      tree.currentId = path[targetDepth];
      return tree.currentId;
    }
    while (pathIds(tree).length - 1 < targetDepth) {
      const current = tree.nodes[tree.currentId];
      if (!current || !current.children.length) break;
      tree.currentId = current.children[0];
    }
    return tree.currentId;
  }

  function goToEnd(tree) {
    const seen = new Set();
    while (tree.nodes[tree.currentId] && tree.nodes[tree.currentId].children.length) {
      if (seen.has(tree.currentId)) break;
      seen.add(tree.currentId);
      tree.currentId = tree.nodes[tree.currentId].children[0];
    }
    return tree.currentId;
  }

  function navigateParent(tree) {
    const node = tree.nodes[tree.currentId];
    if (node && node.parentId && tree.nodes[node.parentId]) tree.currentId = node.parentId;
    return tree.currentId;
  }

  function navigatePreferredChild(tree) {
    const node = tree.nodes[tree.currentId];
    if (node && node.children.length) tree.currentId = node.children[0];
    return tree.currentId;
  }

  function navigateSibling(tree, delta) {
    const node = tree.nodes[tree.currentId];
    if (!node || !node.parentId) return tree.currentId;
    const siblings = tree.nodes[node.parentId].children;
    if (siblings.length < 2) return tree.currentId;
    const index = siblings.indexOf(node.id);
    if (index < 0) return tree.currentId;
    const offset = delta < 0 ? -1 : 1;
    tree.currentId = siblings[(index + offset + siblings.length) % siblings.length];
    return tree.currentId;
  }

  function promotePath(tree, nodeId) {
    const path = pathIds(tree, nodeId || tree.currentId);
    for (let i = 1; i < path.length; i++) {
      const childId = path[i];
      const parent = tree.nodes[path[i - 1]];
      const index = parent.children.indexOf(childId);
      if (index > 0) {
        parent.children.splice(index, 1);
        parent.children.unshift(childId);
      }
    }
    return tree.currentId;
  }

  function deleteSubtree(tree, nodeId) {
    const id = nodeId || tree.currentId;
    if (id === tree.rootId || !tree.nodes[id]) return false;
    const node = tree.nodes[id];
    const parent = tree.nodes[node.parentId];
    if (!parent) return false;
    parent.children = parent.children.filter(childId => childId !== id);
    const stack = [id];
    const seen = new Set();
    while (stack.length) {
      const currentId = stack.pop();
      if (seen.has(currentId) || !tree.nodes[currentId]) continue;
      seen.add(currentId);
      stack.push(...tree.nodes[currentId].children);
      delete tree.nodes[currentId];
    }
    tree.currentId = parent.id;
    return true;
  }

  function countNodes(tree) {
    return Math.max(0, Object.keys(tree.nodes || {}).length - 1);
  }

  function serializeTree(tree) {
    return JSON.stringify(tree);
  }

  function deserializeTree(serialized, fallbackStartFen) {
    let parsed;
    try {
      if (typeof serialized === 'string' && serialized.length > MAX_SERIALIZED_TREE_LENGTH) {
        return createTree(fallbackStartFen);
      }
      parsed = typeof serialized === 'string' ? JSON.parse(serialized) : serialized;
    } catch (error) {
      return createTree(fallbackStartFen);
    }
    if (!parsed || parsed.version !== TREE_VERSION || !parsed.nodes || typeof parsed.nodes !== 'object') {
      return createTree(fallbackStartFen);
    }
    const tree = createTree(parsed.startFen || fallbackStartFen);
    tree.nodes = {
      [ROOT_ID]: {
        id: ROOT_ID,
        parentId: null,
        raw: null,
        children: [],
      },
    };
    const sourceRoot = parsed.nodes[ROOT_ID];
    if (!sourceRoot || !Array.isArray(sourceRoot.children)) return createTree(fallbackStartFen);

    let acceptedCount = 0;
    let exceededLimits = false;
    const pending = [];
    for (let index = sourceRoot.children.length - 1; index >= 0; index--) {
      const childId = sourceRoot.children[index];
      if (typeof childId === 'string') {
        pending.push({ sourceId: childId, parentId: ROOT_ID, depth: 1 });
      }
    }
    while (pending.length) {
      const { sourceId, parentId, depth } = pending.pop();
      if (depth > MAX_TREE_DEPTH || acceptedCount >= MAX_TREE_NODES) {
        exceededLimits = true;
        break;
      }
      const source = parsed.nodes[sourceId];
      if (!source || !MOVE_RE.test(String(source.raw || '')) || tree.nodes[sourceId]) {
        continue;
      }
      const node = {
        id: sourceId,
        parentId,
        raw: String(source.raw),
        children: [],
      };
      tree.nodes[sourceId] = node;
      tree.nodes[parentId].children.push(sourceId);
      acceptedCount++;
      if (Array.isArray(source.children)) {
        for (let index = source.children.length - 1; index >= 0; index--) {
          const childId = source.children[index];
          if (typeof childId === 'string') {
            pending.push({
              sourceId: childId,
              parentId: sourceId,
              depth: depth + 1,
            });
          }
        }
      }
    }
    if (exceededLimits) return createTree(fallbackStartFen);

    const numericIds = Object.keys(tree.nodes)
      .map(id => /^n(\d+)$/.exec(id))
      .filter(Boolean)
      .map(match => parseInt(match[1], 10));
    const requestedNextId = parseInt(parsed.nextId, 10);
    const safeNumericIds = numericIds.filter(Number.isSafeInteger);
    const minimumNextId = safeNumericIds.length ? Math.max(...safeNumericIds) + 1 : 1;
    const safeRequestedNextId = Number.isSafeInteger(requestedNextId) && requestedNextId > 0
      ? requestedNextId
      : 1;
    tree.nextId = Math.max(
      1,
      safeRequestedNextId,
      Number.isSafeInteger(minimumNextId) ? minimumNextId : 1
    );
    tree.currentId = tree.nodes[parsed.currentId] ? parsed.currentId : ROOT_ID;
    return tree;
  }

  function validateTree(tree, ChessConstructor) {
    for (const nodeId of Object.keys(tree.nodes || {})) {
      if (nodeId === tree.rootId) continue;
      const line = deriveLine(tree.startFen, activeTokens(tree, nodeId), ChessConstructor);
      if (!line.valid) return { valid: false, nodeId, error: line.error };
    }
    return { valid: true, nodeId: null, error: null };
  }

  function activePayload(tree, extra, ChessConstructor) {
    const moves = activeTokens(tree);
    if (ChessConstructor) {
      const line = deriveLine(tree.startFen, moves, ChessConstructor);
      if (!line.valid || line.rawMoves.length !== moves.length) {
        throw new Error(line.error || 'Active variation does not match its derived position');
      }
    }
    return Object.assign({
      fen: String(tree.startFen || 'startpos').trim() || 'startpos',
      moves,
    }, extra || {});
  }

  function prepareArchivedGame(game, ChessConstructor) {
    if (!game || typeof game !== 'object') {
      throw new Error('The archived game response is invalid.');
    }
    const startFen = typeof game.initial_fen === 'string' && game.initial_fen.trim()
      ? game.initial_fen.trim()
      : 'startpos';
    if (!Array.isArray(game.moves) || game.moves.length > MAX_TREE_DEPTH) {
      throw new Error('The archived game move list is invalid.');
    }
    const moves = game.moves.slice();
    if (moves.some(move => typeof move !== 'string' || !MOVE_RE.test(move))) {
      throw new Error('The archived game contains invalid move data.');
    }
    const line = deriveLine(startFen, moves, ChessConstructor);
    if (!line.valid || line.rawMoves.length !== moves.length) {
      throw new Error(line.error || 'The archived game is not a legal variant line.');
    }
    return { startFen, moves, line };
  }

  return {
    START_FEN,
    MOVE_RE,
    TREE_VERSION,
    ROOT_ID,
    normalizedStartFen,
    pieceColor,
    charAt,
    applyUciToFen,
    loadChess,
    hasLegalSecondHalf,
    canCompleteDoubleMove,
    uciToSan,
    deriveLine,
    deriveTreeMetadata,
    createTree,
    pathIds,
    activeTokens,
    activeLineFromTreeMetadata,
    addMoves,
    treeFromMoves,
    goToNode,
    goToDepth,
    goToEnd,
    navigateParent,
    navigatePreferredChild,
    navigateSibling,
    promotePath,
    deleteSubtree,
    countNodes,
    serializeTree,
    deserializeTree,
    parseAbilityState,
    canonicalVariantFen,
    validateTree,
    activePayload,
    prepareArchivedGame,
  };
});
