#!/usr/bin/env bash

set -euo pipefail

usage() {
    cat <<'EOF'
Uso: ./scripts/deploy_oracle_vm.sh [opciones]

Sincroniza este repo hacia la VM de Oracle por SSH usando rsync,
sin necesidad de hacer push/pull en Git, y reinicia el servicio remoto.
Carga defaults desde config/deploy.env si existe.
El sync elimina archivos remotos obsoletos del repo, pero preserva rutas excluidas.

Opciones:
  --env-file <ruta>       Archivo .env para cargar la config del despliegue.
  --host <usuario@ip>     Host SSH remoto.
  --key <ruta>            Ruta a la llave privada SSH.
  --remote-dir <ruta>     Directorio destino del repo en la VM.
  --service <nombre>      Nombre del servicio systemd remoto.
  --sync-only             Solo sincroniza archivos; no reinicia el servicio.
  --dry-run               Muestra qué cambiaría sin copiar ni reiniciar.
  -h, --help              Muestra esta ayuda.

Defaults:
  env file: <repo>/config/deploy.env
EOF
}

load_env_file() {
    local env_file="$1"

    if [[ ! -f "$env_file" ]]; then
        return 0
    fi

    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
}

resolve_path() {
    local raw_path="$1"

    if [[ "$raw_path" == ~* ]]; then
        raw_path="${raw_path/#\~/$HOME}"
    fi

    if [[ "$raw_path" != /* ]]; then
        raw_path="$repo_root/$raw_path"
    fi

    printf '%s\n' "$raw_path"
}

exclude_repo_relative_path() {
    local absolute_path="$1"

    case "$absolute_path" in
        "$repo_root"/*)
            rsync_args+=("--exclude=${absolute_path#"$repo_root"/}")
            ;;
    esac
}

resolve_unit_working_directory() {
    local unit_file="$1"

    awk -F= '/^WorkingDirectory=/{print $2; exit}' "$unit_file"
}

validate_deploy_target() {
    local unit_file="$1"
    local resolved_remote_dir="$2"
    local resolved_service_name="$3"
    local expected_remote_dir=""
    local expected_service_name=""

    if [[ ! -f "$unit_file" ]]; then
        echo "No existe la unidad systemd esperada: $unit_file" >&2
        exit 1
    fi

    expected_service_name="$(basename "$unit_file" .service)"
    expected_remote_dir="$(resolve_unit_working_directory "$unit_file")"

    if [[ "$resolved_service_name" != "$expected_service_name" ]]; then
        echo "Configuracion invalida: este repo instala ${expected_service_name}.service, pero se resolvio ${resolved_service_name}.service. Corrige $deploy_env_file o usa --service $expected_service_name." >&2
        exit 1
    fi

    if [[ -n "$expected_remote_dir" ]] && [[ "$resolved_remote_dir" != "$expected_remote_dir" ]]; then
        echo "Configuracion invalida: este repo espera remote_dir=$expected_remote_dir segun $unit_file, pero se resolvio $resolved_remote_dir. Corrige $deploy_env_file o usa --remote-dir $expected_remote_dir." >&2
        exit 1
    fi
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
unit_file="$repo_root/systemd/polymarket-insider-finder.service"
deploy_env_file="$repo_root/config/deploy.env"
cli_remote_host=""
cli_ssh_key_path=""
cli_remote_dir=""
cli_service_name=""
sync_only=0
dry_run=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-file)
            deploy_env_file="${2:?Falta valor para --env-file}"
            shift 2
            ;;
        --host)
            cli_remote_host="${2:?Falta valor para --host}"
            shift 2
            ;;
        --key)
            cli_ssh_key_path="${2:?Falta valor para --key}"
            shift 2
            ;;
        --remote-dir)
            cli_remote_dir="${2:?Falta valor para --remote-dir}"
            shift 2
            ;;
        --service)
            cli_service_name="${2:?Falta valor para --service}"
            shift 2
            ;;
        --sync-only)
            sync_only=1
            shift
            ;;
        --dry-run)
            dry_run=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Argumento desconocido: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

deploy_env_file="$(resolve_path "$deploy_env_file")"
load_env_file "$deploy_env_file"

remote_host="${cli_remote_host:-${POLYMARKET_DEPLOY_HOST:-}}"
ssh_key_path="${cli_ssh_key_path:-${POLYMARKET_DEPLOY_SSH_KEY:-}}"
remote_dir="${cli_remote_dir:-${POLYMARKET_DEPLOY_REMOTE_DIR:-/home/ubuntu/polymarket_insider_finder}}"
service_name="${cli_service_name:-${POLYMARKET_DEPLOY_SERVICE:-polymarket-insider-finder}}"

validate_deploy_target "$unit_file" "$remote_dir" "$service_name"

if [[ -z "$remote_host" ]]; then
    echo "Falta configurar POLYMARKET_DEPLOY_HOST en $deploy_env_file o pasar --host." >&2
    exit 1
fi

if [[ -z "$ssh_key_path" ]]; then
    echo "Falta configurar POLYMARKET_DEPLOY_SSH_KEY en $deploy_env_file o pasar --key." >&2
    exit 1
fi

ssh_key_path="$(resolve_path "$ssh_key_path")"

for required_command in rsync ssh; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
        echo "Falta dependencia requerida: $required_command" >&2
        exit 1
    fi
done

if [[ ! -f "$ssh_key_path" ]]; then
    echo "No existe la llave SSH: $ssh_key_path" >&2
    exit 1
fi

ssh_options=(-i "$ssh_key_path" -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes)
rsync_ssh_command="ssh -i \"$ssh_key_path\" -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
rsync_args=(
    -az
    --human-readable
    --itemize-changes
    --delete
    --exclude=.git/
    --exclude=__pycache__/
    --exclude=.DS_Store
    --exclude=data/
    --exclude=logs/
    --exclude=config/deploy.env
    --exclude=config/telegram.env
)

exclude_repo_relative_path "$deploy_env_file"
exclude_repo_relative_path "$ssh_key_path"

if [[ "$dry_run" -eq 1 ]]; then
    rsync_args+=(--dry-run)
fi

echo "==> Preparando directorio remoto $remote_dir en $remote_host"
ssh "${ssh_options[@]}" "$remote_host" "mkdir -p '$remote_dir'"

echo "==> Sincronizando archivos"
rsync "${rsync_args[@]}" -e "$rsync_ssh_command" "$repo_root/" "$remote_host:$remote_dir/"

if [[ "$dry_run" -eq 1 ]]; then
    echo "==> Dry-run completado. No se copiaron archivos ni se reinició el servicio."
    exit 0
fi

if [[ "$sync_only" -eq 1 ]]; then
    echo "==> Sincronización completada. Se omitió el reinicio del servicio."
    exit 0
fi

echo "==> Reinstalando unidad systemd y reiniciando $service_name"
ssh -T "${ssh_options[@]}" "$remote_host" bash -se -- "$remote_dir" "$service_name" <<'REMOTE'
set -euo pipefail

remote_dir="$1"
service_name="$2"

cd "$remote_dir"
sudo install -m 644 systemd/$(basename "$service_name").service "/etc/systemd/system/${service_name}.service"
sudo systemctl daemon-reload
sudo systemctl restart "$service_name"
sudo systemctl status "$service_name" --no-pager --lines=20
REMOTE

echo "==> Despliegue completado"