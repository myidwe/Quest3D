#!/usr/bin/env bash
# Called after archive checks by bootstrap-host.ps1. Never updates rolling repositories.
set -euo pipefail
mapfile -t TASK_PACKAGES < /quest3d-packages.txt
TASK_RUNTIME=()
for TASK_PACKAGE in "${TASK_PACKAGES[@]}"; do
  if [[ "$TASK_PACKAGE" == */msys2-runtime-* ]]; then
    TASK_RUNTIME+=("$TASK_PACKAGE")
  fi
done
if [[ "${1:-}" == runtime ]]; then
  if ((${#TASK_RUNTIME[@]})); then
    pacman -U --needed --noconfirm "${TASK_RUNTIME[@]}"
  fi
else
  pacman -U --needed --noconfirm "${TASK_PACKAGES[@]}"
fi
