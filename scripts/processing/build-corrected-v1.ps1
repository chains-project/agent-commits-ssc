<#
[IN]: original annual sample directory plus incomplete_retry directory.
[OUT]: corrected annual sample directory with merged query_segments.csv, sha_channels_raw.csv, summaries, raw-item copies, and corrected_v1_manifest.csv.
[POS]: Reconciles the first annual collection run with retry outputs before stable data products are built.
[SYNC]: If corrected output schema or retry merge semantics change, update scripts/CLAUDE.md and scripts/OUTPUTS.md.
#>param(
    [string]$SourceDir = ".\scripts\pilot\annual_sample_2026-06-10T11-06-53Z",
    [string]$RetryDir = ".\scripts\pilot\annual_sample_2026-06-10T11-06-53Z\incomplete_retry",
    [string]$OutputDir = ".\scripts\pilot\annual_sample_2026-06-10T11-06-53Z_corrected_v1"
)

$ErrorActionPreference = "Stop"

function Export-AppendCsv {
    param([object[]]$Rows, [string]$Path)
    if (-not $Rows -or $Rows.Count -eq 0) { return }
    if (Test-Path -LiteralPath $Path) {
        $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Append -Encoding UTF8
    } else {
        $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Encoding UTF8
    }
}

function Segment-Key {
    param([object]$Row)
    return "$($Row.tier)|$($Row.agent)|$($Row.channel)|$($Row.segment_start_utc)|$($Row.segment_end_utc)"
}

function Write-Summaries {
    param(
        [string]$OutputDir,
        [string]$RawPath,
        [string]$SegmentPath
    )

    $rawRows = Import-Csv -LiteralPath $RawPath
    $segments = Import-Csv -LiteralPath $SegmentPath

    $channelSets = @{}
    $agentSets = @{}
    $uniqueRows = @{}
    foreach ($row in $rawRows) {
        $channelKey = "$($row.tier)|$($row.agent)|$($row.channel)|$($row.mode)"
        if (-not $channelSets.ContainsKey($channelKey)) { $channelSets[$channelKey] = @{} }
        $channelSets[$channelKey][$row.repo_sha] = $true

        if (-not $agentSets.ContainsKey($row.agent)) { $agentSets[$row.agent] = @{} }
        $agentSets[$row.agent][$row.repo_sha] = $true

        if (-not $uniqueRows.ContainsKey($row.repo_sha)) {
            $uniqueRows[$row.repo_sha] = [pscustomobject]@{
                agent = $row.agent
                tiers = New-Object System.Collections.Generic.HashSet[string]
                repo = $row.repo
                sha = $row.sha
                repo_sha = $row.repo_sha
                channels = New-Object System.Collections.Generic.HashSet[string]
                modes = New-Object System.Collections.Generic.HashSet[string]
                author_name = $row.author_name
                author_email = $row.author_email
                author_date = $row.author_date
                html_url = $row.html_url
                message_first_line = $row.message_first_line
            }
        }
        [void]$uniqueRows[$row.repo_sha].tiers.Add($row.tier)
        [void]$uniqueRows[$row.repo_sha].channels.Add($row.channel)
        [void]$uniqueRows[$row.repo_sha].modes.Add($row.mode)
    }

    $channelRows = New-Object System.Collections.Generic.List[object]
    foreach ($key in ($channelSets.Keys | Sort-Object)) {
        $parts = $key -split "\|"
        $matchingRaw = @($rawRows | Where-Object { $_.tier -eq $parts[0] -and $_.agent -eq $parts[1] -and $_.channel -eq $parts[2] -and $_.mode -eq $parts[3] })
        $matchingSegments = @($segments | Where-Object { $_.tier -eq $parts[0] -and $_.agent -eq $parts[1] -and $_.channel -eq $parts[2] -and $_.mode -eq $parts[3] })
        $channelRows.Add([pscustomobject]@{
            tier = $parts[0]
            agent = $parts[1]
            channel = $parts[2]
            mode = $parts[3]
            raw_rows_before_dedup = $matchingRaw.Count
            unique_repo_sha_after_dedup = $channelSets[$key].Count
            segment_rows = $matchingSegments.Count
            total_count_sum = ($matchingSegments | Measure-Object total_count -Sum).Sum
            fetched_unique_sha_sum = ($matchingSegments | Measure-Object fetched_unique_sha -Sum).Sum
            split_probe_segments = @($matchingSegments | Where-Object { $_.status -eq "split_probe" }).Count
            incomplete_segments = @($matchingSegments | Where-Object { $_.incomplete_results -eq "True" }).Count
            cap_bound_segments = @($matchingSegments | Where-Object { $_.cap_bound -eq "True" }).Count
            error_segments = @($matchingSegments | Where-Object { $_.status -like "error*" }).Count
        }) | Out-Null
    }
    $channelRows | Export-Csv -LiteralPath (Join-Path $OutputDir "channel_counts.csv") -NoTypeInformation -Encoding UTF8

    $agentRows = New-Object System.Collections.Generic.List[object]
    foreach ($agent in ($agentSets.Keys | Sort-Object)) {
        $rawForAgent = @($rawRows | Where-Object { $_.agent -eq $agent })
        $sumChannelUnique = 0
        foreach ($key in $channelSets.Keys) {
            if ($key -like "*|$agent|*") { $sumChannelUnique += $channelSets[$key].Count }
        }
        $ratio = if ($sumChannelUnique -gt 0) { [math]::Round($agentSets[$agent].Count / $sumChannelUnique, 6) } else { 0 }
        $agentRows.Add([pscustomobject]@{
            agent = $agent
            raw_rows_before_dedup = $rawForAgent.Count
            sum_channel_unique_before_cross_channel_dedup = $sumChannelUnique
            unique_repo_sha_after_dedup = $agentSets[$agent].Count
            cross_channel_dedup_ratio = $ratio
        }) | Out-Null
    }
    $agentRows | Export-Csv -LiteralPath (Join-Path $OutputDir "agent_counts.csv") -NoTypeInformation -Encoding UTF8

    $filterRows = New-Object System.Collections.Generic.List[object]
    $filterRows.Add([pscustomobject]@{ scope = "all"; raw_rows_before_dedup = $rawRows.Count; unique_repo_sha_after_dedup = $uniqueRows.Count }) | Out-Null
    foreach ($agent in ($agentSets.Keys | Sort-Object)) {
        $filterRows.Add([pscustomobject]@{ scope = "agent:$agent"; raw_rows_before_dedup = @($rawRows | Where-Object { $_.agent -eq $agent }).Count; unique_repo_sha_after_dedup = $agentSets[$agent].Count }) | Out-Null
    }
    foreach ($tier in (@($rawRows | Select-Object -ExpandProperty tier -Unique) | Sort-Object)) {
        $tierRows = @($rawRows | Where-Object { $_.tier -eq $tier })
        $tierUnique = @($tierRows | Select-Object -ExpandProperty repo_sha -Unique)
        $filterRows.Add([pscustomobject]@{ scope = "tier:$tier"; raw_rows_before_dedup = $tierRows.Count; unique_repo_sha_after_dedup = $tierUnique.Count }) | Out-Null
    }
    $filterRows | Export-Csv -LiteralPath (Join-Path $OutputDir "filter_counts.csv") -NoTypeInformation -Encoding UTF8

    $uniqueOut = foreach ($key in ($uniqueRows.Keys | Sort-Object)) {
        $row = $uniqueRows[$key]
        [pscustomobject]@{
            agent = $row.agent
            tiers = (($row.tiers.ToArray() | Sort-Object) -join ";")
            repo = $row.repo
            sha = $row.sha
            repo_sha = $row.repo_sha
            channels = (($row.channels.ToArray() | Sort-Object) -join ";")
            modes = (($row.modes.ToArray() | Sort-Object) -join ";")
            author_name = $row.author_name
            author_email = $row.author_email
            author_date = $row.author_date
            html_url = $row.html_url
            message_first_line = $row.message_first_line
        }
    }
    $uniqueOut | Export-Csv -LiteralPath (Join-Path $OutputDir "unique_commits.csv") -NoTypeInformation -Encoding UTF8

    $pairRows = New-Object System.Collections.Generic.List[object]
    foreach ($agent in ($agentSets.Keys | Sort-Object)) {
        $keys = @($channelSets.Keys | Where-Object { $_ -like "*|$agent|*" } | Sort-Object)
        for ($i = 0; $i -lt $keys.Count; $i++) {
            for ($j = $i + 1; $j -lt $keys.Count; $j++) {
                $left = $keys[$i]
                $right = $keys[$j]
                $leftSet = $channelSets[$left]
                $rightSet = $channelSets[$right]
                $intersection = 0
                foreach ($sha in $leftSet.Keys) {
                    if ($rightSet.ContainsKey($sha)) { $intersection++ }
                }
                $union = $leftSet.Count + $rightSet.Count - $intersection
                $jaccard = if ($union -gt 0) { [math]::Round($intersection / $union, 6) } else { 0 }
                $leftParts = $left -split "\|"
                $rightParts = $right -split "\|"
                $pairRows.Add([pscustomobject]@{
                    agent = $agent
                    left_tier = $leftParts[0]
                    left_channel = $leftParts[2]
                    right_tier = $rightParts[0]
                    right_channel = $rightParts[2]
                    left_unique_repo_sha = $leftSet.Count
                    right_unique_repo_sha = $rightSet.Count
                    intersection_repo_sha = $intersection
                    union_repo_sha = $union
                    jaccard = $jaccard
                }) | Out-Null
            }
        }
    }
    $pairRows | Export-Csv -LiteralPath (Join-Path $OutputDir "pairwise_overlap.csv") -NoTypeInformation -Encoding UTF8
}

$source = (Resolve-Path -LiteralPath $SourceDir).Path
$retry = (Resolve-Path -LiteralPath $RetryDir).Path
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$sourceSegments = Join-Path $source "query_segments.csv"
$sourceRaw = Join-Path $source "sha_channels_raw.csv"
$retrySegments = Join-Path $retry "query_segments_retry.csv"
$retryRaw = Join-Path $retry "sha_channels_raw_retry.csv"
$retryRawIndex = Join-Path $retry "raw_commit_items_index_retry.csv"

$incompleteKeys = @{}
foreach ($row in Import-Csv -LiteralPath $sourceSegments) {
    if ($row.status -eq "incomplete_terminal") {
        $incompleteKeys[(Segment-Key $row)] = $true
    }
}

foreach ($name in @("channels_compressed.csv", "sampled_windows.csv", "weekly_audit_schedule.csv", "monthly_audit_schedule.csv")) {
    Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $OutputDir $name) -Force
}

$correctedSegmentsPath = Join-Path $OutputDir "query_segments.csv"
if (Test-Path -LiteralPath $correctedSegmentsPath) { Remove-Item -LiteralPath $correctedSegmentsPath -Force }
$segmentBatch = New-Object System.Collections.Generic.List[object]
foreach ($row in Import-Csv -LiteralPath $sourceSegments) {
    if ($incompleteKeys.ContainsKey((Segment-Key $row))) { continue }
    $segmentBatch.Add($row) | Out-Null
    if ($segmentBatch.Count -ge 10000) {
        Export-AppendCsv -Rows @($segmentBatch.ToArray()) -Path $correctedSegmentsPath
        $segmentBatch.Clear()
    }
}
Export-AppendCsv -Rows @($segmentBatch.ToArray()) -Path $correctedSegmentsPath
Export-AppendCsv -Rows @(Import-Csv -LiteralPath $retrySegments) -Path $correctedSegmentsPath

$correctedRawPath = Join-Path $OutputDir "sha_channels_raw.csv"
if (Test-Path -LiteralPath $correctedRawPath) { Remove-Item -LiteralPath $correctedRawPath -Force }
$rawBatch = New-Object System.Collections.Generic.List[object]
foreach ($row in Import-Csv -LiteralPath $sourceRaw) {
    if ($incompleteKeys.ContainsKey((Segment-Key $row))) { continue }
    $rawBatch.Add($row) | Out-Null
    if ($rawBatch.Count -ge 10000) {
        Export-AppendCsv -Rows @($rawBatch.ToArray()) -Path $correctedRawPath
        $rawBatch.Clear()
    }
}
Export-AppendCsv -Rows @($rawBatch.ToArray()) -Path $correctedRawPath
if (Test-Path -LiteralPath $retryRaw) {
    Export-AppendCsv -Rows @(Import-Csv -LiteralPath $retryRaw) -Path $correctedRawPath
}

$rawItemSource = Join-Path $source "raw_commit_items"
if (Test-Path -LiteralPath $rawItemSource) {
    Copy-Item -LiteralPath $rawItemSource -Destination (Join-Path $OutputDir "raw_commit_items") -Recurse -Force
}
$rawItemRetry = Join-Path $retry "raw_commit_items"
if (Test-Path -LiteralPath $rawItemRetry) {
    Copy-Item -LiteralPath $rawItemRetry -Destination (Join-Path $OutputDir "raw_commit_items_retry") -Recurse -Force
}

$correctedIndex = Join-Path $OutputDir "raw_commit_items_index.csv"
Copy-Item -LiteralPath (Join-Path $source "raw_commit_items_index.csv") -Destination $correctedIndex -Force
if (Test-Path -LiteralPath $retryRawIndex) {
    $retryIndexRows = foreach ($row in Import-Csv -LiteralPath $retryRawIndex) {
        $row.raw_item_path = $row.raw_item_path -replace "^raw_commit_items/", "raw_commit_items_retry/"
        $row
    }
    Export-AppendCsv -Rows @($retryIndexRows) -Path $correctedIndex
}

Write-Summaries -OutputDir $OutputDir -RawPath $correctedRawPath -SegmentPath $correctedSegmentsPath

@(
    [pscustomobject]@{ key = "source_dir"; value = $source },
    [pscustomobject]@{ key = "retry_dir"; value = $retry },
    [pscustomobject]@{ key = "corrected_dir"; value = (Resolve-Path -LiteralPath $OutputDir).Path },
    [pscustomobject]@{ key = "removed_incomplete_segments"; value = $incompleteKeys.Count },
    [pscustomobject]@{ key = "created_utc"; value = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") }
) | Export-Csv -LiteralPath (Join-Path $OutputDir "corrected_v1_manifest.csv") -NoTypeInformation -Encoding UTF8

Write-Output "corrected_dir=$OutputDir"
Write-Output "removed_incomplete_segments=$($incompleteKeys.Count)"
Write-Output "wrote=$(Join-Path $OutputDir 'query_segments.csv')"
Write-Output "wrote=$(Join-Path $OutputDir 'sha_channels_raw.csv')"
Write-Output "wrote=$(Join-Path $OutputDir 'unique_commits.csv')"
Write-Output "wrote=$(Join-Path $OutputDir 'corrected_v1_manifest.csv')"
