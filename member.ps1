[CmdletBinding(DefaultParameterSetName = 'Start')]
param(
    [Parameter(ParameterSetName = 'Start')] [string] $Name,
    [Parameter(ParameterSetName = 'Start')] [switch] $NoBrowser,
    [Parameter(ParameterSetName = 'Start')] [string] $BrowserPath,
    [Parameter(ParameterSetName = 'Start')] [ValidatePattern('^[0-9.]+$')] [string] $Group = '239.255.42.99',
    [Parameter(ParameterSetName = 'Start')] [ValidateRange(1, 65535)] [int] $UdpPort = 50420,
    [Parameter(ParameterSetName = 'Start')] [ValidatePattern('^[0-9.]+$')] [string] $Interface = '127.0.0.1',
    [Parameter(ParameterSetName = 'Start')] [ValidateRange(1, 255)] [int] $Ttl = 1,
    [Parameter(ParameterSetName = 'Stop', Mandatory)] [ValidatePattern('^[0-9a-f]{12}$')] [string] $Stop
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$membersRoot = Join-Path $root '.demo/members'

if ($PSCmdlet.ParameterSetName -eq 'Stop') {
    $recordPath = Join-Path $membersRoot "$Stop/launch.json"
    if (-not (Test-Path -LiteralPath $recordPath -PathType Leaf)) {
        throw "Member $Stop was not launched by this script."
    }
    $record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if ($null -eq $process -or $process.HasExited -or
        [string]$process.StartTime.ToUniversalTime().Ticks -ne $record.startedUtcTicks) {
        Write-Output "Member $Stop is already stopped."
        return
    }
    Stop-Process -InputObject $process
    if (-not $process.WaitForExit(5000)) { throw "Timed out stopping member $Stop." }
    Write-Output "Stopped member $Stop."
    return
}

function Find-WindowBrowser {
    if ($BrowserPath) {
        if (-not (Test-Path -LiteralPath $BrowserPath -PathType Leaf)) {
            throw "Browser executable not found: $BrowserPath"
        }
        return (Resolve-Path -LiteralPath $BrowserPath).Path
    }
    $programFiles32 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
    foreach ($directory in @($env:ProgramW6432, $env:ProgramFiles, $programFiles32, $env:LOCALAPPDATA)) {
        if (-not $directory) { continue }
        foreach ($application in @('Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe')) {
            $candidate = Join-Path $directory $application
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    throw 'Chrome or Edge was not found. Specify -BrowserPath or run with -NoBrowser.'
}

function Quote-ProcessArgument([string] $Value) {
    # Start-Process joins ArgumentList into one Windows command line.
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

$python = Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Create .venv using Python 3.13 and install requirements.txt first. See README.md.'
}
if (-not $NoBrowser) { $browser = Find-WindowBrowser }

$id = [guid]::NewGuid().ToString('N').Substring(0, 12)
if (-not $PSBoundParameters.ContainsKey('Name')) { $Name = "Member-$id" }
if ([string]::IsNullOrWhiteSpace($Name) -or $Name -match '[\x00-\x1f]' -or
    [Text.Encoding]::UTF8.GetByteCount($Name) -gt 48) {
    throw 'The member name must be 1-48 UTF-8 bytes without control characters.'
}

$relativeData = ".demo/members/$id"
$data = Join-Path $membersRoot $id
$null = New-Item -ItemType Directory -Path $data
$log = Join-Path $data 'node.log'
$launch = @{
    FilePath = $python
    ArgumentList = @('-X', 'utf8', '-m', 'multicast_chat', (Quote-ProcessArgument "--name=$Name"),
        '--http-port', '0', '--data-dir', $relativeData, '--group', $Group,
        '--udp-port', $UdpPort, '--interface', $Interface, '--ttl', $Ttl)
    WorkingDirectory = $root
    RedirectStandardError = $log
    RedirectStandardOutput = Join-Path $data 'stdout.log'
    PassThru = $true
    WindowStyle = 'Hidden'
}

$process = $null
$started = $false
try {
    $process = Start-Process @launch
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $url = $null
    $addressPattern = 'Uvicorn running on (http://127\.0\.0\.1:[0-9]+)'
    while ($clock.Elapsed.TotalSeconds -lt 15 -and -not $process.HasExited) {
        $listener = Select-String -LiteralPath $log -Pattern $addressPattern -List -ErrorAction SilentlyContinue
        if ($listener) {
            $candidate = $listener.Matches[0].Groups[1].Value
            try {
                $response = Invoke-WebRequest -Uri "$candidate/api/state" -UseBasicParsing -TimeoutSec 2
                $json = [Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray())
                $state = ConvertFrom-Json -InputObject $json
                if ($state.name -ceq $Name) {
                    $url = $candidate
                    break
                }
            } catch {
                if ($process.HasExited) { break }
            }
        }
        Start-Sleep -Milliseconds 100
    }
    if (-not $url -or $process.HasExited) { throw "Member $id failed to start. Check $log." }

    @{
        pid = $process.Id
        startedUtcTicks = [string]$process.StartTime.ToUniversalTime().Ticks
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $data 'launch.json') -Encoding UTF8

    if (-not $NoBrowser) {
        Start-Process -FilePath $browser -ArgumentList @('--new-window', "$url/")
    }
    $started = $true
    Write-Output "$Name  $url/"
    Write-Output "Log  $log"
    Write-Output "Stop  .\member.ps1 -Stop $id"
} finally {
    if (-not $started -and $null -ne $process -and -not $process.HasExited) {
        Stop-Process -InputObject $process
        $null = $process.WaitForExit(5000)
    }
}
