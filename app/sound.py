"""
Ding-Ton ueber den HDMI-Ausgang (Beamer-Lautsprecher).

Der Ton wird einmal beim Start erzeugt (Glocke aus zwei abklingenden Sinustoenen) und mit
'aplay' (ALSA) abgespielt. Beim Raspberry Pi 4/5 heisst der erste HDMI-Ausgang
"hdmi:CARD=vc4hdmi0,DEV=0" - die ALSA-Einstellungen fuer diesen Geraetenamen wandeln das
Signal passend fuer HDMI um. Anderes Geraet: AUDIO_DEVICE in docker-compose.yml
(Liste der Geraete: docker compose exec billard aplay -L).
"""

import math
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import wave

DEFAULT_DEVICE = "hdmi:CARD=vc4hdmi0,DEV=0"


def make_ding(path, rate=48000, seconds=1.2):
    """Glockenton: Grundton + Oberton, schneller Anschlag, langsames Abklingen."""
    n = int(rate * seconds)
    frames = bytearray()
    for i in range(n):
        t = i / rate
        attack = min(1.0, t / 0.005)
        v = (0.55 * math.sin(2 * math.pi * 1318.5 * t) * math.exp(-t * 3.2)      # E6
             + 0.30 * math.sin(2 * math.pi * 1975.5 * t) * math.exp(-t * 5.0)    # H6
             + 0.15 * math.sin(2 * math.pi * 2637.0 * t) * math.exp(-t * 8.0))   # E7
        s = int(max(-1.0, min(1.0, v * attack)) * 0.8 * 32767)
        frames += struct.pack("<hh", s, s)                                       # Stereo
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


class Sound:
    def __init__(self, device=None):
        self.device = device or os.environ.get("AUDIO_DEVICE") or DEFAULT_DEVICE
        self.path = os.path.join(tempfile.gettempdir(), "billard-ding.wav")
        self.error = ""
        self._lock = threading.Lock()
        try:
            make_ding(self.path)
        except OSError as e:
            self.error = f"Ton konnte nicht erzeugt werden: {e}"

    def ding(self, wait=False):
        """Ding abspielen. Ohne wait im Hintergrund (blockiert das Spiel nicht).
        Mit wait: wartet bis zu 5 s und liefert eine Fehlermeldung oder ''."""
        if not shutil.which("aplay"):
            self.error = "aplay fehlt (Paket alsa-utils)"
            return self.error
        cmd = ["aplay", "-q", "-D", self.device, self.path]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except OSError as e:
            self.error = str(e)
            return self.error
        if not wait:
            threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
            return ""
        return self._watch(proc)

    def _watch(self, proc):
        try:
            _, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            err = b"Zeitueberschreitung"
        with self._lock:
            if proc.returncode:
                self.error = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["Fehler"]
                self.error = f"Ton auf {self.device}: {self.error[0]}"
                print(f"[ton] {self.error}", flush=True)
            else:
                self.error = ""
        return self.error
