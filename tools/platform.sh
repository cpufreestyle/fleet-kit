#!/usr/bin/env bash
# FleetKit platform abstraction.
#
# One service API, three backends:
#   macos   -> launchd plists in ~/Library/LaunchAgents
#   windows -> Task Scheduler tasks + a generated .cmd env wrapper
#   linux   -> detached self-restarting shell wrapper
#
# Source it (`source tools/platform.sh`) from install.sh, uninstall.sh,
# deploy.sh, bridges/finish.sh and every tools/*.sh script, then call only the
# fleet_* helpers so platform-specific bits live in exactly one place.
# shellcheck shell=bash

# FLEET_OS forces a backend (macos|windows|linux) and is read on every call so
# tests can flip it after sourcing this file.
fleet_os() {
  if [ -n "${FLEET_OS:-}" ]; then
    printf '%s\n' "$FLEET_OS"
    return 0
  fi
  case "${OSTYPE:-$(uname -s 2>/dev/null || true)}" in
    darwin*|Darwin*) echo macos; return 0 ;;
    msys*|MSYS*|MINGW*|cygwin*|CYGWIN*|Windows_NT*) echo windows; return 0 ;;
    linux*|Linux*) echo linux; return 0 ;;
  esac
  case "$(uname -s 2>/dev/null || true)" in
    Darwin*) echo macos ;;
    Linux*) echo linux ;;
    MINGW*|MSYS*|CYGWIN*|Windows_NT*) echo windows ;;
    *) echo unknown ;;
  esac
}

fleet_is_macos() { [ "$(fleet_os)" = "macos" ]; }
fleet_is_windows() { [ "$(fleet_os)" = "windows" ]; }
fleet_is_linux() { [ "$(fleet_os)" = "linux" ]; }

# ---------- directories ----------

fleet_service_dir() {
  if [ -n "${FLEET_SERVICE_DIR:-}" ]; then
    printf '%s\n' "$FLEET_SERVICE_DIR"
    return 0
  fi
  case "$(fleet_os)" in
    macos) printf '%s\n' "${HOME}/Library/LaunchAgents" ;;
    windows)
      if [ -n "${LOCALAPPDATA:-}" ]; then printf '%s\n' "${LOCALAPPDATA}/FleetKit/services"
      elif [ -n "${APPDATA:-}" ]; then printf '%s\n' "${APPDATA}/FleetKit/services"
      else printf '%s\n' "${HOME}/AppData/Local/FleetKit/services"; fi
      ;;
    *) printf '%s\n' "${XDG_DATA_HOME:-${HOME}/.local/share}/FleetKit/services" ;;
  esac
}

fleet_log_dir() {
  if [ -n "${FLEET_LOG_DIR:-}" ]; then
    printf '%s\n' "$FLEET_LOG_DIR"
    return 0
  fi
  case "$(fleet_os)" in
    windows) printf '%s\n' "${TEMP:-${TMP:-${LOCALAPPDATA:-/tmp}}}/fleet-logs" ;;
    *) printf '%s\n' "/tmp/fleet-logs" ;;
  esac
}

# ---------- interpreters ----------

fleet_system_python() {
  local candidate
  for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  printf 'python3\n'
}

# venv layout differs: posix uses .venv/bin/python, windows .venv/Scripts/python.exe
fleet_venv_python() {
  local root="$1"
  if fleet_is_windows; then
    if [ -x "${root}/.venv/Scripts/python.exe" ]; then printf '%s\n' "${root}/.venv/Scripts/python.exe"; return 0; fi
  else
    if [ -x "${root}/.venv/bin/python" ]; then printf '%s\n' "${root}/.venv/bin/python"; return 0; fi
    if [ -x "${root}/.venv/bin/python3" ]; then printf '%s\n' "${root}/.venv/bin/python3"; return 0; fi
  fi
  fleet_system_python
}

fleet_detect_path() {
  local value="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  local nodebin=""
  if fleet_is_macos; then
    [ -d /opt/homebrew/bin ] && value="/opt/homebrew/bin:${value}"
  fi
  if fleet_is_windows; then
    value="${PATH:-}"
    [ -n "${SYSTEMROOT:-}" ] && value="${SYSTEMROOT}/System32:${value}"
  else
    nodebin="$(command -v node || true)"
    [ -n "$nodebin" ] && value="$(dirname "$nodebin"):${value}"
  fi
  printf '%s\n' "$value"
}

# ---------- ports ----------

fleet_port_in_use_py() {
  local port="$1"
  local py
  py="$(fleet_system_python)"
  "$py" - "$port" <<'PY' >/dev/null 2>&1
import socket, sys
port = int(sys.argv[1])
s = socket.socket()
s.settimeout(0.4)
try:
    s.connect(("127.0.0.1", port))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

fleet_port_in_use() {
  local port="$1"
  if fleet_is_windows; then
    if command -v netstat >/dev/null 2>&1; then
      netstat -ano 2>/dev/null | grep -E "[.:]${port}[[:space:]]+.*LISTENING" >/dev/null 2>&1 && return 0
    fi
    fleet_port_in_use_py "$port"
    return $?
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  fi
  fleet_port_in_use_py "$port"
}

# ---------- windows helpers ----------

_fleet_schtasks() {
  if command -v schtasks >/dev/null 2>&1; then echo schtasks; return 0; fi
  if command -v schtasks.exe >/dev/null 2>&1; then echo schtasks.exe; return 0; fi
  for candidate in /c/Windows/System32/schtasks.exe /c/Windows/SysWOW64/schtasks.exe; do
    [ -x "$candidate" ] && { echo "$candidate"; return 0; }
  done
  echo schtasks
}

# cmd.exe wants % doubled inside a batch file
_fleet_cmd_escape() {
  printf '%s' "$1" | sed 's/%/%%/g'
}

# MSYS/bash paths (/c/..., /tmp/...) are meaningless to cmd.exe and Task
# Scheduler; convert them to Windows form before writing anything the
# Windows backend executes.
_fleet_win_path() {
  local p="$1"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w -- "$p" 2>/dev/null || printf '%s' "$p"
  else
    printf '%s' "$p"
  fi
}

# converts every absolute-path token of a string (arg lists) to Windows form
_fleet_win_tokens() {
  local s="$1" out="" tok
  for tok in $s; do
    case "$tok" in
      /*) tok="$(_fleet_win_path "$tok")" ;;
    esac
    out="${out}${out:+ }${tok}"
  done
  printf '%s' "$out"
}

# converts one env value for cmd.exe: PATH entries become Windows paths
# joined with ';' (CreateProcess cannot resolve MSYS entries); a value that
# is itself an absolute path (HOME, auth-pool dirs) is converted whole
_fleet_win_value() {
  local key="$1" v="$2" pout="" e
  if [ "$key" = "PATH" ]; then
    local oifs="$IFS"; IFS=':'
    for e in $v; do
      case "$e" in
        /*) e="$(_fleet_win_path "$e")" ;;
      esac
      pout="${pout}${pout:+;}${e}"
    done
    IFS="$oifs"
    printf '%s' "$pout"
    return 0
  fi
  case "$v" in
    /*) printf '%s' "$(_fleet_win_path "$v")" ;;
    *) printf '%s' "$v" ;;
  esac
}

# current user SID: schtasks rejects a bare LogonTrigger (no UserId) for
# non-elevated users, and an InteractiveToken principal needs it too
_fleet_user_sid() {
  local who="" sid=""
  if [ -n "${SYSTEMROOT:-}" ]; then
    who="$(cygpath -u "$SYSTEMROOT" 2>/dev/null || echo '')/System32/whoami.exe"
  fi
  [ -n "$who" ] && [ -x "$who" ] || who="/c/Windows/System32/whoami.exe"
  if [ -x "$who" ]; then
    sid="$("$who" //user 2>/dev/null | awk 'NF{last=$NF} END{print last}')"
  fi
  case "$sid" in
    S-1-*) printf '%s' "$sid" ;;
    *) printf '' ;;
  esac
}

# powershell.exe backs the windows service backend: Git Bash has no setsid, so
# long-running services are hosted by a detached hidden PowerShell supervisor
_fleet_win_powershell() {
  local cand=""
  if [ -n "${SYSTEMROOT:-}" ]; then
    cand="$(cygpath -u "$SYSTEMROOT" 2>/dev/null || echo '')/System32/WindowsPowerShell/v1.0/powershell.exe"
  fi
  if [ -n "$cand" ] && [ -x "$cand" ]; then printf '%s' "$cand"; return 0; fi
  command -v powershell.exe 2>/dev/null || command -v pwsh.exe 2>/dev/null || printf ''
}

# detach $2 so it outlives the shell that spawned it ($1 = powershell.exe).
# Start-Process resolves executables the way Windows does, so it needs the
# native path form, not the /c/... form bash uses to run powershell itself.
_fleet_win_spawn() {
  local ps="$1" target="$2" pswin
  [ -n "$ps" ] && [ -n "$target" ] || return 1
  pswin="$(_fleet_win_path "$ps")"
  "$ps" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden \
    -Command "Start-Process -FilePath '$pswin' -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File','$target' -WindowStyle Hidden" \
    >/dev/null 2>&1
}

_fleet_win_read_pid() {
  local file="$1"
  [ -f "$file" ] || return 0
  tr -dc '0-9' < "$file" 2>/dev/null || true
}

_fleet_win_pid_alive() {
  local pid="$1"
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  "$(_fleet_win_powershell)" -NoProfile -Command \
    "if (Get-Process -Id $pid -ErrorAction SilentlyContinue) { exit 0 }; exit 1" >/dev/null 2>&1
}

_fleet_win_kill_pid() {
  local pid="$1"
  case "$pid" in ''|*[!0-9]*) return 0 ;; esac
  taskkill //F //T //PID "$pid" >/dev/null 2>&1 || true
}

# Stop a windows service: kill the supervisor first (so it cannot restart the
# wrapper), then its recorded child. Any task left by an older task-scheduler
# based install is deleted so it cannot fire the wrapper again.
_fleet_win_stop_service() {
  local label="$1" dir pid ps pat
  dir="$(fleet_service_dir)"
  fleet_task_delete "$label" >/dev/null 2>&1 || true
  for pid in "$(_fleet_win_read_pid "$dir/${label}.super.pid")" "$(_fleet_win_read_pid "$dir/${label}.child.pid")"; do
    _fleet_win_kill_pid "$pid"
    rm -f "$dir/${label}.super.pid" "$dir/${label}.child.pid"
  done
  # sweep strays: a supervisor killed between writing its pidfile and forking,
  # plus anything an earlier install left running
  ps="$(_fleet_win_powershell)"
  [ -n "$ps" ] || return 0
  for pat in "${label}-super.ps1" "${label}.cmd" "${label}.task.xml"; do
    "$ps" -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.ProcessId -ne \$PID -and \$_.CommandLine -like '*$pat*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue }" >/dev/null 2>&1 || true
  done
  return 0
}

# writes <label>-super.ps1: a restart loop around the generated .cmd wrapper.
# Both the supervisor PID and the child PID are recorded to disk so stop and
# status never have to guess which process to signal.
_fleet_write_ps_supervisor() {
  local path="$1" label="$2" wrapper="$3" wait="${4:-5}"
  local dir superpid childpid
  dir="$(dirname "$path")"
  superpid="$(_fleet_win_path "${dir}/${label}.super.pid")"
  childpid="$(_fleet_win_path "${dir}/${label}.child.pid")"
  cat > "$path" <<PS
# FleetKit supervisor for $label. Do not edit; regenerated on install.
\$ErrorActionPreference = 'SilentlyContinue'
\$superPid  = '$superpid'
\$childPid  = '$childpid'
\$wrapper   = '$wrapper'
\$restartIn = $wait
Set-Content -Path \$superPid -Value \$PID -Force
while (\$true) {
  \$child = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', \$wrapper -PassThru -WindowStyle Hidden
  if (\$child) {
    Set-Content -Path \$childPid -Value \$child.Id -Force
    \$child.WaitForExit()
  }
  Start-Sleep -Seconds \$restartIn
}
PS
}

# writes <service-dir>/<label>.cmd that sets the env then runs the bridge
_fleet_write_cmd_wrapper() {
  local path="$1" workdir="$2" interpreter="$3" script="$4" extra="$5" envpairs="$6" logfile="$7"
  local entry k v
  path="$(_fleet_win_path "$path")"
  workdir="$(_fleet_win_path "$workdir")"
  interpreter="$(_fleet_win_path "$interpreter")"
  script="$(_fleet_win_path "$script")"
  extra="$(_fleet_win_tokens "$extra")"
  if [ -n "$logfile" ]; then logfile="$(_fleet_win_path "$logfile")"; fi
  {
    echo '@echo off'
    echo 'setlocal'
    local oifs="$IFS"
    IFS=';'
    for entry in $envpairs; do
      [ -n "$entry" ] || continue
      k="${entry%%=*}"
      v="${entry#*=}"
      v="$(_fleet_win_value "$k" "$v")"
      printf 'set "%s=%s"\n' "$(_fleet_cmd_escape "$k")" "$(_fleet_cmd_escape "$v")"
    done
    IFS="$oifs"
    printf 'cd /d "%s"\n' "$workdir"
    printf '"%s" "%s"' "$interpreter" "$script"
    if [ -n "$extra" ]; then printf ' %s' "$extra"; fi
    if [ -n "$logfile" ]; then printf ' >> "%s" 2>&1' "$logfile"; fi
    printf '\nendlocal\n'
  } | sed 's/$/\r/' > "$path"
}

# writes <service-dir>/<label>.sh used by the linux backend
_fleet_write_sh_wrapper() {
  local path="$1" workdir="$2" interpreter="$3" script="$4" extra="$5" envpairs="$6" logfile="$7"
  local entry k v
  {
    echo '#!/usr/bin/env bash'
    echo 'set -u'
    local oifs="$IFS"
    IFS=';'
    for entry in $envpairs; do
      [ -n "$entry" ] || continue
      k="${entry%%=*}"
      v="${entry#*=}"
      printf 'export %s=%s\n' "$k" "$(printf '%s' "$v" | sed "s/'/'\\\\''/g; s/^/'/; s/\$/'/")"
    done
    IFS="$oifs"
    printf 'cd "%s" || exit 1\n' "$workdir"
    printf 'while true; do\n'
    printf '  "%s" "%s"' "$interpreter" "$script"
    [ -n "$extra" ] && printf ' %s' "$extra"
    [ -n "$logfile" ] && printf ' >> "%s" 2>&1' "$logfile"
    printf '\n  sleep 2\ndone\n'
  } > "$path"
  chmod +x "$path"
}

_fleet_task_xml() {
  # $1 = logon trigger (true/false), $2 = interval seconds, $3 = daily HH:MM,
  # $4 = command, $5 = arguments, $6 = workdir
  local want_logon="$1" interval="$2" daily="$3" command="$4" args="$5" workdir="$6"
  local triggers="" sid userid logonuser
  sid="$(_fleet_user_sid)"
  if [ -n "$sid" ]; then
    userid="      <UserId>${sid}</UserId>
"
    logonuser="      <UserId>${sid}</UserId>
"
  else
    userid=""
    logonuser=""
  fi
  if [ "$want_logon" = "true" ]; then
    triggers="${triggers}    <LogonTrigger>
      <Enabled>true</Enabled>
${logonuser}    </LogonTrigger>
"
  fi
  if [ -n "$interval" ]; then
    triggers="${triggers}    <TimeTrigger>
      <StartBoundary>2020-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT${interval}S</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
"
  fi
  if [ -n "$daily" ]; then
    triggers="${triggers}    <CalendarTrigger>
      <StartBoundary>2020-01-01T${daily}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
"
  fi
  cat <<XML
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>FleetKit service</Description>
  </RegistrationInfo>
  <Triggers>
${triggers}  </Triggers>
  <Principals>
    <Principal id="Author">
${userid}      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>${command}</Command>
      <Arguments>${args}</Arguments>
      <WorkingDirectory>${workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
XML
}

fleet_task_install() {
  # label, want_logon, interval, daily, command, arguments, workdir
  local label="$1" want_logon="$2" interval="$3" daily="$4" command="$5" args="$6" workdir="$7"
  local dir xml
  dir="$(fleet_service_dir)"
  mkdir -p "$dir"
  xml="${dir}/${label}.task.xml"
  # schtasks/MSXML needs real UTF-16 bytes to match the declaration; a
  # mismatch fails as a misleading "Access is denied", so transcode with BOM
  if command -v iconv >/dev/null 2>&1; then
    { printf '\377\376'
      _fleet_task_xml "$want_logon" "$interval" "$daily" "$command" "$args" "$workdir" | iconv -f UTF-8 -t UTF-16LE
    } > "$xml"
  else
    _fleet_task_xml "$want_logon" "$interval" "$daily" "$command" "$args" "$workdir" > "$xml"
  fi
  local st
  st="$(_fleet_schtasks)"
  "$st" //Create //TN "$label" //XML "$xml" //F >/dev/null 2>&1 || \
    "$st" /Create /TN "$label" /XML "$xml" /F >/dev/null 2>&1
}

fleet_task_run() {
  local st
  st="$(_fleet_schtasks)"
  "$st" //Run //TN "$1" >/dev/null 2>&1 || "$st" /Run /TN "$1" >/dev/null 2>&1
}

fleet_task_end() {
  local st
  st="$(_fleet_schtasks)"
  "$st" //End //TN "$1" >/dev/null 2>&1 || "$st" /End /TN "$1" >/dev/null 2>&1
}

fleet_task_delete() {
  local st
  st="$(_fleet_schtasks)"
  "$st" //Delete //TN "$1" //F >/dev/null 2>&1 || "$st" /Delete /TN "$1" /F >/dev/null 2>&1
}

fleet_task_exists() {
  local st
  st="$(_fleet_schtasks)"
  "$st" //Query //TN "$1" >/dev/null 2>&1 && return 0
  "$st" /Query /TN "$1" >/dev/null 2>&1
}

fleet_task_status() {
  # echoes "running" / "ready" / "missing"
  local label="$1" st out
  st="$(_fleet_schtasks)"
  out="$("$st" //Query //TN "$label" //V //FO LIST 2>/dev/null || "$st" /Query /TN "$label" /V /FO LIST 2>/dev/null || true)"
  [ -n "$out" ] || { echo missing; return 1; }
  if printf '%s' "$out" | grep -qiE '^[[:space:]]*Status:[[:space:]]*Running'; then
    echo running
  else
    echo ready
  fi
}

# ---------- service API ----------
# fleet_service_install LABEL WORKDIR ENVFIXED INTERPRETER SCRIPT EXTRA_ARGS
#   ENVFIXED is a ';'-separated list of KEY=VALUE pairs (may be empty).

fleet_service_install() {
  local label="$1" workdir="$2" envpairs="$3" interpreter="$4" script="$5" extra="${6:-}"
  local dir logdir logfile
  dir="$(fleet_service_dir)"
  logdir="$(fleet_log_dir)"
  mkdir -p "$dir" "$logdir"
  logfile="${logdir}/${label}.log"

  if fleet_is_macos; then
    _fleet_service_install_macos "$label" "$workdir" "$envpairs" "$interpreter" "$script" "$extra" "$logfile"
  elif fleet_is_windows; then
    local wrapper
    wrapper="$(_fleet_win_path "${dir}/${label}.cmd")"
    _fleet_write_cmd_wrapper "$wrapper" "$workdir" "$interpreter" "$script" "$extra" "$envpairs" "$logfile"
    local supervisor
    _fleet_win_stop_service "$label"
    supervisor="${dir}/${label}-super.ps1"
    _fleet_write_ps_supervisor "$supervisor" "$label" "$wrapper"
    _fleet_win_spawn "$(_fleet_win_powershell)" "$(_fleet_win_path "$supervisor")" || return 1
  else
    local wrapper="${dir}/${label}.sh"
    _fleet_write_sh_wrapper "$wrapper" "$workdir" "$interpreter" "$script" "$extra" "$envpairs" "$logfile"
    pkill -f "$wrapper" >/dev/null 2>&1 || true
    setsid nohup bash "$wrapper" >/dev/null 2>&1 &
  fi
}

_fleet_service_install_macos() {
  local label="$1" workdir="$2" envpairs="$3" interpreter="$4" script="$5" extra="$6" logfile="$7"
  local dir plist entry k v env_xml="" args_xml="" arg oifs
  dir="$(fleet_service_dir)"
  plist="${dir}/${label}.plist"
  oifs="$IFS"
  IFS=';'
  for entry in $envpairs; do
    [ -n "$entry" ] || continue
    k="${entry%%=*}"
    v="${entry#*=}"
    env_xml="${env_xml}    <key>${k}</key>
    <string>${v}</string>
"
  done
  IFS="$oifs"
  if [ -n "$extra" ]; then
    IFS=' '
    for arg in $extra; do
      args_xml="${args_xml}    <string>${arg}</string>
"
    done
    IFS="$oifs"
  fi
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>EnvironmentVariables</key>
  <dict>
${env_xml}  </dict>
  <key>KeepAlive</key>
  <true/>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${interpreter}</string>
    <string>${script}</string>
${args_xml}  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardErrorPath</key>
  <string>${logfile}</string>
  <key>StandardOutPath</key>
  <string>${logfile}</string>
  <key>WorkingDirectory</key>
  <string>${workdir}</string>
</dict>
</plist>
PLIST
  launchctl bootout "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
  local booted=0
  for _try in 1 2 3 4 5; do
    if launchctl bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1; then booted=1; break; fi
    sleep 1
  done
  if [ "$booted" != "1" ]; then
    launchctl bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
  fi
  launchctl kickstart -k "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
}

fleet_service_start() {
  local label="$1"
  if fleet_is_macos; then
    launchctl kickstart -k "gui/$(id -u)/${label}" >/dev/null 2>&1 || return 1
  elif fleet_is_windows; then
    local dir pid supervisor
    dir="$(fleet_service_dir)"
    supervisor="${dir}/${label}-super.ps1"
    [ -f "$supervisor" ] || return 1
    pid="$(_fleet_win_read_pid "$dir/${label}.super.pid")"
    if _fleet_win_pid_alive "$pid"; then return 0; fi
    _fleet_win_stop_service "$label"
    _fleet_win_spawn "$(_fleet_win_powershell)" "$(_fleet_win_path "$supervisor")" || return 1
  else
    local wrapper
    wrapper="$(fleet_service_dir)/${label}.sh"
    [ -f "$wrapper" ] || return 1
    setsid nohup bash "$wrapper" >/dev/null 2>&1 &
  fi
  return 0
}

fleet_service_stop() {
  local label="$1"
  if fleet_is_macos; then
    launchctl kill TERM "gui/$(id -u)/${label}" >/dev/null 2>&1 || \
      launchctl bootout "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
  elif fleet_is_windows; then
    _fleet_win_stop_service "$label"
  else
    pkill -f "$(fleet_service_dir)/${label}.sh" >/dev/null 2>&1 || true
  fi
  return 0
}

fleet_service_restart() {
  local label="$1"
  fleet_service_stop "$label"
  sleep 1
  fleet_service_start "$label"
}

fleet_service_remove() {
  local label="$1"
  if fleet_is_macos; then
    local plist
    plist="$(fleet_service_dir)/${label}.plist"
    if [ -f "$plist" ]; then
      launchctl bootout "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
      rm -f "$plist"
      return 0
    fi
    return 1
  elif fleet_is_windows; then
    _fleet_win_stop_service "$label"
    rm -f "$(fleet_service_dir)/${label}.cmd" "$(fleet_service_dir)/${label}.task.xml" \
          "$(fleet_service_dir)/${label}-super.ps1" \
          "$(fleet_service_dir)/${label}.super.pid" "$(fleet_service_dir)/${label}.child.pid"
    return 0
  else
    pkill -f "$(fleet_service_dir)/${label}.sh" >/dev/null 2>&1 || true
    rm -f "$(fleet_service_dir)/${label}.sh"
    return 0
  fi
}

fleet_service_exists() {
  local label="$1"
  if fleet_is_macos; then
    [ -f "$(fleet_service_dir)/${label}.plist" ]
  elif fleet_is_windows; then
    [ -f "$(fleet_service_dir)/${label}-super.ps1" ] || fleet_task_exists "$label"
  else
    [ -f "$(fleet_service_dir)/${label}.sh" ]
  fi
}

fleet_service_status() {
  # echoes "running" / "ready" / "missing"
  local label="$1"
  if fleet_is_macos; then
    [ -f "$(fleet_service_dir)/${label}.plist" ] || { echo missing; return 1; }
    if launchctl print "gui/$(id -u)/${label}" 2>/dev/null | grep -q 'state = running'; then
      echo running
    else
      echo ready
    fi
  elif fleet_is_windows; then
    local dir pid
    dir="$(fleet_service_dir)"
    if [ ! -f "${dir}/${label}-super.ps1" ] && [ ! -f "${dir}/${label}.cmd" ] && ! fleet_task_exists "$label"; then
      echo missing; return 1
    fi
    for pid in "$(_fleet_win_read_pid "$dir/${label}.child.pid")" "$(_fleet_win_read_pid "$dir/${label}.super.pid")"; do
      if _fleet_win_pid_alive "$pid"; then echo running; return 0; fi
    done
    echo ready
  else
    [ -f "$(fleet_service_dir)/${label}.sh" ] || { echo missing; return 1; }
    if pgrep -f "$(fleet_service_dir)/${label}.sh" >/dev/null 2>&1; then echo running; else echo ready; fi
  fi
}

# ---------- timer API (launchd StartInterval / daily calendar) ----------
# fleet_timer_install LABEL INTERVAL_SECONDS INTERPRETER SCRIPT EXTRA_ARGS
fleet_timer_install() {
  local label="$1" interval="$2" interpreter="$3" script="$4" extra="${5:-}"
  local dir logdir logfile
  dir="$(fleet_service_dir)"
  logdir="$(fleet_log_dir)"
  mkdir -p "$dir" "$logdir"
  logfile="${logdir}/${label}.log"
  if fleet_is_macos; then
    local args_xml="" arg oifs="$IFS"
    if [ -n "$extra" ]; then
      IFS=' '
      for arg in $extra; do args_xml="${args_xml}    <string>${arg}</string>
"; done
      IFS="$oifs"
    fi
    cat > "${dir}/${label}.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${interpreter}</string>
    <string>${script}</string>
${args_xml}  </array>
  <key>StartInterval</key>
  <integer>${interval}</integer>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardErrorPath</key>
  <string>${logfile}</string>
  <key>StandardOutPath</key>
  <string>${logfile}</string>
</dict>
</plist>
PLIST
    launchctl bootout "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "${dir}/${label}.plist" >/dev/null 2>&1 || true
  elif fleet_is_windows; then
    local wrapper
    wrapper="$(_fleet_win_path "${dir}/${label}.cmd")"
    _fleet_write_cmd_wrapper "$wrapper" "$(pwd)" "$interpreter" "$script" "$extra" "" "$logfile"
    fleet_task_install "$label" "false" "$interval" "" "cmd.exe" "/c \"${wrapper}\"" "$(_fleet_win_path "$(pwd)")"
  else
    local wrapper="${dir}/${label}.sh"
    printf '#!/usr/bin/env bash\nwhile true; do\n  "%s" "%s" %s >> "%s" 2>&1\n  sleep %s\ndone\n' \
      "$interpreter" "$script" "$extra" "$logfile" "$interval" > "$wrapper"
    chmod +x "$wrapper"
    pkill -f "$wrapper" >/dev/null 2>&1 || true
    setsid nohup bash "$wrapper" >/dev/null 2>&1 &
  fi
}

# fleet_timer_daily LABEL HH:MM INTERPRETER SCRIPT EXTRA_ARGS
fleet_timer_daily() {
  local label="$1" at="$2" interpreter="$3" script="$4" extra="${5:-}"
  local dir logdir logfile
  dir="$(fleet_service_dir)"
  logdir="$(fleet_log_dir)"
  mkdir -p "$dir" "$logdir"
  logfile="${logdir}/${label}.log"
  if fleet_is_macos; then
    local hour="${at%%:*}" minute="${at##*:}"
    local args_xml="" arg oifs="$IFS"
    if [ -n "$extra" ]; then
      IFS=' '
      for arg in $extra; do args_xml="${args_xml}    <string>${arg}</string>
"; done
      IFS="$oifs"
    fi
    cat > "${dir}/${label}.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${interpreter}</string>
    <string>${script}</string>
${args_xml}  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>${hour}</integer>
    <key>Minute</key>
    <integer>${minute}</integer>
  </dict>
  <key>StandardErrorPath</key>
  <string>${logfile}</string>
  <key>StandardOutPath</key>
  <string>${logfile}</string>
</dict>
</plist>
PLIST
    launchctl bootout "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "${dir}/${label}.plist" >/dev/null 2>&1 || true
  elif fleet_is_windows; then
    local wrapper
    wrapper="$(_fleet_win_path "${dir}/${label}.cmd")"
    _fleet_write_cmd_wrapper "$wrapper" "$(pwd)" "$interpreter" "$script" "$extra" "" "$logfile"
    fleet_task_install "$label" "false" "" "$at" "cmd.exe" "/c \"${wrapper}\"" "$(_fleet_win_path "$(pwd)")"
  else
    fleet_timer_install "$label" 86400 "$interpreter" "$script" "$extra"
  fi
}
