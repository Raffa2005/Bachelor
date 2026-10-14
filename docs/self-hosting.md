# Self-Hosting

The browser communicates with a Python HTTP service, which launches the native
Double Strike Chess engine on the hosting machine. Static file hosting alone
cannot run the engine. The Python server uses the standard library; no Node.js
build is required to serve the frontend.

## Native Build

Follow the dependency installation and clone commands in the [README](../README.md).
Use Python 3.11 or newer, a C++20 compiler, Meson, Ninja, pkg-config, OpenBLAS,
Eigen, Abseil, and zlib. Linux is the supported target of the setup scripts.
The engine's upstream build documentation covers other operating systems.

```sh
./setup.sh --backend blas --jobs 4
DM_BACKEND_OPTIONS='threads=4' DM_ENGINE_THREADS=1 ./run.sh --engine blas
```

The build uses `native_arch=true`, so build on the CPU that will run it, or one
with the same instruction set. For a portable executable, use
`tools/build_engine.sh --backend blas --portable` after setup.

### CPU Performance

BLAS inference defaults to **one thread**. This is particularly slow with the
supplied transformer network. Pass `DM_BACKEND_OPTIONS='threads=4'` (or another
measured value) when launching the site. The backend calls
`openblas_set_num_threads`, so it overrides the usual OpenBLAS environment
setting. `DM_ENGINE_THREADS` controls separate MCTS search workers and defaults
to one. Increasing both indiscriminately can oversubscribe the machine,
especially when serving multiple sessions.

Benchmark representative analysis requests on your hardware. A higher thread
count is not always faster. Keep resources available for the server and other
sessions. `./run.sh --minibatch-size 32` can change the inference batch size;
compare latency as well as nodes per second.

### NVIDIA CUDA

Install a compatible NVIDIA driver and CUDA toolkit, with `nvcc` on `PATH`.
Keep the CPU development dependencies installed as well.

```sh
./setup.sh --backend cuda --jobs 4
./run.sh --engine cuda
```

The build enables the native CUDA backend and uses `cuda-auto` at runtime.
To explicitly target a GPU architecture, pass the corresponding Meson option,
for example `--meson-option '-Dcc_cuda=89'` for compute capability 8.9. Check your
actual GPU before selecting this value. CUDA's supported host compiler must
also match your toolkit.

### Intel SYCL

Install Intel oneAPI with its C++ compiler, oneMKL, and the driver/runtime for
your GPU. Initialize its environment before building and running:

```sh
source /opt/intel/oneapi/setvars.sh --include-intel-llvm
CC=icx CXX=icpx AR=llvm-ar ./setup.sh --backend sycl --jobs 4
./run.sh --engine sycl
```

This enables Level Zero SYCL and retains BLAS as a CPU option. The auto backend
chooses a supported implementation. Driver, precision, and device support vary;
verify your installation with the included smoke test.

### Further Tuning

- [lc0 build instructions](https://github.com/LeelaChessZero/lc0#building-and-running-lc0)
- [lc0 engine parameters](https://lczero.org/play/configuration/flags/)
- [OpenBLAS build configuration](https://www.openmathlib.org/OpenBLAS/docs/build_system/)
- [OpenBLAS runtime settings](https://www.openmathlib.org/OpenBLAS/docs/runtime_variables/)
- [CUDA installation guide for Linux](https://docs.nvidia.com/cuda/cuda-installation-guide-linux/)

Use these references to tune this fork; retain the manifest-pinned source and
variant network. Additional Meson options can be passed to `setup.sh` with
repeatable `--meson-option '-Dname=value'` arguments. GPU backends generally offer
much better throughput than the portable CPU configuration.

## Optional MoE Model

We also provide MoE1, paired with the unchanged BT3 network. Install it alongside
the default v43 model when building from source:

```sh
./setup.sh --backend blas --jobs 4 --with-moe
DM_BACKEND_OPTIONS='threads=4' ./run.sh --engine blas
```

Setup downloads all three files anonymously and verifies their sizes and SHA-256
hashes against `runtime/manifest.json`. Its small inference checks cover MoE1's
expert phase, both-spent positions, and the mid-double continuation. MoE requires
memory for both networks. Substitute your hardware's backend as described above.

For an existing installation, stop the server, install the pair, and restart:

```sh
python3 tools/model_manager.py --model moe1-step64000 sync
python3 tools/model_manager.py --model moe1-step64000 verify
./run.sh --hosted --engine blas
```

In **Play**, enable **Advanced** and choose MoE1 in the model selector. In
**Analysis**, select MoE1 under engine settings. v43 remains the default; to
explicitly make MoE1 the default for a server, use:

```sh
./run.sh --hosted --engine blas --model moe1-step64000
```

The server binds the expert to its verified BT3 fallback for both play and
analysis. It routes to BT3 whenever both ability flags are spent, including the
continuation between the two halves of the final double move. BT3 is not offered
as a separate variant model. An incomplete or altered pair is refused; hosted
requests cannot provide fallback paths. Stop the server before replacing model
files. Existing games retain their recorded model identity.

## Public Deployment

The local profile exposes operator controls and filesystem-oriented analysis
tools. For a public site, **always use `--hosted`**, keep the Python listener on
loopback, and put an HTTPS reverse proxy in front of it.

```sh
export DM_PUBLIC_ORIGIN=https://chess.example.org
export DM_DATABASE_PATH="$PWD/.runtime/games.sqlite3"
export DM_ACCESS_DATABASE_PATH="$PWD/.runtime/access.sqlite3"
export DM_BACKEND_OPTIONS='threads=4'
./run.sh --hosted --engine blas --host 127.0.0.1 --port 8082
```

Use your own domain, not the example domain. `DM_PUBLIC_ORIGIN` controls origin
validation and secure cookies. The server creates a persistent CSRF secret
beside its game database with restrictive permissions. You can instead supply
`DM_CSRF_SECRET` with at least 32 bytes of secret material. Keep the databases
and secret private and outside the served `web/` directory.

For example, a Caddy configuration is:

```caddyfile
chess.example.org {
    reverse_proxy 127.0.0.1:8082 {
        flush_interval -1
    }
}
```

Allow streamed responses through the proxy. If using nginx, disable proxy
buffering for the engine stream and use an adequate read timeout. Run the
service under an unprivileged account with a process supervisor that supplies
the same environment and working directory. Permit public traffic only to the
reverse proxy; the engine port should remain private.

The site stores games and access settings in SQLite. Avoid sharing these files
between multiple independent server instances. Resource limits and the request
queue bound engine work, but this is still a single-machine deployment.

### Access Controls

Run administration commands with the same database environment as the server:

```sh
./access.sh status
./access.sh open
./access.sh invite-only
./access.sh create 'Guest' --uses 1 --origin https://chess.example.org
./access.sh list
```

Invitation links grant access; do not publish them. `tools/game_admin.py --help`
provides game administration commands. The public game archive is intentionally
visible; do not enter private information into public player names.

## Updates

Stop the server before replacing its executable or network. With a clean
checkout:

```sh
git pull --ff-only
git submodule update --init src/lc0
./setup.sh --backend blas --jobs 4
./run.sh --hosted --engine blas
```

Restore your deployment environment when restarting. Model downloads are pinned
by size and SHA-256 in `runtime/manifest.json`; do not substitute a different
network under the same filename.

## Quick Container Test

`docker compose up --build` creates a portable CPU build and downloads the same
verified network. It binds only to `127.0.0.1:8082`, runs without root privileges,
and stores data in a named volume. `docker compose down` stops it without
deleting games. Source builds are the recommended deployment route, especially
for GPU use and CPU-specific optimization.

```sh
docker compose exec inference ./access.sh status
docker compose exec inference ./access.sh open
```

The image includes engine source and license notices under
`/usr/share/doc/double-move-inference/`.
