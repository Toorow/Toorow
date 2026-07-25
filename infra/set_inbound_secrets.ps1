<#
.SYNOPSIS
  Pose les 3 secrets inbound (Mailgun) dans GCP Secret Manager depuis .env.

.DESCRIPTION
  Lit le .env a la racine du repo et cree/versionne, dans le projet GCP cible :
    - inbound-signing-secret  <- INBOUND_SIGNING_SECRET (HTTP webhook signing key Mailgun EU)
    - inbound-worker-secret   <- INBOUND_WORKER_SECRET (genere si vide, puis persiste dans .env)
    - platform-db-url         <- PLATFORM_DB_URL (deja present dans .env)

  Idempotent : cree le secret s'il n'existe pas, sinon ajoute une nouvelle version.
  Ne JAMAIS afficher les valeurs. Les valeurs sont ecrites via un fichier temporaire
  UTF-8 sans BOM et sans newline finale (sinon le secret serait corrompu par un \n).

.EXAMPLE
  pwsh infra/set_inbound_secrets.ps1
  pwsh infra/set_inbound_secrets.ps1 -Project toorow -RotateWorker
#>
[CmdletBinding()]
param(
    [string]$Project = "toorow",
    # Regenere le worker-secret meme s'il est deja rempli dans .env.
    [switch]$RotateWorker
)

$ErrorActionPreference = "Stop"

# --- localise et parse le .env (a la racine du repo, un cran au-dessus de infra/) ---
$envPath = Join-Path $PSScriptRoot "..\.env"
if (-not (Test-Path $envPath)) { throw ".env introuvable a $envPath" }

function Read-DotEnv([string]$path) {
    $map = @{}
    foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
        if ($line -match '^\s*#') { continue }
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $line.Substring(0, $idx).Trim()
        $val = $line.Substring($idx + 1)   # NE PAS trimmer : un DSN peut finir par un espace signifiant
        $map[$key] = $val
    }
    return $map
}

$envMap = Read-DotEnv $envPath

# --- verifie gcloud ---
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "gcloud introuvable dans le PATH. Installe le Google Cloud SDK, puis 'gcloud auth login'."
}

# --- worker-secret : genere (cryptographique) si vide ou -RotateWorker ---
$worker = $envMap["INBOUND_WORKER_SECRET"]
if ($RotateWorker -or [string]::IsNullOrWhiteSpace($worker)) {
    $bytes = New-Object byte[] 48
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $worker = [Convert]::ToBase64String($bytes)
    # Persiste dans .env (remplace la ligne INBOUND_WORKER_SECRET=... telle quelle).
    $content = Get-Content -LiteralPath $envPath -Raw -Encoding UTF8
    if ($content -match '(?m)^INBOUND_WORKER_SECRET=.*$') {
        $content = [regex]::Replace($content, '(?m)^INBOUND_WORKER_SECRET=.*$', "INBOUND_WORKER_SECRET=$worker")
    } else {
        $content = $content.TrimEnd("`r","`n") + "`nINBOUND_WORKER_SECRET=$worker`n"
    }
    [System.IO.File]::WriteAllText($envPath, $content, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "worker-secret : genere et ecrit dans .env." -ForegroundColor Green
}

# --- valide les entrees requises ---
# La signing key vit deja dans .env sous MAILGUN_WEBHOOK_SIGNING_KEY ; on prend
# INBOUND_SIGNING_SECRET en priorite (contrat runtime), sinon ce fallback.
$signing = $envMap["INBOUND_SIGNING_SECRET"]
if ([string]::IsNullOrWhiteSpace($signing)) { $signing = $envMap["MAILGUN_WEBHOOK_SIGNING_KEY"] }
$dbUrl   = $envMap["PLATFORM_DB_URL"]

$missing = @()
if ([string]::IsNullOrWhiteSpace($signing)) {
    $missing += "INBOUND_SIGNING_SECRET / MAILGUN_WEBHOOK_SIGNING_KEY (aucun rempli dans .env)"
}
if ([string]::IsNullOrWhiteSpace($dbUrl)) {
    $missing += "PLATFORM_DB_URL (DSN Postgres/Supabase, deja attendu dans .env)"
}
if ($missing.Count -gt 0) {
    throw "Valeurs manquantes dans .env :`n  - " + ($missing -join "`n  - ")
}

# --- pose un secret (cree si absent, sinon nouvelle version) sans fuiter la valeur ---
function Set-Secret([string]$name, [string]$value) {
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        # UTF-8 sans BOM, sans newline finale : le secret est la valeur brute exacte.
        [System.IO.File]::WriteAllText($tmp, $value, (New-Object System.Text.UTF8Encoding($false)))

        & gcloud secrets describe $name --project $Project *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  creation du secret '$name'..." -ForegroundColor DarkGray
            & gcloud secrets create $name --project $Project --replication-policy=automatic | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "echec 'gcloud secrets create $name'" }
        }
        & gcloud secrets versions add $name --project $Project --data-file="$tmp" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "echec 'gcloud secrets versions add $name'" }
        Write-Host "  OK  $name (nouvelle version posee)" -ForegroundColor Green
    }
    finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Projet GCP : $Project" -ForegroundColor Cyan
Set-Secret "inbound-signing-secret" $signing
Set-Secret "inbound-worker-secret"  $worker
Set-Secret "platform-db-url"        $dbUrl

Write-Host "`nTermine. Les 3 secrets sont poses dans Secret Manager (projet $Project)." -ForegroundColor Cyan
Write-Host "Rappel : 'terraform apply' cree les references de secrets ; ce script pose les VALEURS." -ForegroundColor DarkGray
