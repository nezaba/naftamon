
param(
    [switch]$Rebuild,
    [switch]$RebuildVolumes
)

$ErrorActionPreference = 'Stop'

$webDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $webDir

try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker CLI no está disponible en PATH.'
    }

    docker info | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker no está iniciado. Iniciá Docker Desktop.'
    }

    if (-not (Test-Path -LiteralPath '.env' -PathType Leaf)) {
        Copy-Item -LiteralPath '.env.example' -Destination '.env'
        Write-Host 'Se creó web/.env a partir de .env.example.'
    }

    $keyDir = './.local'

    foreach ($line in [System.IO.File]::ReadLines((Join-Path $webDir '.env'))) {
        if ($line -match '^\s*NOTIFICATION_KEY_DIR\s*=\s*(.*?)\s*$') {
            $keyDir = $Matches[1].Trim().Trim('"', "'")
        }
    }

    if (Test-Path Env:NOTIFICATION_KEY_DIR) {
        $keyDir = $env:NOTIFICATION_KEY_DIR
    }

    if ([string]::IsNullOrWhiteSpace($keyDir)) {
        throw 'NOTIFICATION_KEY_DIR no puede estar vacío.'
    }

    if (-not [System.IO.Path]::IsPathRooted($keyDir)) {
        $keyDir = Join-Path $webDir $keyDir
    }

    New-Item -ItemType Directory -Path $keyDir -Force | Out-Null
    $keyFile = Join-Path $keyDir 'notification.key'

    docker compose config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw 'La configuración de Docker Compose no es válida.'
    }

    # ------------------------------------------
    # REBUILD COMPLETO
    # ------------------------------------------

    if ($Rebuild -or $RebuildVolumes) {
        Write-Host 'Iniciando reconstrucción completa...' -ForegroundColor Yellow

        if ($RebuildVolumes) {
            Write-Host 'ATENCIÓN: se eliminarán los volúmenes de Docker Compose.' -ForegroundColor Red
            docker compose down --rmi all --volumes --remove-orphans
        }
        else {
            Write-Host 'Conservando los volúmenes existentes.' -ForegroundColor Cyan
            docker compose down --rmi all --remove-orphans
        }

        if ($LASTEXITCODE -ne 0) {
            throw 'No se pudo limpiar el entorno Docker.'
        }

        Write-Host 'Reconstruyendo imágenes sin caché...' -ForegroundColor Yellow

        docker compose build --no-cache --pull

        if ($LASTEXITCODE -ne 0) {
            throw 'No se pudieron reconstruir las imágenes.'
        }
    }

    # ------------------------------------------
    # CLAVE DE NOTIFICACIONES
    # ------------------------------------------

    if ((Test-Path -LiteralPath $keyFile -PathType Leaf) -and
        (Get-Item -LiteralPath $keyFile).Length -eq 0) {
        throw "La clave de notificaciones existe pero está vacía: $keyFile"
    }

    if (-not (Test-Path -LiteralPath $keyFile -PathType Leaf)) {
        if (-not ($Rebuild -or $RebuildVolumes)) {
            docker compose build web

            if ($LASTEXITCODE -ne 0) {
                throw 'No se pudo construir la imagen web.'
            }
        }

        $generatedOutput = @(
            docker compose run --rm --no-deps -T web python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
        )

        if ($LASTEXITCODE -ne 0 -or
            $generatedOutput.Count -ne 1 -or
            $generatedOutput[0] -notmatch '^[A-Za-z0-9_-]{43}=$') {
            throw 'No se pudo generar una clave de notificaciones válida.'
        }

        $generatedKey = [string]$generatedOutput[0]

        [System.IO.File]::WriteAllText(
            $keyFile,
            "$generatedKey`n",
            [System.Text.UTF8Encoding]::new($false)
        )

        $generatedKey = $null

        Write-Host 'Se creó la clave local de notificaciones.'
        Write-Host 'Guardá una copia segura junto con el backup de SQLite.'
    }

    # ------------------------------------------
    # INICIAR SERVICIOS
    # ------------------------------------------

    Write-Host 'Iniciando servicios...' -ForegroundColor Green

    if ($Rebuild -or $RebuildVolumes) {
        docker compose up -d --no-build --force-recreate web notifications
    }
    else {
        docker compose up -d --build web notifications
    }

    if ($LASTEXITCODE -ne 0) {
        throw 'No se pudieron iniciar web y notifications.'
    }

    docker compose ps web notifications

    if ($LASTEXITCODE -ne 0) {
        throw 'No se pudo consultar el estado de los contenedores.'
    }

    $publishedAddress = docker compose port web 8000

    if ($LASTEXITCODE -eq 0 -and $publishedAddress) {
        Write-Host "Dashboard: http://$($publishedAddress.Trim())"
    }

    Write-Host 'Proceso completado.' -ForegroundColor Green
}
finally {
    Pop-Location
}
