$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$py = Join-Path $root ".venv\\Scripts\\python.exe"

if (-not (Test-Path $py)) {
  throw "Missing venv python at $py. Run .\\scripts\\setup_windows.ps1 first."
}

& $py -m uvicorn backend.main:app --reload --port 8008

