$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating .venv..."
    python -m venv .venv
}

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$bmo = Join-Path $repoRoot ".venv\Scripts\bmo-pi.exe"

& $python -m pip install -e .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path ".env")) {
    Write-Warning ".env is missing. Copy .env.example to .env and add your Gemini/ElevenLabs credentials."
}

$mockSetting = $null
if (Test-Path ".env") {
    $mockSetting = Select-String -Path ".env" -Pattern '^\s*BMO_MOCK_CLOUD\s*=\s*true\s*$' -CaseSensitive:$false
}
if ($mockSetting) {
    Write-Warning "BMO_MOCK_CLOUD=true is set in .env. Change it to false to use real Gemini/ElevenLabs services."
}

Write-Host "Starting the browser simulator in REAL cloud mode..."
Write-Host "Open http://127.0.0.1:8765"
& $bmo --simulator --debug @args
exit $LASTEXITCODE
