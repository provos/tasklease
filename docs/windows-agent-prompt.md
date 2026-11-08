# Windows acceptance agent prompt

Use the following prompt with the credential-free acceptance ZIP and its
producer-provided SHA-256. The ZIP contains the exact source snapshot, wheel
and integrity manifest; it needs no Git checkout, Node, Cloudflare login or
R2 credentials. Transfer the ZIP to Windows through your normal file-copy
channel. Do not upload repository source to a public service.

---

Run native Windows acceptance for the supplied tasklease candidate. Work in a
new directory under `%TEMP%` and leave existing checkouts, Python environments
and tasklease configuration untouched. Use the supplied ZIP path and SHA-256,
not a guessed branch or a fresh registry installation of tasklease.

Use Python 3.12 or newer and uv. If either is unavailable, report the missing
prerequisite. Keep package security and minimum-age checks enabled. The only
network access needed is normal installation of Python/test dependencies;
there must be no Cloudflare deployment, credential access or production task
traffic. Never print environment variables, token files or `.env` files.

In PowerShell, replace the first two values with the provided inputs:

```powershell
$ErrorActionPreference = 'Stop'
$zip = (Resolve-Path 'C:\path\to\tasklease-acceptance.zip').Path
$expectedSha256 = 'PRODUCER_PROVIDED_SHA256'
$actualSha256 = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSha256 -ne $expectedSha256.ToLowerInvariant()) { throw 'Candidate ZIP hash mismatch' }

$base = Join-Path $env:TEMP ('tasklease-native-' + [guid]::NewGuid().ToString('N'))
$bundle = Join-Path $base 'bundle'
New-Item -ItemType Directory -Path $bundle | Out-Null
Expand-Archive -LiteralPath $zip -DestinationPath $bundle
$manifest = Get-Content -LiteralPath (Join-Path $bundle 'acceptance-manifest.json') -Raw | ConvertFrom-Json
$wheel = Join-Path $bundle $manifest.wheel
$venv = Join-Path $base 'venv'
uv venv --python 3.12 $venv
if ($LASTEXITCODE -ne 0) { throw 'Could not create the acceptance environment' }
$python = Join-Path $venv 'Scripts\python.exe'
$cutoff = [DateTime]::UtcNow.AddDays(-7).ToString('yyyy-MM-ddTHH:mm:ssZ')
uv pip install --python $python --exclude-newer $cutoff $wheel 'pytest>=8,<10' 'jsonschema>=4,<5'
if ($LASTEXITCODE -ne 0) { throw 'Could not install the acceptance dependencies' }

$report = Join-Path $base 'native-report.json'
& $python (Join-Path $bundle 'source\scripts\native_acceptance.py') --bundle-root $bundle --archive $zip --expected-sha256 $expectedSha256 --report $report
$acceptanceExit = $LASTEXITCODE
Get-Content -LiteralPath $report -Raw
Write-Output ('Acceptance exit code: ' + $acceptanceExit)
```

The harness binds every declared file to this exact ZIP, checks a clean installed wheel
outside the source directory with Node absent from `PATH`, and runs the full
Python runtime suite. It excludes exactly the source-build and pinned-Wrangler
deployment suites, which require Node and run separately on Linux. It records
JUnit counts and every failed/skipped case, plus complete probe and pytest
logs (including the uv version probe) beside the JSON report. Do not change these exclusions or weaken tests
to obtain a passing result.

Also check the packaged Windows task XML using the local Task Scheduler COM
parser, without registering or starting a persistent task:

```powershell
$taskXml = & $python -I -m tasklease worker template --platform windows
if ($LASTEXITCODE -ne 0) { throw 'Could not export the Windows template' }
$account = [Security.SecurityElement]::Escape([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$definition = $service.NewTask(0)
$definition.XmlText = (($taskXml -join "`n").Replace('USER_ID', $account))
Write-Output 'Native Task Scheduler XML parsing passed; no task was registered.'
```

Microsoft documents this read/write
[TaskDefinition.XmlText property](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskdefinition-xmltext).
If the local Task Scheduler service is unavailable, report that check as
unverified rather than silently omitting it.

Return the JSON report and a short result containing:

- Windows version/build and architecture; Python and uv versions.
- ZIP SHA-256, source commit, source-tree hash, wheel hash and whether the
  producer explicitly supplied a dirty candidate.
- Exact commands, installed-wheel/no-Node probe result, passed/failed/skipped
  counts and all skip reasons.
- Any failure's full traceback and the relevant complete log section, with
  file/line references and a likely cause. Preserve the report and logs.
- Whether native Task Scheduler XML parsing succeeded. No task should have
  been registered and no worker should be left running.

Pay particular attention to actual Windows file publication/no-replace races,
`CON` and `Foo`/`foo` request identifiers, corrupt/duplicate replies, worker
deadlines and stop files, concurrent monitor connections, Host/path guards,
cache reuse and packaged resources. These are exercised by the runtime suite;
do not replace them with Linux-style mocks. POSIX-only signal skips on Windows
are expected and must be listed.

If a product failure is found, report it before altering the candidate. A
proposed focused patch is useful, but a modified candidate needs a fresh
producer hash and another run; do not claim that the original candidate passed.
