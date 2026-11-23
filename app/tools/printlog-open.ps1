# PrintLog: Ordner-Links (printlog-open:...) im Windows-Explorer öffnen.
#
# Einrichten: Rechtsklick auf diese Datei -> "Mit PowerShell ausführen".
# Das Skript kopiert sich nach %LOCALAPPDATA%\printlog und meldet das
# Link-Protokoll "printlog-open" für deinen Benutzer an (kein Admin nötig).
#
# Danach ruft der Browser dieses Skript mit dem angeklickten Link auf.
# Es dekodiert den Pfad und öffnet ihn nur im Explorer: einen Ordner direkt,
# bei einer Datei den Ordner mit markierter Datei. Es führt nie etwas aus.
#
# Entfernen: printlog-open-entfernen.reg aus den PrintLog-Einstellungen öffnen.

param([string]$Url)

$ErrorActionPreference = 'Stop'
$Scheme = 'printlog-open'

function Show-Message([string]$Text, [int]$Icon = 48) {
    try { (New-Object -ComObject WScript.Shell).Popup($Text, 0, 'PrintLog', $Icon) | Out-Null }
    catch { Write-Host $Text }
}

function Install-Handler {
    $dir = Join-Path $env:LOCALAPPDATA 'printlog'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $target = Join-Path $dir 'printlog-open.ps1'
    if ($PSCommandPath -ne $target) {
        Copy-Item -LiteralPath $PSCommandPath -Destination $target -Force
    }
    Unblock-File -LiteralPath $target -ErrorAction SilentlyContinue

    # Immer Windows PowerShell 5.1, auch wenn die Einrichtung aus PowerShell 7 läuft.
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $cmd = '"' + $ps + '" -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $target + '" "%1"'

    $key = "HKCU:\Software\Classes\$Scheme"
    New-Item -Path "$key\shell\open\command" -Force | Out-Null
    Set-ItemProperty -LiteralPath $key -Name '(default)' -Value 'URL:PrintLog Ordner öffnen'
    Set-ItemProperty -LiteralPath $key -Name 'URL Protocol' -Value ''
    Set-ItemProperty -LiteralPath "$key\shell\open\command" -Name '(default)' -Value $cmd

    Show-Message ("Fertig. Ordner-Links aus PrintLog öffnen sich jetzt im Explorer.`n`nSkript: $target") 64
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
    return $p
}

if (-not $Url) { Install-Handler; exit 0 }

$path = Get-PathFromUrl $Url
if (-not $path) {
    Show-Message "Ungültiger Ordner-Link:`n$Url"
    exit 1
}

$explorer = Join-Path $env:SystemRoot 'explorer.exe'
if (Test-Path -LiteralPath $path -PathType Container) {
    Start-Process -FilePath $explorer -ArgumentList ('"' + $path + '"')
} elseif (Test-Path -LiteralPath $path -PathType Leaf) {
    Start-Process -FilePath $explorer -ArgumentList ('/select,"' + $path + '"')
} else {
    Show-Message "Ordner nicht gefunden:`n$path"
    exit 1
}
