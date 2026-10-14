# Double Strike Chess

Play at **[doublestrikechess.com](https://doublestrikechess.com)**, or host the
site on your own machine. Each player can make two consecutive moves once per
game. The site includes play against the engine, position analysis, and game
replays.

**The chess engine runs on the server.** Visitors only need a browser; your
hosting machine supplies the CPU/GPU resources for every engine request.

## Build From Source (Recommended)

Use a native source build for a lasting installation. The supplied network is
large, and the baseline single-threaded CPU backend is very slow. Build for your
hardware, configure inference threads, and use a supported GPU when available.
Docker is provided for quick testing only.

On Ubuntu 24.04 or a comparable Linux distribution, install Python 3.11+, a
C++20 compiler, Meson, Ninja, OpenBLAS, and the development libraries:

```sh
sudo apt-get update
sudo apt-get install git python3 build-essential meson ninja-build pkg-config \
  libopenblas-dev libeigen3-dev libabsl-dev zlib1g-dev

git clone https://github.com/Raffa2005/Bachelor.git
cd Bachelor
git submodule update --init src/lc0
./setup.sh --backend blas --jobs 4
DM_BACKEND_OPTIONS='threads=4' ./run.sh --engine blas
```

Open **http://127.0.0.1:8082**. Setup downloads the public `v43-latest` network,
checks its SHA-256, and builds the pinned variant engine with native CPU
optimizations. No GitHub account or token is required. Allow about 200 MB for
the network and additional space for the compiler build.

Choose the inference thread count to suit your CPU. `DM_BACKEND_OPTIONS` sets
the BLAS library's threads; `DM_ENGINE_THREADS` separately controls search
workers. Setting only `OPENBLAS_NUM_THREADS` is insufficient because this
engine explicitly configures the BLAS thread count.

For an NVIDIA GPU with the CUDA toolkit installed:

```sh
./setup.sh --backend cuda --jobs 4
./run.sh --engine cuda
```

See **[Self-Hosting](docs/self-hosting.md)** for Intel GPU/SYCL builds, hardware
tuning, HTTPS deployment, persistent storage, access controls, and updates.
Use this repository's pinned engine fork: an unmodified regular-chess lc0
binary does not implement the variant.

To also install the released MoE1/BT3 pair, add `--with-moe` to `setup.sh`.
Both files are downloaded and integrity-checked. v43 stays the default; MoE1
can then be selected in Play's Advanced settings and in Analysis. See
[MoE installation and selection](docs/self-hosting.md#optional-moe-model).

## Quick Docker Test

```sh
docker compose up --build
```

Open http://127.0.0.1:8082. This portable CPU image is convenient for trying the
site, but a hardware-tuned source build is strongly recommended for hosting.
Game data persists in the `game-data` volume. No download credentials are
needed.

## Repository

| Path | Contents |
| --- | --- |
| `web/` | Browser application and vendored assets |
| `tools/` | HTTP server, engine integration, administration, tests |
| `src/lc0/` | Pinned Double Strike Chess engine submodule |
| `runtime/manifest.json` | Engine revision and model checksums |
| `docs/` | Self-hosting instructions |
| `research/` | Optional training source, configuration, and scheduler example |

Training is not required to run the site. Advanced users can start with the
[research notes](research/README.md). Released networks are described in the
[model card](MODEL_CARD.md).
Older model checkpoints can be published on request.

## Tests

With Python 3.11+ and Node.js 20+:

```sh
python3 -m unittest discover -s tools -p 'test_*.py'
node --test web/tests/*.test.js
```

`setup.sh` also runs a one-visit inference smoke test against the installed
engine and network.

## Licenses

We license our original site/server code under MIT. The lc0 engine and the combined
browser application containing Chessground are GPL-3.0-or-later. Individual
dependencies retain their own notices and licenses. See [Licenses](LICENSES.md),
[LICENSE](LICENSE), and the [model card](MODEL_CARD.md) for scope and attribution.
