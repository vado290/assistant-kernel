#!/usr/bin/env bash
set -euo pipefail

service_name="${1:-}"
case "${service_name}" in
  assistant-kernel-api|assistant-kernel-mcp|assistant-kernel-discord|assistant-kernel-feishu|assistant-kernel-run-worker|assistant-kernel-maintenance|assistant-kernel-scheduler) ;;
  *) echo "unsupported Kernel service: ${service_name}" >&2; exit 64 ;;
esac

kernel_workspace="${AI_HOME:-/home/v/AI}"
kernel_data="${ASSISTANT_DATA_ROOT:-${kernel_workspace}/assistant-data}"
# Production always runs the fixed prod venv; scripts/deploy.ps1 installs into it.
binary="${kernel_data}/runtime/kernel-venvs/prod/bin/${service_name}"
# Transitional fallback until the first prod deploy exists (remove in rebuild phase 4).
if [[ ! -x "${binary}" && -f "${kernel_data}/runtime/kernel-current.txt" ]]; then
  binary="${kernel_data}/runtime/kernel-venvs/$(tr -d '\r\n' < "${kernel_data}/runtime/kernel-current.txt")/bin/${service_name}"
fi
if [[ ! -x "${binary}" ]]; then
  echo "Kernel executable is missing: ${binary}" >&2
  exit 66
fi
exec /usr/bin/env -u PYTHONPATH -u PYTHONHOME "${binary}" "${@:2}"
