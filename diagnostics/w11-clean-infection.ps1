# w11-clean-infection.ps1
# Removes the loader found on 2026-10-08 and hunts for anything else like it.
# Run in the Windows 11 guest, ELEVATED PowerShell:
#     powershell -ExecutionPolicy Bypass -File .\w11-clean-infection.ps1
# Options:
#     -RemoveActivator   also remove the "Activation-Renewal" KMS task (see note at the bottom)
#     -SkipFullScan      skip the Defender full scan (slow)
#     -OfflineScan       finish by scheduling a Defender Offline scan (reboots the VM)
#
# What it REMOVES (known indicators, from the incident):
#   scheduled task  \XblGameCachesTask  (conhost --headless powershell -ep bypass -file C:\ProgramData\onedrive-sync.ps1)
#   file            C:\ProgramData\onedrive-sync.ps1   (326416 bytes, SHA256 C773BD52...B60F)
#   any running powershell that was launched on that file
# What it only REPORTS (nothing else is deleted without you looking):
#   other tasks/run keys/WMI consumers/services/profiles that look like loaders,
#   other .ps1 files that build code at runtime, Defender exclusions, odd hosts entries.
# What it does NOT do, and cannot: undo what the loader did in 5 months. It ran as your user every
# minute since 2026-05-14. Rebuilding the guest is the only way to be sure it is clean.
# ASCII only on purpose (PowerShell 5.1 misreads UTF-8 without BOM).

param([switch]$RemoveActivator, [switch]$SkipFullScan, [switch]$OfflineScan)

$ErrorActionPreference = 'Continue'
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Run this from an elevated PowerShell (Run as administrator).' -ForegroundColor Red; exit 1
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$work  = 'C:\ProgramData\cleanup-' + $stamp
New-Item -ItemType Directory -Force -Path $work | Out-Null
Start-Transcript -Path (Join-Path $work 'transcript.txt') | Out-Null
$findings = New-Object System.Collections.ArrayList
function Note($msg) { [void]$findings.Add($msg); Write-Host ('  [!] ' + $msg) -ForegroundColor Yellow }
function Step($msg) { Write-Host ''; Write-Host ('== ' + $msg) -ForegroundColor Cyan }

$badScript = 'C:\ProgramData\onedrive-sync.ps1'
$badTask   = 'XblGameCachesTask'
$knownHash = 'C773BD5246FBAE23A88966139E1BD14298FCC0246651920255F7DDF49B35B60F'

# ---------------------------------------------------------------- 1
Step '1. Quarantine a copy of the loader (so it can be analysed later), then record its hash'
if (Test-Path -LiteralPath $badScript) {
    $h = (Get-FileHash -LiteralPath $badScript -Algorithm SHA256).Hash
    Write-Host ('  SHA256 ' + $h + $(if ($h -eq $knownHash) { '  (matches the incident sample)' } else { '  (DIFFERS from the incident sample, still removing)' }))
    Copy-Item -LiteralPath $badScript -Destination (Join-Path $work 'onedrive-sync.ps1.QUARANTINE') -Force
    attrib +r (Join-Path $work 'onedrive-sync.ps1.QUARANTINE') | Out-Null
} else { Write-Host '  loader file not present (already removed?)' }

# ---------------------------------------------------------------- 2
Step '2. Stop anything running the loader'
$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and ($_.CommandLine -like '*onedrive-sync.ps1*') -and ($_.ProcessId -ne $PID)
}
foreach ($p in $procs) {
    Write-Host ('  killing pid ' + $p.ProcessId + ' : ' + $p.CommandLine)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
if (-not $procs) { Write-Host '  none running' }

# ---------------------------------------------------------------- 3
Step '3. Remove the scheduled task and the file'
$t = Get-ScheduledTask -TaskName $badTask -ErrorAction SilentlyContinue
if ($t) {
    Export-ScheduledTask -TaskName $badTask | Out-File (Join-Path $work ($badTask + '.xml')) -Encoding ascii
    Unregister-ScheduledTask -TaskName $badTask -Confirm:$false
    Write-Host '  task removed (XML saved in the work folder)'
} else { Write-Host '  task not present' }
if (Test-Path -LiteralPath $badScript) {
    attrib -s -h -r $badScript 2>$null | Out-Null
    Remove-Item -LiteralPath $badScript -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $badScript) { Note 'could not delete onedrive-sync.ps1 (locked?). Reboot and run again.' } else { Write-Host '  file deleted' }
}

# ---------------------------------------------------------------- 4
Step '4. Hunt for more of the same (report only)'
Write-Host '  scheduled tasks that run script hosts or headless conhost, outside \Microsoft\:'
Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\Microsoft\*' } | ForEach-Object {
    $tk = $_
    foreach ($a in $tk.Actions) {
        $cmd = ($a.Execute + ' ' + $a.Arguments)
        if ($cmd -match 'powershell|pwsh|cmd(\.exe)?\s+/c|mshta|wscript|cscript|rundll32|regsvr32|conhost.*--headless|-enc|-ep\s+bypass|ProgramData|\\Temp\\|AppData') {
            Note ('task ' + $tk.TaskPath + $tk.TaskName + ' [' + $tk.State + '] :: ' + $cmd.Trim())
        }
    }
}
Write-Host '  Run / RunOnce keys:'
foreach ($k in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run','HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce','HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run','HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run') {
    $props = (Get-ItemProperty $k -ErrorAction SilentlyContinue).PSObject.Properties | Where-Object { $_.Name -notmatch '^PS' }
    foreach ($p in $props) { if ($p.Value -match 'powershell|pwsh|wscript|cscript|mshta|-enc|ProgramData|\\Temp\\') { Note ('run key ' + $k + ' ' + $p.Name + ' = ' + $p.Value) } }
}
Write-Host '  WMI event consumers (the default SCM Event Log Filter is normal):'
Get-CimInstance -Namespace root\subscription -ClassName __EventFilter -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne 'SCM Event Log Filter' } | ForEach-Object { Note ('WMI filter ' + $_.Name) }
Get-CimInstance -Namespace root\subscription -ClassName CommandLineEventConsumer -ErrorAction SilentlyContinue | ForEach-Object { Note ('WMI CommandLine consumer ' + $_.Name + ' :: ' + $_.CommandLineTemplate) }
Get-CimInstance -Namespace root\subscription -ClassName ActiveScriptEventConsumer -ErrorAction SilentlyContinue | ForEach-Object { Note ('WMI ActiveScript consumer ' + $_.Name) }
Write-Host '  services outside system paths that start automatically:'
Get-CimInstance Win32_Service | Where-Object { $_.StartMode -eq 'Auto' -and $_.PathName -notmatch 'system32|\\Windows\\|Microsoft|VMware|Program Files' } | ForEach-Object { Note ('service ' + $_.Name + ' :: ' + $_.PathName) }
Write-Host '  PowerShell profiles (persistence by profile):'
foreach ($pf in $PROFILE.AllUsersAllHosts, $PROFILE.AllUsersCurrentHost, $PROFILE.CurrentUserAllHosts, $PROFILE.CurrentUserCurrentHost) {
    if ($pf -and (Test-Path -LiteralPath $pf)) { Note ('PowerShell profile exists: ' + $pf + ' (' + (Get-Item $pf).Length + ' bytes), read it') }
}
Write-Host '  startup folders:'
foreach ($sf in "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp", "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup") {
    Get-ChildItem -LiteralPath $sf -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne 'desktop.ini' } | ForEach-Object { Note ('startup item ' + $_.FullName) }
}
Write-Host '  other scripts that build code at runtime (same technique as the loader):'
$roots = @('C:\ProgramData', "$env:APPDATA", "$env:LOCALAPPDATA\Temp", 'C:\Windows\Temp', 'C:\Users\Public')
foreach ($r in $roots) {
    Get-ChildItem -LiteralPath $r -Recurse -Force -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -ieq '.ps1' -and $_.Length -gt 20000 -and $_.FullName -notlike "$work*" } | ForEach-Object {
        $head = Get-Content -LiteralPath $_.FullName -TotalCount 400 -ErrorAction SilentlyContinue | Out-String
        if ($head -match 'DefineDynamicAssembly|Reflection\.Emit|FromBase64String|-bxor|\[Runtime\.InteropServices\.Marshal\]::GetDelegateForFunctionPointer') {
            Note ('suspicious script ' + $_.FullName + ' (' + $_.Length + ' bytes)')
        }
    }
}
Write-Host '  living-off-the-land processes XWorm-style loaders inject into (CasPol/RegAsm/MSBuild running outside their normal folders):'
Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(CasPol|RegAsm|RegSvcs|MSBuild|InstallUtil)\.exe$' } | ForEach-Object {
    $par = (Get-CimInstance Win32_Process -Filter ('ProcessId=' + $_.ParentProcessId) -ErrorAction SilentlyContinue).Name
    if ($_.ExecutablePath -notmatch 'Microsoft\.NET|Visual Studio|MSBuild') { Note ('process ' + $_.Name + ' pid ' + $_.ProcessId + ' parent ' + $par + ' path ' + $_.ExecutablePath) }
}
Write-Host '  scheduled-task creation events (Security 4698) in the log, for the timeline (needs task auditing, may be empty):'
Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4698} -MaxEvents 200 -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.Message -match 'Task Name:\s+(\S+)') { $tn = $Matches[1]; if ($tn -notlike '\Microsoft\*') { Write-Host ('   ' + $_.TimeCreated + ' ' + $tn) } }
}
Write-Host '  known XWorm-family task names / startup script types:'
foreach ($n in 'EscansUpdate') { if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) { Note ('task ' + $n + ' exists') } }
Get-ChildItem -LiteralPath $env:APPDATA -Force -File -Recurse -Depth 1 -ErrorAction SilentlyContinue | Where-Object { $_.Extension -match '^\.(vbs|bat|lnk)$' } | ForEach-Object { Note ('script/shortcut in AppData: ' + $_.FullName) }
Write-Host '  Defender exclusions (an infection usually adds one):'
$mp = Get-MpPreference
foreach ($x in @($mp.ExclusionPath) + @($mp.ExclusionProcess) + @($mp.ExclusionExtension)) { if ($x) { Note ('Defender exclusion: ' + $x) } }

# ---------------------------------------------------------------- 5
Step '5. Block the beacon domains locally (host DNS already does; this covers the guest if it ever resolves elsewhere)'
$hosts = 'C:\Windows\System32\drivers\etc\hosts'
Copy-Item $hosts (Join-Path $work 'hosts.before') -Force
$cur = Get-Content $hosts -Raw
foreach ($d in 'olive3451.com','gcrenwp.top','tracker.gcrenwp.top','githubusercontent.co','raw.githubusercontent.co','steamcommunity.co','calude.ai') {
    if ($cur -notmatch ('(?m)^\s*0\.0\.0\.0\s+' + [regex]::Escape($d) + '\s*$')) { Add-Content -Path $hosts -Value ('0.0.0.0 ' + $d) -Encoding ascii }
}
Write-Host '  done (original saved as hosts.before)'

# ---------------------------------------------------------------- 6
Step '6. Hardening that cannot break normal use'
Set-NetFirewallProfile -Profile Domain,Private,Public -Enabled True
Set-SmbServerConfiguration -EnableSMB1Protocol $false -Force -ErrorAction SilentlyContinue
Disable-WindowsOptionalFeature -Online -FeatureName SMB1Protocol -NoRestart -ErrorAction SilentlyContinue | Out-Null
New-Item -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\DNSClient' -Force | Out-Null
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\DNSClient' -Name EnableMulticast -Value 0 -Type DWord
Set-MpPreference -DisableRealtimeMonitoring $false -PUAProtection Enabled -EnableNetworkProtection Enabled -MAPSReporting Advanced -CloudBlockLevel High -SubmitSamplesConsent SendSafeSamples
Write-Host '  firewall on, SMB1 off, LLMNR off, Defender PUA/network protection/cloud level raised'
Write-Host '  NOT touched: SSH (port 22) and SMB (445) are listening. Restrict them yourself if you do not need them.'

# ---------------------------------------------------------------- 7
Step '7. Defender: update signatures, scan'
Update-MpSignature -ErrorAction SilentlyContinue
Start-MpScan -ScanType QuickScan
if (-not $SkipFullScan) { Write-Host '  full scan (this takes a while)...'; Start-MpScan -ScanType FullScan }
Write-Host '  threats Defender knows about:'
Get-MpThreatDetection | Select-Object -First 20 | ForEach-Object { Write-Host ('   ' + $_.InitialDetectionTime + ' ' + $_.ThreatID + ' ' + ($_.Resources -join ',')) }

# ---------------------------------------------------------------- 8
if ($RemoveActivator) {
    Step '8. Remove the Activation-Renewal KMS task (piracy tooling, a likely way in)'
    Export-ScheduledTask -TaskName 'Activation-Renewal' -ErrorAction SilentlyContinue | Out-File (Join-Path $work 'Activation-Renewal.xml') -Encoding ascii
    Unregister-ScheduledTask -TaskName 'Activation-Renewal' -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath "$env:ProgramFiles\Activation-Renewal" -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host '  removed. Windows will show as not activated once the KMS grace period runs out (about 180 days). Use a licence you own.'
} else {
    Step '8. Activation-Renewal task left in place (run again with -RemoveActivator to remove it)'
}

# ---------------------------------------------------------------- 9
Step '9. Verify'
$still = @()
if (Get-ScheduledTask -TaskName $badTask -ErrorAction SilentlyContinue) { $still += 'task still present' }
if (Test-Path -LiteralPath $badScript) { $still += 'loader file still present' }
if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*onedrive-sync.ps1*' -and $_.ProcessId -ne $PID }) { $still += 'loader process still running' }
if ($still.Count -eq 0) { Write-Host '  known indicators: gone' -ForegroundColor Green } else { $still | ForEach-Object { Write-Host ('  STILL: ' + $_) -ForegroundColor Red } }
Write-Host ''
Write-Host ('  items needing your eyes: ' + $findings.Count)
$findings | ForEach-Object { Write-Host ('   - ' + $_) }
$findings | Out-File (Join-Path $work 'findings.txt') -Encoding ascii

if ($OfflineScan) { Write-Host 'Scheduling Defender Offline scan, the VM will reboot...'; Stop-Transcript | Out-Null; Start-MpWDOScan; exit 0 }

Write-Host ''
Write-Host 'STILL TO DO BY HAND (a script cannot do these):' -ForegroundColor Cyan
Write-Host '  1. From a CLEAN device: change passwords and revoke sessions for everything used in this VM, email and password-manager first.'
Write-Host '  2. Treat browser-saved passwords, tokens, SSH keys and wallets in this VM as exposed.'
Write-Host '  3. Rebuild the guest from a clean ISO when you can. This script removes what it knows about; it cannot prove the rest is clean.'
Write-Host ('  Log and evidence: ' + $work)
Stop-Transcript | Out-Null
