param(
    [Parameter(Mandatory = $true)]
    [string]$DatabaseBackup,
    [string]$UploadsBackup = "",
    [string]$OutputReport = ""
)

$ErrorActionPreference = "Stop"
$startedAt = Get-Date
$resolvedBackup = [System.IO.Path]::GetFullPath($DatabaseBackup)
if (-not (Test-Path -LiteralPath $resolvedBackup -PathType Leaf)) {
    throw "Backup file does not exist: $resolvedBackup"
}
$resolvedUploads = ""
if ($UploadsBackup) {
    $resolvedUploads = [System.IO.Path]::GetFullPath($UploadsBackup)
    if (-not (Test-Path -LiteralPath $resolvedUploads -PathType Container)) {
        throw "Uploads backup does not exist: $resolvedUploads"
    }
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$drillDatabase = "rag_restore_drill_$stamp"
if ($drillDatabase -notmatch '^rag_restore_drill_[0-9_]+$') {
    throw "Unsafe temporary database name: $drillDatabase"
}
$containerBackup = "rag-restore-$stamp.sql"
$postgresContainer = (docker compose ps -q postgres).Trim()
if ($LASTEXITCODE -ne 0 -or -not $postgresContainer) {
    throw "Cannot resolve the PostgreSQL container."
}

function Invoke-Scalar([string]$Database, [string]$Sql) {
    $result = docker compose exec -T postgres psql -U rag -d $Database -tAc $Sql
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL validation query failed against $Database."
    }
    return $result.Trim()
}

function Get-TableFingerprint([string]$Database, [string]$Table) {
    if ($Table -notin @(
        "knowledge_documents", "knowledge_chunks", "retrieval_traces", "langchain_pg_embedding"
    )) {
        throw "Unsafe table name: $Table"
    }
    $sql = "SELECT md5(COALESCE(string_agg(row_to_json(t)::text, E'\n' ORDER BY t.id::text), '')) FROM $Table AS t"
    return Invoke-Scalar $Database $sql
}

$exists = Invoke-Scalar "postgres" "SELECT count(*) FROM pg_database WHERE datname='$drillDatabase'"
if ($exists -ne "0") {
    throw "Safety stop: temporary database already exists: $drillDatabase"
}

$created = $false
try {
    docker compose exec -T postgres createdb -U rag $drillDatabase
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot create isolated restore database."
    }
    $created = $true
    docker cp $resolvedBackup "${postgresContainer}:/tmp/$containerBackup"
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot copy the backup into the PostgreSQL container."
    }
    docker compose exec -T postgres psql -U rag -d $drillDatabase -v ON_ERROR_STOP=1 `
        -f "/tmp/$containerBackup" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Database restore failed."
    }

    $tables = @(
        "knowledge_documents", "knowledge_chunks", "retrieval_traces", "langchain_pg_embedding"
    )
    $fingerprints = @()
    foreach ($table in $tables) {
        $live = Get-TableFingerprint "rag" $table
        $restored = Get-TableFingerprint $drillDatabase $table
        $fingerprints += [pscustomobject][ordered]@{
            table = $table
            live_md5 = $live
            restored_md5 = $restored
            match = ($live -eq $restored)
        }
    }
    $publicTables = [int](Invoke-Scalar $drillDatabase `
        "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
    $vectorExtension = Invoke-Scalar $drillDatabase `
        "SELECT count(*) FROM pg_extension WHERE extname='vector'"

    $uploadFiles = @()
    if ($resolvedUploads) {
        $uploadFiles = @(
            Get-ChildItem -LiteralPath $resolvedUploads -Recurse -File | ForEach-Object {
                [pscustomobject][ordered]@{
                    path = $_.FullName.Substring($resolvedUploads.Length).TrimStart("\", "/")
                    bytes = $_.Length
                    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
            }
        )
    }

    $report = [ordered]@{
        completed_at = (Get-Date).ToUniversalTime().ToString("o")
        source_backup = $resolvedBackup
        source_sha256 = (Get-FileHash -LiteralPath $resolvedBackup -Algorithm SHA256).Hash.ToLowerInvariant()
        isolated_database = $drillDatabase
        restored_public_tables = $publicTables
        vector_extension_present = ($vectorExtension -eq "1")
        table_fingerprints = $fingerprints
        all_database_fingerprints_match = -not ($fingerprints.match -contains $false)
        upload_file_count = $uploadFiles.Count
        upload_total_bytes = [long](($uploadFiles | ForEach-Object { $_.bytes } | Measure-Object -Sum).Sum)
        upload_files = $uploadFiles
        elapsed_seconds = [math]::Round(((Get-Date) - $startedAt).TotalSeconds, 3)
        cleanup = "temporary database and container backup removed"
    }
    if ($OutputReport) {
        $resolvedReport = [System.IO.Path]::GetFullPath($OutputReport)
        $reportDirectory = Split-Path -Parent $resolvedReport
        New-Item -ItemType Directory -Path $reportDirectory -Force | Out-Null
        $report | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $resolvedReport -Encoding utf8
    }
    $report | ConvertTo-Json -Depth 7
}
finally {
    if ($created) {
        docker compose exec -T postgres dropdb -U rag --if-exists $drillDatabase | Out-Null
    }
    docker exec $postgresContainer rm -f "/tmp/$containerBackup" 2>$null
}
