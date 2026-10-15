
#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------
# PARÁMETROS
# ------------------------------------------

rebuild=false
rebuild_volumes=false

for arg in "$@"; do
  case "$arg" in
    --rebuild)
      rebuild=true
      ;;
    --rebuild-volumes)
      rebuild=true
      rebuild_volumes=true
      ;;
    -h|--help)
      echo "Uso: $0 [--rebuild] [--rebuild-volumes]"
      echo ""
      echo "  --rebuild          Reconstruye todas las imágenes sin caché."
      echo "                     Conserva los volúmenes."
      echo "  --rebuild-volumes  Reconstruye las imágenes y elimina los"
      echo "                     volúmenes de Docker Compose."
      exit 0
      ;;
    *)
      echo "Parámetro desconocido: $arg" >&2
      exit 1
      ;;
  esac
done

# ------------------------------------------
# CONFIGURACIÓN INICIAL
# ------------------------------------------

web_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$web_dir"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI no está disponible en PATH." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker no está iniciado. Iniciá Docker Desktop o el daemon de Docker." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Se creó web/.env a partir de .env.example. Revisá BIND_ADDRESS antes de publicar en la LAN."
fi

# ------------------------------------------
# DIRECTORIO DE CLAVES
# ------------------------------------------

key_dir=./.local

while IFS= read -r line || [[ -n "$line" ]]; do
  if [[ "$line" =~ ^[[:space:]]*NOTIFICATION_KEY_DIR[[:space:]]*=(.*)$ ]]; then
    key_dir="${BASH_REMATCH[1]%$'\r'}"
    key_dir="${key_dir%%[[:space:]]#*}"
    key_dir="${key_dir#\"}"
    key_dir="${key_dir%\"}"
    key_dir="${key_dir#\'}"
    key_dir="${key_dir%\'}"
  fi
done < .env

key_dir="${NOTIFICATION_KEY_DIR-$key_dir}"

if [[ -z "$key_dir" ]]; then
  echo "NOTIFICATION_KEY_DIR no puede estar vacío." >&2
  exit 1
fi

umask 077
mkdir -p -- "$key_dir"

key_file="$key_dir/notification.key"

docker compose config --quiet

# ------------------------------------------
# REBUILD COMPLETO
# ------------------------------------------

if [[ "$rebuild" == true ]]; then
  echo "Iniciando reconstrucción completa..."

  if [[ "$rebuild_volumes" == true ]]; then
    echo "ATENCIÓN: se eliminarán los volúmenes de Docker Compose."

    docker compose down \
      --rmi all \
      --volumes \
      --remove-orphans
  else
    echo "Conservando los volúmenes existentes."

    docker compose down \
      --rmi all \
      --remove-orphans
  fi

  echo "Reconstruyendo todas las imágenes sin caché..."

  docker compose build \
    --no-cache \
    --pull

  echo "Reconstrucción de imágenes finalizada."
fi

# ------------------------------------------
# CLAVE DE NOTIFICACIONES
# ------------------------------------------

if [[ -f "$key_file" && ! -s "$key_file" ]]; then
  echo "La clave de notificaciones existe pero está vacía: $key_file" >&2
  exit 1
fi

if [[ ! -f "$key_file" ]]; then

  if [[ "$rebuild" == false ]]; then
    docker compose build web
  fi

  generated_key="$(
    docker compose run --rm --no-deps -T web \
      python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
  )"

  if [[ ! "$generated_key" =~ ^[A-Za-z0-9_-]{43}=$ ]]; then
    echo "No se pudo generar una clave de notificaciones válida." >&2
    exit 1
  fi

  printf '%s\n' "$generated_key" > "$key_file"
  unset generated_key

  chmod 600 -- "$key_file"

  echo "Se creó la clave local de notificaciones."
  echo "Guardá una copia segura junto con el backup de SQLite."
  echo "Si restaurás una base anterior con tokens cifrados, necesitás su clave original."
fi

# ------------------------------------------
# INICIAR SERVICIOS
# ------------------------------------------

echo "Iniciando servicios..."

if [[ "$rebuild" == true ]]; then
  docker compose up \
    -d \
    --no-build \
    --force-recreate \
    web notifications
else
  docker compose up \
    -d \
    --build \
    web notifications
fi

# ------------------------------------------
# ESTADO
# ------------------------------------------

docker compose ps web notifications

if published_address="$(docker compose port web 8000 2>/dev/null)"; then
  echo "Dashboard: http://$published_address"
fi

echo "Proceso completado correctamente."
