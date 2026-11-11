# Install frontend design skills for Cursor (project-local).
# Run from repo root: .\scripts\setup-frontend-skills.ps1

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "Installing taste-skill..." -ForegroundColor Cyan
npx --yes skills@latest add https://github.com/Leonxlnx/taste-skill --yes

Write-Host "Installing emilkowalski/skills..." -ForegroundColor Cyan
npx --yes skills@latest add emilkowalski/skills --yes

Write-Host "Installing ui-ux-pro-max..." -ForegroundColor Cyan
npx --yes ui-ux-pro-max-cli init --ai cursor

Write-Host "Installing impeccable..." -ForegroundColor Cyan
echo "1`n1`n" | npx --yes impeccable install --providers=cursor --scope=project

Write-Host "Syncing .agents/skills -> .cursor/skills..." -ForegroundColor Cyan
$src = Join-Path (Get-Location) ".agents\skills"
$dst = Join-Path (Get-Location) ".cursor\skills"
if (Test-Path $src) {
    Get-ChildItem $src -Directory | Where-Object { $_.Name -ne 'impeccable' } | ForEach-Object {
        Copy-Item -Path $_.FullName -Destination (Join-Path $dst $_.Name) -Recurse -Force
    }
}

Write-Host "Done. Restart Cursor to load skills." -ForegroundColor Green
