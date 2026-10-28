<#
.SYNOPSIS
  Install/update snmp_exporter on Windows (Phase G · Step G.4).

.DESCRIPTION
  This script is idempotent: running it again shouldn't throw an error and shouldn't break anything.
  - Gets snmp_exporter from GitHub releases with pinned version (does not download if same version is installed)
  - With NSSM, it turns it into a Windows service and sets the log rotation
  - Firewall rules: default localhost only; with -AllowSubnetAccess to scrape from another machine
  - puts monitoring/snmp.yml (from the same repository) next to the binary

  The listen-address and the firewall come from the same switch (decision G.4.2):
    Default → 127.0.0.1:9116 and -RemoteAddress 127.0.0.1,::1
    -AllowSubnetAccess→ 0.0.0.0:9116 and -RemoteAddress LocalSubnet
  The default reason for localhost: the consumer of this exporter is `core/exporter_client.py`
  is running on the same machine; The exporter has no authentication, so open the
  9116 on the subnet means readability of printer data for the entire network (and an SNMP reflector).

.NOTES
  exit codes:
    0 = successful
    1 = significant error (download/extract/start service/verify failed)
    2 = manual action required (not admin, snmp.yml not in repository, file locked, wrong version)
  Execution:
    powershell -ExecutionPolicy Bypass -File windows\install-snmp-exporter.ps1
    powershell -ExecutionPolicy Bypass -File windows\install-snmp-exporter.ps1 -AllowSubnetAccess
  Delete:
    nssm remove printguard-snmp-exporter confirm
  Version update:
    Just change $SNMP_EXPORTER_VERSION in the same file and run the script again.
  This script **does not run** in the development sandbox (it is not Windows, downloading is also prohibited);
  There, only the structure/parser is measured. Document: docs/windows-services.md
#>
# =============================================================================
# @file:         windows/install-snmp-exporter.ps1
# @brief:        Installs or updates snmp_exporter as a Windows service with NSSM.
# @details:      Responsibilities: Download the pinned release, create the service, write the firewall rule and copy monitoring/snmp.yml.
# @dependencies: PowerShell 5.1+, NSSM, GitHub releases (snmp_exporter), monitoring/snmp.yml
# @notes:        Idempotent. Default listen address is localhost only; -AllowSubnetAccess opens it to the subnet.
# =============================================================================

[CmdletBinding()]
param(
    # With this switch, the exporter listens on all interfaces and accepts the firewall subnet.
    [switch]$AllowSubnetAccess,
    # If you get a value, the sha256 of the downloaded zip will be compared to it (empty = just print the hash).
    [string]$ExpectedSha256 = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# Pinned Versions
$SNMP_EXPORTER_VERSION = "0.26.0"
$NSSM_VERSION          = "2.24"

# Paths and Names
$ServiceName = "printguard-snmp-exporter"
$DisplayName = "PrintGuard SNMP Exporter"
$Port        = 9116

$RepoRoot     = Split-Path -Parent $PSScriptRoot
$SpecConfig   = Join-Path $RepoRoot "monitoring\snmp.yml"
$InstallDir   = "C:\PrintGuard\snmp-exporter"
$DataDir      = "C:\PrintGuard\data"
$LogDir       = "C:\PrintGuard\logs"
$LogFile      = Join-Path $LogDir "install-snmp-exporter.log"
$ExePath      = Join-Path $InstallDir "snmp_exporter.exe"
$NssmPath     = Join-Path $InstallDir "nssm.exe"
$TargetConfig = Join-Path $InstallDir "snmp.yml"
$OutLog       = Join-Path $LogDir "snmp-exporter.log"
$ErrLog       = Join-Path $LogDir "snmp-exporter.err.log"

$ExporterZip  = "snmp_exporter-$SNMP_EXPORTER_VERSION.windows-amd64.zip"
$ExporterUrl  = "https://github.com/prometheus/snmp_exporter/releases/download/v$SNMP_EXPORTER_VERSION/$ExporterZip"
$NssmZip      = "nssm-$NSSM_VERSION.zip"
$NssmUrl      = "https://nssm.cc/release/$NssmZip"

# Network mode: one switch, two variables
# -RemoteAddress takes an array; ""127.0.0.1,::1"" is invalid as a single string.
if ($AllowSubnetAccess) {
    $ListenAddress  = "0.0.0.0:9116"
    $FirewallRemote = @("LocalSubnet")
} else {
    $ListenAddress  = "127.0.0.1:9116"
    $FirewallRemote = @("127.0.0.1", "::1")
}
$FirewallRemoteText = $FirewallRemote -join ","          # Same as "127.0.0.1,::1" for message/comparison

# Auxiliary functions
function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Write-Host $line
    if (Test-Path -LiteralPath $LogDir) {
        Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    }
}

function Stop-WithCode {
    param([int]$Code, [string]$Message)
    Write-Log $Message "ERROR"
    Write-Log ("exit=" + $Code) "ERROR"
    exit $Code
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-FileSha {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-InstalledVersion {
    if (-not (Test-Path -LiteralPath $ExePath)) { return $null }
    try {
        $raw = & $ExePath --version 2>&1 | Select-Object -First 1
        if ("$raw" -match '(\d+\.\d+\.\d+)') { return $Matches[1] }
        Write-Log "خروجیِ --version قابلِ تفسیر نبود: $raw" "WARN"
    } catch {
        Write-Log "نسخهٔ باینریِ موجود خوانده نشد: $($_.Exception.Message)" "WARN"
    }
    return $null
}

function Invoke-Nssm {
    param([string[]]$Arguments, [switch]$AllowFailure)
    $out = & $NssmPath @Arguments 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0 -and -not $AllowFailure) {
        throw ("nssm " + ($Arguments -join " ") + " rc=" + $code + " : " + ($out -join " | "))
    }
    return [pscustomobject]@{ Code = $code; Output = ($out -join "`n") }
}

function Install-FromZip {
    # A zip from the pinned source ⇒ extract ⇒ just the file in $InstallDir
    param([string]$Url, [string]$ExeName, [string]$PreferFolder)
    $tmp = Join-Path $env:TEMP ("printguard-snmp-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    try {
        $zip = Join-Path $tmp (Split-Path -Leaf $Url)
        Write-Log ("دریافت: " + $Url)
        Invoke-WebRequest -Uri $Url -OutFile $zip -UseBasicParsing
        $hash = Get-FileSha $zip
        Write-Log ("sha256 " + (Split-Path -Leaf $Url) + " = " + $hash)
        if ($ExpectedSha256) {
            if ($hash -ne $ExpectedSha256.Trim().ToLowerInvariant()) {
                Stop-WithCode 1 "checksumِ فایلِ دانلودی با -ExpectedSha256 نمی‌خواند — نصب متوقف شد"
            }
            Write-Log "checksum تأیید شد"
        } else {
            Write-Log "-ExpectedSha256 داده نشد ⇒ فقط چاپ شد؛ با صفحهٔ انتشار مقایسه‌اش کنید" "WARN"
        }
        Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
        $candidates = @(Get-ChildItem -LiteralPath $tmp -Recurse -Filter $ExeName -ErrorAction SilentlyContinue)
        if ($candidates.Count -eq 0) {
            Stop-WithCode 2 ("در archive هیچ " + $ExeName + " پیدا نشد — منبع یا نسخه را دستی بررسی کنید: " + $Url)
        }
        $pick = $candidates[0]
        if ($PreferFolder) {
            $better = @($candidates | Where-Object { $_.FullName -match $PreferFolder })
            if ($better.Count -gt 0) { $pick = $better[0] }
        }
        Copy-Item -LiteralPath $pick.FullName -Destination (Join-Path $InstallDir $ExeName) -Force
        Write-Log ("نصب شد: " + (Join-Path $InstallDir $ExeName) + "  (از " + $pick.Name + ")")
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Test-ServiceRunning {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    return [bool]($svc -and $svc.Status -eq "Running")
}

function Stop-ServiceQuiet {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $svc) { return }
    if ($svc.Status -eq "Stopped") { return }
    Write-Log ("توقفِ سرویسِ " + $ServiceName)
    if (Test-Path -LiteralPath $NssmPath) {
        & $NssmPath stop $ServiceName 2>&1 | Out-Null
    }
    try {
        Stop-Service -Name $ServiceName -Force -ErrorAction Stop
    } catch {
        Write-Log ("توقفِ کامل انجام نشد (ادامه با بازنویسی فایل‌ها): " + $_.Exception.Message) "WARN"
    }
}

function Update-FirewallRule {
    $rule = Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue
    if ($rule) {
        $addr = (($rule | Get-NetFirewallAddressFilter).RemoteAddress) -join ","
        $port = (($rule | Get-NetFirewallPortFilter).LocalPort) -join ","
        if (($addr -split "," | Sort-Object) -join "," -eq ($FirewallRemote | Sort-Object) -join "," -and "$port" -eq "$Port") {
            Write-Log ("قاعدهٔ فایروال از قبل درست است (RemoteAddress=" + $addr + ")")
            return
        }
        Write-Log ("قاعدهٔ فایروال کهنه است (RemoteAddress=" + $addr + ", LocalPort=" + $port + ") ⇒ بازسازی")
        Remove-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue
    }
    New-NetFirewallRule -DisplayName $DisplayName -Direction Inbound -LocalPort $Port `
        -Protocol TCP -Action Allow -RemoteAddress $FirewallRemote -Profile Any `
        -Description "PrintGuard: snmp_exporter scrape port (mode: $ListenAddress)" | Out-Null
    Write-Log ("قاعدهٔ فایروال نصب شد: Inbound TCP " + $Port + " از " + $FirewallRemoteText)
}

function Set-V3Environment {
    # Passwords are never passed through the command line (in non-loopback these are seen in the process directory):
    # They are written directly in the Service Parameters.
    $vars = @("SNMP_V3_USERNAME", "SNMP_V3_AUTH_PASSPHRASE", "SNMP_V3_PRIV_PASSPHRASE")
    $extra = @()
    foreach ($v in $vars) {
        if (Test-Path ("Env:" + $v)) { $extra += ($v + "=" + (Get-Item ("Env:" + $v)).Value) }
    }
    if ($extra.Count -eq 0) {
        Write-Log "متغیرهای SNMP_V3_* در محیط نبودند ⇒ secure_v3 تا تنظیم‌شدنشان استفاده‌ناپذیر است (spec: docs/snmp-exporter-setup.md §۸)" "WARN"
        return
    }
    $keyPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
    if (-not (Test-Path -LiteralPath $keyPath)) {
        Write-Log "کلیدِ Parametersِ سرویس پیدا نشد ⇒ AppEnvironmentExtra را دستی تنظیم کنید" "WARN"
        return
    }
    New-ItemProperty -Path $keyPath -Name "AppEnvironmentExtra" -PropertyType MultiString -Value $extra -Force | Out-Null
    Write-Log ("AppEnvironmentExtra تنظیم شد (فقط نام‌ها: " + (($extra | ForEach-Object { ($_ -split "=", 2)[0] }) -join ", ") + ")")
}

function Test-Endpoint {
    # A few tries: The service is just up and binding takes a moment
    for ($i = 0; $i -lt 10; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri ("http://localhost:" + $Port + "/") -UseBasicParsing -TimeoutSec 10
            if ($resp.StatusCode -eq 200) { return $true }
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    return $false
}

# Main stream
Write-Log "=== PrintGuard · snmp_exporter installer ==="
Write-Log ("snmp_exporter=" + $SNMP_EXPORTER_VERSION + "  nssm=" + $NSSM_VERSION + "  listen=" + $ListenAddress + "  remote=" + $FirewallRemoteText)
if ($AllowSubnetAccess) {
    Write-Log "-AllowSubnetAccess: پورت 9116 برای LocalSubnet باز می‌شود. exporter احرازِ هویت ندارد؛ فقط وقتی Prometheus روی ماشین دیگری است لازم است." "WARN"
} else {
    Write-Log "حالتِ پیش‌فرض (امن): فقط localhost. اگر Prometheus روی ماشین دیگری است، با -AllowSubnetAccess اجرا کنید."
}

try {
    if (-not (Test-IsAdmin)) {
        Stop-WithCode 2 "این اسکریپت ادمین می‌خواهد: در PowerShellِ Elevated دوباره اجرا کنید"
    }
    if (-not [Environment]::Is64BitOperatingSystem) {
        Stop-WithCode 2 "ویندوز ۳۲بیتی است و asset منتشرشده windows-amd64 است"
    }
    if (-not (Test-Path -LiteralPath $SpecConfig)) {
        Stop-WithCode 2 ("نبودِ " + $SpecConfig + " — اول بازتولیدش کنید: python tools\build_snmp_yml.py")
    }
    foreach ($d in @($InstallDir, $DataDir, $LogDir)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
    Write-Log ("مسیرها: install=" + $InstallDir + "  data=" + $DataDir + "  logs=" + $LogDir)

    # 1) snmp_exporter binary — if it is the same version installed, it will not be downloaded
    $installed = Get-InstalledVersion
    if ($installed -eq $SNMP_EXPORTER_VERSION) {
        Write-Log ("نسخهٔ " + $installed + " از قبل نصب است ⇒ دانلود رد شد (idempotent)")
    } else {
        if ($installed) {
            Write-Log ("به‌روزرسانی: " + $installed + " → " + $SNMP_EXPORTER_VERSION)
            Stop-ServiceQuiet
        } else {
            Write-Log "باینری نصب نیست ⇒ نصبِ اولیه"
        }
        Install-FromZip -Url $ExporterUrl -ExeName "snmp_exporter.exe" -PreferFolder $null
    }

    # 2) NSSM — Download only if not
    if (-not (Test-Path -LiteralPath $NssmPath)) {
        $onPath = Get-Command nssm.exe -ErrorAction SilentlyContinue
        if ($onPath) {
            Copy-Item -LiteralPath $onPath.Source -Destination $NssmPath -Force
            Write-Log ("nssm از PATH کپی شد: " + $onPath.Source)
        } else {
            Install-FromZip -Url $NssmUrl -ExeName "nssm.exe" -PreferFolder "win64"
        }
    } else {
        Write-Log "nssm.exe از قبل در مسیر نصب است ⇒ دانلود رد شد"
    }

    # 3) config — copy of monitoring/snmp.yml (unchanged; the file is OS independent)
    if ((Test-Path -LiteralPath $TargetConfig) -and ((Get-FileSha $SpecConfig) -eq (Get-FileSha $TargetConfig))) {
        Write-Log "snmp.yml در محلِ نصب عیناً همان فایلِ مخزن است ⇒ کپی نشد"
    } else {
        Copy-Item -LiteralPath $SpecConfig -Destination $TargetConfig -Force
        Write-Log ("snmp.yml کپی شد: " + $TargetConfig + "  sha256=" + (Get-FileSha $TargetConfig))
    }

    # 4) Service: install or update
    # The manual equivalent of this step (same as Invoke-Nssm does; for troubleshooting on the server):
    #   nssm install printguard-snmp-exporter C:\PrintGuard\snmp-exporter\snmp_exporter.exe
    #   nssm set printguard-snmp-exporter AppParameters "--config.file=…\snmp.yml --web.listen-address=… --config.expand-env"
    #   nssm set printguard-snmp-exporter AppDirectory C:\PrintGuard\snmp-exporter
    #   nssm set printguard-snmp-exporter AppStdout C:\PrintGuard\logs\snmp-exporter.log
    #   nssm set printguard-snmp-exporter AppStderr C:\PrintGuard\logs\snmp-exporter.err.log
    #   nssm set printguard-snmp-exporter AppRotateFiles 1
    #   nssm set printguard-snmp-exporter AppRotateBytes 10485760
    #   nssm set printguard-snmp-exporter Start SERVICE_AUTO_START
    #   nssm start printguard-snmp-exporter
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $svc) {
        Write-Log ("ایجادِ سرویسِ " + $ServiceName)
        Invoke-Nssm @("install", $ServiceName, $ExePath) | Out-Null
    } else {
        Write-Log ("سرویس از قبل هست ⇒ به‌روزرسانیِ تنظیمات (بدونِ install دوباره)")
        Stop-ServiceQuiet
    }

    $appArgs = "--config.file=" + $TargetConfig + " --web.listen-address=" + $ListenAddress + " --config.expand-env"
    $settings = @(
        @("AppParameters",  $appArgs),
        @("AppDirectory",   $InstallDir),
        @("AppStdout",      $OutLog),
        @("AppStderr",      $ErrLog),
        @("AppRotateFiles", "1"),
        @("AppRotateBytes", "10485760"),
        @("Start",          "SERVICE_AUTO_START")
    )
    foreach ($pair in $settings) {
        Invoke-Nssm @("set", $ServiceName, $pair[0], $pair[1]) | Out-Null
        Write-Log ("nssm set " + $pair[0] + " = " + $pair[1])
    }
    Set-V3Environment

    # 5) Firewall
    Update-FirewallRule

    # 6) Start and confirm
    Write-Log ("شروعِ سرویسِ " + $ServiceName)
    Invoke-Nssm @("start", $ServiceName) | Out-Null
    $ready = $false
    for ($i = 0; $i -lt 10; $i++) {
        if (Test-ServiceRunning) { $ready = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) {
        Stop-WithCode 1 ("سرویس در حالتِ Running نیست — «Get-EventLog -LogName Application -Source " + $ServiceName + "» و " + $ErrLog + " را ببینید")
    }
    if (-not (Test-Endpoint)) {
        Stop-WithCode 1 ("پاسخ 200 از http://localhost:" + $Port + "/ گرفته نشد؛ لاگ: " + $OutLog)
    }

    # 7) Final report
    Write-Log "─" "INFO"
    Write-Log ("موفق: سرویسِ " + $ServiceName + " در حالِ اجراست")
    Write-Log ("  binary   : " + $ExePath + "  (v" + $SNMP_EXPORTER_VERSION + ")")
    Write-Log ("  config   : " + $TargetConfig + "  (= monitoring/snmp.yml از مخزن)")
    Write-Log ("  listen   : " + $ListenAddress + "   mode=" + $(if ($AllowSubnetAccess) { "subnet" } else { "localhost-only" }))
    Write-Log ("  firewall : Inbound TCP " + $Port + " از " + $FirewallRemoteText)
    Write-Log ("  logs     : " + $OutLog + " / " + $ErrLog + "  (rotation: 10485760 بایت)")
    Write-Log ("  probe    : curl http://localhost:" + $Port + "/snmp?target=<ip>&module=printer_hp&auth=public_v2")
    Write-Log ("  remove   : nssm remove " + $ServiceName + " confirm")
    Write-Log "مرحلهٔ بعد: G.5 (core/exporter_client.py) — تا آنجا هیچ مصرف‌کننده‌ای این پورت را صدا نمی‌زند."
    exit 0
} catch {
    Write-Log ("خطای غیرمنتظره: " + $_.Exception.Message) "ERROR"
    Write-Log ("جزئیات: " + $_.ScriptStackTrace) "ERROR"
    exit 1
}
