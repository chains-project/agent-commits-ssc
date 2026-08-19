<#
[IN]: a collection output directory containing sha_channels_raw.csv and query_segments.csv.
[OUT]: regenerated channel_counts.csv, agent_counts.csv, filter_counts.csv, unique_commits.csv, and pairwise_overlap.csv in that directory.
[POS]: Fast summary rebuild utility for existing annual/corrected collection directories.
[SYNC]: If summary definitions or filenames change, update scripts/CLAUDE.md and scripts/OUTPUTS.md.
#>param(
    [string]$OutputDir = ".\scripts\pilot\annual_sample_2026-06-10T11-06-53Z_corrected_v1"
)

$ErrorActionPreference = "Stop"

function New-Set {
    return ,[System.Collections.Generic.HashSet[string]]::new()
}

function Ensure-Set {
    param([hashtable]$Table, [string]$Key)
    if (-not $Table.ContainsKey($Key)) { $Table[$Key] = New-Set }
    return ,$Table[$Key]
}

function Inc {
    param([hashtable]$Table, [string]$Key, [int64]$N = 1)
    if (-not $Table.ContainsKey($Key)) { $Table[$Key] = [int64]0 }
    $Table[$Key] += $N
}

$rawPath = Join-Path $OutputDir "sha_channels_raw.csv"
$segmentPath = Join-Path $OutputDir "query_segments.csv"
if (-not (Test-Path -LiteralPath $rawPath)) { throw "Missing $rawPath" }
if (-not (Test-Path -LiteralPath $segmentPath)) { throw "Missing $segmentPath" }

$channelSets = @{}
$channelRawCounts = @{}
$agentSets = @{}
$agentRawCounts = @{}
$tierSets = @{}
$tierRawCounts = @{}
$uniqueRows = @{}
$rawCount = [int64]0

Write-Output "scan_raw=$rawPath"
Import-Csv -LiteralPath $rawPath | ForEach-Object {
    $rawCount++
    $channelKey = "$($_.tier)|$($_.agent)|$($_.channel)|$($_.mode)"
    [void](Ensure-Set $channelSets $channelKey).Add($_.repo_sha)
    Inc $channelRawCounts $channelKey

    [void](Ensure-Set $agentSets $_.agent).Add($_.repo_sha)
    Inc $agentRawCounts $_.agent

    [void](Ensure-Set $tierSets $_.tier).Add($_.repo_sha)
    Inc $tierRawCounts $_.tier

    if (-not $uniqueRows.ContainsKey($_.repo_sha)) {
        $uniqueRows[$_.repo_sha] = [pscustomobject]@{
            agent = $_.agent
            tiers = (New-Set)
            repo = $_.repo
            sha = $_.sha
            repo_sha = $_.repo_sha
            channels = (New-Set)
            modes = (New-Set)
            author_name = $_.author_name
            author_email = $_.author_email
            author_date = $_.author_date
            html_url = $_.html_url
            message_first_line = $_.message_first_line
        }
    }
    [void]$uniqueRows[$_.repo_sha].tiers.Add($_.tier)
    [void]$uniqueRows[$_.repo_sha].channels.Add($_.channel)
    [void]$uniqueRows[$_.repo_sha].modes.Add($_.mode)
}

$segmentStats = @{}
Write-Output "scan_segments=$segmentPath"
Import-Csv -LiteralPath $segmentPath | ForEach-Object {
    $key = "$($_.tier)|$($_.agent)|$($_.channel)|$($_.mode)"
    if (-not $segmentStats.ContainsKey($key)) {
        $segmentStats[$key] = [pscustomobject]@{
            segment_rows = 0
            total_count_sum = [int64]0
            fetched_unique_sha_sum = [int64]0
            split_probe_segments = 0
            incomplete_segments = 0
            cap_bound_segments = 0
            error_segments = 0
        }
    }
    $s = $segmentStats[$key]
    $s.segment_rows++
    $s.total_count_sum += [int64]$_.total_count
    $s.fetched_unique_sha_sum += [int64]$_.fetched_unique_sha
    if ($_.status -eq "split_probe") { $s.split_probe_segments++ }
    if ($_.incomplete_results -eq "True") { $s.incomplete_segments++ }
    if ($_.cap_bound -eq "True") { $s.cap_bound_segments++ }
    if ($_.status -like "error*") { $s.error_segments++ }
}

$channelRows = foreach ($key in ($channelSets.Keys | Sort-Object)) {
    $parts = $key -split "\|"
    $s = $segmentStats[$key]
    if (-not $s) {
        $s = [pscustomobject]@{
            segment_rows = 0; total_count_sum = 0; fetched_unique_sha_sum = 0
            split_probe_segments = 0; incomplete_segments = 0; cap_bound_segments = 0; error_segments = 0
        }
    }
    [pscustomobject]@{
        tier = $parts[0]
        agent = $parts[1]
        channel = $parts[2]
        mode = $parts[3]
        raw_rows_before_dedup = $channelRawCounts[$key]
        unique_repo_sha_after_dedup = $channelSets[$key].Count
        segment_rows = $s.segment_rows
        total_count_sum = $s.total_count_sum
        fetched_unique_sha_sum = $s.fetched_unique_sha_sum
        split_probe_segments = $s.split_probe_segments
        incomplete_segments = $s.incomplete_segments
        cap_bound_segments = $s.cap_bound_segments
        error_segments = $s.error_segments
    }
}
$channelRows | Export-Csv -LiteralPath (Join-Path $OutputDir "channel_counts.csv") -NoTypeInformation -Encoding UTF8

$agentRows = foreach ($agent in ($agentSets.Keys | Sort-Object)) {
    $sumChannelUnique = [int64]0
    foreach ($key in $channelSets.Keys) {
        $parts = $key -split "\|"
        if ($parts[1] -eq $agent) { $sumChannelUnique += $channelSets[$key].Count }
    }
    [pscustomobject]@{
        agent = $agent
        raw_rows_before_dedup = $agentRawCounts[$agent]
        sum_channel_unique_before_cross_channel_dedup = $sumChannelUnique
        unique_repo_sha_after_dedup = $agentSets[$agent].Count
        cross_channel_dedup_ratio = if ($sumChannelUnique -gt 0) { [math]::Round($agentSets[$agent].Count / $sumChannelUnique, 6) } else { 0 }
    }
}
$agentRows | Export-Csv -LiteralPath (Join-Path $OutputDir "agent_counts.csv") -NoTypeInformation -Encoding UTF8

$filterRows = New-Object System.Collections.Generic.List[object]
$filterRows.Add([pscustomobject]@{ scope = "all"; raw_rows_before_dedup = $rawCount; unique_repo_sha_after_dedup = $uniqueRows.Count }) | Out-Null
foreach ($agent in ($agentSets.Keys | Sort-Object)) {
    $filterRows.Add([pscustomobject]@{ scope = "agent:$agent"; raw_rows_before_dedup = $agentRawCounts[$agent]; unique_repo_sha_after_dedup = $agentSets[$agent].Count }) | Out-Null
}
foreach ($tier in ($tierSets.Keys | Sort-Object)) {
    $filterRows.Add([pscustomobject]@{ scope = "tier:$tier"; raw_rows_before_dedup = $tierRawCounts[$tier]; unique_repo_sha_after_dedup = $tierSets[$tier].Count }) | Out-Null
}
$filterRows | Export-Csv -LiteralPath (Join-Path $OutputDir "filter_counts.csv") -NoTypeInformation -Encoding UTF8

Write-Output "write_unique_commits"
$uniqueOut = foreach ($key in ($uniqueRows.Keys | Sort-Object)) {
    $row = $uniqueRows[$key]
    [pscustomobject]@{
        agent = $row.agent
        tiers = ((@($row.tiers) | Sort-Object) -join ";")
        repo = $row.repo
        sha = $row.sha
        repo_sha = $row.repo_sha
        channels = ((@($row.channels) | Sort-Object) -join ";")
        modes = ((@($row.modes) | Sort-Object) -join ";")
        author_name = $row.author_name
        author_email = $row.author_email
        author_date = $row.author_date
        html_url = $row.html_url
        message_first_line = $row.message_first_line
    }
}
$uniqueOut | Export-Csv -LiteralPath (Join-Path $OutputDir "unique_commits.csv") -NoTypeInformation -Encoding UTF8

Write-Output "write_pairwise_overlap"
$pairRows = New-Object System.Collections.Generic.List[object]
foreach ($agent in ($agentSets.Keys | Sort-Object)) {
    $keys = @($channelSets.Keys | Where-Object { ($_ -split "\|")[1] -eq $agent } | Sort-Object)
    for ($i = 0; $i -lt $keys.Count; $i++) {
        for ($j = $i + 1; $j -lt $keys.Count; $j++) {
            $left = $keys[$i]
            $right = $keys[$j]
            $leftSet = $channelSets[$left]
            $rightSet = $channelSets[$right]
            $small = $leftSet
            $large = $rightSet
            if ($rightSet.Count -lt $leftSet.Count) {
                $small = $rightSet
                $large = $leftSet
            }
            $intersection = 0
            foreach ($sha in $small) {
                if ($large.Contains($sha)) { $intersection++ }
            }
            $union = $leftSet.Count + $rightSet.Count - $intersection
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
                jaccard = if ($union -gt 0) { [math]::Round($intersection / $union, 6) } else { 0 }
            }) | Out-Null
        }
    }
}
$pairRows | Export-Csv -LiteralPath (Join-Path $OutputDir "pairwise_overlap.csv") -NoTypeInformation -Encoding UTF8

Write-Output "summary_complete=$OutputDir"
