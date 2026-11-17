"""Prüft das JavaScript in allen HTML-Dateien auf Syntaxfehler (node --check).
    python3 scripts/check_html_js.py app/*.html training_server/*.html
"""
import re
import subprocess
import sys
import tempfile

fehler = 0
for pfad in sys.argv[1:]:
    html = open(pfad, encoding="utf-8").read()
    for i, m in enumerate(re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I)):
        code = m.group(1)
        if not code.strip():
            continue
        zeile = html.count("\n", 0, m.start(1)) + 1
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write("\n" * (zeile - 1) + code)        # Zeilennummern wie in der HTML-Datei
        r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
        if r.returncode:
            fehler += 1
            print(f"{pfad} (Script ab Zeile {zeile}):\n{r.stderr.replace(f.name, pfad)}")
        else:
            print(f"ok  {pfad} Script {i + 1}")
sys.exit(1 if fehler else 0)
