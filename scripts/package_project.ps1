$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "==> $msg" }

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$out = Join-Path $root ("analytics_governance_portable_" + $stamp + ".zip")
$stage = Join-Path $root ".package_staging"

Write-Step "Packaging repo (portable) to: $out"

# Stage a clean copy using robocopy so access-denied folders don't break packaging.
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$xd = @(
  ".venv",
  ".runtime_venv",
  ".tmp_runtime",
  "runtime_logs",
  ".pytest_cache",
  ".package_staging",
  "frontend-react\\node_modules",
  "frontend-react\\dist"
)

Write-Step "Staging files (robocopy)"
robocopy $root $stage /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XD $xd /XF *.pyc *.pyo > $null
if ($LASTEXITCODE -gt 7) {
  throw "robocopy failed with exit code $LASTEXITCODE"
}

Write-Step "Creating zip"
if (Test-Path $out) { Remove-Item -Force $out }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $out -Force

Write-Step "Cleaning staging folder"
Remove-Item -Recurse -Force $stage

Write-Step "Done."
