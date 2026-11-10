# User worker startup

The wheel ships the canonical templates under `tasklease/worker_templates`.
Export one with `tl worker template --platform systemd`, `launchd`, `windows`
or `windows-stop`. Replace every example path, `WORKER_LABEL`, `DOMAIN`,
`TOPIC`, and Windows `USER_ID` before installing it. Use an absolute `tl`
executable in the environment containing your handler plugin. Put credentials
in the configured token file; the templates contain no credentials.

Each worker on a machine needs a unique label. Configure a plugin in the
config file's `[worker]` table; the default handler is echo. Complete the
[operator and identity setup](../README.md#operator-setup) first. Local config
and token files should be private to your user. On macOS/Linux, run
`chmod 600 /path/to/config.toml /path/to/worker.token`; `tl` refuses token files
with group or other permission bits set. On Windows, use user-restricted ACLs.
Default config paths and absolute, relative and `~` token paths are documented
under [Client configuration](../README.md#client-configuration).

## Linux

Save the edited [unit](../python/src/tasklease/worker_templates/systemd.service)
as `~/.config/systemd/user/tasklease-worker.service`, then run:

```sh
systemctl --user daemon-reload
systemctl --user enable --now tasklease-worker.service
systemctl --user status tasklease-worker.service
```

Quote arguments containing spaces using systemd's double-quoted argument
syntax; escape literal backslashes and quotes. Double any literal `%` as
`%%`. The `:` command prefix disables environment-variable substitution.
`systemctl --user stop tasklease-worker.service` sends SIGHUP and waits for
active work to finish. `KillMode=mixed` sends the initial signal only to the
worker, so handler subprocesses can finish. A second signal can end an uncooperative worker:
`systemctl --user kill --kill-whom=main --signal=SIGHUP tasklease-worker.service`.
The [systemd service reference](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
defines restart and stop-timeout behavior.

## macOS

Save the edited [plist](../python/src/tasklease/worker_templates/launchd.plist)
as `~/Library/LaunchAgents/dev.tasklease.WORKER_LABEL.plist`. Escape XML text
such as `&` as `&amp;`; arguments are separate array elements, with no shell
quoting or tilde expansion. Check and load it:

```sh
plutil -lint ~/Library/LaunchAgents/dev.tasklease.WORKER_LABEL.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/dev.tasklease.WORKER_LABEL.plist
```

Run `tl --config /absolute/path/to/config.toml --worker WORKER_LABEL worker stop`
to finish the current task. Successful exit does not restart the worker;
`launchctl kickstart gui/UID/dev.tasklease.WORKER_LABEL` starts it again.
Use `launchctl bootout gui/UID/dev.tasklease.WORKER_LABEL` to unload it.
Unload only after the graceful stop has completed when work must finish.
Unloading, logging out or shutting down permits 60 seconds before forced
termination; the lease recovers unfinished work. This bounded timeout follows
the installed `launchd.plist(5)` guidance against an infinite shutdown wait.
See Apple's [launch agent guide](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).

## Windows

Save and edit the [scheduled task](../python/src/tasklease/worker_templates/windows.xml).
Use your `DOMAIN\user` or user SID for `USER_ID`. XML escaping applies to
paths. Save the edited file as UTF-8. Its XML declaration deliberately omits
an encoding attribute so the same definition also works as the Unicode string
accepted by Task Scheduler's `XmlText` API and PowerShell's `-Xml` argument.
The task runs in the logged-in user's session, with a logon trigger,
failure restarts and no execution time limit. Import and start it in PowerShell:

```powershell
Register-ScheduledTask -TaskName 'tasklease-worker' -Xml (Get-Content -Raw -Encoding UTF8 .\worker.xml)
Start-ScheduledTask -TaskName 'tasklease-worker'
```

Edit and run [the stop script](../python/src/tasklease/worker_templates/windows-stop.ps1)
to finish active work. Single quotes inside PowerShell paths must be doubled.
Successful exit does not trigger a failure restart. For immediate termination,
`Stop-ScheduledTask -TaskName 'tasklease-worker'` interrupts active work; the
lease makes it available again. Do not use immediate termination for a normal
graceful stop. Remove the task with `Unregister-ScheduledTask` when finished.
Microsoft documents [task XML](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema),
[failure restarts](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-restartonfailure-settingstype-element)
and [unlimited run time](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-executiontimelimit-settingstype-element).
