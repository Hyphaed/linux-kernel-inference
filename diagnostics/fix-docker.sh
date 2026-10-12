#!/bin/bash
# Fix the Docker exposure found 2026-10-08. Run as YOURSELF (you are in the docker group):
#     bash diagnostics/fix-docker.sh            # prints the plan, changes nothing
#     bash diagnostics/fix-docker.sh --apply    # does it
# What it changes, and nothing else:
#  1. penedesaprop-postgres: compose ports -> 127.0.0.1:5433, restart -> "no" (your request),
#     container recreated from the compose file; the named volume `pgdata` is kept; left STOPPED.
#  2. so5076-sqlpro: renamed to so5076-sqlpro-old (kept as a backup, never deleted here) and a
#     new container with the same image, env and data bind-mount is created, published on
#     127.0.0.1:14331 and on the VMware NAT address (vmnet8, for the Windows VM backend), restart "no"; left STOPPED. The SA password is copied container-to-
#     container through a 0600 temp file that is shredded; it is never printed.
#  3. /etc/docker/daemon.json: adds "ip": "127.0.0.1" so any future `-p 5433:5432` without an
#     address binds to localhost. Needs a docker restart (bounces invest-postgres; it has
#     restart=unless-stopped so it returns). Asks first.
# Not changed: invest-postgres (already 127.0.0.1:5434), the docker-lan-block unit (kept as a
# second layer), the docker group (membership is root-equivalent; that is a decision, not a fix).
set -u
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1
COMPOSE=~/Dev/penedesaprop/docker-compose.yml
SQL=so5076-sqlpro
run() { if [ $APPLY = 1 ]; then "$@"; else echo "  [plan] $*"; fi; }
[ "$(id -u)" = 0 ] && { echo "run as your user"; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker not reachable"; exit 1; }

echo "== 1. penedesaprop-postgres"
if grep -q '"5433:5432"' "$COMPOSE"; then
  run cp -a "$COMPOSE" "$COMPOSE.bak-$(date +%Y%m%d)"
  run sed -i -e 's#- "5433:5432"#- "127.0.0.1:5433:5432"#' -e 's#restart: unless-stopped#restart: "no"#' "$COMPOSE"
  run docker compose -f "$COMPOSE" up --no-start --force-recreate postgres
else
  echo "  compose already changed (or ports line differs), skipping"
fi

echo "== 2. $SQL"
if docker inspect "$SQL" >/dev/null 2>&1 && ! docker inspect "$SQL-old" >/dev/null 2>&1; then
  BIND=$(docker inspect "$SQL" --format '{{range .Mounts}}{{if eq .Destination "/var/opt/mssql/data"}}{{.Source}}{{end}}{{end}}')
  IMG=$(docker inspect "$SQL" --format '{{.Config.Image}}')
  [ -n "$BIND" ] || { echo "  cannot find the data bind-mount, aborting this step"; BIND=; }
  if [ -n "$BIND" ]; then
    [ -d "$BIND" ] || echo "  WARNING: $BIND is not mounted right now (external drive?). Container is created anyway."
    if [ "$(docker inspect "$SQL" --format '{{.State.Running}}')" = true ]; then run docker stop "$SQL"; fi
    ENVF=$(mktemp); chmod 600 "$ENVF"
    docker inspect "$SQL" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^(MSSQL_|ACCEPT_EULA)' > "$ENVF"
    run docker rename "$SQL" "$SQL-old"
    # The Windows VM backend reaches this DB at 172.16.77.1,14331 (vmnet8, see PRO_SQL_SERVER_FROM_VM in
    # PRL.PortalEmpleado.NG/.env), so publish on that address too. Binding only 127.0.0.1 cuts the VM off.
    VMIP=$(ip -4 -o addr show vmnet8 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
    PORTS=(-p 127.0.0.1:14331:1433); [ -n "$VMIP" ] && PORTS+=(-p "$VMIP:14331:1433")
    run docker create --name "$SQL" --restart=no --env-file "$ENVF" "${PORTS[@]}" \
        -v "$BIND:/var/opt/mssql/data" "$IMG"
    shred -u "$ENVF" 2>/dev/null || rm -f "$ENVF"
  fi
else
  echo "  $SQL-old already exists or $SQL missing, skipping"
fi

echo "== 3. daemon default bind address"
if grep -q '"ip"' /etc/docker/daemon.json 2>/dev/null; then
  echo "  already set"
else
  if [ $APPLY = 1 ]; then
    read -r -p "  Restart Docker now (invest-postgres bounces)? [y/N] " a
    if [ "$a" = y ]; then
      BAK=/etc/docker/daemon.json.bak-$(date +%Y%m%d)
      sudo cp -a --update=none /etc/docker/daemon.json "$BAK"
      # Build from the backup into a temp file, validate, then install. Piping straight into
      # `sudo tee /etc/docker/daemon.json` truncates it before python can read it (this
      # script did exactly that once and blanked the file, dropping the nvidia runtime).
      NEW=$(mktemp)
      if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["ip"]="127.0.0.1"; print(json.dumps(d,indent=4))' "$BAK" > "$NEW" \
         && python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$NEW"; then
        sudo install -m 644 -o root -g root "$NEW" /etc/docker/daemon.json
      else
        echo "  could not build a valid config, daemon.json left untouched"; rm -f "$NEW"; exit 1
      fi
      rm -f "$NEW"
      sudo systemctl restart docker
    else echo "  skipped"; fi
  else echo "  [plan] add \"ip\": \"127.0.0.1\" to /etc/docker/daemon.json, restart docker (asks first)"; fi
fi

echo "== result"
docker ps -a --format '  {{.Names}}  {{.Status}}  {{.Ports}}'
for n in penedesaprop-postgres $SQL; do echo "  $n restart=$(docker inspect $n --format '{{.HostConfig.RestartPolicy.Name}}' 2>/dev/null) ports=$(docker inspect $n --format '{{json .HostConfig.PortBindings}}' 2>/dev/null)"; done
[ $APPLY = 1 ] || echo "(plan only: nothing changed. Re-run with --apply)"
echo "Start when needed:  docker start penedesaprop-postgres   docker start $SQL"
echo "After checking the new SQL container works:  docker rm $SQL-old"
