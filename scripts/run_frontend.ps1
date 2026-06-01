$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$fe = Join-Path $root "frontend-react"

if (-not (Test-Path (Join-Path $fe "package.json"))) {
  throw "frontend-react not found at $fe."
}

Push-Location $fe
try {
  npm run dev
} finally {
  Pop-Location
}

