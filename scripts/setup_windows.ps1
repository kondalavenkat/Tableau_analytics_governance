$ErrorActionPreference = "Stop"

function Write-Step($msg) {
  Write-Host "==> $msg"
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvDir = Join-Path $root ".venv"
$venvPython = Join-Path $venvDir "Scripts\\python.exe"

Write-Step "Repo root: $root"

function Get-PythonCmd {
  if (Get-Command py -ErrorAction SilentlyContinue) {
    # Prefer newest available, but allow fallback.
    foreach ($v in @("3.14","3.13","3.12","3.11")) {
      try {
        & py "-$v" -c "import sys; print(sys.version)" *> $null
        return @("py", "-$v")
      } catch {}
    }
    return @("py", "-3")
  }
  if (Get-Command python -ErrorAction SilentlyContinue) {
    return @("python")
  }
  throw "Python not found. Install Python 3.12+ (recommended) or add it to PATH."
}

$pyCmd = Get-PythonCmd
Write-Step ("Using Python: " + ($pyCmd -join " "))

if (Test-Path $venvDir) {
  Write-Step "Existing .venv found (keeping it)."
} else {
  Write-Step "Creating virtual environment (.venv)"
  & $pyCmd -m venv --without-pip $venvDir

  Write-Step "Bootstrapping pip in the venv"
  try {
    & $venvPython -m ensurepip --upgrade --default-pip
  } catch {
    # Some locked-down environments break ensurepip unless we pre-load a workaround.
    Write-Step "ensurepip failed; retrying with repo sitecustomize enabled"
    $env:PYTHONPATH = $root
    & $venvPython -m ensurepip --upgrade --default-pip
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
  }
}

Write-Step "Installing backend dependencies"
& $venvPython -m pip install -r (Join-Path $root "requirements.txt") -r (Join-Path $root "requirements_web.txt")

Write-Step "Validating backend imports"
& $venvPython -c "import fastapi, uvicorn, sqlglot, lxml; import backend.main; print('backend import ok')"

Write-Step "Checking Python temp directory write access"
$tempOk = $true
try {
  & $venvPython -c "import tempfile, os; d=tempfile.mkdtemp(); open(os.path.join(d,'x.txt'),'wb').write(b'ok'); print('temp ok')"
} catch {
  $tempOk = $false
}

if (-not $tempOk) {
  Write-Step "Temp write failed; installing sitecustomize workaround into the venv"
  Copy-Item -Force (Join-Path $root "sitecustomize.py") (Join-Path $venvDir "Lib\\site-packages\\sitecustomize.py")
  & $venvPython -c "import tempfile, os; d=tempfile.mkdtemp(); open(os.path.join(d,'x.txt'),'wb').write(b'ok'); print('temp ok after patch')"
}

if (Test-Path (Join-Path $root "frontend-react\\package.json")) {
  if (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Step "Installing frontend dependencies (npm install)"
    Push-Location (Join-Path $root "frontend-react")
    try {
      npm install
    } finally {
      Pop-Location
    }
  } else {
    Write-Step "npm not found; skip frontend install (install Node.js 18+ to run the UI)."
  }
}

Write-Step "Setup complete."

