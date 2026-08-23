#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# Keep machine/account-specific settings reproducible without committing or
# copying them into the image. Existing environment variables win, so callers
# can still make a one-off override.
if [[ -f "$project_dir/.env" ]]; then
  while IFS= read -r env_line || [[ -n "$env_line" ]]; do
    [[ "$env_line" =~ ^[[:space:]]*($|#) ]] && continue
    env_key=${env_line%%=*}
    env_value=${env_line#*=}
    [[ "$env_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ ! -v "$env_key" ]]; then
      printf -v "$env_key" '%s' "$env_value"
      export "$env_key"
    fi
  done < "$project_dir/.env"
fi
container_name=bowxt
image_name=bowxt:0.4.0
volume_name=bowxt-home
context_dir=$project_dir
vnc_scope=${VNC_SCOPE:-window}
plugin_host_dir=${BOWXT_AGENT_PLUGIN_HOST_DIR:-$project_dir/../kjfwd-bot}
if [[ -f "$plugin_host_dir/bowxt-agent.json" ]]; then
  plugin_host_dir=$(cd -- "$plugin_host_dir" && pwd)
elif [[ -n "${BOWXT_AGENT_PLUGIN_HOST_DIR:-}" ]]; then
  echo "BOWXT_AGENT_PLUGIN_HOST_DIR does not contain bowxt-agent.json: $plugin_host_dir" >&2
  exit 2
else
  plugin_host_dir=""
fi

docker_run() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  if sg docker -c 'docker info >/dev/null 2>&1'; then
    local command
    printf -v command '%q ' docker "$@"
    sg docker -c "$command"
    return
  fi
  echo "Docker daemon is unavailable. Log out/in after joining the docker group." >&2
  return 1
}

wait_healthy() {
  local status
  for _ in $(seq 1 60); do
    status=$(docker_run inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_name" 2>/dev/null || true)
    if [[ "$status" == healthy ]]; then
      echo "container is healthy"
      return 0
    fi
    if [[ "$status" == unhealthy || "$status" == exited ]]; then
      docker_run logs --tail=160 "$container_name"
      return 1
    fi
    sleep 2
  done
  echo "timed out waiting for container health" >&2
  docker_run logs --tail=160 "$container_name"
  return 1
}

case "${1:-help}" in
  build)
    docker_run build --pull --tag "$image_name" \
      --file "$project_dir/Dockerfile" "$context_dir"
    ;;
  up)
    if docker_run inspect "$container_name" >/dev/null 2>&1; then
      current_image=$(docker_run inspect -f '{{.Image}}' "$container_name")
      desired_image=$(docker_run image inspect -f '{{.Id}}' "$image_name")
      current_scope=$(docker_run inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container_name" | sed -n 's/^VNC_SCOPE=//p')
      current_poll=$(docker_run inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container_name" | sed -n 's/^BOWXT_POLL_GAP=//p')
      current_action=$(docker_run inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container_name" | sed -n 's/^BOWXT_ACTION_DELAY=//p')
      current_mode=$(docker_run inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container_name" | sed -n 's/^BOWXT_SYNC_MODE=//p')
      current_sender=$(docker_run inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container_name" | sed -n 's/^BOWXT_UIA_SENDER=//p')
      current_my_names=$(docker_run inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container_name" | sed -n 's/^BOWXT_MY_NAMES=//p')
      current_plugin=$(docker_run inspect -f '{{range .Mounts}}{{if eq .Destination "/opt/bowxt-agents/kjfwd-bot"}}{{.Source}}{{end}}{{end}}' "$container_name")
      if [[ "$current_image" != "$desired_image" || "$current_scope" != "$vnc_scope" \
        || "$current_poll" != "${BOWXT_POLL_GAP:-1.5}" \
        || "$current_action" != "${BOWXT_ACTION_DELAY:-0.12}" \
        || "$current_mode" != "${BOWXT_SYNC_MODE:-polling}" \
        || "$current_sender" != "${BOWXT_UIA_SENDER:-1}" \
        || "$current_my_names" != "${BOWXT_MY_NAMES:-}" \
        || "$current_plugin" != "$plugin_host_dir" ]]; then
        echo "replacing container to apply image/display mode (login volume is preserved)"
        docker_run rm -f "$container_name" >/dev/null
      else
        docker_run start "$container_name" >/dev/null
      fi
    fi
    if ! docker_run inspect "$container_name" >/dev/null 2>&1; then
      docker_run volume create "$volume_name" >/dev/null
      plugin_args=(--env BOWXT_AGENT_PLUGIN_DIRS=/opt/bowxt-agents:/home/wechat/.local/share/bowxt/plugins)
      if [[ -n "$plugin_host_dir" ]]; then
        plugin_args+=(--volume "$plugin_host_dir:/opt/bowxt-agents/kjfwd-bot:ro,z")
      fi
      docker_run run -d \
        --name "$container_name" \
        --hostname "$container_name" \
        --restart unless-stopped \
        --security-opt no-new-privileges:true \
        --cap-drop ALL \
        --cap-add CHOWN \
        --cap-add DAC_OVERRIDE \
        --cap-add FOWNER \
        --cap-add KILL \
        --cap-add SETGID \
        --cap-add SETUID \
        --shm-size 1g \
        --env DISPLAY=:1 \
        --env VNC_RESOLUTION=1280x900 \
        --env VNC_DEPTH=24 \
        --env VNC_SCOPE="$vnc_scope" \
        --env BOWXT_POLL_GAP="${BOWXT_POLL_GAP:-1.5}" \
        --env BOWXT_ACTION_DELAY="${BOWXT_ACTION_DELAY:-0.12}" \
        --env BOWXT_SYNC_MODE="${BOWXT_SYNC_MODE:-polling}" \
        --env BOWXT_UIA_SENDER="${BOWXT_UIA_SENDER:-1}" \
        --env BOWXT_MY_NAMES="${BOWXT_MY_NAMES:-}" \
        "${plugin_args[@]}" \
        --env TZ=Asia/Shanghai \
        --publish "127.0.0.1:${NOVNC_PORT:-6080}:6080" \
        --publish "127.0.0.1:${VNC_PORT:-5900}:5900" \
        --publish "127.0.0.1:${WEB_PORT:-8787}:8787" \
        --volume "$volume_name:/home/wechat" \
        "$image_name" >/dev/null
    fi
    wait_healthy
    echo "bowxt: http://127.0.0.1:${WEB_PORT:-8787}/"
    echo "noVNC: http://127.0.0.1:${NOVNC_PORT:-6080}/vnc.html?autoconnect=1&resize=scale&reconnect=1&reconnect_delay=1000"
    echo "VNC scope: $vnc_scope"
    if [[ -n "$plugin_host_dir" ]]; then
      echo "Agent plugin: $plugin_host_dir"
    else
      echo "Agent plugin: none (set BOWXT_AGENT_PLUGIN_HOST_DIR to install one)"
    fi
    ;;
  open)
    xdg-open "http://127.0.0.1:${WEB_PORT:-8787}/" >/dev/null 2>&1 &
    ;;
  vnc)
    xdg-open "http://127.0.0.1:${NOVNC_PORT:-6080}/vnc.html?autoconnect=1&resize=scale&reconnect=1&reconnect_delay=1000" >/dev/null 2>&1 &
    ;;
  status)
    docker_run ps -a --filter "name=^/${container_name}$"
    ;;
  logs)
    docker_run logs -f --tail=160 "$container_name"
    ;;
  unit)
    docker_run exec -u wechat "$container_name" env PYTHONPATH=/opt/bowxt/src \
      python3 -m unittest discover -s /opt/bowxt/tests -v
    ;;
  doctor)
    docker_run exec -u wechat "$container_name" /usr/local/bin/bowxt-session \
      /opt/bowxt-venv/bin/bowxt doctor --tree
    ;;
  ready)
    docker_run exec -u wechat "$container_name" /usr/local/bin/bowxt-session \
      /opt/bowxt-venv/bin/bowxt doctor
    ;;
  input-method)
    docker_run exec -u wechat "$container_name" /usr/local/bin/bowxt-session sh -lc \
      'state=$(fcitx5-remote); printf "fcitx5 state: %s (0=closed, 1=inactive, 2=active)\n" "$state"; printf "configured default: "; sed -n "s/^DefaultIM=//p" ~/.config/fcitx5/profile; printf "XMODIFIERS=%s\nQT_IM_MODULE=%s\n" "$XMODIFIERS" "$QT_IM_MODULE"'
    ;;
  add-chat)
    [[ -n "${2:-}" ]] || { echo "usage: ./manage.sh add-chat NAME [contact|group|unknown]" >&2; exit 2; }
    chat_type=${3:-unknown}
    payload=$(python3 -c 'import json,sys; print(json.dumps({"name":sys.argv[1],"chat_type":sys.argv[2]}, ensure_ascii=False))' "$2" "$chat_type")
    curl -fsS -H 'Content-Type: application/json' -d "$payload" \
      "http://127.0.0.1:${WEB_PORT:-8787}/api/chats"
    printf '\n'
    ;;
  shell)
    docker_run exec -it -u wechat "$container_name" /usr/local/bin/bowxt-session bash
    ;;
  down)
    docker_run stop "$container_name"
    ;;
  purge-login)
    echo "This removes the persisted WeChat login and local container chat data." >&2
    read -r -p "Type PURGE to continue: " answer
    [[ "$answer" == PURGE ]] || exit 1
    docker_run stop "$container_name" >/dev/null 2>&1 || true
    docker_run rm "$container_name" >/dev/null 2>&1 || true
    docker_run volume rm "$volume_name"
    ;;
  *)
    cat <<'EOF'
Usage: ./manage.sh COMMAND

  build        build the pinned image
  up           start and wait for a healthy desktop
  open         open the bowxt web IM
  vnc          open the single-window noVNC page
  status       show container status
  logs         follow service logs
  unit         run bowxt unit tests in the container
  doctor       inspect the live WeChat AT-SPI tree (redacted)
  ready        check whether the logged-in main UI is ready
  input-method show the active fcitx5 input method and environment
  add-chat     add a persistent monitored chat: NAME [contact|group|unknown]
  shell        open a shell inside the desktop D-Bus session
  down         stop the container, preserving login data
  purge-login  remove the persistent login volume after confirmation
EOF
    ;;
esac
