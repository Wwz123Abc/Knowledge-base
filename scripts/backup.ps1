param(
    [string]$OutputDirectory = ".\backups",
    [switch]$KeepInheritedPermissions
)

$ErrorActionPreference = "Stop"
$startedAt = Get-Date
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $resolvedOutput -Force | Out-Null
if ($env:OS -eq "Windows_NT" -and -not $KeepInheritedPermissions) {
    $currentUserSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $resolvedOutput /inheritance:r `
        /grant:r "*$currentUserSid`:(OI)(CI)F" `
        "*S-1-5-18:(OI)(CI)F" `
        "*S-1-5-32-544:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot restrict backup directory permissions."
    }
    # Explicit file permissions repair older backups that already disabled inheritance.
    & icacls.exe $resolvedOutput /grant:r `
        "*$currentUserSid`:F" "*S-1-5-18:F" "*S-1-5-32-544:F" /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot apply backup permissions recursively."
    }
}
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$databaseFile = Join-Path $resolvedOutput "rag-$stamp.sql"
$stderrFile = Join-Path $resolvedOutput "rag-$stamp.stderr.log"
$uploadsDirectory = Join-Path $resolvedOutput "uploads-$stamp"
$manifestFile = Join-Path $resolvedOutput "manifest-$stamp.json"

$docker = (Get-Command docker -ErrorAction Stop).Source
$dumpArguments = @(
    "compose", "exec", "-T", "postgres", "pg_dump",
    "-U", "rag", "-d", "rag", "--clean", "--if-exists"
)
$dump = Start-Process -FilePath $docker -ArgumentList $dumpArguments -NoNewWindow `
    -Wait -PassThru -RedirectStandardOutput $databaseFile -RedirectStandardError $stderrFile
if ($dump.ExitCode -ne 0) {
    $errorDetail = Get-Content -Raw -LiteralPath $stderrFile -ErrorAction SilentlyContinue
    throw "PostgreSQL backup failed with exit code $($dump.ExitCode): $errorDetail"
}
Remove-Item -LiteralPath $stderrFile -Force

$apiContainer = (docker compose ps -q api).Trim()
if ($LASTEXITCODE -ne 0 -or -not $apiContainer) {
    throw "Cannot resolve the API container."
}
New-Item -ItemType Directory -Path $uploadsDirectory -Force | Out-Null
docker cp "${apiContainer}:/app/data/uploads/." $uploadsDirectory
if ($LASTEXITCODE -ne 0) {
    throw "Upload-file backup failed."
}

$countSql = @"
SELECT json_build_object(
  'documents', (SELECT count(*) FROM knowledge_documents),
  'chunks', (SELECT count(*) FROM knowledge_chunks),
  'traces', (SELECT count(*) FROM retrieval_traces),
  'vectors', (SELECT count(*) FROM langchain_pg_embedding)
);
"@
$counts = docker compose exec -T postgres psql -U rag -d rag -tAc $countSql
if ($LASTEXITCODE -ne 0) {
    throw "Cannot read database backup counters."
}

$uploadFiles = @(
    Get-ChildItem -LiteralPath $uploadsDirectory -Recurse -File | ForEach-Object {
        [pscustomobject][ordered]@{
            path = $_.FullName.Substring($uploadsDirectory.Length).TrimStart("\", "/")
            bytes = $_.Length
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
)
$manifest = [ordered]@{
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    database_file = [System.IO.Path]::GetFileName($databaseFile)
    database_bytes = (Get-Item -LiteralPath $databaseFile).Length
    database_sha256 = (Get-FileHash -LiteralPath $databaseFile -Algorithm SHA256).Hash.ToLowerInvariant()
    database_counts = ($counts.Trim() | ConvertFrom-Json)
    uploads_directory = [System.IO.Path]::GetFileName($uploadsDirectory)
    upload_file_count = $uploadFiles.Count
    upload_total_bytes = [long](($uploadFiles | ForEach-Object { $_.bytes } | Measure-Object -Sum).Sum)
    upload_files = $uploadFiles
    elapsed_seconds = [math]::Round(((Get-Date) - $startedAt).TotalSeconds, 3)
    security = [ordered]@{
        inherited_permissions = [bool]$KeepInheritedPermissions
        encryption = "Use BitLocker, EFS, or an encrypted backup destination"
    }
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestFile -Encoding utf8

Write-Host "Database backup: $databaseFile"
Write-Host "Uploads backup:  $uploadsDirectory"
Write-Host "Manifest:        $manifestFile"
