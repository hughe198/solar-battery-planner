# Start both servers for local testing from PowerShell: runs start.sh in Git Bash.
# PowerShell can't run a .sh file itself (Windows opens it in an editor instead).
# Stop both with Ctrl+C.
$ErrorActionPreference = 'Stop'

# Git for Windows' bash, not C:\Windows\System32\bash.exe (that one is WSL).
$candidates = @()
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) { $candidates += Join-Path (Split-Path (Split-Path $git.Source)) 'bin\bash.exe' }
$candidates += "$env:ProgramFiles\Git\bin\bash.exe", "${env:ProgramFiles(x86)}\Git\bin\bash.exe"
$bash = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $bash) {
    Write-Error 'Git Bash was not found. Install Git for Windows, or run start.sh from a bash shell.'
    exit 1
}

Push-Location $PSScriptRoot
try { & $bash ./start.sh }
finally { Pop-Location }
exit $LASTEXITCODE
