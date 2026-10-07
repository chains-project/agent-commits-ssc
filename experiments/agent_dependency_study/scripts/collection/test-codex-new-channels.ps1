<#
[IN]: GITHUB_TOKEN, Codex candidate channel queries, UTC range, and PR fetch limits.
[OUT]: scripts/pilot/codex_new_channel_test_<timestamp>/ with commit/pr candidates, raw JSON, channel summaries, and final_summary.csv.
[POS]: Focused audit for validating new Codex identity channels before they enter main collectors.
[SYNC]: If candidate channels or validation output names change, update scripts/CLAUDE.md and scripts/OUTPUTS.md.
#>param(
    [string]$SinceUtc = "2026-05-01T00:00:00Z",
    [string]$UntilUtc = "2026-06-01T00:00:00Z",
    [int]$PerPage = 100,
    [int]$MaxSearchPages = 2,
    [int]$MaxPrsToFetch = 30,
    [int]$SleepSeconds = 3,
    [int]$MaxRetries = 4,
    [int]$RetryBaseSeconds = 15,
    [string]$OutputDir = ""
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
    if (Test-Path -LiteralPath $Path) {
        $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Append -Encoding UTF8
    } else {
        $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Encoding UTF8
    }
}

function Save-Json {
    param(
        [object]$Data,
        [string]$Path
    )
    $dir = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $Data | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Invoke-GitHubWithRetry {
    param(
        [hashtable]$Headers,
        [string]$Uri
    )
    for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
        try {
            $data = Invoke-RestMethod -Uri $Uri -Headers $Headers -Method Get -TimeoutSec 60
            return [pscustomobject]@{ Data = $data; Error = ""; Attempts = $attempt }
        } catch {
            $msg = $_.Exception.Message
            if ($attempt -ge $MaxRetries) {
                return [pscustomobject]@{ Data = $null; Error = $msg; Attempts = $attempt }
            }
            $delay = $RetryBaseSeconds * $attempt
            Write-Warning "request failed attempt=$attempt; sleeping ${delay}s; $msg"
            Start-Sleep -Seconds $delay
        }
    }
}

function New-CommitChannel {
    param([string]$Name, [string]$Query, [string]$Reason)
    [pscustomobject]@{
        name = $Name
        query = $Query
        reason = $Reason
    }
}

function New-PrChannel {
    param([string]$Name, [string]$Query, [string]$Reason)
    [pscustomobject]@{
        name = $Name
        query = $Query
        reason = $Reason
    }
}

function Get-RepoAndNumberFromPullUrl {
    param([string]$HtmlUrl)
    if ($HtmlUrl -match "^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)") {
        return [pscustomobject]@{
            owner = $Matches[1]
            repo_name = $Matches[2]
            repo = "$($Matches[1])/$($Matches[2])"
            number = [int]$Matches[3]
        }
    }
    return $null
}

$since = Parse-Utc $SinceUtc
$until = Parse-Utc $UntilUtc
if ($since -ge $until) { throw "SinceUtc must be earlier than UntilUtc." }

if (-not $OutputDir) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH-mm-ssZ")
    $OutputDir = Join-Path (Join-Path (Split-Path -Parent $PSScriptRoot) "pilot") "codex_new_channel_test_$stamp"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$rawDir = Join-Path $OutputDir "raw"
New-Item -ItemType Directory -Force -Path $rawDir | Out-Null

$token = Read-GitHubToken
$headers = @{
    "Accept" = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent" = "codex-new-channel-pilot"
}
$commitHeaders = $headers.Clone()
$commitHeaders["Accept"] = "application/vnd.github.cloak-preview+json"
if ($token) {
    $headers["Authorization"] = "token $token"
    $commitHeaders["Authorization"] = "token $token"
}

$dateSpan = "$(Format-Utc $since)..$(Format-Utc $until.AddSeconds(-1))"
$dateOnlySpan = "$($since.ToString("yyyy-MM-dd"))..$($until.AddDays(-1).ToString("yyyy-MM-dd"))"

$commitChannels = @(
    (New-CommitChannel baseline_coauthored "`"Co-authored-by: Codex`"" "existing high-precision baseline"),
    (New-CommitChannel baseline_email "author-email:codex@openai.com" "existing author email baseline"),
    (New-CommitChannel codex_task_url "`"chatgpt.com/codex/tasks`"" "possible Codex task URL copied into commit message"),
    (New-CommitChannel codex_task_text "`"Codex task`"" "possible task wording in commit message"),
    (New-CommitChannel openai_codex_text "`"OpenAI Codex`"" "possible product name in commit message"),
    (New-CommitChannel generated_by_codex "`"Generated by Codex`"" "possible generated-by marker"),
    (New-CommitChannel created_by_codex "`"Created by Codex`"" "possible created-by marker"),
    (New-CommitChannel openai_code_agent_author "author:openai-code-agent[bot]" "possible GitHub App bot account"),
    (New-CommitChannel openai_code_agent_committer "committer:openai-code-agent[bot]" "possible GitHub App bot committer")
)

$prChannels = @(
    (New-PrChannel pr_body_task_url_created "`"chatgpt.com/codex/tasks`" is:pr created:$dateOnlySpan" "high-confidence Codex task URL in PR created in range"),
    (New-PrChannel pr_body_task_url_updated "`"chatgpt.com/codex/tasks`" is:pr updated:$dateOnlySpan" "high-confidence Codex task URL in PR updated in range"),
    (New-PrChannel pr_comments_at_codex_updated "`"@codex`" is:pr in:comments updated:$dateOnlySpan" "Codex invocation in PR comments"),
    (New-PrChannel pr_body_openai_codex_updated "`"OpenAI Codex`" is:pr updated:$dateOnlySpan" "product name in PR text")
)

$commitSummaryPath = Join-Path $OutputDir "commit_channel_summary.csv"
$commitRowsPath = Join-Path $OutputDir "commit_candidates.csv"
$prSummaryPath = Join-Path $OutputDir "pr_channel_summary.csv"
$prRowsPath = Join-Path $OutputDir "pr_candidates.csv"
$prCommitRowsPath = Join-Path $OutputDir "pr_commits.csv"

Write-Output "output_dir=$OutputDir"
Write-Output "range_utc=$(Format-Utc $since)..$(Format-Utc $until)"
Write-Output "commit_channels=$($commitChannels.Count) pr_channels=$($prChannels.Count) per_page=$PerPage max_search_pages=$MaxSearchPages max_prs_to_fetch=$MaxPrsToFetch"

$seenCommitRepoSha = @{}
foreach ($channel in $commitChannels) {
    $query = "$($channel.query) author-date:$dateSpan"
    $channelFetched = 0
    $channelUnique = @{}
    $totalCount = -1
    $incomplete = $false
    $lastError = ""
    for ($page = 1; $page -le $MaxSearchPages; $page++) {
        $uri = "https://api.github.com/search/commits?q=$([uri]::EscapeDataString($query))&per_page=$PerPage&page=$page&sort=author-date&order=desc"
        $result = Invoke-GitHubWithRetry -Headers $commitHeaders -Uri $uri
        if ($result.Error) {
            $lastError = $result.Error
            break
        }
        if ($page -eq 1) {
            $totalCount = [int]$result.Data.total_count
            $incomplete = [bool]$result.Data.incomplete_results
        }
        Save-Json -Data $result.Data -Path (Join-Path $rawDir "commit_$($channel.name)_page$page.json")
        foreach ($item in @($result.Data.items)) {
            $repo = ""
            if ($item.repository) { $repo = [string]$item.repository.full_name }
            $sha = [string]$item.sha
            $repoSha = "$repo|$sha"
            if (-not $sha) { continue }
            $channelFetched++
            $channelUnique[$repoSha] = $true
            $seenCommitRepoSha[$repoSha] = $true
            $message = [string]$item.commit.message
            $messageFirst = ""
            if ($message) { $messageFirst = ($message -split "`r?`n")[0] }
            Export-AppendCsv -Rows @([pscustomobject]@{
                source = "commit_search"
                channel = $channel.name
                query = $channel.query
                repo = $repo
                sha = $sha
                repo_sha = $repoSha
                author_name = [string]$item.commit.author.name
                author_email = [string]$item.commit.author.email
                author_date = [string]$item.commit.author.date
                committer_email = [string]$item.commit.committer.email
                html_url = [string]$item.html_url
                message_first_line = $messageFirst
            }) -Path $commitRowsPath
        }
        if (@($result.Data.items).Count -lt $PerPage) { break }
        Start-Sleep -Seconds $SleepSeconds
    }
    Export-AppendCsv -Rows @([pscustomobject]@{
        channel = $channel.name
        query = $channel.query
        reason = $channel.reason
        total_count = $totalCount
        incomplete_results = $incomplete
        fetched_rows = $channelFetched
        unique_repo_sha = $channelUnique.Count
        error = $lastError
    }) -Path $commitSummaryPath
    Write-Output "commit_channel=$($channel.name) total=$totalCount fetched=$channelFetched unique=$($channelUnique.Count) error=$lastError"
    Start-Sleep -Seconds $SleepSeconds
}

$seenPr = @{}
$prsToFetch = New-Object System.Collections.Generic.List[object]
foreach ($channel in $prChannels) {
    $channelFetched = 0
    $channelUnique = @{}
    $totalCount = -1
    $incomplete = $false
    $lastError = ""
    for ($page = 1; $page -le $MaxSearchPages; $page++) {
        $uri = "https://api.github.com/search/issues?q=$([uri]::EscapeDataString($channel.query))&per_page=$PerPage&page=$page&sort=updated&order=desc"
        $result = Invoke-GitHubWithRetry -Headers $headers -Uri $uri
        if ($result.Error) {
            $lastError = $result.Error
            break
        }
        if ($page -eq 1) {
            $totalCount = [int]$result.Data.total_count
            $incomplete = [bool]$result.Data.incomplete_results
        }
        Save-Json -Data $result.Data -Path (Join-Path $rawDir "pr_$($channel.name)_page$page.json")
        foreach ($item in @($result.Data.items)) {
            if (-not $item.pull_request) { continue }
            $parsed = Get-RepoAndNumberFromPullUrl -HtmlUrl ([string]$item.html_url)
            if (-not $parsed) { continue }
            $prKey = "$($parsed.repo)#$($parsed.number)"
            $channelFetched++
            $channelUnique[$prKey] = $true
            Export-AppendCsv -Rows @([pscustomobject]@{
                source = "pr_search"
                channel = $channel.name
                query = $channel.query
                repo = $parsed.repo
                pull_number = $parsed.number
                pr_key = $prKey
                title = [string]$item.title
                state = [string]$item.state
                created_at = [string]$item.created_at
                updated_at = [string]$item.updated_at
                html_url = [string]$item.html_url
                body_prefix = (([string]$item.body) -replace "`r?`n", " ").Substring(0, [Math]::Min(220, (([string]$item.body).Length)))
            }) -Path $prRowsPath
            if (-not $seenPr.ContainsKey($prKey) -and $prsToFetch.Count -lt $MaxPrsToFetch) {
                $seenPr[$prKey] = $true
                $prsToFetch.Add([pscustomobject]@{
                    repo = $parsed.repo
                    owner = $parsed.owner
                    repo_name = $parsed.repo_name
                    pull_number = $parsed.number
                    pr_key = $prKey
                    first_channel = $channel.name
                    html_url = [string]$item.html_url
                }) | Out-Null
            }
        }
        if (@($result.Data.items).Count -lt $PerPage) { break }
        Start-Sleep -Seconds $SleepSeconds
    }
    Export-AppendCsv -Rows @([pscustomobject]@{
        channel = $channel.name
        query = $channel.query
        reason = $channel.reason
        total_count = $totalCount
        incomplete_results = $incomplete
        fetched_pr_rows = $channelFetched
        unique_prs = $channelUnique.Count
        error = $lastError
    }) -Path $prSummaryPath
    Write-Output "pr_channel=$($channel.name) total=$totalCount fetched_pr_rows=$channelFetched unique_prs=$($channelUnique.Count) error=$lastError"
    Start-Sleep -Seconds $SleepSeconds
}

$seenPrCommitRepoSha = @{}
foreach ($pr in @($prsToFetch.ToArray())) {
    $uri = "https://api.github.com/repos/$($pr.owner)/$($pr.repo_name)/pulls/$($pr.pull_number)/commits?per_page=100"
    $result = Invoke-GitHubWithRetry -Headers $headers -Uri $uri
    if ($result.Error) {
        Export-AppendCsv -Rows @([pscustomobject]@{
            source = "pr_commits"
            first_channel = $pr.first_channel
            repo = $pr.repo
            pull_number = $pr.pull_number
            pr_key = $pr.pr_key
            sha = ""
            repo_sha = ""
            author_name = ""
            author_email = ""
            author_date = ""
            html_url = $pr.html_url
            message_first_line = ""
            error = $result.Error
        }) -Path $prCommitRowsPath
        continue
    }
    Save-Json -Data $result.Data -Path (Join-Path $rawDir ("pr_commits_" + ($pr.pr_key -replace "[/#]", "_") + ".json"))
    foreach ($commitItem in @($result.Data)) {
        $sha = [string]$commitItem.sha
        if (-not $sha) { continue }
        $repoSha = "$($pr.repo)|$sha"
        $seenPrCommitRepoSha[$repoSha] = $true
        $message = [string]$commitItem.commit.message
        $messageFirst = ""
        if ($message) { $messageFirst = ($message -split "`r?`n")[0] }
        Export-AppendCsv -Rows @([pscustomobject]@{
            source = "pr_commits"
            first_channel = $pr.first_channel
            repo = $pr.repo
            pull_number = $pr.pull_number
            pr_key = $pr.pr_key
            sha = $sha
            repo_sha = $repoSha
            author_name = [string]$commitItem.commit.author.name
            author_email = [string]$commitItem.commit.author.email
            author_date = [string]$commitItem.commit.author.date
            html_url = [string]$commitItem.html_url
            message_first_line = $messageFirst
            error = ""
        }) -Path $prCommitRowsPath
    }
    Start-Sleep -Seconds $SleepSeconds
}

$overlap = 0
foreach ($key in $seenPrCommitRepoSha.Keys) {
    if ($seenCommitRepoSha.ContainsKey($key)) { $overlap++ }
}

$finalSummary = [pscustomobject]@{
    since_utc = Format-Utc $since
    until_utc = Format-Utc $until
    commit_search_unique_repo_sha = $seenCommitRepoSha.Count
    unique_prs_selected_for_commit_fetch = $seenPr.Count
    pr_commit_unique_repo_sha = $seenPrCommitRepoSha.Count
    overlap_repo_sha_between_commit_search_and_pr_commits = $overlap
    output_dir = $OutputDir
}
$finalSummary | Export-Csv -LiteralPath (Join-Path $OutputDir "final_summary.csv") -NoTypeInformation -Encoding UTF8
$finalSummary | Format-List

Write-Output "wrote=$commitSummaryPath"
Write-Output "wrote=$commitRowsPath"
Write-Output "wrote=$prSummaryPath"
Write-Output "wrote=$prRowsPath"
Write-Output "wrote=$prCommitRowsPath"
Write-Output "wrote=$(Join-Path $OutputDir 'final_summary.csv')"
Write-Output "wrote=$rawDir"
