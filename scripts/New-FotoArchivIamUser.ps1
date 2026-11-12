<#
.SYNOPSIS
  Legt den IAM-Benutzer fuer foto-archiv an (ohne Loeschrechte) und hinterlegt
  dessen Zugangsschluessel als AWS-CLI-Profil "foto-archiv".

.DESCRIPTION
  Rechte des Benutzers (nur auf den angegebenen Bucket):
    erlaubt : ListBucket, GetBucketLocation, GetBucketVersioning, ListBucketMultipartUploads,
              PutObject, GetObject, GetObjectAttributes, RestoreObject,
              AbortMultipartUpload, ListMultipartUploadParts
    verboten: DeleteObject(Version), DeleteBucket, Bucket-Policy/Lifecycle/Versioning aendern,
              Object-Lock-Retention aendern/umgehen (explizites Deny)

  Hinweis: PutObject kann ein bestehendes Objekt ueberschreiben. Das Tool verhindert das
  selbst (bedingter Upload mit If-None-Match), serverseitig schuetzt erst die Bucket-
  Versionierung (-EnableVersioning): dann bleibt jede alte Fassung erhalten und der
  Tool-Benutzer kann keine Versionen loeschen.

  Ausgefuehrt wird mit einem Admin-Profil (-AdminProfile), das IAM-Rechte besitzt.

.EXAMPLE
  .\scripts\New-FotoArchivIamUser.ps1 -BucketName mein-foto-bucket -EnableVersioning

.EXAMPLE
  .\scripts\New-FotoArchivIamUser.ps1 -BucketName mein-foto-bucket -AdminProfile admin

.EXAMPLE
  .\scripts\New-FotoArchivIamUser.ps1 -BucketName mein-foto-bucket -RotateKey
  # neuen Schluessel erzeugen, Profil aktualisieren, alte Schluessel loeschen

.EXAMPLE
  .\scripts\New-FotoArchivIamUser.ps1 -Remove
  # Schluessel loeschen und Profil "foto-archiv" aus ~/.aws entfernen (Benutzer bleibt)

.EXAMPLE
  .\scripts\New-FotoArchivIamUser.ps1 -Remove -DeleteUser
#>
[CmdletBinding(DefaultParameterSetName = 'Create')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Create')]
    [string]$BucketName,

    [string]$UserName = 'foto-archiv-tool',
    [string]$AdminProfile = 'default',
    [string]$ToolProfile = 'foto-archiv',
    [string]$Region = 'eu-west-1',

    [Parameter(ParameterSetName = 'Create')]
    [switch]$EnableVersioning,

    [Parameter(ParameterSetName = 'Create')]
    [switch]$RotateKey,

    [Parameter(Mandatory, ParameterSetName = 'Remove')]
    [switch]$Remove,

    [Parameter(ParameterSetName = 'Remove')]
    [switch]$DeleteUser
)

Set-StrictMode -Version Latest
$PolicyName = 'foto-archiv-s3'

function Invoke-Aws {
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$AwsProfile = $AdminProfile,
        [switch]$AllowFail,
        [switch]$NoJson
    )
    # PS 5.1: stderr nativer Programme nicht als Fehler werten, Exitcode selbst pruefen
    $eap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $extra = @('--profile', $AwsProfile)
        if (-not $NoJson) { $extra += @('--output', 'json') }
        $raw = & aws @Arguments @extra 2>&1
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $eap }

    $stdout = ($raw | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n"
    $stderr = ($raw | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } |
        ForEach-Object { $_.ToString() }) -join "`n"

    if ($code -ne 0) {
        if ($AllowFail) { return [pscustomobject]@{ Ok = $false; Error = $stderr; Json = $null } }
        throw "aws $($Arguments -join ' ') fehlgeschlagen (Exit $code):`n$stderr"
    }
    $json = $null
    if (-not $NoJson -and $stdout.Trim()) { $json = $stdout | ConvertFrom-Json }
    [pscustomobject]@{ Ok = $true; Error = $null; Json = $json; Text = $stdout }
}

function Remove-IniSection {
    param([string]$Path, [string]$Section)
    if (-not (Test-Path $Path)) { return $false }
    $lines = Get-Content -LiteralPath $Path
    $out = New-Object System.Collections.Generic.List[string]
    $skip = $false; $found = $false
    foreach ($l in $lines) {
        if ($l -match '^\s*\[(.+)\]\s*$') {
            $skip = ($Matches[1].Trim() -eq $Section)
            if ($skip) { $found = $true }
        }
        if (-not $skip) { $out.Add($l) }
    }
    if ($found) {
        [IO.File]::WriteAllLines($Path, $out, (New-Object Text.UTF8Encoding $false))
    }
    return $found
}

$awsDir = Join-Path $env:USERPROFILE '.aws'
$credFile = Join-Path $awsDir 'credentials'
$confFile = Join-Path $awsDir 'config'

# ---------------------------------------------------------------- Admin-Identitaet
$id = Invoke-Aws -Arguments @('sts', 'get-caller-identity')
Write-Host "Admin-Profil '$AdminProfile': $($id.Json.Arn)" -ForegroundColor Cyan

# ================================================================= REMOVE
if ($PSCmdlet.ParameterSetName -eq 'Remove') {
    $u = Invoke-Aws -Arguments @('iam', 'get-user', '--user-name', $UserName) -AllowFail
    if ($u.Ok) {
        $keys = (Invoke-Aws -Arguments @('iam', 'list-access-keys', '--user-name', $UserName)).Json.AccessKeyMetadata
        foreach ($k in @($keys)) {
            if ($null -eq $k) { continue }
            Invoke-Aws -Arguments @('iam', 'delete-access-key', '--user-name', $UserName, '--access-key-id', $k.AccessKeyId) | Out-Null
            Write-Host "Zugangsschluessel $($k.AccessKeyId) geloescht."
        }
        if ($DeleteUser) {
            Invoke-Aws -Arguments @('iam', 'delete-user-policy', '--user-name', $UserName, '--policy-name', $PolicyName) -AllowFail | Out-Null
            Invoke-Aws -Arguments @('iam', 'delete-user', '--user-name', $UserName) | Out-Null
            Write-Host "IAM-Benutzer $UserName geloescht."
        }
    }
    else { Write-Host "IAM-Benutzer $UserName existiert nicht." }

    if (Remove-IniSection -Path $credFile -Section $ToolProfile) { Write-Host "Profil [$ToolProfile] aus $credFile entfernt." }
    if (Remove-IniSection -Path $confFile -Section "profile $ToolProfile") { Write-Host "Profil [profile $ToolProfile] aus $confFile entfernt." }
    Write-Host 'Fertig.' -ForegroundColor Green
    return
}

# ================================================================= CREATE
# ---------------------------------------------------------------- Bucket pruefen
$hb = Invoke-Aws -Arguments @('s3api', 'head-bucket', '--bucket', $BucketName) -AllowFail
if (-not $hb.Ok) {
    if ($hb.Error -match '404|Not Found|NoSuchBucket') { throw "Bucket '$BucketName' existiert nicht." }
    Write-Warning "head-bucket nicht moeglich (evtl. fehlende Rechte des Admin-Profils): $($hb.Error)"
}
else {
    $hbRegion = $hb.Json.BucketRegion
    if ($hbRegion -and $hbRegion -ne $Region) {
        Write-Warning "Bucket liegt in $hbRegion, nicht in $Region. Verwende $hbRegion fuer das Profil."
        $Region = $hbRegion
    }
}

# ---------------------------------------------------------------- Versionierung
$ver = Invoke-Aws -Arguments @('s3api', 'get-bucket-versioning', '--bucket', $BucketName) -AllowFail
$verStatus = if ($ver.Ok -and $ver.Json -and ($ver.Json.PSObject.Properties.Name -contains 'Status')) { $ver.Json.Status } else { 'nie aktiviert' }
if (-not $ver.Ok) { $verStatus = 'unbekannt' }
Write-Host "Bucket-Versionierung: $verStatus"
if ($verStatus -ne 'Enabled') {
    if ($EnableVersioning) {
        Invoke-Aws -Arguments @('s3api', 'put-bucket-versioning', '--bucket', $BucketName,
            '--versioning-configuration', 'Status=Enabled') -NoJson | Out-Null
        Write-Host 'Versionierung aktiviert.' -ForegroundColor Green
    }
    else {
        Write-Warning ('Versionierung ist nicht aktiv: ein PutObject auf einen bestehenden Key ' +
            'wuerde die alte Fassung ersetzen. Empfehlung: Skript mit -EnableVersioning ausfuehren.')
    }
}

# ---------------------------------------------------------------- Benutzer
$u = Invoke-Aws -Arguments @('iam', 'get-user', '--user-name', $UserName) -AllowFail
if (-not $u.Ok) {
    Invoke-Aws -Arguments @('iam', 'create-user', '--user-name', $UserName,
        '--tags', 'Key=app,Value=foto-archiv') | Out-Null
    Write-Host "IAM-Benutzer $UserName angelegt." -ForegroundColor Green
}
else { Write-Host "IAM-Benutzer $UserName existiert bereits." }

# ---------------------------------------------------------------- Policy
$bucketArn = "arn:aws:s3:::$BucketName"
$policy = [ordered]@{
    Version   = '2012-10-17'
    Statement = @(
        [ordered]@{
            Sid      = 'BucketRead'
            Effect   = 'Allow'
            Action   = @('s3:ListBucket', 's3:GetBucketLocation', 's3:GetBucketVersioning',
                's3:ListBucketMultipartUploads')
            Resource = $bucketArn
        },
        [ordered]@{
            Sid      = 'ObjectWriteReadRestore'
            Effect   = 'Allow'
            Action   = @('s3:PutObject', 's3:GetObject', 's3:GetObjectAttributes', 's3:RestoreObject',
                's3:AbortMultipartUpload', 's3:ListMultipartUploadParts')
            Resource = "$bucketArn/*"
        },
        [ordered]@{
            Sid      = 'DenyAnyDeletion'
            Effect   = 'Deny'
            Action   = @('s3:DeleteObject', 's3:DeleteObjectVersion', 's3:DeleteBucket',
                's3:PutBucketVersioning', 's3:PutLifecycleConfiguration', 's3:PutBucketPolicy',
                's3:DeleteBucketPolicy', 's3:PutObjectRetention', 's3:BypassGovernanceRetention',
                's3:PutObjectLegalHold', 's3:PutBucketObjectLockConfiguration')
            Resource = @($bucketArn, "$bucketArn/*")
        }
    )
}
$policyFile = Join-Path ([IO.Path]::GetTempPath()) "foto-archiv-policy-$PID.json"
[IO.File]::WriteAllText($policyFile, ($policy | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding $false))
try {
    Invoke-Aws -Arguments @('iam', 'put-user-policy', '--user-name', $UserName, '--policy-name', $PolicyName,
        '--policy-document', "file://$policyFile") -NoJson | Out-Null
}
finally { Remove-Item -LiteralPath $policyFile -ErrorAction SilentlyContinue }
Write-Host "Inline-Policy '$PolicyName' gesetzt (Bucket $BucketName, ohne Loeschrechte)." -ForegroundColor Green

# ---------------------------------------------------------------- Zugangsschluessel
$keys = @((Invoke-Aws -Arguments @('iam', 'list-access-keys', '--user-name', $UserName)).Json.AccessKeyMetadata | Where-Object { $_ })
$eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
$profileKey = (& aws configure get aws_access_key_id --profile $ToolProfile 2>$null)
$ErrorActionPreference = $eap
$profileHasValidKey = $profileKey -and ($keys | Where-Object { $_.AccessKeyId -eq $profileKey })

if ($profileHasValidKey -and -not $RotateKey) {
    Write-Host "Profil '$ToolProfile' nutzt bereits Schluessel $profileKey - keine Aenderung (neu erzeugen: -RotateKey)."
}
else {
    $oldKeys = @($keys)
    if ($oldKeys.Count -ge 2) {
        throw "Benutzer hat bereits 2 Zugangsschluessel (AWS-Limit). Erst mit -Remove aufraeumen."
    }
    $nk = (Invoke-Aws -Arguments @('iam', 'create-access-key', '--user-name', $UserName)).Json.AccessKey
    & aws configure set aws_access_key_id $nk.AccessKeyId --profile $ToolProfile
    & aws configure set aws_secret_access_key $nk.SecretAccessKey --profile $ToolProfile
    & aws configure set region $Region --profile $ToolProfile
    if ($LASTEXITCODE -ne 0) { throw 'aws configure set fehlgeschlagen.' }
    $nk = $null
    Write-Host "Neuer Schluessel im Profil '$ToolProfile' hinterlegt ($credFile)." -ForegroundColor Green
    foreach ($k in $oldKeys) {
        Invoke-Aws -Arguments @('iam', 'delete-access-key', '--user-name', $UserName, '--access-key-id', $k.AccessKeyId) | Out-Null
        Write-Host "Alter Schluessel $($k.AccessKeyId) geloescht."
    }
}

# ---------------------------------------------------------------- Selbsttest
Write-Host 'Selbsttest (IAM braucht ggf. einige Sekunden bis die Rechte greifen) ...'
$ok = $false
for ($i = 0; $i -lt 12 -and -not $ok; $i++) {
    $t = Invoke-Aws -AwsProfile $ToolProfile -Arguments @('s3api', 'list-objects-v2', '--bucket', $BucketName, '--max-keys', '1') -AllowFail
    if ($t.Ok) { $ok = $true } else { Start-Sleep -Seconds 5 }
}
if (-not $ok) { Write-Warning "ListBucket mit Profil '$ToolProfile' schlug fehl: $($t.Error)" }
else { Write-Host '  ListBucket ............ erlaubt (ok)' -ForegroundColor Green }

# Loeschversuch auf einen garantiert nicht existierenden Key: harmlos, muss AccessDenied liefern
$probeKey = "_foto-archiv-permission-probe/does-not-exist-$([guid]::NewGuid())"
$d = Invoke-Aws -AwsProfile $ToolProfile -Arguments @('s3api', 'delete-object', '--bucket', $BucketName, '--key', $probeKey) -AllowFail
if (-not $d.Ok -and $d.Error -match 'AccessDenied') {
    Write-Host '  DeleteObject .......... verweigert (ok)' -ForegroundColor Green
}
else {
    Write-Warning "  DeleteObject wurde NICHT verweigert - Policy pruefen! $($d.Error)"
}

Write-Host ''
Write-Host "Fertig. Das Tool verwendet das AWS-Profil '$ToolProfile'." -ForegroundColor Cyan
Write-Host "Test:      aws s3 ls s3://$BucketName/ --profile $ToolProfile"
Write-Host "Entfernen: .\scripts\New-FotoArchivIamUser.ps1 -Remove [-DeleteUser] -AdminProfile $AdminProfile"
