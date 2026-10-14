# Browser frontend source and license notice

Our browser frontend delivered from this directory combines our original
HTML, CSS, and JavaScript with
[@lichess-org/chessground 10.1.1](vendor/chessground/10.1.1/README.md).
Chessground is licensed under GNU GPL version 3 or, at your option, any later
version. The combined browser frontend is distributed under
GPL-3.0-or-later.

Our original browser files remain available under the repository's root
MIT License when used separately. Those files are also offered under
GPL-3.0-or-later as part of this combined frontend. This notice does not
change the separate licenses or notices for the server, engine, model, or
other third-party browser assets.

The SAN figurines use Lichess’s `lichess-chess.woff2`, attributed to the
pgn4web authors and distributed upstream under GPL-2.0-or-later. We use
its “or later” option through GPL version 3; provenance is recorded in
[`vendor/lichess/SOURCE.md`](vendor/lichess/SOURCE.md).

## Chessground corresponding source

The exact vendored Chessground package is included at:

```text
vendor/chessground/10.1.1/
```

It contains:

- the full unminified TypeScript source in `src/`
- the unminified JavaScript distribution modules, source maps, and type
  declarations in `dist/`
- the minified ES module loaded by this site at
  `dist/chessground.min.js`
- the distributed CSS assets in `assets/`
- the package version, dependency information, and build commands in
  `package.json`
- the upstream README and complete GPL license

When served by this application, those files are available at
`/vendor/chessground/10.1.1/`. The complete GPL terms are in
[`vendor/chessground/10.1.1/LICENSE`](vendor/chessground/10.1.1/LICENSE).
The active `assets/chessground.cburnett.css` theme embeds Cburnett SVG pieces,
attributed here to Colin M.L. Burnett. The
[original Cburnett artwork](https://commons.wikimedia.org/wiki/Template:SVG_chess_pieces)
is offered under several licenses, including GPL-2.0-or-later; this
redistribution uses that GPL option through GPL version 3. Chessground supplies
the embedded stylesheet under its package-wide GPL-3.0-or-later declaration
and contains no separate artwork notice.

Notices and license texts for the remaining vendored browser dependencies are
listed in
[`vendor/THIRD_PARTY_NOTICES.md`](vendor/THIRD_PARTY_NOTICES.md).
