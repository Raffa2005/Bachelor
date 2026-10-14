# Vendored browser dependencies

These files are kept in the repository so the inference site does not require
third-party CDNs at runtime.

| Component | Version | License | Source |
|---|---:|---|---|
| Chessground | 10.1.1 | GPL-3.0-or-later | <https://www.npmjs.com/package/@lichess-org/chessground/v/10.1.1> |
| jQuery | 3.7.1 | MIT | <https://www.npmjs.com/package/jquery/v/3.7.1> |
| chess.js | 0.12.0 | BSD-2-Clause | <https://www.npmjs.com/package/chess.js/v/0.12.0> |
| Lucide | 1.26.0 | ISC | <https://www.npmjs.com/package/lucide/v/1.26.0> |
| Lichess chess figurine font | lila commit `b75fcce` | GPL-2.0-or-later (redistributed under GPL-3.0-or-later) | <https://github.com/lichess-org/lila/blob/b75fcce50b2d763441d7a52e4906b1ddb456210d/public/font/lichess-chess.woff2> |

The current analysis, play, and replay boards use Chessground. Its complete
GPL-3.0-or-later license and the exact vendored package source are in
[`chessground/10.1.1/`](chessground/10.1.1/).
The active piece theme comes from Chessground's
`assets/chessground.cburnett.css`, which embeds the Cburnett SVG pieces
attributed here to Colin M.L. Burnett. The
[original artwork](https://commons.wikimedia.org/wiki/Template:SVG_chess_pieces)
is offered under several licenses, including GPL-2.0-or-later; this
redistribution uses that GPL option through GPL version 3. Chessground 10.1.1
supplies the embedded stylesheet under its package-wide GPL-3.0-or-later
declaration and contains no separate artwork notice.

The upstream license text for each component listed above is stored beside
that component.

The figurine font’s exact upstream source and copying classification are
recorded in [`lichess/SOURCE.md`](lichess/SOURCE.md). Its GPL “or later”
option is exercised through version 3, whose full text is stored at
[`chessground/10.1.1/LICENSE`](chessground/10.1.1/LICENSE).
