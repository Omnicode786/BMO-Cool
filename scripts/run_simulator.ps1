param(
    [switch]$DebugLogs
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating .venv..."
    python -m venv .venv
}

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$bmo = Join-Path $repoRoot ".venv\Scripts\bmo-pi.exe"

& $python -m pip install -q -e .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path ".env")) {
    Write-Error ".env is missing. Copy .env.example to .env and add your Gemini/ElevenLabs credentials."
}

$mockSetting = Select-String -Path ".env" -Pattern '^\s*BMO_MOCK_CLOUD\s*=\s*true\s*$' -CaseSensitive:$false
if ($mockSetting) {
    Write-Error "BMO_MOCK_CLOUD=true is set in .env. Change it to false for the real simulator."
}

Write-Host "Starting BMO browser simulator with REAL Gemini + ElevenLabs..."
Write-Host "Open http://127.0.0.1:8765"
Write-Host "Simulator defaults to Touch-to-talk. Start Mic + Speaker, hold TOUCH while speaking, then release."

$runArgs = @("--simulator")
if ($DebugLogs) {
    Write-Host "Verbose debug logging enabled."
    $runArgs += "--debug"
}

& $bmo @runArgs @args
exit $LASTEXITCODE
