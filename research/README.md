# Training and Experiments

These files are optional. Hosting the website only requires the released
network and the engine described in the main README.

`trainer/` is the modified JAX/Flax lc0 training pipeline. Initialize it only
when needed:

```sh
git submodule update --init --recursive research/trainer
```

Its README describes the build and training commands. `configs/single-network.textproto`
is a full-capacity training configuration with portable relative paths. Run
commands from the repository root when using that configuration, or adapt its
data and checkpoint paths. Training requires compatible self-play chunks, an
initialized checkpoint, a supported GPU, and substantially more memory than
site inference. It is not part of `setup.sh`.

`slurm/train.sbatch` is an optional scheduler template. Supply your own resource
selection and initialize your software environment before submitting it. It
does not contain a cluster account or partition.

## Engine Options

The modified engine includes self-play and model export support for the two
ability planes, the mid-double-move state, and the split policy. Inspect
`src/lc0` and the trainer's configuration schema for the supported settings.
For a mixture-of-experts configuration, load the MoE1 ability-phase network and
set the UCI `FallbackWeights` option to the unmodified BT3 base network. Routing
then uses BT3 when neither player retains an ability, including a pending
second half of the final double move.

The website defaults to v43. Its optional MoE installation configures both
networks for play and analysis; see [self-hosting](../docs/self-hosting.md#optional-moe-model).

## Licensing

We license our original project modifications under MIT. The trainer is derived from
[LeelaChessZero/lczero-training](https://github.com/LeelaChessZero/lczero-training);
the contribution license does not relicense upstream material. The engine
submodule remains GPL-3.0-or-later. See [component notices](../LICENSES.md).
