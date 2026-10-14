# Container Source and Licenses

The image is a portable CPU configuration for quick testing of Double Strike
Chess. Native, hardware-tuned source builds are recommended for hosting.

Original server and site files are MIT licensed. The combined browser
application uses GPL-3.0-or-later Chessground; its exact source and dependency
notices are included under `/opt/double-move/web/vendor/`.

The engine is GPL-3.0-or-later. This directory contains:

- `lc0-source.tar.gz`: the corresponding engine source at the manifest revision.
- `lc0-subprojects.tar.gz`: fetched build dependencies, including their notices.
- `lc0-COPYING`: the engine's GPL text.
- `build_engine.sh`: the engine build script.
- `PROJECT-LICENSE`: the original project's MIT license.
- `LICENSES.md`: component attribution and scope.
- `MODEL_CARD.md`: pretrained model origin and contribution-license scope.

The complete application source and Dockerfile are available from
https://github.com/Raffa2005/Bachelor. The image's installed manifest at
`/opt/double-move/runtime/manifest.json` records the engine revision and model
checksum. Operating-system libraries retain their package license notices in
`/usr/share/doc/`.
