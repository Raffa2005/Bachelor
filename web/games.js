(function (root) {
  'use strict';

  if (!root || !root.document) return;

  const document = root.document;
  const publicApi = root.DoubleMovePublic;
  if (!publicApi) return;

  function start() {
    if (!document.body || document.body.dataset.page !== 'games') return;

    const byId = id => document.getElementById(id);
    const analysisState = root.DoubleMoveAnalysisState;
    const ChessConstructor = root.Chess;
    const reducedMotion = root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const animationSpeed = reducedMotion ? 0 : publicApi.BOARD_ANIMATION_MS;
    const gameId = publicApi.gameIdFromPath(root.location && root.location.pathname, 'games');

    let cursor = null;
    let loadingArchive = false;
    let detailGame = null;
    let replay = null;
    let replayIndex = 0;
    let replayBoard = null;
    const pendingArchiveBoards = [];

    function completedRecord(payload) {
      if (!payload || typeof payload !== 'object') return payload;
      return payload.game || payload.record || payload.data && (payload.data.game || payload.data.record) || payload;
    }

    function completedList(payload) {
      if (Array.isArray(payload)) return { games: payload, next_cursor: null };
      if (!payload || typeof payload !== 'object') return { games: [], next_cursor: null };
      const games = payload.games || payload.items || payload.results || [];
      return {
        games: Array.isArray(games) ? games : [],
        next_cursor: payload.next_cursor != null ? payload.next_cursor : payload.cursor || null,
      };
    }

    function readableDate(value) {
      if (!value) return '';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return new Intl.DateTimeFormat(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      }).format(date);
    }

    function mountArchiveBoard(element, fen) {
      if (!element || !fen) return;
      if (!root.Chessground) {
        pendingArchiveBoards.push({ element, fen });
        return;
      }
      root.Chessground(element, {
        fen: String(fen),
        orientation: 'white',
        viewOnly: true,
        coordinates: false,
        animation: { enabled: false },
        highlight: { lastMove: false, check: false },
        movable: {
          free: false,
          color: undefined,
          dests: new Map(),
          showDests: false,
        },
        premovable: { enabled: false },
        draggable: { enabled: false },
        selectable: { enabled: false },
        drawable: { enabled: false, visible: false },
      });
    }

    function flushArchiveBoards() {
      if (!root.Chessground) return;
      while (pendingArchiveBoards.length) {
        const pending = pendingArchiveBoards.shift();
        if (!pending.element.isConnected) continue;
        mountArchiveBoard(pending.element, pending.fen);
      }
    }

    function gameStrength(game) {
      let label = String(
        game.preset_label ||
        game.strength_label ||
        game.preset && (game.preset.label || game.preset.name) ||
        game.preset_id ||
        '',
      );
      const nodes = Number(game && game.nodes);
      if (
        Number.isFinite(nodes) &&
        nodes >= 1 &&
        !new RegExp('\\b' + String(nodes) + '\\s+visits?\\b', 'i').test(label)
      ) {
        label = (label ? label + ' · ' : '') +
          nodes + ' visit' + (nodes === 1 ? '' : 's');
      }
      return label;
    }

    function gameClock(game) {
      const clocks = game && game.clocks;
      if (typeof clocks === 'string') return clocks;
      return String(
        game.clock_label ||
        game.clock && (game.clock.label || game.clock.name) ||
        clocks && (clocks.label || clocks.name || clocks.clock_id || clocks.id) ||
        game.clock_id ||
        '',
      );
    }

    function appendMeta(container, values) {
      container.replaceChildren();
      values.filter(Boolean).forEach(value => {
        const span = document.createElement('span');
        span.textContent = String(value);
        container.appendChild(span);
      });
    }

    function archiveCard(game) {
      const id = String(game.id || '');
      const link = document.createElement('a');
      link.className = 'archive-card';
      link.href = '/games/' + encodeURIComponent(id);
      link.setAttribute(
        'aria-label',
        'View ' + publicApi.playerName(game, 'w') + ' versus ' +
          publicApi.playerName(game, 'b') + '. ' +
          publicApi.humanOutcomeDisplay(game),
      );

      const head = document.createElement('div');
      head.className = 'archive-result';
      const result = document.createElement('strong');
      result.textContent = publicApi.humanOutcomeDisplay(game);
      const date = document.createElement('span');
      date.className = 'meta-badge';
      date.textContent = readableDate(game.completed_at || game.finished_at || game.created_at) || 'Completed';
      head.append(result, date);

      const players = document.createElement('div');
      players.className = 'archive-players';
      ['w', 'b'].forEach(color => {
        const row = document.createElement('div');
        row.className = 'archive-player';
        const name = document.createElement('span');
        name.textContent = publicApi.playerName(game, color);
        const side = document.createElement('span');
        side.className = 'meta-badge';
        side.textContent = publicApi.colorName ? publicApi.colorName(color) : color === 'w' ? 'White' : 'Black';
        row.append(name, side);
        players.appendChild(row);
      });

      const meta = document.createElement('div');
      meta.className = 'archive-meta';
      const takebacks = Math.max(0, Number(game.takeback_count) || 0);
      appendMeta(meta, [
        gameStrength(game),
        publicApi.temperatureDisplay(game),
        gameClock(game),
        takebacks + ' takeback' + (takebacks === 1 ? '' : 's'),
      ]);

      const body = document.createElement('div');
      body.className = 'archive-card-body';
      const board = document.createElement('div');
      board.className = 'archive-final-board cg-wrap';
      board.setAttribute('role', 'img');
      board.setAttribute('aria-label', 'Final position');
      const summary = document.createElement('div');
      summary.className = 'archive-card-summary';
      summary.append(players, meta);
      body.append(board, summary);
      link.append(head, body);
      mountArchiveBoard(board, game.final_fen || game.current_fen);
      return link;
    }

    async function loadArchive(reset) {
      if (loadingArchive) return;
      loadingArchive = true;
      const loading = byId('archiveLoading');
      const error = byId('archiveError');
      const empty = byId('archiveEmpty');
      const grid = byId('archiveGrid');
      const button = byId('loadMoreButton');
      if (reset) {
        cursor = null;
        grid.replaceChildren();
        loading.hidden = false;
        grid.hidden = true;
        empty.hidden = true;
        error.hidden = true;
      } else {
        button.disabled = true;
        byId('pagerMessage').textContent = 'Loading…';
      }
      const params = new URLSearchParams({ limit: '20' });
      if (cursor != null && cursor !== '') params.set('cursor', String(cursor));
      try {
        const payload = await publicApi.requestJson('/api/games?' + params.toString());
        const page = completedList(payload);
        page.games.forEach(game => grid.appendChild(archiveCard(game)));
        cursor = page.next_cursor;
        loading.hidden = true;
        error.hidden = true;
        const count = grid.childElementCount;
        grid.hidden = count === 0;
        empty.hidden = count !== 0;
        byId('archivePager').hidden = !cursor || count === 0;
        byId('pagerMessage').textContent = '';
      } catch (requestError) {
        loading.hidden = true;
        if (grid.childElementCount === 0) {
          error.hidden = false;
          grid.hidden = true;
          byId('archiveErrorText').textContent = requestError.message || 'Please try again in a moment.';
        } else {
          byId('pagerMessage').textContent = requestError.message || 'Could not load more games.';
        }
      } finally {
        loadingArchive = false;
        button.disabled = false;
      }
    }

    function appendReplayMoves(cell, moves) {
      moves.forEach(move => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'move-token replay-move' +
          (move.isDoubleStart || move.isSecondLeg ? ' is-double' : '');
        button.dataset.replayIndex = String(move.index + 1);
        button.setAttribute(
          'aria-label',
          'Show position after ' + move.san +
            (move.isDoubleStart
              ? ', first leg of a double move'
              : move.isSecondLeg
                ? ', second leg of a double move'
                : ''),
        );
        const notation = document.createElement('strong');
        notation.textContent = move.san;
        button.appendChild(notation);
        button.addEventListener('click', () => showReplayPosition(move.index + 1));
        cell.appendChild(button);
      });
    }

    function renderDetailMoves() {
      const list = byId('detailMoveList');
      list.replaceChildren();
      publicApi.groupMovePairs(replay).forEach(group => {
        const item = document.createElement('li');
        const number = document.createElement('span');
        number.className = 'move-index';
        number.textContent = group.moveNumber + '.';
        const white = document.createElement('span');
        white.className = 'move-cell white';
        white.setAttribute('aria-label', 'White moves');
        const black = document.createElement('span');
        black.className = 'move-cell black';
        black.setAttribute('aria-label', 'Black moves');
        appendReplayMoves(white, group.white);
        appendReplayMoves(black, group.black);
        item.className = 'move-pair';
        item.append(number, white, black);
        list.appendChild(item);
      });
    }

    function replayHighlight(move, index) {
      const isDouble = Boolean(
        move &&
        index > 0 &&
        Array.isArray(replay && replay.doubleFlags) &&
        replay.doubleFlags[index - 1] === true
      );
      const custom = new Map();
      if (isDouble) {
        custom.set(move.slice(0, 2), 'double-highlight');
        custom.set(move.slice(2, 4), 'double-highlight');
      }
      return { isDouble, custom };
    }

    function ensureReplayBoard() {
      if (replayBoard || !root.Chessground) return;
      const move = replayIndex > 0 ? replay.rawMoves[replayIndex - 1] : null;
      const moveHighlight = replayHighlight(move, replayIndex);
      replayBoard = root.Chessground(byId('replayBoard'), {
        fen: replay.fens[replayIndex] || publicApi.START_FEN,
        orientation: 'white',
        viewOnly: true,
        coordinates: true,
        coordinatesOnSquares: false,
        ranksPosition: 'left',
        lastMove: move ? [move.slice(0, 2), move.slice(2, 4)] : null,
        highlight: {
          lastMove: Boolean(move) && !moveHighlight.isDouble,
          check: false,
          custom: moveHighlight.custom,
        },
        animation: {
          enabled: animationSpeed > 0,
          duration: publicApi.BOARD_ANIMATION_MS,
        },
        movable: {
          free: false,
          color: undefined,
          dests: new Map(),
          showDests: false,
        },
        premovable: { enabled: false },
        draggable: { enabled: false },
        selectable: { enabled: false },
        drawable: { enabled: false, visible: false },
      });
    }

    function highlightReplayMove() {
      byId('detailMoveList').querySelectorAll('button.replay-move').forEach(button => {
        const current = Number(button.dataset.replayIndex) === replayIndex;
        button.classList.toggle('current', current);
        if (current) button.setAttribute('aria-current', 'step');
        else button.removeAttribute('aria-current');
      });
    }

    function showReplayPosition(index) {
      if (!replay) return;
      replayIndex = Math.max(0, Math.min(Number(index) || 0, replay.rawMoves.length));
      ensureReplayBoard();
      const move = replayIndex > 0 ? replay.rawMoves[replayIndex - 1] : null;
      const moveHighlight = replayHighlight(move, replayIndex);
      if (replayBoard) {
        replayBoard.set({
          fen: replay.fens[replayIndex],
          lastMove: move ? [move.slice(0, 2), move.slice(2, 4)] : null,
          highlight: {
            lastMove: Boolean(move) && !moveHighlight.isDouble,
            check: false,
            custom: moveHighlight.custom,
          },
          animation: {
            enabled: animationSpeed > 0,
            duration: publicApi.BOARD_ANIMATION_MS,
          },
        });
      }
      byId('replayBoard').setAttribute(
        'aria-label',
        'Completed Double Move Chess game board, position ' +
          replayIndex + ' of ' + replay.rawMoves.length + '.',
      );
      byId('replayCounter').textContent = replayIndex + ' / ' + replay.rawMoves.length;
      byId('replayStartButton').disabled = replayIndex === 0;
      byId('replayBackButton').disabled = replayIndex === 0;
      byId('replayForwardButton').disabled = replayIndex === replay.rawMoves.length;
      byId('replayEndButton').disabled = replayIndex === replay.rawMoves.length;
      highlightReplayMove();
    }

    function renderDetail() {
      replay = publicApi.deriveReplay(detailGame, analysisState, ChessConstructor);
      replayIndex = replay.rawMoves.length;
      byId('detailHeading').textContent = publicApi.playerName(detailGame, 'w') +
        ' vs ' + publicApi.playerName(detailGame, 'b');
      byId('detailResult').textContent = publicApi.humanOutcomeDisplay(detailGame);
      byId('detailTermination').textContent = publicApi.terminationDisplay(detailGame.termination);
      byId('detailWhiteName').textContent = publicApi.playerName(detailGame, 'w');
      byId('detailBlackName').textContent = publicApi.playerName(detailGame, 'b');
      const takebacks = Math.max(0, Number(detailGame.takeback_count) || 0);
      appendMeta(byId('detailMeta'), [
        gameStrength(detailGame),
        publicApi.temperatureDisplay(detailGame),
        gameClock(detailGame),
        takebacks + ' takeback' + (takebacks === 1 ? '' : 's'),
        readableDate(detailGame.completed_at || detailGame.finished_at),
      ]);
      const analyzeLink = byId('detailAnalyzeLink');
      analyzeLink.href = '/analysis?game=' +
        encodeURIComponent(String(detailGame.id || gameId));
      analyzeLink.hidden = false;
      renderDetailMoves();
      byId('detailLoading').hidden = true;
      byId('detailError').hidden = true;
      byId('detailContent').hidden = false;
      const revealBoard = function () {
        ensureReplayBoard();
        showReplayPosition(replayIndex);
      };
      if (typeof root.requestAnimationFrame === 'function') {
        root.requestAnimationFrame(revealBoard);
      } else {
        root.setTimeout(revealBoard, 0);
      }
    }

    async function loadDetail() {
      byId('detailLoading').hidden = false;
      byId('detailContent').hidden = true;
      byId('detailError').hidden = true;
      byId('detailAnalyzeLink').hidden = true;
      try {
        const payload = await publicApi.requestJson('/api/games/' + encodeURIComponent(gameId));
        detailGame = completedRecord(payload);
        if (!detailGame || !detailGame.id) throw new publicApi.ApiError('The game response was incomplete.', 0, payload);
        renderDetail();
      } catch (requestError) {
        byId('detailLoading').hidden = true;
        byId('detailContent').hidden = true;
        byId('detailError').hidden = false;
        byId('detailErrorText').textContent = requestError.message || 'The game may not exist or may not be public.';
      }
    }

    byId('loadMoreButton').addEventListener('click', () => loadArchive(false));
    byId('retryArchiveButton').addEventListener('click', () => loadArchive(true));
    byId('retryDetailButton').addEventListener('click', loadDetail);
    byId('replayStartButton').addEventListener('click', () => showReplayPosition(0));
    byId('replayBackButton').addEventListener('click', () => showReplayPosition(replayIndex - 1));
    byId('replayForwardButton').addEventListener('click', () => showReplayPosition(replayIndex + 1));
    byId('replayEndButton').addEventListener('click', () => showReplayPosition(replay.rawMoves.length));
    root.addEventListener('chessground-ready', flushArchiveBoards);
    document.addEventListener('keydown', event => {
      if (!detailGame || event.target && /^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName)) return;
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      if (event.key === 'ArrowLeft') showReplayPosition(replayIndex - 1);
      else if (event.key === 'ArrowRight') showReplayPosition(replayIndex + 1);
      else if (event.key === 'Home') showReplayPosition(0);
      else showReplayPosition(replay.rawMoves.length);
    });
    if (!root.Chessground) {
      root.addEventListener('chessground-ready', function () {
        if (replay) showReplayPosition(replayIndex);
      }, { once: true });
    }
    if (gameId) {
      byId('archiveView').hidden = true;
      byId('detailView').hidden = false;
      loadDetail();
    } else {
      byId('archiveView').hidden = false;
      byId('detailView').hidden = true;
      loadArchive(true);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);
