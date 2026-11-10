#!/usr/bin/env bash
# Start the backend (http://127.0.0.1:8000) and frontend (http://localhost:4200) for local
# testing, and stop both with Ctrl+C. Works in Git Bash on Windows and in bash on Linux/macOS.
# The backend has no --reload, so run this again after changing backend code or backend/.env.
set -uo pipefail
cd "$(dirname "$0")"
root=$PWD

if [[ -x backend/.venv/Scripts/python.exe ]]; then
  python=$root/backend/.venv/Scripts/python.exe
elif [[ -x backend/.venv/bin/python ]]; then
  python=$root/backend/.venv/bin/python
else
  echo "No backend/.venv yet: set it up as in the README's 'Running locally'." >&2
  exit 1
fi
if [[ ! -d frontend/node_modules ]]; then
  echo "No frontend/node_modules yet: run 'npm install' in frontend first." >&2
  exit 1
fi
for port in 8000 4200; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "Port $port is already in use: stop whatever is running there first." >&2
    exit 1
  fi
done

writes=$(grep -E '^SBP_INVERTER_WRITES=' backend/.env 2>/dev/null | cut -d= -f2)
echo "Inverter writes: ${writes:-false (not set)}"

(cd backend && exec "$python" -m uvicorn app.main:app --host 127.0.0.1 --port 8000) &
backend=$!
# In a real terminal the Angular CLI stops to ask about autocompletion (and analytics), which a
# background job can't answer, so it exits. These two settings skip just those questions.
(cd frontend && NG_FORCE_AUTOCOMPLETE=false NG_CLI_ANALYTICS=false exec npm start </dev/null) &
frontend=$!

stopped=
stop() {
  [[ -n $stopped ]] && return
  stopped=1
  echo
  echo "Stopping both servers..."
  for pid in "$backend" "$frontend"; do
    if [[ -r /proc/$pid/winpid ]]; then
      # Git Bash: Windows programs (python, node) aren't killed with their bash parent, which
      # left node holding port 4200; end the whole Windows process tree instead.
      taskkill //F //T //PID "$(cat "/proc/$pid/winpid")" >/dev/null 2>&1
    fi
    pkill -P "$pid" 2>/dev/null
    kill "$pid" 2>/dev/null
  done
  wait 2>/dev/null
  echo "Stopped."
}
trap stop EXIT
trap 'exit 130' INT TERM

echo "Backend:  http://127.0.0.1:8000  (API docs at /docs)"
echo "Frontend: http://localhost:4200"
echo "Press Ctrl+C to stop both."
# If either stops by itself, stop the other too.
wait -n
