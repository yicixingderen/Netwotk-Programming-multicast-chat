[CmdletBinding()]
param(
    [switch] $NoBrowser,
    [switch] $FaultDemo,
    [ValidateRange(1, 65533)] [int] $HttpBase = 8761,
    [ValidatePattern('^[0-9.]+$')] [string] $Group = '239.255.42.99',
    [ValidateRange(1, 65535)] [int] $UdpPort = 50420,
    [ValidatePattern('^[0-9.]+$')] [string] $Interface = '127.0.0.1',
    [ValidateRange(1, 255)] [int] $Ttl = 1,
    [ValidateRange(1, 1024)] [int] $FileLimitMiB = 32,
    [ValidateRange(16, 1024)] [int] $RateKiB = 320
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Create .venv using Python 3.13 and install requirements.txt first. See README.md.'
}

$tag = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 6)
$relativeRun = ".demo/$tag"
$run = Join-Path $root $relativeRun
New-Item -Path $run -ItemType Directory -Force | Out-Null
$nodes = @()
try {
    $offset = 0
    foreach ($name in @('A', 'B', 'C')) {
        $port = $HttpBase + $offset
        $offset++
        $nodeArgs = @('-X', 'utf8', '-m', 'multicast_chat', '--name', $name,
            '--http-port', $port, '--data-dir', "$relativeRun/$name",
            '--group', $Group, '--udp-port', $UdpPort, '--interface', $Interface, '--ttl', $Ttl,
            '--file-limit-mib', $FileLimitMiB, '--rate-kib', $RateKiB)
        if ($FaultDemo -and $name -eq 'A') { $nodeArgs += @('--fault-demo', '--verbose') }
        $log = Join-Path $run "$name.log"
        $launch = @{
            FilePath = $python
            ArgumentList = $nodeArgs
            WorkingDirectory = $root
            RedirectStandardError = $log
            PassThru = $true
            WindowStyle = 'Hidden'
        }
        $process = Start-Process @launch
        $nodes += [PSCustomObject]@{
            Name = $name
            Process = $process
            Url = "http://127.0.0.1:$port"
            Log = $log
        }
    }
    foreach ($node in $nodes) {
        $deadline = (Get-Date).AddSeconds(15)
        $ready = $false
        while ((Get-Date) -lt $deadline -and -not $node.Process.HasExited) {
            try {
                $state = Invoke-RestMethod -Uri "$($node.Url)/api/state" -TimeoutSec 2
                if ($state.name -eq $node.Name -and (Select-String -LiteralPath $node.Log -SimpleMatch 'Uvicorn running')) {
                    $ready = $true
                    break
                }
            } catch {
                if ($node.Process.HasExited) { break }
            }
            Start-Sleep -Milliseconds 160
        }
        if (-not $ready) { throw "Node $($node.Name) failed to start. Check $($node.Log)." }
        Write-Host "$($node.Name) $($node.Url)  (log $($node.Log))"
        if (-not $NoBrowser) { Start-Process -FilePath $node.Url }
    }
    Read-Host 'Press Enter to stop the three nodes' | Out-Null
} finally {
    foreach ($node in $nodes) {
        if (-not $node.Process.HasExited) {
            Stop-Process -InputObject $node.Process
            $null = $node.Process.WaitForExit(3000)
        }
    }
}
