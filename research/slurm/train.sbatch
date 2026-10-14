#!/usr/bin/env bash
#SBATCH --job-name=double-strike-training
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --gres=gpu:1
#SBATCH --time=04:00:00

set -euo pipefail
: "${PROJECT_ROOT:?Set PROJECT_ROOT to the cloned repository}"
: "${TRAINER_PYTHON:?Set TRAINER_PYTHON to the trainer virtualenv Python}"
cd "$PROJECT_ROOT"
exec "$TRAINER_PYTHON" -m lczero_training.commands.train \
  --config "$PROJECT_ROOT/research/configs/single-network.textproto"
