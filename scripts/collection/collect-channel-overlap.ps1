<#
[IN]: GITHUB_TOKEN from scripts/.env or environment, channel definitions in this file, and a short UTC window.
[OUT]: scripts/pilot/overlap_<timestamp>/ with channels.csv, windows.csv, channel_windows.csv, sha_channels.csv, channel_summary.csv, pairwise_overlap.csv.
[POS]: Measures identity-channel overlap and validates candidate channels before annual sampling.
[SYNC]: If channel definitions or output table names change, update scripts/CLAUDE.md and scripts/OUTPUTS.md.
#>param(
    [int]$Hours = 1,
    [int]$WindowMinutes = 5,
    [int]$PerPage = 100,
    [int]$MaxPages = 10,
    [int]$SleepSeconds = 5,
    [ValidateSet("all", "existing", "candidate")]
    [string]$ChannelSet = "all",
    [string[]]$Agents = @("claude", "codex", "copilot", "cursor"),
    [string]$SinceUtc = "",
    [string]$UntilUtc = "",
    [string]$OutputDir = "",
    [switch]$Resume,
    [switch]$ListChannels,
    [int]$MaxWindows = 0
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

function New-Channel {
    param(
        [string]$Kind,
        [string]$Agent,
        [string]$Channel,
        [string]$Mode,
        [string]$Query,
        [string]$Note
    )
    [pscustomobject]@{
        kind = $Kind
        agent = $Agent
        channel = $Channel
        mode = $Mode
        query = $Query
        note = $Note
    }
}

function Get-Channels {
    $channels = @(
        (New-Channel existing claude cli_email author "author-email:noreply@anthropic.com" "current CHANNELS"),
        (New-Channel existing claude app_bot_email author "author-email:242468646+Claude@users.noreply.github.com" "current CHANNELS"),
        (New-Channel existing claude app_bot_slug author "author:anthropic-code-agent[bot]" "current CHANNELS"),
        (New-Channel existing claude cli_user author "author:Claude" "current CHANNELS; broad username risk"),
        (New-Channel existing claude msg_cli_email coauthor "`"noreply@anthropic.com`"" "current CHANNELS"),
        (New-Channel candidate claude author_claude_email author "author-email:claude@anthropic.com" "candidate direct author email"),
        (New-Channel candidate claude author_claude_code_email author "author-email:claude-code@anthropic.com" "candidate direct author email"),
        (New-Channel candidate claude msg_coauthored_by_claude coauthor "`"Co-authored-by: Claude`"" "candidate trailer; likely overlaps msg_cli_email"),
        (New-Channel candidate claude msg_generated_with_claude_code marker "`"Generated with Claude Code`"" "candidate marker; likely overlaps msg_cli_email"),

        (New-Channel existing codex cli_email author "author-email:codex@openai.com" "current CHANNELS"),
        (New-Channel existing codex new_bot_email author "author-email:242516109+Codex@users.noreply.github.com" "current CHANNELS"),
        (New-Channel existing codex legacy_connector author "author:chatgpt-codex-connector[bot]" "current CHANNELS"),
        (New-Channel existing codex user_login author "author:codex" "current CHANNELS; broad username risk"),
        (New-Channel existing codex msg_noreply coauthor "`"noreply@openai.com`"" "current CHANNELS"),
        (New-Channel existing codex msg_coauthored coauthor "`"Co-authored-by: Codex`"" "current CHANNELS"),
        (New-Channel candidate codex msg_codex_openai_email coauthor "`"codex@openai.com`"" "candidate trailer/body email"),
        (New-Channel candidate codex msg_openai_codex coauthor "`"Co-authored-by: OpenAI Codex`"" "candidate specific trailer"),
        (New-Channel candidate codex author_openai_noreply author "author-email:noreply@openai.com" "candidate low-volume direct author"),

        (New-Channel existing copilot swe_agent_bot author "author-email:198982749+Copilot@users.noreply.github.com" "current CHANNELS"),
        (New-Channel existing copilot msg_noreply coauthor "`"copilot@users.noreply.github.com`"" "current CHANNELS"),
        (New-Channel existing copilot msg_bot_slug coauthor "`"copilot-swe-agent[bot]`"" "current CHANNELS"),
        (New-Channel candidate copilot author_slug author "author:copilot-swe-agent[bot]" "alias/check for swe_agent_bot"),
        (New-Channel candidate copilot msg_coauthored_by_copilot coauthor "`"Co-authored-by: Copilot`"" "candidate broader trailer"),
        (New-Channel candidate copilot msg_github_copilot coauthor "`"Co-authored-by: GitHub Copilot`"" "candidate specific trailer"),
        (New-Channel candidate copilot author_copilot_noreply author "author-email:copilot@users.noreply.github.com" "candidate low-volume direct author"),

        (New-Channel existing cursor cursoragent_email author "author-email:cursoragent@cursor.com" "current CHANNELS"),
        (New-Channel existing cursor bg_agent_email author "author-email:agent@cursor.com" "current CHANNELS"),
        (New-Channel existing cursor bot_noreply author "author-email:206951365+cursor[bot]@users.noreply.github.com" "current CHANNELS"),
        (New-Channel existing cursor msg_cursoragent coauthor "`"cursoragent@cursor.com`"" "current CHANNELS"),
        (New-Channel candidate cursor author_slug author "author:cursor[bot]" "alias/check for bot_noreply"),
        (New-Channel candidate cursor msg_coauthored_by_cursor coauthor "`"Co-authored-by: Cursor`"" "candidate broad trailer"),
        (New-Channel candidate cursor author_cursor_at_cursor author "author-email:cursor@cursor.com" "candidate very low volume"),
        (New-Channel candidate cursor msg_agent_cursor_email coauthor "`"agent@cursor.com`"" "candidate often zero")
    )

    $agentSet = @{}
    foreach ($agent in $Agents) {
        $agentSet[$agent.ToLowerInvariant()] = $true
    }

    $channels = $channels | Where-Object {
        $agentSet.ContainsKey($_.agent) -and ($ChannelSet -eq "all" -or $_.kind -eq $ChannelSet)
    }
    return @($channels)
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
    if (-not $Rows -or $Rows.Count -eq 0) {
        return
    }
    if (Test-Path -LiteralPath $Path) {
        $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Append -Encoding UTF8
    } else {
        $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Encoding UTF8
    }
}

function Invoke-CommitSearchPage {
    param(
        [hashtable]$Headers,
        [string]$Query,
        [int]$Page
    )
    $encoded = [uri]::EscapeDataString($Query)
    $uri = "https://api.github.com/search/commits?q=$encoded&per_page=$PerPage&page=$Page&sort=author-date&order=desc"
    return Invoke-RestMethod -Uri $uri -Headers $Headers -Method Get -TimeoutSec 45
}

function Fetch-WindowChannel {
    param(
        [hashtable]$Headers,
        [object]$Channel,
        [datetime]$Start,
        [datetime]$End
    )

    $startText = Format-Utc $Start
    $endText = Format-Utc $End
    $query = "$($Channel.query) author-date:$startText..$endText"

    $rows = New-Object System.Collections.Generic.List[object]
    $seen = @{}
    $total = 0
    $incomplete = $false
    $errorMessage = ""

    for ($page = 1; $page -le $MaxPages; $page++) {
        try {
            $data = Invoke-CommitSearchPage -Headers $Headers -Query $query -Page $page
        } catch {
            $errorMessage = $_.Exception.Message
            break
        }

        if ($page -eq 1) {
            $total = [int]$data.total_count
            $incomplete = [bool]$data.incomplete_results
        }

        $items = @($data.items)
        foreach ($item in $items) {
            $sha = [string]$item.sha
            if (-not $sha -or $seen.ContainsKey($sha)) {
                continue
            }
            $seen[$sha] = $true
            $commit = $item.commit
            $author = $commit.author
            $committer = $commit.committer
            $repo = ""
            if ($item.repository) {
                $repo = [string]$item.repository.full_name
            }
            $message = [string]$commit.message
            $messageFirst = ""
            if ($message) {
                $messageFirst = ($message -split "`r?`n")[0]
            }
            $rows.Add([pscustomobject]@{
                kind = $Channel.kind
                agent = $Channel.agent
                channel = $Channel.channel
                mode = $Channel.mode
                window_start_utc = $startText
                window_end_utc = $endText
                sha = $sha
                repo = $repo
                author_name = [string]$author.name
                author_email = [string]$author.email
                author_date = [string]$author.date
                committer_email = [string]$committer.email
                html_url = [string]$item.html_url
                message_first_line = $messageFirst
            })
        }

        if ($items.Count -lt $PerPage) {
            break
        }
        Start-Sleep -Seconds $SleepSeconds
    }

    $capBound = ($total -gt $rows.Count) -and ($rows.Count -ge ($PerPage * $MaxPages))
    $status = "ok"
    if ($errorMessage) {
        $status = "error"
    } elseif ($capBound) {
        $status = "cap_bound"
    }

    $windowRow = [pscustomobject]@{
        kind = $Channel.kind
        agent = $Channel.agent
        channel = $Channel.channel
        mode = $Channel.mode
        query = $Channel.query
        window_start_utc = $startText
        window_end_utc = $endText
        total_count = $total
        fetched_unique_sha = $rows.Count
        incomplete_results = $incomplete
        cap_bound = $capBound
        status = $status
        error = $errorMessage
    }

    $shaRows = @($rows.ToArray())
    return [pscustomobject]@{
        WindowRow = $windowRow
        ShaRows = $shaRows
    }
}

function New-Windows {
    param(
        [datetime]$Start,
        [datetime]$End
    )
    $windows = New-Object System.Collections.Generic.List[object]
    $cursor = $Start
    while ($cursor -lt $End) {
        $next = $cursor.AddMinutes($WindowMinutes)
        if ($next -gt $End) {
            $next = $End
        }
        $windows.Add([pscustomobject]@{
            window_start_utc = Format-Utc $cursor
            window_end_utc = Format-Utc $next
        })
        $cursor = $next
    }
    if ($MaxWindows -gt 0 -and $windows.Count -gt $MaxWindows) {
        return @($windows.ToArray() | Select-Object -First $MaxWindows)
    }
    return @($windows.ToArray())
}

function Compute-Overlap {
    param(
        [string]$ShaPath,
        [string]$WindowPath,
        [string]$SummaryPath,
        [string]$PairwisePath
    )

    if (-not (Test-Path -LiteralPath $ShaPath)) {
        throw "No SHA rows were collected: $ShaPath"
    }

    $shaRows = Import-Csv -LiteralPath $ShaPath
    $windowRows = @()
    if (Test-Path -LiteralPath $WindowPath) {
        $windowRows = Import-Csv -LiteralPath $WindowPath
    }

    $channelSets = @{}
    foreach ($row in $shaRows) {
        $key = "$($row.kind)|$($row.agent)|$($row.channel)|$($row.mode)"
        if (-not $channelSets.ContainsKey($key)) {
            $channelSets[$key] = @{}
        }
        $channelSets[$key][$row.sha] = $true
    }

    $summaryRows = foreach ($key in ($channelSets.Keys | Sort-Object)) {
        $parts = $key -split "\|", 4
        $matchingWindows = @($windowRows | Where-Object {
            $_.kind -eq $parts[0] -and $_.agent -eq $parts[1] -and $_.channel -eq $parts[2]
        })
        [pscustomobject]@{
            kind = $parts[0]
            agent = $parts[1]
            channel = $parts[2]
            mode = $parts[3]
            unique_sha = $channelSets[$key].Count
            windows = $matchingWindows.Count
            total_count_sum = ($matchingWindows | Measure-Object total_count -Sum).Sum
            fetched_unique_sha_sum = ($matchingWindows | Measure-Object fetched_unique_sha -Sum).Sum
            cap_bound_windows = @($matchingWindows | Where-Object { $_.cap_bound -eq "True" }).Count
            error_windows = @($matchingWindows | Where-Object { $_.status -eq "error" }).Count
        }
    }
    $summaryRows | Export-Csv -LiteralPath $SummaryPath -NoTypeInformation -Encoding UTF8

    $keys = @($channelSets.Keys | Sort-Object)
    $pairs = New-Object System.Collections.Generic.List[object]
    for ($i = 0; $i -lt $keys.Count; $i++) {
        for ($j = $i + 1; $j -lt $keys.Count; $j++) {
            $leftKey = $keys[$i]
            $rightKey = $keys[$j]
            $leftSet = $channelSets[$leftKey]
            $rightSet = $channelSets[$rightKey]
            $leftParts = $leftKey -split "\|", 4
            $rightParts = $rightKey -split "\|", 4

            $intersection = 0
            foreach ($sha in $leftSet.Keys) {
                if ($rightSet.ContainsKey($sha)) {
                    $intersection++
                }
            }
            $union = $leftSet.Count + $rightSet.Count - $intersection
            $jaccard = 0.0
            if ($union -gt 0) {
                $jaccard = [math]::Round($intersection / $union, 6)
            }
            $leftPct = 0.0
            if ($leftSet.Count -gt 0) {
                $leftPct = [math]::Round($intersection / $leftSet.Count, 6)
            }
            $rightPct = 0.0
            if ($rightSet.Count -gt 0) {
                $rightPct = [math]::Round($intersection / $rightSet.Count, 6)
            }

            $pairs.Add([pscustomobject]@{
                left_kind = $leftParts[0]
                left_agent = $leftParts[1]
                left_channel = $leftParts[2]
                left_mode = $leftParts[3]
                right_kind = $rightParts[0]
                right_agent = $rightParts[1]
                right_channel = $rightParts[2]
                right_mode = $rightParts[3]
                same_agent = ($leftParts[1] -eq $rightParts[1])
                left_unique_sha = $leftSet.Count
                right_unique_sha = $rightSet.Count
                intersection_sha = $intersection
                union_sha = $union
                jaccard = $jaccard
                overlap_left_pct = $leftPct
                overlap_right_pct = $rightPct
            })
        }
    }
    $pairs | Export-Csv -LiteralPath $PairwisePath -NoTypeInformation -Encoding UTF8
}

$channels = Get-Channels
if ($ListChannels) {
    $channels | Format-Table kind, agent, channel, mode, query -AutoSize
    exit 0
}

if ($channels.Count -eq 0) {
    throw "No channels selected. Check -Agents and -ChannelSet."
}

$until = if ($UntilUtc) { Parse-Utc $UntilUtc } else { (Get-Date).ToUniversalTime() }
$since = if ($SinceUtc) { Parse-Utc $SinceUtc } else { $until.AddHours(-1 * $Hours) }
if ($since -ge $until) {
    throw "SinceUtc must be earlier than UntilUtc."
}

if (-not $OutputDir) {
    $stamp = $until.ToString("yyyy-MM-ddTHH-mm-ssZ")
    $OutputDir = Join-Path (Join-Path (Split-Path -Parent $PSScriptRoot) "pilot") "overlap_$stamp"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$token = Read-GitHubToken
$headers = @{
    "Accept" = "application/vnd.github.cloak-preview+json"
    "User-Agent" = "agent-channel-overlap"
}
if ($token) {
    $headers["Authorization"] = "token $token"
}

$windows = @(New-Windows -Start $since -End $until)
$channelsPath = Join-Path $OutputDir "channels.csv"
$windowsPath = Join-Path $OutputDir "windows.csv"
$windowResultsPath = Join-Path $OutputDir "channel_windows.csv"
$shaPath = Join-Path $OutputDir "sha_channels.csv"
$summaryPath = Join-Path $OutputDir "channel_summary.csv"
$pairwisePath = Join-Path $OutputDir "pairwise_overlap.csv"

$channels | Export-Csv -LiteralPath $channelsPath -NoTypeInformation -Encoding UTF8
$windows | Export-Csv -LiteralPath $windowsPath -NoTypeInformation -Encoding UTF8

$completed = @{}
if ($Resume -and (Test-Path -LiteralPath $windowResultsPath)) {
    foreach ($row in Import-Csv -LiteralPath $windowResultsPath) {
        if ($row.status -ne "error") {
            $completed["$($row.kind)|$($row.agent)|$($row.channel)|$($row.window_start_utc)|$($row.window_end_utc)"] = $true
        }
    }
}

Write-Output "output_dir=$OutputDir"
Write-Output "window_utc=$(Format-Utc $since)..$(Format-Utc $until)"
Write-Output "windows=$($windows.Count) window_minutes=$WindowMinutes channels=$($channels.Count)"
Write-Output "max_pages=$MaxPages per_page=$PerPage sleep_seconds=$SleepSeconds"

foreach ($window in $windows) {
    $start = Parse-Utc $window.window_start_utc
    $end = Parse-Utc $window.window_end_utc
    foreach ($channel in $channels) {
        $key = "$($channel.kind)|$($channel.agent)|$($channel.channel)|$($window.window_start_utc)|$($window.window_end_utc)"
        if ($completed.ContainsKey($key)) {
            continue
        }
        Write-Output "fetch $($window.window_start_utc)..$($window.window_end_utc) $($channel.agent)/$($channel.channel)"
        $result = Fetch-WindowChannel -Headers $headers -Channel $channel -Start $start -End $end
        Export-AppendCsv -Rows @($result.WindowRow) -Path $windowResultsPath
        Export-AppendCsv -Rows @($result.ShaRows) -Path $shaPath
        if ($result.WindowRow.status -eq "error") {
            Write-Warning "error $($channel.agent)/$($channel.channel): $($result.WindowRow.error)"
        } elseif ($result.WindowRow.cap_bound -eq $true) {
            Write-Warning "cap_bound $($channel.agent)/$($channel.channel): total=$($result.WindowRow.total_count) fetched=$($result.WindowRow.fetched_unique_sha)"
        }
        Start-Sleep -Seconds $SleepSeconds
    }
}

Compute-Overlap -ShaPath $shaPath -WindowPath $windowResultsPath -SummaryPath $summaryPath -PairwisePath $pairwisePath

Write-Output "wrote=$channelsPath"
Write-Output "wrote=$windowsPath"
Write-Output "wrote=$windowResultsPath"
Write-Output "wrote=$shaPath"
Write-Output "wrote=$summaryPath"
Write-Output "wrote=$pairwisePath"
