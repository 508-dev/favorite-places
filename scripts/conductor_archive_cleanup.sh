#!/usr/bin/env bash
set -u

workspace="${CONDUCTOR_WORKSPACE_PATH:-$(pwd -P)}"

terminate_pids() {
  if [ "$#" -gt 0 ]; then
    kill -TERM "$@" 2>/dev/null || true
  fi
}

kill_pids() {
  if [ "$#" -gt 0 ]; then
    kill -KILL "$@" 2>/dev/null || true
  fi
}

cleanup_port_range() {
  if [ -z "${CONDUCTOR_PORT:-}" ]; then
    return
  fi

  case "$CONDUCTOR_PORT" in
    ''|*[!0-9]*)
      return
      ;;
  esac

  local port="$CONDUCTOR_PORT"
  local end=$((CONDUCTOR_PORT + 9))
  local pids

  while [ "$port" -le "$end" ]; do
    pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "$pids" ]; then
      terminate_pids $pids
    fi
    port=$((port + 1))
  done

  sleep 2

  port="$CONDUCTOR_PORT"
  while [ "$port" -le "$end" ]; do
    pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "$pids" ]; then
      kill_pids $pids
    fi
    port=$((port + 1))
  done
}

cleanup_workspace_processes() {
  local pattern="$workspace/.*(scripts/dev\\.mjs|node_modules/.bin/astro|node_modules/.bin/vite|node_modules/.bin/playwright|build_data\\.py|refresh_profiles\\.py|evaluate_llms\\.py|cloakbrowser)"

  pkill -TERM -f "$pattern" 2>/dev/null || true
  sleep 2
  pkill -KILL -f "$pattern" 2>/dev/null || true
}

cleanup_port_range
cleanup_workspace_processes
