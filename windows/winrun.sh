#!/usr/bin/env bash
# Send a local PowerShell script to the Windows executor without hitting the
# cmd.exe 8191-char command line limit:
#   -EncodedCommand carries only a tiny UTF16LE base64 stub,
#   the real script travels as UTF-8 base64 on stdin.
# Usage: winrun.sh <script.ps1>
set -euo pipefail
script_path="$1"
stub="\$ErrorActionPreference='Stop'; \$b=[Console]::In.ReadToEnd(); Invoke-Expression ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(\$b)))"
encoded="$(python3 - "$stub" <<'PY'
import base64, sys
sys.stdout.write(base64.b64encode(sys.argv[1].encode('utf-16-le')).decode('ascii'))
PY
)"
base64 -w0 "$script_path" | ssh -T -F /dev/null -i /home/jiangli/.ssh/win_executor_key \
  -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no \
  -o LogLevel=ERROR ASUS@192.168.1.112 \
  "powershell.exe -NoProfile -NonInteractive -EncodedCommand ${encoded}"
