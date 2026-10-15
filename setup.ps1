<#
One-shot setup + build for the GreytHR punch-in app (native Windows).

Run from PowerShell in the repo folder:
    powershell -ExecutionPolicy Bypass -File .\setup.ps1

Steps: install deps -> install Chromium -> make sure .env is filled
       (asks you if not) -> dry-run login test -> build dist\punch_in.exe

Flags:
    -NoCheck   skip the dry-run login test
    -NoBuild   skip building the exe
#>
[CmdletBinding()]
param(
    [switch]$NoCheck,
    [switch]$NoBuild
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$App = "punch_in"

function Info($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host " ok $m" -ForegroundColor Green }
function Warn($m) { Write-Host " !! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "error: $m" -ForegroundColor Red; exit 1 }

function Test-Interactive { return (-not [Console]::IsInputRedirected) }

# --- .env helpers ---
function Get-EnvValue([string]$Key) {
    if (-not (Test-Path .env)) { return "" }
    $line = Get-Content .env | Where-Object { $_ -match "^$Key=" } | Select-Object -First 1
    if ($line) { return ($line -replace "^$Key=", "").Trim() }
    return ""
}

function Test-Filled([string]$Key) {
    $v = Get-EnvValue $Key
    return ($v -ne "" -and $v -ne "your_employee_id" -and $v -ne "your_password")
}

function Set-EnvValue([string]$Key, [string]$Value) {
    $lines = @(Get-Content .env)
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -like "$Key=*") { $lines[$i] = "$Key=$Value"; $found = $true; break }
    }
    if (-not $found) { $lines += "$Key=$Value" }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines((Join-Path $PWD ".env"), $lines, $utf8NoBom)
}

function Read-Secret([string]$Prompt) {
    $sec = Read-Host $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

# 1. uv ----------------------------------------------------------------------
Info "Checking uv"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Die "uv not found. Install it, then reopen PowerShell and re-run:`n  powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`""
}
Ok (uv --version)

if (-not (Test-Path pyproject.toml)) { Die "pyproject.toml not found in $PWD" }
if (-not (Test-Path "$App.py"))      { Die "$App.py not found in $PWD" }

# 2. Python deps -------------------------------------------------------------
Info "Installing dependencies (uv sync)"
uv sync
if ($LASTEXITCODE -ne 0) { Die "uv sync failed" }
Ok "virtualenv ready"

# 3. Playwright browser ------------------------------------------------------
Info "Installing Playwright Chromium"
uv run playwright install chromium
if ($LASTEXITCODE -ne 0) { Die "playwright install failed" }
Ok "Chromium installed (used when CHROMIUM_PATH is blank)"

# 4. .env (must be filled before we go any further) --------------------------
Info "Checking .env"
if (-not (Test-Path .env)) {
    if (-not (Test-Path .env.example)) { Die ".env.example missing" }
    Copy-Item .env.example .env
    Ok "created .env from .env.example"
}

if ((Test-Filled "GREYTHR_USER") -and (Test-Filled "GREYTHR_PASS")) {
    Ok ".env has your GreytHR details"
}
else {
    Warn ".env is missing your GreytHR details."
    if (-not (Test-Interactive)) {
        Die "Open .env, fill GREYTHR_USER and GREYTHR_PASS, then re-run setup.ps1"
    }

    Write-Host "Enter your details now (saved to .env, which is git-ignored)."
    $user = ""
    while ($user -eq "") { $user = (Read-Host "GreytHR username").Trim() }
    $pass = ""
    while ($pass -eq "") { $pass = Read-Secret "GreytHR password (hidden)" }
    Set-EnvValue "GREYTHR_USER" $user
    Set-EnvValue "GREYTHR_PASS" $pass

    if ((Get-EnvValue "TEAMS_WEBHOOK_URL") -eq "") {
        $hook = (Read-Host "Teams webhook URL (Enter to skip)").Trim()
        if ($hook -ne "") { Set-EnvValue "TEAMS_WEBHOOK_URL" $hook }
    }
    Ok "details saved to .env"
}

# 5. Dry-run login test ------------------------------------------------------
if (-not $NoCheck) {
    Info "Dry-run login test (will NOT click Sign In)"
    uv run python "$App.py" --dry-run --force --no-delay
    if ($LASTEXITCODE -eq 0) {
        Ok "dry-run passed"
    }
    else {
        Warn "Dry-run failed. Check logs\punch.log and screenshots\ for details."
        if (Test-Interactive) {
            $ans = Read-Host "Build the exe anyway? [y/N]"
            if ($ans -notmatch "^[Yy]") { Die "Stopped before build. Fix the issue and re-run." }
        }
        else { Die "Stopped before build." }
    }
}

# 6. Build the exe -----------------------------------------------------------
if (-not $NoBuild) {
    Info "Building $App.exe (takes a minute)"

    # Use the project's PyInstaller if present, else a temporary one
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    uv run python -c "import PyInstaller" *> $null
    $hasPyi = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prev

    if ($hasPyi) { $pyi = @("run", "pyinstaller") }
    else         { $pyi = @("run", "--with", "pyinstaller", "pyinstaller") }

    & uv @pyi --noconfirm --onefile --noconsole `
        --name $App `
        --collect-all playwright `
        --hidden-import win11toast `
        "$App.py"
    if ($LASTEXITCODE -ne 0) { Die "PyInstaller build failed" }

    New-Item -ItemType Directory -Force dist | Out-Null
    Copy-Item .env dist\.env -Force
    Ok "built dist\$App.exe and copied .env next to it"
    Warn "dist\.env contains your password. Don't share the dist folder."
}

Info "Done"
Write-Host @"
Run it:
    uv run python $App.py --dry-run     # test from source, no click
    .\dist\$App.exe --dry-run --force --no-delay

Startup / Task Scheduler:
    Point it at the full path of dist\$App.exe (the .env beside it is used).
    The build is windowed, so nothing pops up; check dist\logs\punch.log.
    Rebuild after any code change:  .\setup.ps1 -NoCheck
"@
