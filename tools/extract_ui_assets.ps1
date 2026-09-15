<#
  extract_ui_assets.ps1 - pull CS2 UI images out of pak_01_dir.vpk with
  Source2Viewer-CLI into static/assets/.
  PowerShell mirror of tools/extract_ui_assets.sh - keep the two in lockstep.

    csgo/pak_01_dir.vpk -> Source2Viewer-CLI -> panorama/images/{...}
                        -> flatten -> assetindex.py (tint + index) ->
    static/assets/{skillgroups,map_icons,overheadmaps,equipment,deathnotice,premier}/ + manifest.json

  Outputs are Valve content (static/assets/ is gitignored) - the directory's
  presence is the feature switch. Run once per game update.

  Usage:
    extract_ui_assets.ps1 [kinds...] [-List] [-Manifest] [-Help]
      kinds     : any of  skillgroups map_icons overheadmaps equipment deathnotice premier  (default: all)
      -Manifest : skip extraction, just re-tint + rebuild manifest.json (needs Python)
      -List     : list the kinds and their VPK subtrees, then exit

  Config (env var wins over the analysis_data file):
    CS2_GAME_DIR / analysis_data/cs2_game_dir           CS2 install dir (has game\csgo\pak01_dir.vpk)
    CS2_PAK_VPK  (optional)                             full path to the .vpk, bypassing the search
    SOURCE2VIEWER_CLI / analysis_data/source2viewer_cli path to Source2Viewer-CLI
#>
[CmdletBinding()]
param(
  [Parameter(ValueFromRemainingArguments = $true)] [string[]] $Kinds,
  [switch] $List,
  [switch] $Manifest,
  [switch] $Help
)
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here
# CS2VIEWER_ASSETS_DIR is paths.ASSETS_DIR, passed by app.py - the frozen
# desktop build keeps assets under the user's data dir, not static\.
$Out  = if ($env:CS2VIEWER_ASSETS_DIR) { $env:CS2VIEWER_ASSETS_DIR } else { Join-Path $Root 'static\assets' }
# Scratch goes to TEMP, like the .sh's mktemp - never into the install tree.
$Work = Join-Path ([IO.Path]::GetTempPath()) 'cs2viewer_ui_assets'

$Subtree = [ordered]@{
  skillgroups  = 'panorama/images/icons/skillgroups'
  map_icons    = 'panorama/images/map_icons'
  overheadmaps = 'panorama/images/overheadmaps'
  equipment    = 'panorama/images/icons/equipment'
  deathnotice  = 'panorama/images/hud/deathnotice'
  premier      = 'panorama/images/icons/ui'
}

# deathnotice: the 8 killfeed condition icons - extracted by exact path like
# skillgroups, since the folder also holds unrelated hud art.
$DeathnoticeFiles = @('blind_kill','icon_headshot','icon_suicide','inairkill','noscope','penetrate','smoke_kill','smokegrenade_impact')

# premier: the Premier rating banner. Extracted by exact path - icons/ui is a
# large grab-bag folder. The game ships one grey banner and wash-colours it per
# rating tier at runtime; the grey files are copied as-is and assetindex.py
# flattens that multiply into the seven tier files the manifest indexes. See
# that module for the colours and the why.
$PremierFiles = @('premier_rating_bg_large','premier_rating_bg_large_none')

if ($Help) {
  Get-Help $MyInvocation.MyCommand.Path -Detailed
  return
}
if ($List) {
  Write-Host 'UI asset kinds (VPK subtree inside pak_01_dir.vpk -> static/assets/<kind>/):'
  foreach ($k in $Subtree.Keys) { Write-Host ("  " + ('{0,-13} {1}' -f $k, $Subtree[$k])) }
  return
}

# Env var wins, else the analysis_data file (honors CS2VIEWER_DATA_DIR + data/ layout).
function Get-Setting([string]$EnvName, [string]$FileName) {
  $v = [Environment]::GetEnvironmentVariable($EnvName)
  if ($v) { return $v }
  $ddir = if ($env:CS2VIEWER_DATA_DIR) { $env:CS2VIEWER_DATA_DIR } else { Join-Path $Root 'data' }
  foreach ($c in @((Join-Path $ddir "analysis_data\$FileName"),
                   (Join-Path $Root "data\analysis_data\$FileName"),
                   (Join-Path $Root "analysis_data\$FileName"))) {
    if (Test-Path $c) { return (Get-Content -Raw $c).Trim() }
  }
  return $null
}

# Tinting the Premier banner and writing manifest.json are assetindex.py's job.
# The app runs it in-process and sets CS2VIEWER_SKIP_INDEX - an installed app
# has no python for this script to call. Run by hand, it is called from here.
function Invoke-Manifest {
  if ($env:CS2VIEWER_SKIP_INDEX) { Write-Host '==> extraction done; the app tints + indexes'; return }
  Write-Host '==> tinting + rebuilding manifest...'
  $py = Get-Command python, py, python3 -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $py) { throw "python not found - finish with 'Rebuild index only' on the app's Settings page." }
  & $py.Source (Join-Path $Root 'assetindex.py') $Out
  if ($LASTEXITCODE -ne 0) { throw "indexing failed (exit $LASTEXITCODE)" }
}

if ($Manifest) { Invoke-Manifest; return }

# Selected kinds (default: all); validate names.
$selected = if ($Kinds) { $Kinds } else { @($Subtree.Keys) }
foreach ($k in $selected) {
  if (-not $Subtree.Contains($k)) { throw "unknown kind: $k (want: $($Subtree.Keys -join ' '))" }
}

# Resolve Source2Viewer-CLI.
$cli = Get-Setting 'SOURCE2VIEWER_CLI' 'source2viewer_cli'
if (-not $cli) {
  $gui = Get-Setting 'SOURCE2VIEWER_PATH' 'source2viewer_path'
  if ($gui) {
    foreach ($c in @((Join-Path (Split-Path $gui) 'Source2Viewer-CLI.exe'),
                     (Join-Path (Split-Path $gui) 'Source2Viewer-CLI'))) {
      if (Test-Path $c) { $cli = $c; break }
    }
  }
}
if (-not $cli) { $g = Get-Command 'Source2Viewer-CLI' -ErrorAction SilentlyContinue; if ($g) { $cli = $g.Source } }
if (-not $cli) {
  Write-Error @'
Source2Viewer-CLI not found (a SEPARATE download from the GUI).
  1. Get cli-<os>-x64.zip from https://github.com/ValveResourceFormat/ValveResourceFormat/releases
  2. Point this script at it:
     echo C:\path\to\Source2Viewer-CLI.exe > data\analysis_data\source2viewer_cli
     - or -  $env:SOURCE2VIEWER_CLI = 'C:\path\to\Source2Viewer-CLI.exe'
'@
  exit 1
}

# Resolve the main content VPK. CS2 names it pak01_dir.vpk (no underscore after
# "pak") under game\csgo\; older layouts used csgo\. Try known names in the usual
# subdirs, then glob pak*_dir.vpk. $env:CS2_PAK_VPK overrides everything.
$vpk = $env:CS2_PAK_VPK
if (-not $vpk) {
  $cs2 = Get-Setting 'CS2_GAME_DIR' 'cs2_game_dir'
  if (-not $cs2) { throw 'CS2 game dir not configured ($CS2_GAME_DIR or data\analysis_data\cs2_game_dir)' }
  foreach ($sub in @('game\csgo', 'csgo', '.')) {
    $dir = Join-Path $cs2 $sub
    if (-not (Test-Path $dir)) { continue }
    foreach ($n in @('pak01_dir.vpk', 'pak_01_dir.vpk')) {
      $p = Join-Path $dir $n
      if (Test-Path $p) { $vpk = $p; break }
    }
    if ($vpk) { break }
    $g = Get-ChildItem -Path $dir -Filter 'pak*_dir.vpk' -File -ErrorAction SilentlyContinue | Sort-Object Name
    if ($g) { $vpk = $g[0].FullName; break }
  }
  if (-not $vpk) { throw "no pak*_dir.vpk found under $cs2 (looked in game\csgo, csgo). Set `$env:CS2_PAK_VPK to the file." }
}
if (-not (Test-Path $vpk)) { throw "VPK not found: $vpk" }
Write-Host "==> VPK: $vpk"
Write-Host "==> CLI: $cli"

$imgExt = @('.png', '.jpg', '.jpeg', '.webp', '.svg')
foreach ($kind in $selected) {
  $raw = Join-Path $Work $kind
  $dst = Join-Path $Out  $kind
  # Clear BOTH work and destination so a prior run's files can't linger and get
  # re-indexed (e.g. dangerzone*/wingman* art from an earlier whole-subtree run).
  if (Test-Path $raw) { Remove-Item -Recurse -Force $raw }
  if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
  New-Item -ItemType Directory -Force -Path $raw, $dst | Out-Null
  if ($kind -eq 'skillgroups') {
    # Only the 18 competitive/wingman rank icons matter: skillgroup1..18. They're
    # compiled panorama SVG (.vsvg_c -> .svg). Extract each by exact path so no
    # extra variants (skillgroup0, wingman/premier art, animated) leak in.
    Write-Host '==> [skillgroups] extracting skillgroup1..18.vsvg_c ...'
    foreach ($n in 1..18) {
      & $cli --input $vpk --output $raw --vpk_decompile `
             --vpk_filepath "panorama/images/icons/skillgroups/skillgroup$n.vsvg_c" *> $null
    }
  } elseif ($kind -eq 'premier') {
    Write-Host '==> [premier] extracting the Premier rating banner ...'
    foreach ($n in $PremierFiles) {
      & $cli --input $vpk --output $raw --vpk_decompile `
             --vpk_filepath "panorama/images/icons/ui/$n.vsvg_c" *> $null
    }
  } elseif ($kind -eq 'deathnotice') {
    Write-Host "==> [deathnotice] extracting $($DeathnoticeFiles.Count) killfeed condition icons ..."
    foreach ($n in $DeathnoticeFiles) {
      & $cli --input $vpk --output $raw --vpk_decompile `
             --vpk_filepath "panorama/images/hud/deathnotice/$n.vsvg_c" *> $null
    }
  } else {
    Write-Host "==> [$kind] extracting $($Subtree[$kind]) ..."
    & $cli --input $vpk --output $raw --vpk_decompile --vpk_filepath $Subtree[$kind] *> $null
    if ($LASTEXITCODE -ne 0) { Write-Warning "extraction returned non-zero for $kind (subtree may be absent in this build)" }
  }
  if ($kind -eq 'premier') {
    # The grey source art is copied as-is; assetindex.py tints it into the
    # seven tier banners and never indexes the grey files themselves.
    $found = 0
    foreach ($n in $PremierFiles) {
      $f = Get-ChildItem -Recurse -File $raw -Filter "$n.svg" | Select-Object -First 1
      if (-not $f) { continue }
      Copy-Item -Force $f.FullName (Join-Path $dst "$n.svg"); $found++
      Write-Host "      + $n.svg"
    }
    if (-not $found) { Write-Warning 'premier banner not found in this build - skipping' }
    continue
  }
  # map_icons keeps only the map_icon_* files (the dir also holds screenshots /
  # other art in subfolders); other kinds take every image.
  $found = 0
  Get-ChildItem -Recurse -File $raw |
    Where-Object { $imgExt -contains $_.Extension.ToLower() } |
    Where-Object { $kind -ne 'map_icons' -or $_.Name -like 'map_icon_*' } |
    ForEach-Object {
      Copy-Item -Force $_.FullName (Join-Path $dst $_.Name); $found++
      Write-Host "      + $($_.Name)"
    }
  Write-Host "    $found image(s) -> $dst"
}

Invoke-Manifest
Write-Host ''
Write-Host "done: $Out"
Write-Host 'Verify in the app: reload /match - skill-group icons replace the CSS chips when present.'
