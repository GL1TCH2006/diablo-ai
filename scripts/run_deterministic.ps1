<#
.SYNOPSIS
    Run the deterministic test suite in bounded chunks and report one number.

.DESCRIPTION
    The full suite does not fit in one pytest invocation on this machine: the
    slowest files (test_recovery_p10_repair.py alone is ~100-120 s) would blow any
    single timeout, and one giant invocation gives no information about where time
    or failures went.

    This script therefore:

      1. runs `pytest --collect-only -q` first and prints the collected count, so
         the denominator is known before anything runs;
      2. splits the deterministic files into chunks of about 15 and runs each with
         a HARD 120 second timeout;
      3. bisects automatically when a chunk times out, down to single files and
         then to individual test ids, so no invocation can hang the run;
      4. prints per-chunk and total passed / failed / skipped;
      5. reports any orphan python.exe left behind by a chunk, and kills it;
      6. exits nonzero if anything failed, if any chunk timed out unrecoverably, or
         if the accounted result does not match the collected count.

    Why output goes to files, not pipes (Task R, Batch 5)
    ------------------------------------------------------
    The previous version read pytest output through RedirectStandardOutput pipes
    and then called `.Result` on a ReadToEndAsync task. Two defects combined to
    hang a whole batch:

      * A leaked grandchild inherits the stdout handle. When the real process
        exits, WaitForExit() returns true, but the pipe never reaches EOF because
        the grandchild still holds the write end. `.Result` then blocks forever,
        so the 120 s guard - which had already succeeded - could never be reached.
      * On timeout the old code called `$proc.Kill($true)`. That overload only
        exists on .NET Core; on Windows PowerShell 5.1 it throws, and the
        surrounding `catch { }` swallowed the error, so nothing was killed at all.

    Both are fixed here:
      * stdout/stderr are redirected to temp FILES by Start-Process, so a
        surviving grandchild cannot block the read, and
      * a timeout is enforced with `taskkill /T /F` on the chunk's root PID, which
        kills the whole tree on every supported Windows version.

    Safety properties, deliberately enforced:

      * Uses .venv\Scripts\python.exe only. `python` on PATH on this host is the
        Microsoft Store Application Execution Alias stub, which is why two
        terminal tests fail for environmental reasons.
      * NEVER sets any DIABLO_REAL_* environment variable. The real-model, real-
        Windows and real-Blender suites stay opt-in and stay skipped. If one is
        already set, this script refuses to run at all.
      * Excludes *_real*.py, which also catches *_real_model.py. Two real-model
        suites are NOT named *_real.py, so the name filter alone is not sufficient;
        the glob is used for that reason.
      * Writes nothing inside the repository except this script. Temporary files go
        to the system temp directory.

.PARAMETER ChunkSize
    Files per pytest invocation. Default 15.

.PARAMETER TimeoutSeconds
    Hard per-invocation wall-clock limit. Default 120.

.PARAMETER SmokeTest
    Restrict the run to two fast files, exercising every stage in seconds.

.PARAMETER HangSelfTest
    Prove the timeout actually works: run a synthetic parent that spawns a
    grandchild holding stdout, require the bounded runner to return promptly, kill
    the tree, and assert no orphan survives. Exits without running the suite.

.EXAMPLE
    .\scripts\run_deterministic.ps1

.EXAMPLE
    .\scripts\run_deterministic.ps1 -HangSelfTest

.EXAMPLE
    .\scripts\run_deterministic.ps1 -ChunkSize 10 -TimeoutSeconds 180
#>
[CmdletBinding()]
param(
    [int]    $ChunkSize      = 15,
    [int]    $TimeoutSeconds = 120,
    [switch] $SmokeTest,
    [switch] $HangSelfTest
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$here      = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot  = Split-Path -Parent $here
$testsDir  = Join-Path $repoRoot 'tests'
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
$excludeGl = '*_real*.py'

# Explicit re-inclusions: files whose name matches the opt-in glob but which need
# no hardware, no model and no desktop session, and are therefore part of the
# deterministic suite.
#
# test_m217_restart_real.py is the only one. Despite the suffix it spawns plain
# `sys.executable` interpreters and asserts on files on disk. It was excluded by
# the glob for its whole life, which meant Batch 1's cross-restart fix - the one
# that turned 6 hard failures into 6 passes - was never exercised by the default
# suite. Batch 2 and Batch 3 both reported "the cross-restart tests are fine"
# while never running them.
$includeAnyway = @('test_m217_restart_real.py')

# ── refuse to run with any opt-in gate already set ───────────────────────────
# The gate variables are opt-in for a human to set deliberately. If one is already
# in the environment this script must not be the thing that silently starts a real
# model run, so it stops and says so rather than inheriting an accident.
$tripped = @()
foreach ($name in @('DIABLO_REAL_MODEL_TESTS', 'DIABLO_REAL_WINDOWS_TESTS',
                    'DIABLO_REAL_BLENDER_TESTS')) {
    if (Test-Path "Env:$name") { $tripped += ($name + '=' + [Environment]::GetEnvironmentVariable($name)) }
}
if ($tripped.Count -gt 0) {
    # L1: this used to be Write-Error + `exit 2`. With
    # $ErrorActionPreference='Stop' the Write-Error terminated the script before
    # `exit 2` was reached, so the documented exit code 2 was unreachable and the
    # process actually exited 1. The refusal itself worked; only the contract was
    # wrong. Write-Warning does not throw, so exit 2 is now genuinely reachable.
    Write-Warning ("Refusing to run: an opt-in gate is already set in this " +
                   "environment ($($tripped -join ', ')). This runner is for the " +
                   "deterministic suite only. Clear the variable, or run the " +
                   "gated suite directly.")
    exit 2
}

if (-not (Test-Path -LiteralPath $pythonExe)) {
    Write-Warning "Project interpreter not found: $pythonExe"
    Write-Warning "Create it with: python -m venv .venv"
    exit 2
}

# Every process this script starts is recorded here, so a post-mortem can tell an
# orphan of ours from pre-existing infrastructure (ollama, opencode, rea-agents).
$script:startedRoots  = New-Object System.Collections.ArrayList
$script:orphanEvents  = New-Object System.Collections.ArrayList
# L3: "detected" is not "killed". Enumeration and taskkill race, so a candidate
# can exit in between. Three separate counters, printed separately.
$script:orphansKilled        = 0
$script:orphansAlreadyGone   = 0
$script:orphansSignallingFails = 0

function Get-RepoPython {
    <# All live python.exe processes whose command line points inside this repo. #>
    $out = @()
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop
    } catch {
        return $out
    }
    foreach ($p in $procs) {
        $cl = ''
        try { $cl = [string]$p.CommandLine } catch { }
        if ($cl -and $cl.ToLower().Contains($repoRoot.ToLower())) {
            $out += [pscustomobject]@{
                Pid         = [int]$p.ProcessId
                Parent      = [int]$p.ParentProcessId
                Started     = [datetime] $p.CreationDate
                CommandLine = $cl
            }
        }
    }
    return $out
}

function Stop-ProcessTree {
    <#
      taskkill /T /F on a PID. Works on PS 5.1 where Process.Kill(bool) does not.

      L4: the two silent catch blocks in this script are fixed here and at the
      ExitCode read below. Both used to swallow silently. This one used to hide a
      completely failed kill - the exact defect that left Batch 4's orphans alive
      for ten hours - so it now ALWAYS reports what taskkill said, and returns
      'killed', 'gone' (it had already exited: the enumeration/kill race), or
      'failed'.
    #>
    param([int] $ProcessId, [string] $Why)
    Write-Host ("    [kill-tree] pid {0} ({1})" -f $ProcessId, $Why)
    $result = 'killed'
    try {
        $out = & taskkill.exe /T /F /PID $ProcessId 2>&1
        $text = ($out | Out-String)
        if ($text -match 'not found|not recognized') {
            $result = 'gone'
        } elseif ($text -match 'could not be terminated|Access is denied') {
            $result = 'failed'
        }
        if ($text.Trim()) {
            foreach ($line in ($text -split "`r?`n")) {
                if ($line.Trim()) { Write-Host ("    [kill-tree] pid {0}: {1}" -f $ProcessId, $line.Trim()) }
            }
        }
    } catch {
        $result = 'failed'
        Write-Host ("    [kill-tree] pid {0} could not be signalled: {1}" -f $ProcessId, $_.Exception.Message)
    }
    return $result
}

function Invoke-BoundedProcess {
    <#
      Runs one child process with a hard wall-clock limit, WITHOUT ever blocking on
      a pipe. Returns @{ Status; Code; Output; Seconds; RootPid }.

      Status is 'ok' or 'timeout'.

      The three properties that matter:
        * stdout/stderr go to temp files, so a surviving grandchild that inherited
          the handle cannot stop us reading the results;
        * the timeout is enforced by taskkill /T /F, which kills the whole tree;
        * the root PID is recorded before we wait, so a later orphan sweep can
          find anything that outlived the root.
    #>
    param(
        [string]   $FilePath,
        [string]   $ArgString,
        [int]      $LimitSeconds,
        [string]   $WorkDir
    )

    $stamp = [guid]::NewGuid().ToString('N')
    $outFile = Join-Path $WorkDir ("out_{0}.txt" -f $stamp)
    $errFile = Join-Path $WorkDir ("err_{0}.txt" -f $stamp)
    $startedAt = Get-Date

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $proc = Start-Process -FilePath $FilePath -ArgumentList $ArgString `
                         -WorkingDirectory $repoRoot `
                         -RedirectStandardOutput $outFile `
                         -RedirectStandardError  $errFile `
                         -NoNewWindow -PassThru
    $rootPid = $proc.Id
    [void]$script:startedRoots.Add($rootPid)
    Write-Host ("    [chunk] pid {0} limit {1}s" -f $rootPid, $LimitSeconds)

    $exited = $proc.WaitForExit($LimitSeconds * 1000)
    $sw.Stop()
    $status = 'ok'
    $code = 0
    if (-not $exited) {
        $status = 'timeout'
        $code = -1
        Stop-ProcessTree -ProcessId $rootPid -Why 'chunk exceeded its hard limit' | Out-Null
        # L4: this catch used to be silent. After a kill, WaitForExit can throw if
        # the handle is already gone, which is harmless - but it is now logged so
        # it can never hide a second problem the way the old one hid a failed kill.
        try { [void]$proc.WaitForExit(10000) }
        catch { Write-Host ("    [kill-tree] post-kill WaitForExit threw: {0}" -f $_.Exception.Message) }
    } else {
        try { $code = $proc.ExitCode }
        catch { $code = -1; Write-Host ("    [chunk] pid {0}: ExitCode unavailable: {1}" -f $rootPid, $_.Exception.Message) }
    }

    # Reading files can never block, which is the whole point of the change.
    $text = ''
    if (Test-Path -LiteralPath $outFile) { $text += (Get-Content -LiteralPath $outFile -Raw -ErrorAction SilentlyContinue) }
    if (Test-Path -LiteralPath $errFile) { $text += "`n" + (Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue) }
    foreach ($f in @($outFile, $errFile)) {
        Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
    }

    # Orphan sweep: anything of ours that started during this chunk and is still
    # alive afterwards leaked. Filtered to processes whose command line points
    # inside this repo so unrelated python work on the machine is never touched.
    #
    # L3: enumeration and killing race. A process can exit between the moment it
    # is listed and the moment taskkill is aimed at it, so "detected" is not the
    # same claim as "killed". Each candidate is therefore classified by what
    # taskkill actually reported, and the three counts are printed separately.
    $leaked = @(Get-RepoPython | Where-Object {
        $_.Started -ge $startedAt -and $_.Pid -ne $PID -and $_.Parent -ne $PID
    })
    foreach ($leak in $leaked) {
        [void]$script:orphanEvents.Add(("pid {0} parent {1}: {2}" -f $leak.Pid, $leak.Parent, $leak.CommandLine))
        $outcome = Stop-ProcessTree -ProcessId $leak.Pid -Why 'orphan left behind by this chunk'
        if ($outcome -eq 'gone') {
            $script:orphansAlreadyGone++
        } elseif ($outcome -eq 'failed') {
            $script:orphansSignallingFails++
        } else {
            $script:orphansKilled++
        }
    }

    return @{ Status = $status; Code = $code; Output = $text;
              Seconds = $sw.Elapsed.TotalSeconds; RootPid = $rootPid }
}

function Invoke-PytestBounded {
    <# One pytest invocation with a hard wall-clock limit. #>
    param([string[]] $Targets, [int] $LimitSeconds, [switch] $CollectOnly)

    $argList = @('-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                 '--no-header', '--color=no')
    if ($CollectOnly) { $argList += '--collect-only' }
    else { $argList += @('--tb=line', '-rf') }
    $argList += @($Targets)
    $argString = ($argList | ForEach-Object { '"' + $_ + '"' }) -join ' '
    return Invoke-BoundedProcess -FilePath $pythonExe -ArgString $argString `
                                 -LimitSeconds $LimitSeconds -WorkDir $script:workDir
}

function Get-PytestCounts {
    <# Parses "N passed, N failed, N skipped, N xfailed" out of a -q summary line.
       xfailed MUST be matched: a test that is collected but lands in no bucket
       shows up as a phantom "unaccounted" count and forces exit 3. Measured with it
       missing: 3721 passed + 2 failed + 13 skipped against 3737 collected, i.e.
       exactly one test unaccounted - the single strict xfail in
       tests/test_m217_windows_path_edges.py. #>
    param([string] $Output)
    $c = @{ passed = 0; failed = 0; skipped = 0; error = 0; xfailed = 0 }
    foreach ($m in [regex]::Matches($Output, '(\d+)\s+(passed|failed|skipped|xfailed|error|errors)')) {
        $key = $m.Groups[2].Value
        if ($key -eq 'errors') { $key = 'error' }
        $c[$key] = $c[$key] + [int]$m.Groups[1].Value
    }
    return $c
}

function Get-TestIds {
    <# Collect-only for one file, returning test ids.
       -CollectOnly is load-bearing: without it this runs the whole file (which is
       exactly what timed out), wastes the budget, and parses no ids because -q does
       not list them. A file slower than the chunk limit then reports "could not be
       bisected further" and every test in it goes unaccounted. Measured: with the
       flag missing, test_recovery_p10_repair.py (52 tests, 136.8 s) left 52 tests
       unaccounted and the run exited 3. #>
    param([string] $File)
    $res = Invoke-PytestBounded -Targets @($File) -LimitSeconds 600 -CollectOnly
    return @($res.Output -split "`r?`n" |
             Where-Object { $_ -like 'tests/*::*' } |
             ForEach-Object { $_.Trim() })
}

# ── working directory for redirected output ──────────────────────────────────
$script:workDir = Join-Path ([System.IO.Path]::GetTempPath()) ("diablo_runner_{0}" -f [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $script:workDir -Force | Out-Null

# ── -HangSelfTest ────────────────────────────────────────────────────────────
if ($HangSelfTest) {
    Write-Host "=== HANG SELF-TEST ==="
    Write-Host "Proves the hard timeout works against a grandchild that holds stdout."
    Write-Host "workdir: $script:workDir"
    Write-Host ""

    # The parent spawns a grandchild that sleeps 300 s and inherits stdout, then
    # sleeps itself. That is the exact shape that hung Batch 4: the pipe write end
    # stays open long after the direct process would have exited.
    $parent = Join-Path $script:workDir 'hang_parent.py'
    @'
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
sys.stdout.write("parent=%d child=%d\n" % (os.getpid(), child.pid))
sys.stdout.flush()
time.sleep(300)
'@ | Set-Content -LiteralPath $parent -Encoding ascii

    $before = @(Get-RepoPython | ForEach-Object { $_.Pid })
    Write-Host "python processes before: $($before.Count) (repo-scoped)"

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $res = Invoke-BoundedProcess -FilePath $pythonExe `
                                 -ArgString ('"{0}"' -f $parent) `
                                 -LimitSeconds 10 -WorkDir $script:workDir
    $sw.Stop()

    Write-Host ""
    Write-Host "status        : $($res.Status)"
    Write-Host "elapsed       : $([Math]::Round($sw.Elapsed.TotalSeconds,1))s (limit was 10s; must be well under 15s)"
    Write-Host "child stdout  : $($res.Output.Trim())"
    $after = @(Get-RepoPython | ForEach-Object { $_.Pid })
    Write-Host "orphan events : $($script:orphanEvents.Count)"
    foreach ($e in $script:orphanEvents) { Write-Host "    $e" }
    Write-Host "repo python after: $($after.Count)"

    Remove-Item -LiteralPath $parent -Force -ErrorAction SilentlyContinue

    $ok = $true
    if ($res.Status -ne 'timeout') {
        Write-Host "FAIL: expected status 'timeout' (the parent sleeps 300 s, limit is 10 s)"
        $ok = $false
    }
    if ($sw.Elapsed.TotalSeconds -gt 15) {
        Write-Host "FAIL: the bounded call took longer than 15 s, so the timeout did not bound it"
        $ok = $false
    }
    if (@(Get-RepoPython).Count -gt $before.Count) {
        Write-Host "FAIL: python processes survived the timeout"
        $ok = $false
    }
    if ($ok) { Write-Host ""; Write-Host "HANG SELF-TEST PASS: returned in $([Math]::Round($sw.Elapsed.TotalSeconds,1))s, tree killed, no orphan." }
    else { Write-Host "HANG SELF-TEST FAILED" }
    Remove-Item -LiteralPath $script:workDir -Recurse -Force -ErrorAction SilentlyContinue
    if ($ok) { exit 0 } else { exit 1 }
}

# ── 1. collect-only ──────────────────────────────────────────────────────────

$allFiles = Get-ChildItem -LiteralPath $testsDir -Filter 'test_*.py' -File |
            Sort-Object Name
$deterministic = @($allFiles | Where-Object {
    $_.Name -notlike $excludeGl -or $_.Name -in $includeAnyway
})
$excluded      = @($allFiles | Where-Object {
    $_.Name -like $excludeGl -and $_.Name -notin $includeAnyway
})

if ($SmokeTest) {
    # Exercises every stage of this script - collect, chunk, count, report, exit -
    # in a few seconds, so a regression in the runner itself is caught in seconds
    # rather than after a ten-minute suite run.
    #
    # test_m217_restart_real.py is included deliberately: it is the re-inclusion
    # this task added, and a smoke test that skipped it would not prove the
    # re-inclusion works. It is slow (it spawns interpreters), so the timeout is
    # raised rather than the file dropped.
    $deterministic = @($deterministic | Where-Object {
        $_.Name -in @('test_registry.py', 'test_m217_restart_real.py')
    })
    if ($TimeoutSeconds -lt 180) { $TimeoutSeconds = 180 }
    Write-Host "SMOKE TEST: restricted to $($deterministic.Count) file(s), timeout ${TimeoutSeconds}s"
}

Write-Host "repo        : $repoRoot"
Write-Host "interpreter : $pythonExe"
Write-Host "chunk size  : $ChunkSize"
Write-Host "timeout     : ${TimeoutSeconds}s per invocation (hard, enforced with taskkill /T /F)"
Write-Host "deterministic files : $($deterministic.Count)"
Write-Host "excluded ($excludeGl) : $($excluded.Count)"
foreach ($f in $excluded) { Write-Host "    excluded  $($f.Name)" }
$reIncluded = @($deterministic | Where-Object { $_.Name -in $includeAnyway })
if ($reIncluded.Count -gt 0) {
    Write-Host "re-included despite matching $excludeGl (no hardware needed):"
    foreach ($f in $reIncluded) { Write-Host "    INCLUDED  $($f.Name)" }
}
Write-Host ""

$collect = Invoke-PytestBounded `
    -Targets (@($deterministic | ForEach-Object { $_.FullName })) `
    -LimitSeconds 600 -CollectOnly
$collected = 0
foreach ($m in [regex]::Matches($collect.Output, '(\d+) tests collected')) {
    $collected = [int]$m.Groups[1].Value
}
if ($collected -le 0) {
    Write-Host "COLLECTED: 0 - the collect-only step produced no count."
    Write-Host "--- first 40 lines of its output ---"
    @($collect.Output -split "`r?`n")[0..([Math]::Min(39, @($collect.Output -split "`r?`n").Count - 1))] |
        ForEach-Object { Write-Host "    $_" }
    exit 2
}
Write-Host "COLLECTED: $collected tests"
Write-Host ""

# ── 2. chunked run with bisection ────────────────────────────────────────────

$totals      = @{ passed = 0; failed = 0; skipped = 0; error = 0; xfailed = 0 }
$allFailures = New-Object System.Collections.ArrayList
$chunkNo     = 0

function Show-Chunk {
    param([string] $Label, [hashtable] $Counts, [string] $Status,
          [double] $Seconds, [string] $Output)
    $script:chunkNo++
    $text = ($Counts.Keys | Sort-Object |
             ForEach-Object { "$_=$($Counts[$_])" }) -join ' '
    Write-Host ("chunk {0,-3} [{1,-7}] {2,6:N1}s  {3}" -f $script:chunkNo, $Status, $Seconds, $text)
    if ($text -eq '') { Write-Host "chunk $script:chunkNo  (no counts parsed)" }
    foreach ($line in ($Output -split "`r?`n")) {
        if ($line -like 'FAILED *') { [void]$script:allFailures.Add($line.Trim()) }
    }
}

function Invoke-IdsBounded {
    <# Run an explicit id list, bisecting on timeout.
       NOTE: the @() coercions below are load-bearing. PowerShell unwraps a
       one-element array into a bare scalar when it crosses a parameter boundary,
       and under Set-StrictMode -Version Latest a string has no .Count, so
       `$Ids.Count` then throws "The property 'Count' cannot be found on this
       object" and the run dies after the '1 + 1' split. Found during Batch 4;
       carried into Batch 5 and kept. #>
    param([string[]] $Ids, [string] $Label)
    $Ids = @($Ids)
    if ($Ids.Count -eq 0) { return }
    $res = Invoke-PytestBounded -Targets $Ids -LimitSeconds $TimeoutSeconds
    if ($res.Status -eq 'timeout' -and $Ids.Count -gt 1) {
        $mid = [int]($Ids.Count / 2)
        Write-Host "    bisect $($Ids.Count) ids -> $mid + $($Ids.Count - $mid)"
        Invoke-IdsBounded -Ids $Ids[0..($mid - 1)] -Label $Label
        Invoke-IdsBounded -Ids $Ids[$mid..($Ids.Count - 1)] -Label $Label
        return
    }
    $counts = Get-PytestCounts $res.Output
    foreach ($k in $counts.Keys) { $script:totals[$k] += $counts[$k] }
    Show-Chunk -Label $Label -Counts $counts -Status $res.Status `
               -Seconds $res.Seconds -Output $res.Output
}

# ── slow files get their own chunk with a larger budget (L5) ────────────────
# L5: chunk 14 finished in 119.7 s against a 120 s limit - a 0.3 s margin. On a
# marginally slower machine it would have split, costing two extra chunk
# invocations plus more taskkill churn on a file that legitimately takes ~137 s
# alone. Slow files are therefore pulled out of the generic chunks and given their
# own longer budget. The hard timeout still applies; it is simply a number that
# matches the work instead of one it can never satisfy.
$slowFiles = @{
    'test_recovery_p10_repair.py' = 300
}
$ownChunk  = New-Object System.Collections.Queue
$rest      = New-Object System.Collections.ArrayList
foreach ($f in $deterministic) {
    $budget = $slowFiles[$f.Name]
    if ($budget) {
        $ownChunk.Enqueue(@(@($f.FullName), $budget))
    } else {
        [void]$rest.Add($f.FullName)
    }
}
if ($ownChunk.Count -gt 0) {
    Write-Host "slow files with a dedicated budget:"
    foreach ($f in $deterministic) {
        if ($slowFiles[$f.Name]) { Write-Host "    $($f.Name) : $($slowFiles[$f.Name])s (measured ~137 s alone)" }
    }
    Write-Host ""
}

$queue = New-Object System.Collections.Queue
for ($i = 0; $i -lt $rest.Count; $i += $ChunkSize) {
    $end = [Math]::Min($i + $ChunkSize - 1, $rest.Count - 1)
    $queue.Enqueue(@(@($rest[$i..$end]) | ForEach-Object { $_ }))
}

# The dedicated slow-file chunks run first so a failure there is visible at the
# top of the log rather than buried at the end.
while ($ownChunk.Count -gt 0) {
    $entry = @($ownChunk.Dequeue())
    $only  = @($entry[0])
    $budget = [int]$entry[1]
    Write-Host "    (dedicated budget ${budget}s for $(Split-Path -Leaf $only[0]))"
    $res = Invoke-PytestBounded -Targets $only -LimitSeconds $budget
    $counts = Get-PytestCounts $res.Output
    foreach ($k in $counts.Keys) { $totals[$k] += $counts[$k] }
    $leaf = Split-Path -Leaf $only[0]
    Show-Chunk -Label $leaf -Counts $counts -Status $res.Status `
               -Seconds $res.Seconds -Output $res.Output
}

while ($queue.Count -gt 0) {
    $paths = @($queue.Dequeue())
    $res = Invoke-PytestBounded -Targets $paths -LimitSeconds $TimeoutSeconds

    if ($res.Status -eq 'timeout') {
        if ($paths.Count -gt 1) {
            $mid = [int]($paths.Count / 2)
            Write-Host "    split $($paths.Count) files -> $mid + $($paths.Count - $mid)"
            $queue.Enqueue(@($paths[$mid..($paths.Count - 1)]))
            $queue.Enqueue(@($paths[0..($mid - 1)]))
            continue
        }
        # A single file that will not finish: bisect by test id.
        $leaf = Split-Path -Leaf $paths[0]
        $ids = @(Get-TestIds $paths[0])
        if ($ids.Count -gt 1) {
            Write-Host "    bisect $leaf ($($ids.Count) tests)"
            $mid = [int]($ids.Count / 2)
            Invoke-IdsBounded -Ids $ids[0..($mid - 1)] -Label $leaf
            Invoke-IdsBounded -Ids $ids[$mid..($ids.Count - 1)] -Label $leaf
        } else {
            Write-Host "chunk $leaf TIMED OUT and could not be bisected further"
            $totals['error'] += 1
        }
        continue
    }

    $counts = Get-PytestCounts $res.Output
    foreach ($k in $counts.Keys) { $totals[$k] += $counts[$k] }
    $label = (($paths | ForEach-Object { Split-Path -Leaf $_ }) -join ', ')
    Show-Chunk -Label $label -Counts $counts -Status $res.Status `
               -Seconds $res.Seconds -Output $res.Output
}

# ── 3. report ────────────────────────────────────────────────────────────────

$accounted = $totals['passed'] + $totals['failed'] + $totals['skipped'] + $totals['xfailed'] + $totals['error']

Write-Host ""
Write-Host "=== TOTAL ==="
Write-Host "  collected  $collected"
Write-Host "  passed     $($totals['passed'])"
Write-Host "  failed     $($totals['failed'])"
Write-Host "  skipped    $($totals['skipped'])"
Write-Host "  xfailed    $($totals['xfailed'])"
Write-Host "  errors     $($totals['error'])"
Write-Host "  accounted  $accounted"
if ($accounted -ne $collected) {
    Write-Host "  MISMATCH   $($collected - $accounted) test(s) unaccounted for"
}

if ($allFailures.Count -gt 0) {
    Write-Host ""
    Write-Host "=== FAILURES ($($allFailures.Count)) ==="
    foreach ($f in $allFailures) { Write-Host "  $f" }
}

Write-Host ""
Write-Host "=== PROCESS HYGIENE ==="
Write-Host "  chunk root pids started : $($script:startedRoots.Count)"
Write-Host "  orphan candidates seen  : $($script:orphanEvents.Count)"
Write-Host "    confirmed killed      : $($script:orphansKilled)"
Write-Host "    already gone (raced)  : $($script:orphansAlreadyGone)"
Write-Host "    signalling failed     : $($script:orphansSignallingFails)"
foreach ($e in $script:orphanEvents) { Write-Host "    ORPHAN $e" }
$stillLive = @(Get-RepoPython)
Write-Host "  repo python still live  : $($stillLive.Count)"
foreach ($s in $stillLive) { Write-Host "    LIVE pid $($s.Pid) started $($s.Started.ToString('HH:mm:ss'))" }

Remove-Item -LiteralPath $script:workDir -Recurse -Force -ErrorAction SilentlyContinue

$exit = 0
if ($totals['failed'] -gt 0 -or $totals['error'] -gt 0) { $exit = 1 }
if ($accounted -ne $collected) { $exit = 3 }
if ($script:orphanEvents.Count -gt 0) { $exit = 4 }
Write-Host ""
Write-Host "exit code: $exit"
exit $exit
