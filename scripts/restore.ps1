param(
    [Parameter(Mandatory = $true)]
    [string]$DatabaseBackup,
    [Parameter(Mandatory = $true)]
    [switch]$ConfirmRestore
)

$ErrorActionPreference = "Stop"
$resolvedBackup = [System.IO.Path]::GetFullPath($DatabaseBackup)
if (-not (Test-Path -LiteralPath $resolvedBackup -PathType Leaf)) {
    throw "Backup file does not exist: $resolvedBackup"
}
if (-not $ConfirmRestore) {
    throw "Restore replaces current database objects. Re-run with -ConfirmRestore."
}

Get-Content -Raw -LiteralPath $resolvedBackup | docker compose exec -T postgres psql -U rag -d rag
Write-Host "Database restore completed from $resolvedBackup"

