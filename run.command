set -euo pipefail
cd "$(dirname "$0")"

PY=/usr/bin/python3                    
if [ ! -x .venv/bin/python ]; then
  echo "▸ First run: creating virtual environment…"
  "$PY" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
for tool in ffmpeg yt-dlp; do
  command -v "$tool" >/dev/null || { echo "▸ Installing $tool…"; brew install "$tool"; }
done
exec .venv/bin/python floating_lyrics.py "$@"
