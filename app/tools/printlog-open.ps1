# PrintLog: Ordner-Links (printlog-open:...) im Windows-Explorer öffnen.
#
# Einrichten: printlog-einrichten.cmd aus den PrintLog-Einstellungen per
# Doppelklick starten (oder dieses Skript mit -ExecutionPolicy Bypass).
# Es legt sich nach %LOCALAPPDATA%\printlog und meldet das Link-Protokoll
# "printlog-open" für deinen Benutzer an (kein Admin nötig).
#
# Danach ruft der Browser dieses Skript mit dem angeklickten Link auf:
#  * Ordner: öffnet ihn im Explorer.
#  * 3D-Datei (.3mf, .stl, .step, .stp, .obj) in deinem Projektordner:
#    öffnet sie mit dem Standardprogramm, z. B. Bambu Studio. Den Projektordner
#    wählst du bei der Einrichtung, ohne Wahl wird nie direkt geöffnet.
#  * Jede andere Datei: öffnet den Ordner und markiert die Datei.
# Programme, Skripte oder Dateien außerhalb des Projektordners startet es nie.
#
# Entfernen: printlog-open-entfernen.reg aus den PrintLog-Einstellungen öffnen.

param([string]$Url)

$ErrorActionPreference = 'Stop'
$Scheme = 'printlog-open'
$Dir = Join-Path $env:LOCALAPPDATA 'printlog'
$RootsFile = Join-Path $Dir 'projektordner.txt'
$OpenExt = @('.3mf', '.stl', '.step', '.stp', '.obj')

function Show-Message([string]$Text, [int]$Icon = 48) {
    try { (New-Object -ComObject WScript.Shell).Popup($Text, 0, 'PrintLog', $Icon) | Out-Null }
    catch { Write-Host $Text }
}

function Select-ProjectFolder {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
        $dlg.Description = 'Projektordner wählen: 3D-Dateien (3MF, STL, STEP, OBJ) darin dürfen PrintLog-Links direkt öffnen. Abbrechen = nie direkt öffnen, nur im Explorer zeigen.'
        $dlg.ShowNewFolderButton = $false
        if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $dlg.SelectedPath }
    } catch { }
    return $null
}

function Install-Handler([string]$Self) {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $target = Join-Path $Dir 'printlog-open.ps1'
    if ($Self -and $Self -ne $target) {
        Copy-Item -LiteralPath $Self -Destination $target -Force
    }
    Unblock-File -LiteralPath $target -ErrorAction SilentlyContinue

    $root = Select-ProjectFolder
    if ($root) {
        Set-Content -LiteralPath $RootsFile -Value $root -Encoding UTF8
        $rootInfo = "3D-Dateien aus diesem Ordner öffnen sich direkt:`n$root"
    } else {
        Remove-Item -LiteralPath $RootsFile -ErrorAction SilentlyContinue
        $rootInfo = 'Kein Projektordner gewählt: Dateien werden nur im Explorer markiert.'
    }

    # Immer Windows PowerShell 5.1, auch wenn die Einrichtung aus PowerShell 7 läuft.
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $cmd = '"' + $ps + '" -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $target + '" "%1"'

    $key = "HKCU:\Software\Classes\$Scheme"
    New-Item -Path "$key\shell\open\command" -Force | Out-Null
    Set-ItemProperty -LiteralPath $key -Name '(default)' -Value 'URL:PrintLog Ordner öffnen'
    Set-ItemProperty -LiteralPath $key -Name 'URL Protocol' -Value ''
    Set-ItemProperty -LiteralPath "$key\shell\open\command" -Name '(default)' -Value $cmd

    Show-Message ("Fertig. Ordner-Links aus PrintLog öffnen sich jetzt im Explorer.`n`n$rootInfo`n`nOrdner ändern: Einrichtung erneut starten.") 64
}

function Get-PathFromUrl([string]$Raw) {
    $s = $Raw.Trim()
    if ($s -notmatch "^$Scheme`:") { return $null }
    $s = $s.Substring($Scheme.Length + 1)
    # Manche Browser ergänzen "//" vorne oder "/" hinten.
    $s = $s.TrimStart('/').TrimEnd('/')
    $p = [Uri]::UnescapeDataString($s)   # UTF-8, also auch Umlaute und Leerzeichen
    $p = $p.Replace('/', '\')
    if ($p.Length -gt 3) { $p = $p.TrimEnd('\') }
    if ($p -match '[\x00-\x1f"<>|*?]') { return $null }
    # Nur absolute Pfade: Laufwerk (C:\...) oder Netzwerkfreigabe (\\server\freigabe\...).
    if ($p -notmatch '^[A-Za-z]:\\' -and $p -notmatch '^\\\\[^\\]+\\[^\\]+') { return $null }
    try { $p = [IO.Path]::GetFullPath($p) } catch { return $null }   # löst .. auf
    return $p
}

function Test-DirectOpen([string]$Path) {
    $ext = [IO.Path]::GetExtension($Path).ToLowerInvariant()
    if ($OpenExt -notcontains $ext) { return $false }
    if (-not (Test-Path -LiteralPath $RootsFile)) { return $false }
    foreach ($line in Get-Content -LiteralPath $RootsFile -Encoding UTF8) {
        $root = $line.Trim()
        if (-not $root) { continue }
        try { $root = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\' } catch { continue }
        if ($Path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

if (-not $Url) { Install-Handler $PSCommandPath; exit 0 }

$path = Get-PathFromUrl $Url
if (-not $path) {
    Show-Message "Ungültiger Ordner-Link:`n$Url"
    exit 1
}

$explorer = Join-Path $env:SystemRoot 'explorer.exe'
if (Test-Path -LiteralPath $path -PathType Container) {
    Start-Process -FilePath $explorer -ArgumentList ('"' + $path + '"')
} elseif (Test-Path -LiteralPath $path -PathType Leaf) {
    if (Test-DirectOpen $path) {
        Invoke-Item -LiteralPath $path   # Standardprogramm, z. B. Bambu Studio
    } else {
        Start-Process -FilePath $explorer -ArgumentList ('/select,"' + $path + '"')
    }
} else {
    Show-Message "Ordner nicht gefunden:`n$path"
    exit 1
}
