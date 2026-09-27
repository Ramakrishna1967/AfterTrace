# Native Qdrant install for Windows (spec p31).
# On Windows, use WSL2 for the documented path and store data in the Linux filesystem.
# This script validates the project layout and prints the WSL commands to run.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Write-Host "AfterTrace native install (Windows host)"
Write-Host "1) Open WSL2 (Ubuntu) and cd to the project via the Linux filesystem path."
Write-Host "2) Run: bash ops/install-qdrant.sh"
Write-Host "3) Terminal A (WSL): QDRANT__SERVICE__API_KEY=<key> ./tools/qdrant/qdrant --config-path ops/qdrant.native.yaml"
Write-Host "4) Terminal B: set QDRANT_URL=http://127.0.0.1:6333 and start uvicorn."
if (!(Test-Path "$root\ops\qdrant.native.yaml")) { throw "ops/qdrant.native.yaml missing" }
if (!(Test-Path "$root\fixtures\manifests\manifest_B.json")) { throw "fixtures missing" }
Write-Host "Project layout OK."
