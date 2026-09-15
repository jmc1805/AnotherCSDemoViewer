# build.ps1 - Windows equivalent of build.sh. Keep the two in lockstep
# (same Go targets).
#
#   powershell -ExecutionPolicy Bypass -File build.ps1 [options]
#
# Builds the Go binaries (parser, overwatch) into bin/ - the whole app.
param(
    [switch]$Help
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Show-Usage {
    Write-Host @"
Usage: build.ps1 [options]

Builds the Go binaries (parser, overwatch) into bin/ - that's the whole app
(2D viewer, match stats, Players, and the Overwatch layer).

Options:
  -Help   Show this help and exit.
"@
}

if ($Help) { Show-Usage; exit 0 }

function Invoke-Checked {
    param([string]$Label, [scriptblock]$Cmd)
    & $Cmd
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE)" }
}

Write-Host "Downloading dependencies..."
Invoke-Checked 'go mod tidy' { go mod tidy }
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot 'bin') | Out-Null
Write-Host "Building parser..."
Invoke-Checked 'go build parser' { go build -o bin/parser.exe ./cmd/parser }
Write-Host "Building overwatch extractor..."
Invoke-Checked 'go build overwatch' { go build -o bin/overwatch.exe ./cmd/overwatch }

Write-Host "Done. Binaries: $PSScriptRoot\bin\parser.exe, $PSScriptRoot\bin\overwatch.exe"
