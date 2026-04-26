# Запуск Product (3001) и Order (3002) в отдельных окнах PowerShell.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

function Start-LampService {
    param([string]$ServiceDir, [int]$Port)
    $name = Split-Path $ServiceDir -Leaf
    $venvPy = Join-Path $ServiceDir ".venv\Scripts\python.exe"
    $py = if (Test-Path $venvPy) { $venvPy } else { "python" }
    $cmd = "Set-Location '$ServiceDir'; Write-Host '$name on http://127.0.0.1:$Port/docs' -ForegroundColor Green; & '$py' -m uvicorn app.main:app --reload --host 127.0.0.1 --port $Port"
    Start-Process powershell -ArgumentList @("-NoExit", "-Command", $cmd)
}

Start-LampService (Join-Path $root "product-service") 3001
Start-Sleep -Milliseconds 400
Start-LampService (Join-Path $root "order-service") 3002

$hub = Join-Path $root "dev-hub.html"
if (Test-Path $hub) {
    Start-Process $hub
}

Write-Host "Открыт dev-hub.html. Сервисы: http://127.0.0.1:3001/docs и http://127.0.0.1:3002/docs" -ForegroundColor Cyan
