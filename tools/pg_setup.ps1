# =============================================================================
# @file:         tools/pg_setup.ps1
# @brief:        Start PostgreSQL for start.bat, or ask for an existing or new database.
# @details:      Called by start.bat step 3 only when port 5432 is not listening.
#                Reads PG_BIN / PG_DATA from .env and starts that server. If that fails,
#                it asks: (1) use an existing installation, (2) create a new cluster and
#                database, or (3) skip. The result is saved to .env (PG_BIN, PG_DATA,
#                PG_DSN); old values and duplicates are removed and .env.bak is kept.
#                Passwords are read as SecureString and are never printed or logged.
#                Main API: Read-EnvMap, Save-EnvValues, Build-PgDsn, Invoke-PgSetup
#                Side effects: starts PostgreSQL, may run initdb and createdb, writes .env
# @dependencies: Windows PowerShell 5.1 or PowerShell 7; PostgreSQL binaries
# @notes:        ASCII only: Windows PowerShell 5.1 reads BOM-less UTF-8 incorrectly.
#                Dot-source the file to test the functions without running the menu.
# =============================================================================

param([string]$EnvFile = "")

$ErrorActionPreference = "Stop"
$PgPort = 5432

function Read-EnvMap([string]$Path) {
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        $t = $line.Trim()
        if ($t -eq "" -or $t.StartsWith("#")) { continue }
        $i = $t.IndexOf("=")
        if ($i -lt 1) { continue }
        $key = $t.Substring(0, $i).Trim()
        if (-not $map.ContainsKey($key)) { $map[$key] = $t.Substring($i + 1).Trim() }
    }
    return $map
}

function Save-EnvValues([string]$Path, [hashtable]$Updates) {
    $lines = @()
    if (Test-Path -LiteralPath $Path) {
        Copy-Item -LiteralPath $Path -Destination "$Path.bak" -Force
        $lines = [IO.File]::ReadAllLines($Path)
    }
    $kept = New-Object System.Collections.ArrayList
    foreach ($line in $lines) {
        $t = $line.Trim()
        $key = ""
        if (-not $t.StartsWith("#")) {
            $i = $t.IndexOf("=")
            if ($i -gt 0) { $key = $t.Substring(0, $i).Trim() }
        }
        # Drop every old copy of the keys we write, so no duplicate is left behind.
        if ($key -ne "" -and $Updates.ContainsKey($key)) { continue }
        [void]$kept.Add($line)
    }
    foreach ($k in $Updates.Keys) { [void]$kept.Add("$k=$($Updates[$k])") }
    $enc = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($Path, [string[]]$kept, $enc)
}

function Build-PgDsn([string]$User, [string]$Password, [string]$HostName, [int]$Port, [string]$Database) {
    $u = [Uri]::EscapeDataString($User)
    $p = [Uri]::EscapeDataString($Password)
    return "postgresql://${u}:${p}@${HostName}:${Port}/${Database}"
}

function Get-PlainSecret([string]$Prompt) {
    $sec = Read-Host -Prompt $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Test-PgPort {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect("127.0.0.1", $PgPort, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(1500)) { return $false }
        $client.EndConnect($iar)
        return [bool]$client.Connected
    } catch { return $false }
    finally { $client.Close() }
}

function Wait-PgPort([int]$Seconds = 20) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-PgPort) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Start-PgServer([string]$Bin, [string]$Data) {
    $pgctl = Join-Path $Bin "pg_ctl.exe"
    if (-not (Test-Path -LiteralPath $pgctl)) { return $false }
    $log = Join-Path (Split-Path -Path $Data -Parent) "pg_server.log"
    & $pgctl -D $Data -l $log start | Out-Null
    return (Wait-PgPort 20)
}

function Test-PgLogin([string]$Bin, [string]$User, [string]$Password, [string]$Database) {
    $psql = Join-Path $Bin "psql.exe"
    if (-not (Test-Path -LiteralPath $psql)) { return $false }
    $env:PGPASSWORD = $Password
    try {
        & $psql -h 127.0.0.1 -p $PgPort -U $User -d $Database -tAc "select 1" | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally {
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    }
}

function Use-ExistingPg {
    $bin = Read-Host "Folder that contains pg_ctl.exe and psql.exe (for example C:\Program Files\PostgreSQL\16\bin)"
    $data = Read-Host "Data folder of the cluster (the folder that contains postgresql.conf)"
    if (-not (Test-Path -LiteralPath (Join-Path $bin "pg_ctl.exe"))) { throw "pg_ctl.exe was not found in $bin" }
    if (-not (Test-Path -LiteralPath (Join-Path $data "postgresql.conf"))) { throw "postgresql.conf was not found in $data" }

    $dbName = Read-Host "Database name [printguard]"
    if (-not $dbName) { $dbName = "printguard" }
    $user = Read-Host "Database user [postgres]"
    if (-not $user) { $user = "postgres" }
    $pass = Get-PlainSecret "Password for $user"

    if (-not (Test-PgPort)) {
        $started = Start-PgServer $bin $data
        if (-not $started) { throw "PostgreSQL did not start. See pg_server.log next to the data folder." }
    }
    $ok = Test-PgLogin $bin $user $pass $dbName
    if (-not $ok) { throw "Login to database '$dbName' as '$user' failed. Check the name, user and password, then run start.bat again." }

    return @{
        PG_BIN  = $bin
        PG_DATA = $data
        PG_DSN  = (Build-PgDsn $user $pass "127.0.0.1" $PgPort $dbName)
    }
}

function New-PgCluster {
    $bin = Read-Host "Folder that contains initdb.exe, pg_ctl.exe and createdb.exe"
    $data = Read-Host "New data folder (must not exist, or be empty)"
    foreach ($tool in @("initdb.exe", "pg_ctl.exe", "createdb.exe")) {
        if (-not (Test-Path -LiteralPath (Join-Path $bin $tool))) { throw "$tool was not found in $bin" }
    }
    if (Test-Path -LiteralPath $data) {
        if (@(Get-ChildItem -LiteralPath $data -Force).Count -gt 0) { throw "The data folder is not empty: $data" }
    } else {
        New-Item -ItemType Directory -Path $data | Out-Null
    }

    $dbName = Read-Host "New database name [printguard]"
    if (-not $dbName) { $dbName = "printguard" }
    $user = Read-Host "Superuser name [postgres]"
    if (-not $user) { $user = "postgres" }
    $pass = Get-PlainSecret "New password for $user (at least 8 characters)"
    if ($pass.Length -lt 8) { throw "The password must be at least 8 characters." }

    # initdb reads the password from a temporary file, which is deleted right after.
    $pwfile = Join-Path $env:TEMP ("pgpw_" + [guid]::NewGuid().ToString("N") + ".txt")
    try {
        [IO.File]::WriteAllText($pwfile, $pass, (New-Object System.Text.UTF8Encoding($false)))
        & (Join-Path $bin "initdb.exe") -D $data -U $user --pwfile=$pwfile -E UTF8 --auth=scram-sha-256 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "initdb failed (exit code $LASTEXITCODE)." }
    } finally {
        Remove-Item -LiteralPath $pwfile -Force -ErrorAction SilentlyContinue
    }

    $started = Start-PgServer $bin $data
    if (-not $started) { throw "PostgreSQL did not start from $data." }

    $env:PGPASSWORD = $pass
    try {
        & (Join-Path $bin "createdb.exe") -h 127.0.0.1 -p $PgPort -U $user $dbName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "createdb failed (exit code $LASTEXITCODE)." }
    } finally {
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    }

    return @{
        PG_BIN  = $bin
        PG_DATA = $data
        PG_DSN  = (Build-PgDsn $user $pass "127.0.0.1" $PgPort $dbName)
    }
}

function Invoke-PgSetup([string]$Path) {
    if (Test-PgPort) { return 0 }

    $envMap = Read-EnvMap $Path
    $bin = $envMap["PG_BIN"]
    $data = $envMap["PG_DATA"]
    if ($bin -and $data -and (Test-Path -LiteralPath (Join-Path $bin "pg_ctl.exe"))) {
        Write-Host "[INFO] Starting PostgreSQL from $bin ..."
        if (Start-PgServer $bin $data) {
            Write-Host "[OK] PostgreSQL is listening on port $PgPort."
            return 0
        }
        Write-Host "[WARN] PostgreSQL did not start from the folders saved in .env."
    } else {
        Write-Host "[WARN] PostgreSQL was not found at the saved location (PG_BIN in .env)."
    }

    Write-Host ""
    Write-Host "  [1] Use an existing PostgreSQL (its bin folder, data folder, database and password)"
    Write-Host "  [2] Create a NEW PostgreSQL cluster and database"
    Write-Host "  [3] Skip (start PostgreSQL yourself, then run start.bat again)"
    $choice = Read-Host "Choose 1, 2 or 3"

    try {
        switch ($choice) {
            "1" { $updates = Use-ExistingPg }
            "2" { $updates = New-PgCluster }
            default { Write-Host "[INFO] Skipped."; return 1 }
        }
    } catch {
        Write-Host "[ERROR] $($_.Exception.Message)"
        return 1
    }

    Save-EnvValues $Path $updates
    Write-Host "[OK] .env updated: PG_BIN, PG_DATA and PG_DSN. The password is not shown."
    return 0
}

if ($MyInvocation.InvocationName -ne ".") {
    if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Path $PSScriptRoot -Parent) ".env" }
    exit (Invoke-PgSetup $EnvFile)
}
