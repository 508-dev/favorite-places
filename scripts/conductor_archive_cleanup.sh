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

pid_belongs_to_workspace() {
  local pid="$1"
  local command
  local cwd

  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  case "$command" in
    *"$workspace"*)
      return 0
      ;;
  esac

  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
  case "$cwd" in
    "$workspace"|"$workspace"/*)
      return 0
      ;;
  esac

  return 1
}

workspace_pids_for_pattern() {
  local pattern="$1"
  local pid
  local pids

  pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
  for pid in $pids; do
    if [ "$pid" != "$$" ] && pid_belongs_to_workspace "$pid"; then
      printf '%s\n' "$pid"
    fi
  done
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
    for pid in $pids; do
      if pid_belongs_to_workspace "$pid"; then
        terminate_pids "$pid"
      fi
    done
    port=$((port + 1))
  done

  sleep 2

  port="$CONDUCTOR_PORT"
  while [ "$port" -le "$end" ]; do
    pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    for pid in $pids; do
      if pid_belongs_to_workspace "$pid"; then
        kill_pids "$pid"
      fi
    done
    port=$((port + 1))
  done
}

cleanup_workspace_processes() {
  local pattern="(scripts/dev\\.mjs|node_modules/.bin/astro|node_modules/.bin/vite|node_modules/.bin/playwright|build_data\\.py|refresh_profiles\\.py|evaluate_llms\\.py|cloakbrowser)"
  local pids

  pids="$(workspace_pids_for_pattern "$pattern")"
  if [ -n "$pids" ]; then
    terminate_pids $pids
  fi
  sleep 2
  pids="$(workspace_pids_for_pattern "$pattern")"
  if [ -n "$pids" ]; then
    kill_pids $pids
  fi
}

cleanup_port_range
cleanup_workspace_processes
