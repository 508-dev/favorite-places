#!/usr/bin/env bash
set -u

dry_run=0
workspace="${CONDUCTOR_WORKSPACE_PATH:-$(pwd -P)}"

usage() {
  cat <<'EOF'
Usage: bash scripts/archive_workspace.sh [--dry-run]

Stops long-running processes that belong to this Conductor workspace.

Options:
  --dry-run  Print matching processes without sending TERM or KILL.
  -h, --help Show this help text.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      dry_run=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

describe_pid() {
  local pid="$1"
  ps -p "$pid" -o pid=,command= 2>/dev/null || printf '%s\n' "$pid"
}

command_uses_workspace_path() {
  local command="$1"

  case "$command" in
    *"$workspace"/*|*"$workspace")
      return 0
      ;;
  esac

  return 1
}

terminate_pids() {
  if [ "$#" -gt 0 ]; then
    if [ "$dry_run" -eq 1 ]; then
      for pid in "$@"; do
        printf 'Would TERM '
        describe_pid "$pid"
      done
    else
      kill -TERM "$@" 2>/dev/null || true
    fi
  fi
}

kill_pids() {
  if [ "$#" -gt 0 ]; then
    if [ "$dry_run" -eq 1 ]; then
      for pid in "$@"; do
        printf 'Would KILL '
        describe_pid "$pid"
      done
    else
      kill -KILL "$@" 2>/dev/null || true
    fi
  fi
}

pid_belongs_to_workspace() {
  local pid="$1"
  local command
  local cwd

  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if command_uses_workspace_path "$command"; then
    return 0
  fi

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

  if [ "$dry_run" -eq 0 ]; then
    sleep 2
  fi

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
  if [ "$dry_run" -eq 0 ]; then
    sleep 2
  fi
  pids="$(workspace_pids_for_pattern "$pattern")"
  if [ -n "$pids" ]; then
    kill_pids $pids
  fi
}

cleanup_port_range
cleanup_workspace_processes
