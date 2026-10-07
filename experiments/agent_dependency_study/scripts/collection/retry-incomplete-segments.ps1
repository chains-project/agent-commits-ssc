<#
[IN]: annual sample query_segments.csv, GITHUB_TOKEN, and incomplete/cap-bound segment metadata.
[OUT]: incomplete_retry/ or specified output directory with retried query segments, raw rows, raw_commit_items/*.jsonl.gz, and indexes.
[POS]: Recovers failed or split GitHub Search API segments before corrected_v1 is built.
[SYNC]: If retry splitting, output layout, or raw item index schema changes, update scripts/CLAUDE.md and scripts/OUTPUTS.md.
#>param(
    [string]$SourceDir = ".\scripts\pilot\annual_sample_2026-06-10T11-06-53Z",
    [string]$OutputDir = "",
    [int]$PerPage = 100,
    [int]$MaxPages = 10,
    [int]$SleepSeconds = 5,
    [int]$MaxRetries = 6,
    [int]$RetryBaseSeconds = 30,
    [int]$MinWindowSeconds = 10,
    [int]$MaxSegments = 0
)

$ErrorActionPreference = "Stop"

function Read-GitHubToken {
    $scriptsRoot = Split-Path -Parent $PSScriptRoot
    $envFile = Join-Path $scriptsRoot ".env"
    if (Test-Path -LiteralPath $envFile) {
        foreach ($line in Get-Content -LiteralPath $envFile) {
            if ($line -match "^\s*GITHUB_TOKEN\s*=\s*(.+?)\s*$") {
                return $Matches[1].Trim('"').Trim("'")
            }
        }
    }
    return $env:GITHUB_TOKEN
}

function Format-Utc {
    param([datetime]$Value)
    return $Value.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

function Parse-Utc {
    param([string]$Value)
    return ([datetime]::Parse($Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal)).ToUniversalTime()
}

function Export-AppendCsv {
    param(
        [object[]]$Rows,
        [string]$Path
    )
    if (-not $Rows -or $Rows.Count -eq 0) { return }
    $dir = Split-Path -Parent $Path
    if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    if (Test-Path -LiteralPath $Path) {
        $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Append -Encoding UTF8
    } else {
        $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Encoding UTF8
    }
}

function Export-AppendJsonlGzip {
    param(
        [object[]]$Rows,
        [string]$Path
    )
    if (-not $Rows -or $Rows.Count -eq 0) { return }
    $dir = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $dir | Out-Null

    $fs = [IO.File]::Open($Path, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try {
        $gzip = [IO.Compression.GZipStream]::new($fs, [IO.Compression.CompressionLevel]::Optimal)
        try {
            $encoding = [Text.UTF8Encoding]::new($false)
            $writer = [IO.StreamWriter]::new($gzip, $encoding)
            try {
                foreach ($row in $Rows) {
                    $writer.WriteLine(($row | ConvertTo-Json -Depth 100 -Compress))
                }
            } finally {
                $writer.Dispose()
            }
        } finally {
            if ($gzip) { $gzip.Dispose() }
        }
    } finally {
        $fs.Dispose()
    }
}

function Export-RawCommitItems {
    param(
        [object[]]$RawItems,
        [hashtable]$Seen,
        [string]$RawItemDir,
        [string]$IndexPath
    )
    if (-not $RawItems -or $RawItems.Count -eq 0) { return }

    $byMonth = @{}
    $indexRows = New-Object System.Collections.Generic.List[object]
    foreach ($raw in $RawItems) {
        if (-not $raw.repo_sha -or $Seen.ContainsKey($raw.repo_sha)) { continue }
        $Seen[$raw.repo_sha] = $true
        $month = $raw.original_window_start_utc.Substring(0, 7)
        if (-not $byMonth.ContainsKey($month)) {
            $byMonth[$month] = New-Object System.Collections.Generic.List[object]
        }
        $byMonth[$month].Add($raw) | Out-Null
        $indexRows.Add([pscustomobject]@{
            repo_sha = $raw.repo_sha
            month = $month
            raw_item_path = "raw_commit_items/raw_commit_items_$month.jsonl.gz"
            sha = $raw.sha
            repo = $raw.repo
            first_agent = $raw.agent
            first_tier = $raw.tier
            first_channel = $raw.channel
            first_window_start_utc = $raw.original_window_start_utc
        }) | Out-Null
    }

    foreach ($month in ($byMonth.Keys | Sort-Object)) {
        $path = Join-Path $RawItemDir "raw_commit_items_$month.jsonl.gz"
        Export-AppendJsonlGzip -Rows @($byMonth[$month].ToArray()) -Path $path
    }
    Export-AppendCsv -Rows @($indexRows.ToArray()) -Path $IndexPath
}

function New-QueryForWindow {
    param(
        [object]$Channel,
        [datetime]$Start,
        [datetime]$EndExclusive
    )
    $endInclusive = $EndExclusive.AddSeconds(-1)
    if ($endInclusive -lt $Start) { $endInclusive = $Start }
    return "$($Channel.query) author-date:$(Format-Utc $Start)..$(Format-Utc $endInclusive)"
}

function Invoke-SearchWithRetry {
    param(
        [hashtable]$Headers,
        [string]$Query,
        [int]$Page
    )
    $encoded = [uri]::EscapeDataString($Query)
    $uri = "https://api.github.com/search/commits?q=$encoded&per_page=$PerPage&page=$Page&sort=author-date&order=desc"
    for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
        try {
            $data = Invoke-RestMethod -Uri $uri -Headers $Headers -Method Get -TimeoutSec 45
            return [pscustomobject]@{ Data = $data; Error = ""; Attempts = $attempt }
        } catch {
            $msg = $_.Exception.Message
            if ($attempt -ge $MaxRetries) {
                return [pscustomobject]@{ Data = $null; Error = $msg; Attempts = $attempt }
            }
            $delay = $RetryBaseSeconds * $attempt
            Write-Warning "request failed page=$Page attempt=$attempt; sleeping ${delay}s; $msg"
            Start-Sleep -Seconds $delay
        }
    }
}

function Fetch-ChannelWindow {
    param(
        [hashtable]$Headers,
        [object]$Channel,
        [datetime]$OriginalStart,
        [datetime]$OriginalEnd,
        [datetime]$Start,
        [datetime]$End,
        [int]$Depth
    )

    $query = New-QueryForWindow -Channel $Channel -Start $Start -EndExclusive $End
    $first = Invoke-SearchWithRetry -Headers $Headers -Query $query -Page 1
    $segments = New-Object System.Collections.Generic.List[object]
    $rows = New-Object System.Collections.Generic.List[object]
    $rawItems = New-Object System.Collections.Generic.List[object]

    $durationSeconds = [int]($End - $Start).TotalSeconds
    if ($first.Error) {
        $segments.Add([pscustomobject]@{
            tier = $Channel.tier
            agent = $Channel.agent
            channel = $Channel.channel
            mode = $Channel.mode
            original_window_start_utc = Format-Utc $OriginalStart
            original_window_end_utc = Format-Utc $OriginalEnd
            segment_start_utc = Format-Utc $Start
            segment_end_utc = Format-Utc $End
            depth = $Depth
            query = $Channel.query
            total_count = -1
            fetched_unique_sha = 0
            incomplete_results = $false
            cap_bound = $false
            status = "error"
            attempts = $first.Attempts
            error = $first.Error
        }) | Out-Null
        return [pscustomobject]@{ Segments = @($segments.ToArray()); Rows = @($rows.ToArray()); RawItems = @($rawItems.ToArray()) }
    }

    $data = $first.Data
    $total = [int]$data.total_count
    $incomplete = [bool]$data.incomplete_results
    $maxReturnable = $PerPage * $MaxPages
    $wouldCap = $total -gt $maxReturnable
    $canSplit = $durationSeconds -gt $MinWindowSeconds

    if (($incomplete -or $wouldCap) -and $canSplit) {
        $segments.Add([pscustomobject]@{
            tier = $Channel.tier
            agent = $Channel.agent
            channel = $Channel.channel
            mode = $Channel.mode
            original_window_start_utc = Format-Utc $OriginalStart
            original_window_end_utc = Format-Utc $OriginalEnd
            segment_start_utc = Format-Utc $Start
            segment_end_utc = Format-Utc $End
            depth = $Depth
            query = $Channel.query
            total_count = $total
            fetched_unique_sha = 0
            incomplete_results = $incomplete
            cap_bound = $wouldCap
            status = "split_probe"
            attempts = $first.Attempts
            error = ""
        }) | Out-Null
        $mid = $Start.AddSeconds([math]::Floor($durationSeconds / 2))
        $left = Fetch-ChannelWindow -Headers $Headers -Channel $Channel -OriginalStart $OriginalStart -OriginalEnd $OriginalEnd -Start $Start -End $mid -Depth ($Depth + 1)
        $right = Fetch-ChannelWindow -Headers $Headers -Channel $Channel -OriginalStart $OriginalStart -OriginalEnd $OriginalEnd -Start $mid -End $End -Depth ($Depth + 1)
        foreach ($s in @($left.Segments)) { $segments.Add($s) | Out-Null }
        foreach ($s in @($right.Segments)) { $segments.Add($s) | Out-Null }
        foreach ($r in @($left.Rows)) { $rows.Add($r) | Out-Null }
        foreach ($r in @($right.Rows)) { $rows.Add($r) | Out-Null }
        foreach ($r in @($left.RawItems)) { $rawItems.Add($r) | Out-Null }
        foreach ($r in @($right.RawItems)) { $rawItems.Add($r) | Out-Null }
        return [pscustomobject]@{ Segments = @($segments.ToArray()); Rows = @($rows.ToArray()); RawItems = @($rawItems.ToArray()) }
    }

    $seen = @{}
    $pageCount = [math]::Ceiling($total / $PerPage)
    if ($pageCount -lt 1) { $pageCount = 1 }
    if ($pageCount -gt $MaxPages) { $pageCount = $MaxPages }
    $pageErrors = New-Object System.Collections.Generic.List[string]
    $attempts = $first.Attempts

    for ($page = 1; $page -le $pageCount; $page++) {
        if ($page -eq 1) {
            $pageData = $data
        } else {
            Start-Sleep -Seconds $SleepSeconds
            $pageResult = Invoke-SearchWithRetry -Headers $Headers -Query $query -Page $page
            $attempts += $pageResult.Attempts
            if ($pageResult.Error) {
                $pageErrors.Add("page ${page}: $($pageResult.Error)") | Out-Null
                break
            }
            $pageData = $pageResult.Data
        }

        foreach ($item in @($pageData.items)) {
            $sha = [string]$item.sha
            $repo = ""
            if ($item.repository) { $repo = [string]$item.repository.full_name }
            $rowKey = "$repo|$sha"
            if (-not $sha -or $seen.ContainsKey($rowKey)) { continue }
            $seen[$rowKey] = $true
            $commit = $item.commit
            $author = $commit.author
            $committer = $commit.committer
            $message = [string]$commit.message
            $messageFirst = ""
            if ($message) { $messageFirst = ($message -split "`r?`n")[0] }
            $windowStartText = Format-Utc $OriginalStart
            $windowEndText = Format-Utc $OriginalEnd
            $segmentStartText = Format-Utc $Start
            $segmentEndText = Format-Utc $End
            $rows.Add([pscustomobject]@{
                tier = $Channel.tier
                agent = $Channel.agent
                channel = $Channel.channel
                mode = $Channel.mode
                original_window_start_utc = $windowStartText
                original_window_end_utc = $windowEndText
                segment_start_utc = $segmentStartText
                segment_end_utc = $segmentEndText
                sha = $sha
                repo = $repo
                repo_sha = $rowKey
                author_name = [string]$author.name
                author_email = [string]$author.email
                author_date = [string]$author.date
                committer_email = [string]$committer.email
                html_url = [string]$item.html_url
                message_first_line = $messageFirst
            }) | Out-Null
            $rawItems.Add([pscustomobject]@{
                repo_sha = $rowKey
                sha = $sha
                repo = $repo
                tier = $Channel.tier
                agent = $Channel.agent
                channel = $Channel.channel
                mode = $Channel.mode
                query = $Channel.query
                original_window_start_utc = $windowStartText
                original_window_end_utc = $windowEndText
                segment_start_utc = $segmentStartText
                segment_end_utc = $segmentEndText
                page = $page
                item = $item
            }) | Out-Null
        }
    }

    $terminalCap = ($total -gt $rows.Count) -and ($rows.Count -ge $maxReturnable)
    $status = "ok"
    if ($pageErrors.Count -gt 0) {
        $status = "error_partial"
    } elseif ($terminalCap) {
        $status = "cap_bound_terminal"
    } elseif ($incomplete) {
        $status = "incomplete_terminal"
    }
    $segments.Add([pscustomobject]@{
        tier = $Channel.tier
        agent = $Channel.agent
        channel = $Channel.channel
        mode = $Channel.mode
        original_window_start_utc = Format-Utc $OriginalStart
        original_window_end_utc = Format-Utc $OriginalEnd
        segment_start_utc = Format-Utc $Start
        segment_end_utc = Format-Utc $End
        depth = $Depth
        query = $Channel.query
        total_count = $total
        fetched_unique_sha = $rows.Count
        incomplete_results = $incomplete
        cap_bound = $terminalCap
        status = $status
        attempts = $attempts
        error = ($pageErrors.ToArray() -join " | ")
    }) | Out-Null

    return [pscustomobject]@{ Segments = @($segments.ToArray()); Rows = @($rows.ToArray()); RawItems = @($rawItems.ToArray()) }
}

$sourcePath = Resolve-Path -LiteralPath $SourceDir
$SourceDir = $sourcePath.Path
if (-not $OutputDir) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH-mm-ssZ")
    $OutputDir = Join-Path $SourceDir "incomplete_retry_$stamp"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$sourceSegmentsPath = Join-Path $SourceDir "query_segments.csv"
$sourceRawIndexPath = Join-Path $SourceDir "raw_commit_items_index.csv"
$retrySegmentsPath = Join-Path $OutputDir "query_segments_retry.csv"
$retryRawPath = Join-Path $OutputDir "sha_channels_raw_retry.csv"
$retryRawItemDir = Join-Path $OutputDir "raw_commit_items"
$retryRawIndexPath = Join-Path $OutputDir "raw_commit_items_index_retry.csv"
$summaryPath = Join-Path $OutputDir "retry_summary.csv"

if (-not (Test-Path -LiteralPath $sourceSegmentsPath)) {
    throw "Missing source query_segments.csv: $sourceSegmentsPath"
}

$targets = @(Import-Csv -LiteralPath $sourceSegmentsPath | Where-Object { $_.status -eq "incomplete_terminal" })
if ($MaxSegments -gt 0) { $targets = @($targets | Select-Object -First $MaxSegments) }

$completed = @{}
if (Test-Path -LiteralPath $retrySegmentsPath) {
    foreach ($row in Import-Csv -LiteralPath $retrySegmentsPath) {
        if ($row.status -ne "error" -and $row.status -ne "error_partial" -and $row.status -ne "split_probe" -and $row.status -ne "incomplete_terminal") {
            $completed["$($row.tier)|$($row.agent)|$($row.channel)|$($row.segment_start_utc)|$($row.segment_end_utc)"] = $true
        }
    }
}
if (Test-Path -LiteralPath $summaryPath) {
    foreach ($row in Import-Csv -LiteralPath $summaryPath) {
        $statuses = [string]$row.retry_terminal_statuses
        if ($statuses -and $statuses -notmatch "error|incomplete|cap_bound") {
            $completed["$($row.source_tier)|$($row.source_agent)|$($row.source_channel)|$($row.source_segment_start_utc)|$($row.source_segment_end_utc)"] = $true
        }
    }
}

$rawItemSeen = @{}
if (Test-Path -LiteralPath $sourceRawIndexPath) {
    foreach ($row in Import-Csv -LiteralPath $sourceRawIndexPath) {
        if ($row.repo_sha) { $rawItemSeen[$row.repo_sha] = $true }
    }
}
if (Test-Path -LiteralPath $retryRawIndexPath) {
    foreach ($row in Import-Csv -LiteralPath $retryRawIndexPath) {
        if ($row.repo_sha) { $rawItemSeen[$row.repo_sha] = $true }
    }
}

$token = Read-GitHubToken
$headers = @{
    "Accept" = "application/vnd.github.cloak-preview+json"
    "User-Agent" = "agent-incomplete-retry"
}
if ($token) { $headers["Authorization"] = "token $token" }

Write-Output "source_dir=$SourceDir"
Write-Output "output_dir=$OutputDir"
Write-Output "targets=$($targets.Count) per_page=$PerPage max_pages=$MaxPages min_window_seconds=$MinWindowSeconds sleep_seconds=$SleepSeconds"

$summaryRows = New-Object System.Collections.Generic.List[object]
$i = 0
foreach ($target in $targets) {
    $i++
    $key = "$($target.tier)|$($target.agent)|$($target.channel)|$($target.segment_start_utc)|$($target.segment_end_utc)"
    if ($completed.ContainsKey($key)) { continue }

    $channel = [pscustomobject]@{
        tier = $target.tier
        agent = $target.agent
        channel = $target.channel
        mode = $target.mode
        query = $target.query
    }
    $originalStart = Parse-Utc $target.original_window_start_utc
    $originalEnd = Parse-Utc $target.original_window_end_utc
    $start = Parse-Utc $target.segment_start_utc
    $end = Parse-Utc $target.segment_end_utc

    Write-Output "retry $i/$($targets.Count) $($target.tier)/$($target.agent)/$($target.channel) $($target.segment_start_utc)..$($target.segment_end_utc) old_total=$($target.total_count) old_fetched=$($target.fetched_unique_sha)"
    $result = Fetch-ChannelWindow -Headers $headers -Channel $channel -OriginalStart $originalStart -OriginalEnd $originalEnd -Start $start -End $end -Depth ([int]$target.depth)
    Export-AppendCsv -Rows @($result.Segments) -Path $retrySegmentsPath
    Export-AppendCsv -Rows @($result.Rows) -Path $retryRawPath
    Export-RawCommitItems -RawItems @($result.RawItems) -Seen $rawItemSeen -RawItemDir $retryRawItemDir -IndexPath $retryRawIndexPath

    $terminal = @($result.Segments | Where-Object { $_.status -ne "split_probe" })
    $newRows = @($result.Rows)
    $newUnique = @($newRows | Select-Object -ExpandProperty repo_sha -Unique)
    $summaryRows.Add([pscustomobject]@{
        source_tier = $target.tier
        source_agent = $target.agent
        source_channel = $target.channel
        source_segment_start_utc = $target.segment_start_utc
        source_segment_end_utc = $target.segment_end_utc
        old_total_count = $target.total_count
        old_fetched_unique_sha = $target.fetched_unique_sha
        retry_segment_rows = @($result.Segments).Count
        retry_terminal_statuses = ((@($terminal) | Group-Object status | ForEach-Object { "$($_.Name):$($_.Count)" }) -join ";")
        retry_raw_rows = $newRows.Count
        retry_unique_repo_sha = $newUnique.Count
    }) | Out-Null

    $badSegments = @($result.Segments | Where-Object { $_.status -like "error*" -or $_.status -like "*terminal" })
    foreach ($bad in $badSegments) {
        Write-Warning "$($bad.status) $($bad.agent)/$($bad.channel) $($bad.segment_start_utc)..$($bad.segment_end_utc) total=$($bad.total_count) fetched=$($bad.fetched_unique_sha) $($bad.error)"
    }
    Start-Sleep -Seconds $SleepSeconds
}

Export-AppendCsv -Rows @($summaryRows.ToArray()) -Path $summaryPath
Write-Output "wrote=$retrySegmentsPath"
Write-Output "wrote=$retryRawPath"
Write-Output "wrote=$retryRawItemDir"
Write-Output "wrote=$retryRawIndexPath"
Write-Output "wrote=$summaryPath"
