# Double Strike Chess Networks

## Website Default: v43

`v43-latest` is a single-network model for both the ability phase and the
ordinary-chess phase after both abilities are spent. It is the default for the
website and does not require a fallback network.

| Property | Value |
| --- | --- |
| Release | [v43-latest](https://github.com/Raffa2005/Bachelor/releases/tag/v43-latest) |
| Asset | `net-03204500.pb.gz` |
| Size | 190,333,334 bytes |
| SHA-256 | `2e4a434e62f0b4d5d74a1033c318c9d9c0bc6ff3ae3a16782b2812651aa39e58` |
| Body | 15 transformer layers, width 768, 24 attention heads |
| Inputs | 114 feature planes |
| Policy | 1,858 ordinary actions and 1,858 ability-activating actions |
| Value | Win/draw/loss probabilities |

The exact compatible engine revision is pinned in
[`runtime/manifest.json`](runtime/manifest.json). The setup script verifies the
download before installation. The model requires the modified engine's input
encoding and split policy; it is not interchangeable with ordinary lc0 weights.

## Optional MoE Model

The [MoE1 64,000-step release](https://github.com/Raffa2005/Bachelor/releases/tag/moe1-step64000)
contains the ability-phase expert and its unchanged BT3 fallback. They must be
paired through the engine's `FallbackWeights` option. The site defaults to v43
and does not automatically configure this pairing. See [research](research/README.md).

Older model checkpoints can be published on request.

## Origin and Scope

We fine-tuned both networks from Leela Chess Zero's
`BT3-768x15x24h-swa-2790000.pb.gz` using Double Strike Chess self-play.
The base network has SHA-256
`e3067757d1fc2dfc66947b21d15ace0cedf4c54254fc1de83d77c378a3e8b8e1`.
The [official lc0 catalogue](https://lczero.org/play/networks/bestnets/) links to
the [original BT3 checkpoint](https://storage.lczero.org/files/networks-contrib/BT3-768x15x24h-swa-2790000.pb.gz).

Use these networks for game play, analysis, and experiments. Search budget,
hardware, and move sampling affect behavior.

## License Scope

We offer our original model modifications and accompanying metadata under the
[MIT license](LICENSE). Our grant covers our contributions only; it does not
relicense the underlying BT3 parameters or other upstream material. The lc0
engine's GPL license covers its software and is not a separate license grant
for pretrained network weights.
Keep the base-model attribution and this notice with redistributed models.
