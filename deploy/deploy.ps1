<#
.SYNOPSIS
Deploys Solar Battery Planner from this PC to an Ubuntu server over SSH.

.DESCRIPTION
Runs the tests, builds the frontend, packages the committed backend with the built site,
uploads it with scp and runs deploy/remote.sh on the server with sudo. The server keeps each
release in its own folder and switches a "current" link, backs up the database first, checks
/api/health after restarting and goes back to the previous release by itself if that fails.

Each run asks for your SSH key passphrase twice (scp, then ssh) and your sudo password once.
To type the passphrase once per Windows login instead, start the "OpenSSH Authentication
Agent" service and run ssh-add.

Uses only what Windows 10/11 ships (OpenSSH ssh/scp, tar) plus Git and Node (22.22.3+, 24.15+ or 26+).

.PARAMETER Setup
First run, or to repair: installs python3-venv and Postgres if missing, creates the service
user, folders, database and systemd unit, writes the settings file with setup_env.py, then
deploys. Skips whatever already exists, so it's safe to run again.

.PARAMETER Status
Shows what is set up on the server, which release is live and whether it's healthy.

.PARAMETER Rollback
Switches back to the previous release (or -Release) and restarts. Code only; the database
is left as it is.

.EXAMPLE
.\deploy\deploy.ps1 -Status
.\deploy\deploy.ps1 -Setup
.\deploy\deploy.ps1
.\deploy\deploy.ps1 -SkipTests
.\deploy\deploy.ps1 -Rollback
#>
[CmdletBinding()]
param(
    [switch]$Setup,
    [switch]$Status,
    [switch]$Rollback,
    [string]$Release = '',
    [switch]$SkipTests,
    [switch]$AllowDirty,
    [string]$Config = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

if (@($Setup, $Status, $Rollback | Where-Object { $_ }).Count -gt 1) {
    throw 'Use only one of -Setup, -Status and -Rollback.'
}
if ($Release -and -not $Rollback) { throw '-Release only goes with -Rollback.' }

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Config) { $Config = Join-Path $PSScriptRoot 'deploy.config.psd1' }

function Write-Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "   ok    $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "         $Text" }

# Runs a program and stops the script if it fails. PowerShell doesn't do that for .exe files.
function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments, [string]$WorkDir = $RepoRoot, [string]$What = '')
    Push-Location $WorkDir
    try {
        & $Exe @Arguments
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) {
        if (-not $What) { $What = "$Exe $($Arguments -join ' ')" }
        throw "$What failed (exit code $code)."
    }
}

function Assert-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name not found. $Hint" }
}

# ---------------------------------------------------------------------------- configuration

if (-not (Test-Path $Config)) {
    throw "No config at $Config. Copy deploy\deploy.config.example.psd1 to deploy\deploy.config.psd1 and fill it in."
}
$cfg = @{
    SshPort = 22; SshKey = ''; PublicUrl = ''; BaseDir = '/opt/solar-battery-planner'
    AppUser = 'solar'; ServiceName = 'solar-battery-planner'; DatabaseName = 'solar'
    Port = 8000; Workers = 2; KeepReleases = 5; NightlyCron = $false; LocalPython = ''
}
$loaded = Import-PowerShellDataFile $Config
foreach ($key in $loaded.Keys) { $cfg[$key] = $loaded[$key] }
if (-not $cfg['SshTarget'] -or $cfg['SshTarget'] -like '*example.com') {
    throw "Set SshTarget in $Config."
}
if ($cfg.PublicUrl -like '*example.com*') { $cfg.PublicUrl = '' }

foreach ($key in 'BaseDir', 'AppUser', 'ServiceName', 'DatabaseName', 'PublicUrl') {
    if ("$($cfg[$key])" -match '[\s''"`$;&|<>]') { throw "$key in $Config can't contain spaces, quotes or shell characters." }
}

$sshArgs = @('-p', "$($cfg.SshPort)")
$scpArgs = @('-P', "$($cfg.SshPort)")
if ($cfg.SshKey) {
    $sshArgs += @('-i', $cfg.SshKey)
    $scpArgs += @('-i', $cfg.SshKey)
}

Assert-Command 'ssh' 'Install the Windows "OpenSSH Client" optional feature.'
Assert-Command 'scp' 'Install the Windows "OpenSSH Client" optional feature.'

# Options passed to remote.sh. Values were checked above, so plain spaces are safe here.
$remoteOptions = @(
    '--base', $cfg.BaseDir, '--user', $cfg.AppUser, '--service', $cfg.ServiceName,
    '--db', $cfg.DatabaseName, '--keep', $cfg.KeepReleases, '--port', $cfg.Port,
    '--workers', $cfg.Workers, '--cron', $(if ($cfg.NightlyCron) { 'yes' } else { 'no' })
)
if ($cfg.PublicUrl) { $remoteOptions += @('--origins', $cfg.PublicUrl.TrimEnd('/')) }

function Show-AgentTip {
    if (-not (Get-Command 'ssh-add' -ErrorAction SilentlyContinue)) { return }
    # Windows PowerShell 5.1 turns redirected stderr into errors, which 'Stop' would throw.
    $ErrorActionPreference = 'Continue'
    try { & ssh-add -l 2>&1 | Out-Null; $agentCode = $LASTEXITCODE } catch { $agentCode = 2 }
    $ErrorActionPreference = 'Stop'
    if ($agentCode -ne 0) {
        Write-Note 'Tip: run ssh-add once (with the "OpenSSH Authentication Agent" service started)'
        Write-Note 'to type your key passphrase once per login instead of on every connection.'
    }
}

# Writes remote.sh with Unix line endings, whatever git did on checkout.
function Save-RemoteScript([string]$Destination) {
    $text = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'remote.sh')) -replace "`r`n", "`n"
    [IO.File]::WriteAllText($Destination, $text, (New-Object Text.UTF8Encoding($false)))
}

# Uploads files to /tmp on the server, then runs remote.sh there with sudo in a terminal so
# sudo (and setup_env.py) can ask questions.
function Invoke-Remote {
    param([string]$Mode, [string[]]$Files, [string[]]$Extra = @())
    $tag = 'sbp-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
    $scriptLocal = Join-Path $env:TEMP "$tag-remote.sh"
    Save-RemoteScript $scriptLocal
    try {
        $targets = @($scriptLocal) + $Files
        Write-Step 'Uploading (SSH key passphrase if asked)'
        Invoke-Checked 'scp' ($scpArgs + @('-q') + $targets + @("$($cfg.SshTarget):/tmp/")) -What 'Upload with scp'
        Write-Ok 'uploaded to /tmp'
        Write-Step 'Running on the server (passphrase, then your sudo password)'
        $command = (@('sudo', 'bash', "/tmp/$tag-remote.sh", $Mode) + $Extra + $remoteOptions) -join ' '
        & ssh @sshArgs -t $cfg.SshTarget $command
        if ($LASTEXITCODE -ne 0) { throw "The server step '$Mode' failed (exit code $LASTEXITCODE); see above." }
    } finally {
        Remove-Item $scriptLocal -Force -ErrorAction SilentlyContinue
    }
}

function Test-PublicHealth {
    if (-not $cfg.PublicUrl) { return }
    $url = $cfg.PublicUrl.TrimEnd('/') + '/api/health'
    Write-Step "Checking $url through nginx"
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 15
        Write-Ok "HTTP $($response.StatusCode)"
    } catch {
        Write-Host "   warn  $url didn't answer: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Note 'The app is running on the server; check the nginx site config and certificate.'
    }
}

# ------------------------------------------------------------------------ status / rollback

if ($Status -or $Rollback) {
    Show-AgentTip
    if ($Status) {
        Invoke-Remote -Mode 'status' -Files @()
    } else {
        $extra = @()
        if ($Release) {
            if ($Release -notmatch '^[A-Za-z0-9._-]+$') { throw "Bad release name '$Release'." }
            $extra = @('--id', $Release)
        }
        Invoke-Remote -Mode 'rollback' -Files @() -Extra $extra
        Test-PublicHealth
    }
    return
}

# -------------------------------------------------------------------------- build and deploy

Assert-Command 'git' 'Install Git for Windows.'
Assert-Command 'tar' 'tar.exe ships with Windows 10 1803 and later.'
Assert-Command 'npm' 'Install Node.js 22.22.3+, 24.15+ or 26+.'
Show-AgentTip

Write-Step 'Checking the working copy'
$dirty = (& git -C $RepoRoot status --porcelain) -join "`n"
if ($dirty) {
    if (-not $AllowDirty) {
        Write-Host $dirty
        throw 'There are uncommitted changes. Only committed code is deployed: commit them, or rerun with -AllowDirty to deploy the last commit anyway.'
    }
    Write-Host '   warn  uncommitted changes are NOT included; deploying the last commit' -ForegroundColor Yellow
}
$commit = (& git -C $RepoRoot rev-parse --short=7 HEAD).Trim()
$branch = (& git -C $RepoRoot rev-parse --abbrev-ref HEAD).Trim()
$subject = (& git -C $RepoRoot log -1 --format=%s).Trim()
$id = (Get-Date -Format 'yyyyMMdd-HHmmss') + "-$commit"
Write-Ok "$commit on $branch ($subject)"
if ($branch -ne 'main') { Write-Host "   warn  deploying branch $branch, not main" -ForegroundColor Yellow }

$frontend = Join-Path $RepoRoot 'frontend'
$backend = Join-Path $RepoRoot 'backend'

Write-Step 'Installing frontend packages (npm ci)'
Invoke-Checked 'npm' @('ci', '--no-audit', '--no-fund') -WorkDir $frontend

if (-not $SkipTests) {
    Write-Step 'Backend tests'
    $python = $cfg.LocalPython
    if (-not $python) {
        $venvPython = Join-Path $backend '.venv\Scripts\python.exe'
        if (Test-Path $venvPython) { $python = $venvPython }
        elseif (Get-Command 'py' -ErrorAction SilentlyContinue) { $python = 'py' }
        else { $python = 'python' }
    }
    Invoke-Checked $python @('-m', 'pytest', '-q') -WorkDir $backend -What 'Backend tests (install requirements-dev.txt, or use -SkipTests)'
    Write-Step 'Frontend tests'
    Invoke-Checked 'npx' @('ng', 'test', '--watch=false') -WorkDir $frontend -What 'Frontend tests'
}

Write-Step 'Building the frontend'
Invoke-Checked 'npx' @('ng', 'build') -WorkDir $frontend -What 'ng build'
$dist = Join-Path $frontend 'dist\frontend\browser'
if (-not (Test-Path (Join-Path $dist 'index.html'))) { throw "Build output not found at $dist." }
Write-Ok $dist

Write-Step "Packaging release $id"
$stage = Join-Path $env:TEMP "sbp-$id"
$package = Join-Path $env:TEMP "sbp-$id.tar.gz"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    # Only committed backend files: never a local .env, database or virtualenv.
    $sourceTar = Join-Path $env:TEMP "sbp-$id-src.tar"
    Invoke-Checked 'git' @('archive', '--format=tar', '-o', $sourceTar, 'HEAD', 'backend') -What 'git archive'
    Invoke-Checked 'tar' @('-xf', $sourceTar, '-C', $stage) -What 'Unpacking the backend'
    Remove-Item $sourceTar -Force
    Copy-Item $dist (Join-Path $stage 'frontend') -Recurse
    $version = @(
        "release  $id", "commit   $(& git -C $RepoRoot rev-parse HEAD)", "branch   $branch",
        "subject  $subject", "built    $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz') on $env:COMPUTERNAME"
    ) -join "`n"
    [IO.File]::WriteAllText((Join-Path $stage 'VERSION'), "$version`n", (New-Object Text.UTF8Encoding($false)))
    Invoke-Checked 'tar' @('-czf', $package, '-C', $stage, 'backend', 'frontend', 'VERSION') -What 'Packing the release'
    Write-Ok ('{0} ({1:N1} MB)' -f $package, ((Get-Item $package).Length / 1MB))

    $mode = if ($Setup) { 'setup' } else { 'deploy' }
    Invoke-Remote -Mode $mode -Files @($package) -Extra @('--package', "/tmp/sbp-$id.tar.gz", '--id', $id)
} finally {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $package -Force -ErrorAction SilentlyContinue
}

Test-PublicHealth
Write-Host ''
Write-Host "Deployed $id." -ForegroundColor Green
