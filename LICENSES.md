# Licenses and Attribution

License scope follows the component, rather than the repository containing it.
Preserve copyright notices and license texts when redistributing.

| Component | Terms |
| --- | --- |
| Original Python server, scripts, documentation, and original frontend files | [MIT](LICENSE), Rafael Sabirov |
| Modified lc0 engine | [GPL-3.0-or-later](src/lc0/COPYING), Leela Chess Zero contributors and Double Strike Chess modifications |
| Combined browser application containing Chessground | GPL-3.0-or-later; original files also remain available under MIT |
| Chessground, chess artwork and figurine font | GPL terms detailed in [browser notices](web/vendor/THIRD_PARTY_NOTICES.md) |
| jQuery | MIT |
| chess.js | BSD-2-Clause |
| Lucide | ISC |
| Original training modifications | MIT; upstream training material retains its original rights and notices |
| Original model modifications and metadata | MIT for these contributions; see [model provenance and scope](MODEL_CARD.md#license-scope) |

The engine contains the upstream GPL section 7 permission for certain NVIDIA
libraries. That permission has the scope stated in its source headers, including
the exclusions for BLAS, OpenCL, and SYCL. It does not change those components'
own licenses.

Exact vendored browser sources and license texts are included under
`web/vendor/`. Engine source is provided by the pinned `src/lc0` submodule.
The container also includes its corresponding engine source archive, fetched
build dependencies, their notices, and build script. Libraries installed through the operating system or GPU toolkit retain
their respective licenses.

This project's MIT grant applies only to original contributions. It does not
relicense third-party files or pretrained parameters. Upstream projects are
[LeelaChessZero/lc0](https://github.com/LeelaChessZero/lc0) and
[LeelaChessZero/lczero-training](https://github.com/LeelaChessZero/lczero-training).
