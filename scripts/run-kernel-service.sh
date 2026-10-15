#!/usr/bin/env bash
set -euo pipefail

service_name="${1:-}"
case "${service_name}" in
  assistant-kernel-api|assistant-kernel-mcp|assistant-kernel-core-dispatcher|assistant-kernel-core-gateway) ;;
  *) echo "unsupported Kernel service: ${service_name}" >&2; exit 64 ;;
esac

kernel_workspace="${AI_HOME:-/home/v/AI}"
kernel_data="${ASSISTANT_DATA_ROOT:-${kernel_workspace}/assistant-data}"
# Production always runs the fixed prod venv; scripts/deploy.ps1 installs into it.
binary="${kernel_data}/runtime/kernel-venvs/prod/bin/${service_name}"
if [[ ! -x "${binary}" ]]; then
  echo "Kernel executable is missing: ${binary}" >&2
  exit 66
fi
exec /usr/bin/env -u PYTHONPATH -u PYTHONHOME "${binary}" "${@:2}"
