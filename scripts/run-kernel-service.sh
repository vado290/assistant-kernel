#!/usr/bin/env bash
set -euo pipefail

service_name="${1:-}"
case "${service_name}" in
  assistant-kernel-api|assistant-kernel-mcp|assistant-kernel-discord|assistant-kernel-feishu|assistant-kernel-run-worker|assistant-kernel-maintenance) ;;
  *) echo "unsupported Kernel service: ${service_name}" >&2; exit 64 ;;
esac

version_file="/home/v/AI/assistant-data/runtime/kernel-current.txt"
if [[ ! -f "${version_file}" ]]; then
  echo "Kernel version pointer is missing: ${version_file}" >&2
  exit 66
fi
version="$(tr -d '\r\n' < "${version_file}")"
if [[ ! "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "invalid Kernel version pointer" >&2
  exit 65
fi
binary="/home/v/AI/assistant-data/runtime/kernel-venvs/${version}/bin/${service_name}"
if [[ ! -x "${binary}" ]]; then
  echo "Kernel executable is missing: ${binary}" >&2
  exit 66
fi
exec /usr/bin/env -u PYTHONPATH "${binary}" "${@:2}"
