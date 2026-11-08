$ErrorActionPreference = 'Stop'
# Replace the same executable, config and label used by the scheduled task.
& 'C:\absolute\path\to\tl.exe' --config 'C:\absolute\path\to\config.toml' --worker 'WORKER_LABEL' worker stop
exit $LASTEXITCODE
