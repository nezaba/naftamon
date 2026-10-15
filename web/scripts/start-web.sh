#!/usr/bin/env bash
set -euo pipefail

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

if [[ -f "$key_file" && ! -s "$key_file" ]]; then
  echo "La clave de notificaciones existe pero está vacía: $key_file" >&2
  exit 1
fi
if [[ ! -f "$key_file" ]]; then
  docker compose build web
  generated_key="$(docker compose run --rm --no-deps -T web python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  if [[ ! "$generated_key" =~ ^[A-Za-z0-9_-]{43}=$ ]]; then
    echo "No se pudo generar una clave de notificaciones válida." >&2
    exit 1
  fi
  printf '%s\n' "$generated_key" > "$key_file"
  unset generated_key
  chmod 600 -- "$key_file"
  echo "Se creó la clave local de notificaciones. Guardá una copia segura junto con el backup de SQLite."
  echo "Si restaurás una base anterior con tokens cifrados, necesitás su clave original."
fi

docker compose up -d --build web notifications
docker compose ps web notifications
if published_address="$(docker compose port web 8000 2>/dev/null)"; then
  echo "Dashboard: http://$published_address"
fi
