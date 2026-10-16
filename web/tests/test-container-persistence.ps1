[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$webDirectory = Split-Path -Parent $PSScriptRoot
$projectName = "naftamon-web-persist-$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
$backupPath = Join-Path $env:TEMP "$projectName.sqlite3"
$listener = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = $listener.LocalEndpoint.Port
$listener.Stop()

$previousWebPort = $env:WEB_PORT
$previousBindAddress = $env:BIND_ADDRESS
$env:WEB_PORT = [string]$port
$env:BIND_ADDRESS = "127.0.0.1"
$baseUrl = "http://127.0.0.1:$port"

function Wait-ForHealthyService {
    $deadline = (Get-Date).AddSeconds(60)
    do {
        try {
            $health = Invoke-RestMethod -Uri "$baseUrl/healthz" -TimeoutSec 3
            if ($health.status -eq "ok" -and $health.database -eq "ok") {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    } while ((Get-Date) -lt $deadline)

    throw "NaftaMon Web did not become healthy at $baseUrl."
}

Push-Location $webDirectory
try {
    & docker compose --project-name $projectName up -d --build web
    if ($LASTEXITCODE -ne 0) { throw "docker compose up failed with exit code $LASTEXITCODE." }
    Wait-ForHealthyService

    $now = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    $payload = @{
        timestamp = $now
        source = "NaftaMon"
        complete = $true
        servers = @(@{ server = "Thruk test local"; status = "ok" })
        alerts = @(@{
            server = "Thruk test local"
            backend = "isolated-fixture"
            host = "docker-persistence-smoke"
            service = "disk"
            type = "service"
            state = "WARNING"
            hard = $true
            acknowledged = $false
            downtime = $false
            last_check = $now
            last_change = $now
        })
    } | ConvertTo-Json -Depth 8

    $received = Invoke-RestMethod -Uri "$baseUrl/api/v1/ingest" -Method Post `
        -ContentType "application/json" -Body $payload -TimeoutSec 10
    if (-not $received.accepted -or $received.kind -ne "snapshot") {
        throw "The local test snapshot was not accepted."
    }

    $query = Invoke-RestMethod -Uri "$baseUrl/api/v1/alerts?source_id=$($received.source_id)" -TimeoutSec 10
    if ($query.total -ne 1 -or $query.items[0].host -ne "docker-persistence-smoke") {
        throw "The test alert was not present before the container restart."
    }

    & docker compose --project-name $projectName restart web
    if ($LASTEXITCODE -ne 0) { throw "docker compose restart failed with exit code $LASTEXITCODE." }
    Wait-ForHealthyService

    $afterRestart = Invoke-RestMethod -Uri "$baseUrl/api/v1/alerts?source_id=$($received.source_id)" -TimeoutSec 10
    if ($afterRestart.total -ne 1 -or $afterRestart.items[0].host -ne "docker-persistence-smoke") {
        throw "The test alert was not preserved across the container restart."
    }

    & docker compose --project-name $projectName up -d --force-recreate web
    if ($LASTEXITCODE -ne 0) { throw "docker compose force-recreate failed with exit code $LASTEXITCODE." }
    Wait-ForHealthyService

    $afterRecreate = Invoke-RestMethod -Uri "$baseUrl/api/v1/alerts?source_id=$($received.source_id)" -TimeoutSec 10
    if ($afterRecreate.total -ne 1 -or $afterRecreate.items[0].host -ne "docker-persistence-smoke") {
        throw "The test alert was not preserved across container recreation."
    }

    $rule = @{
        name = "docker-backup-original"
        enabled = $true
        conditions = @{ operator = "AND"; conditions = @(@{ field = "host"; operator = "equals"; value = "docker-persistence-smoke" }) }
        events = @("opened", "state_changed")
        channel_ids = @()
    } | ConvertTo-Json -Depth 8
    Invoke-RestMethod -Uri "$baseUrl/api/v1/notifications/rules" -Method Post `
        -ContentType "application/json" -Body $rule -TimeoutSec 10 | Out-Null
    $configBackup = Invoke-RestMethod -Uri "$baseUrl/api/v1/notifications/backup/export" -Method Post `
        -ContentType "application/json" -Body '{"password":null}' -TimeoutSec 10
    $configBackup.notifications.rules[0].name = "docker-backup-replacement"
    $restorePayload = @{
        backup = $configBackup
        password = $null
        confirm_replace = $true
    } | ConvertTo-Json -Depth 20
    Invoke-RestMethod -Uri "$baseUrl/api/v1/notifications/backup/restore" -Method Post `
        -ContentType "application/json" -Body $restorePayload -TimeoutSec 10 | Out-Null
    $currentRules = Invoke-RestMethod -Uri "$baseUrl/api/v1/notifications/rules" -TimeoutSec 10
    if ($currentRules.items.Count -ne 1 -or $currentRules.items[0].name -ne "docker-backup-replacement") {
        throw "The notification config replacement was not applied before the container restart."
    }

    & docker compose --project-name $projectName exec -T web python -c "import sqlite3; source=sqlite3.connect('/data/naftamon.sqlite3'); target=sqlite3.connect('/tmp/naftamon-backup.sqlite3'); source.backup(target); target.close(); source.close()"
    if ($LASTEXITCODE -ne 0) { throw "SQLite online backup failed with exit code $LASTEXITCODE." }
    & docker compose --project-name $projectName cp web:/tmp/naftamon-backup.sqlite3 $backupPath
    if ($LASTEXITCODE -ne 0) { throw "Copying the SQLite backup to the host failed with exit code $LASTEXITCODE." }

    & docker compose --project-name $projectName run --rm --no-deps web python -c "import sqlite3; db=sqlite3.connect('/data/naftamon.sqlite3'); db.execute('DELETE FROM active_alerts'); db.commit(); db.close()"
    if ($LASTEXITCODE -ne 0) { throw "Preparing the isolated restore check failed with exit code $LASTEXITCODE." }
    $withoutAlert = Invoke-RestMethod -Uri "$baseUrl/api/v1/alerts?source_id=$($received.source_id)" -TimeoutSec 10
    if ($withoutAlert.total -ne 0) { throw "The isolated restore check did not remove the test alert." }

    & docker compose --project-name $projectName stop web
    if ($LASTEXITCODE -ne 0) { throw "Stopping the service for restore failed with exit code $LASTEXITCODE." }
    & docker compose --project-name $projectName cp $backupPath web:/data/naftamon-restore.sqlite3
    if ($LASTEXITCODE -ne 0) { throw "Copying the SQLite backup into the isolated volume failed with exit code $LASTEXITCODE." }
    & docker compose --project-name $projectName run --rm --no-deps web python -c "import os,sqlite3; source=sqlite3.connect('/data/naftamon-restore.sqlite3'); target=sqlite3.connect('/data/naftamon.sqlite3'); source.backup(target); target.close(); source.close(); os.remove('/data/naftamon-restore.sqlite3')"
    if ($LASTEXITCODE -ne 0) { throw "Restoring the SQLite backup failed with exit code $LASTEXITCODE." }
    & docker compose --project-name $projectName start web
    if ($LASTEXITCODE -ne 0) { throw "Starting the service after restore failed with exit code $LASTEXITCODE." }
    Wait-ForHealthyService
    $afterRestore = Invoke-RestMethod -Uri "$baseUrl/api/v1/alerts?source_id=$($received.source_id)" -TimeoutSec 10
    if ($afterRestore.total -ne 1 -or $afterRestore.items[0].host -ne "docker-persistence-smoke") {
        throw "The SQLite backup did not restore the test alert."
    }

    $previousConfig = Invoke-RestMethod -Uri "$baseUrl/api/v1/notifications/backup/previous" -TimeoutSec 10
    if (-not $previousConfig.available -or $previousConfig.rules -ne 1) {
        throw "The pre-restore notification configuration backup did not persist through container restart/recreation."
    }
    Invoke-RestMethod -Uri "$baseUrl/api/v1/notifications/backup/restore-previous" -Method Post `
        -ContentType "application/json" -Body '{"confirm_replace":true}' -TimeoutSec 10 | Out-Null
    $recoveredRules = Invoke-RestMethod -Uri "$baseUrl/api/v1/notifications/rules" -TimeoutSec 10
    if ($recoveredRules.items.Count -ne 1 -or $recoveredRules.items[0].name -ne "docker-backup-original") {
        throw "The pre-restart notification config backup could not be restored after the container restart."
    }

    Write-Output "PASS: SQLite monitoring data and a notification config rollback point survived restart, recreation, online backup and restore (Compose project $projectName)."
}
finally {
    try {
        & docker compose --project-name $projectName down --volumes --remove-orphans
    }
    finally {
        Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
        Pop-Location
        if ($null -eq $previousWebPort) { Remove-Item Env:WEB_PORT -ErrorAction SilentlyContinue } else { $env:WEB_PORT = $previousWebPort }
        if ($null -eq $previousBindAddress) { Remove-Item Env:BIND_ADDRESS -ErrorAction SilentlyContinue } else { $env:BIND_ADDRESS = $previousBindAddress }
    }
}
