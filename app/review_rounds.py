"""
Pruefrunden: dasselbe Bild wird von mehreren Pruefern gleichzeitig festgelegt.

Ablauf:
  1. Admin drueckt "Bild an alle Pruefer": Bild + Vorschlag des KI-Chips (falls bereit)
     geht an alle Pruefer, die gerade angemeldet und online sind (Teilnehmer).
  2. Jeder legt die Kugeln auf seinem iPad fest und drueckt "Speichern".
  3. Haben alle gespeichert: Ergebnisse werden zusammengefuehrt (Mehrheit), der Beamer
     zeigt "Alle fertig - weiter!" und es klingt ein Ding ueber HDMI.

Zusammenfuehren (consensus):
  * Kugeln verschiedener Pruefer an (fast) derselben Stelle sind dieselbe Kugel.
  * Eine Kugel zaehlt, wenn die Mehrheit der Pruefer sie markiert hat. Position = Mittelwert,
    Nummer = Mehrheit der Stimmen.
  * Keine Mehrheit (z. B. 2 Pruefer, einer sagt 3, einer 11; oder nur die Haelfte sieht
    dort eine Kugel) -> "uneinig". Dann landet das Bild bei den offenen Bildern, die
    strittigen Kugeln sind mit "?" markiert, der Admin entscheidet.
  * Sind sich alle einig, geht das Bild direkt in die Trainingsdaten.

KI-Abgleich (sobald ein Modell geladen ist):
  * Der KI-Chip macht zu jedem Bild einen Vorschlag. Die Pruefer legen die Kugeln trotzdem
    selbst fest - standardmaessig OHNE den Vorschlag zu sehen (ehrlicher Vergleich;
    im Admin-Panel umschaltbar).
  * Danach wird das Ergebnis der Pruefer mit dem Vorschlag verglichen. Wo die KI falsch lag
    (andere Nummer, Kugel uebersehen, Kugel gesehen, wo keine ist), kommt ein "?" mit Notiz
    ("KI: 3 · Pruefer: 11"). Das Bild geht dann zum Admin, der die "?" bestaetigt.
  * Lag die KI ueberall richtig (und die Pruefer sind sich einig), geht das Bild direkt in
    die Trainingsdaten. Die Trefferquote der KI zaehlt immer gegen das Pruefer-Ergebnis.

Ablage: data/runden/<id>.jpg + <id>.json (alle Einzelergebnisse bleiben erhalten).
"""

import json
import os
import threading
import time
from collections import Counter

from ballnum import color_of_number, kind_of_number
from recorder import _ball_dict, ball_num, missing_numbers


def consensus(submissions, radius):
    """submissions: {name: [ball-dicts]} -> (kugeln, strittig, einigkeit 0..1)."""
    names = list(submissions)
    n = len(names)
    clusters = []                                   # [{"pts": [(name, x, y, nummer)]}]
    max_d2 = (0.8 * radius) ** 2
    for name in names:
        for b in submissions[name]:
            num = ball_num(b)
            best, bd = None, max_d2
            for c in clusters:
                if any(p[0] == name for p in c["pts"]):
                    continue
                cx = sum(p[1] for p in c["pts"]) / len(c["pts"])
                cy = sum(p[2] for p in c["pts"]) / len(c["pts"])
                d2 = (cx - b["x"]) ** 2 + (cy - b["y"]) ** 2
                if d2 <= bd:
                    best, bd = c, d2
            if best is None:
                clusters.append({"pts": [(name, float(b["x"]), float(b["y"]), num)]})
            else:
                best["pts"].append((name, float(b["x"]), float(b["y"]), num))

    final, disputed, unanimous = [], [], 0
    for c in clusters:
        k = len(c["pts"])
        x = sum(p[1] for p in c["pts"]) / k
        y = sum(p[2] for p in c["pts"]) / k
        votes = Counter(p[3] for p in c["pts"])
        top, cnt = votes.most_common(1)[0]
        if k == n and cnt == n:
            unanimous += 1
        if 2 * k < n:
            continue                                # nur eine Minderheit sieht hier eine Kugel
        if 2 * k == n and n > 1:
            disputed.append({"x": x, "y": y, "why": "nur die Hälfte hat hier eine Kugel markiert",
                             "votes": {p[0]: p[3] for p in c["pts"]}})
            continue
        if 2 * cnt <= k and k > 1:
            disputed.append({"x": x, "y": y, "why": "keine Mehrheit für eine Nummer",
                             "votes": {p[0]: p[3] for p in c["pts"]}})
            continue
        final.append({"x": round(x, 1), "y": round(y, 1), "number": top,
                      "kind": kind_of_number(top), "color": color_of_number(top)})
    agreement = unanimous / len(clusters) if clusters else 1.0
    return final, disputed, agreement


def _label(n):
    return "W" if n == 0 else ("-" if n is None else str(n))


def compare_ai(final, suggestions, radius):
    """Pruefer-Ergebnis gegen KI-Vorschlag. -> (abweichungen, richtig, gesamt).
    Jede Abweichung: {"x", "y", "ai": Nummer|None, "human": Nummer|None, "note": Text}."""
    sugg = [dict(b, number=ball_num(b)) for b in suggestions]
    used, diffs, correct = set(), [], 0
    max_d2 = (0.8 * radius) ** 2
    for b in final:
        best, bd = None, max_d2
        for i, s in enumerate(sugg):
            if i in used:
                continue
            d2 = (s["x"] - b["x"]) ** 2 + (s["y"] - b["y"]) ** 2
            if d2 <= bd:
                best, bd = i, d2
        if best is None:
            diffs.append({"x": b["x"], "y": b["y"], "ai": None, "human": b["number"],
                          "note": f"KI: übersehen · Prüfer: {_label(b['number'])}"})
            continue
        used.add(best)
        if sugg[best]["number"] == b["number"]:
            correct += 1
        else:
            diffs.append({"x": b["x"], "y": b["y"], "ai": sugg[best]["number"], "human": b["number"],
                          "note": f"KI: {_label(sugg[best]['number'])} · Prüfer: {_label(b['number'])}"})
    for i, s in enumerate(sugg):
        if i not in used:
            diffs.append({"x": s["x"], "y": s["y"], "ai": s["number"], "human": None,
                          "note": f"KI: {_label(s['number'])} · Prüfer: keine Kugel"})
    total = len(final) + sum(1 for d in diffs if d["human"] is None)
    return diffs, correct, total


class RoundManager:
    def __init__(self, recorder, data_dir, sound=None, online_s=20.0, clock=time.time):
        self.recorder = recorder
        self.dir = os.path.join(data_dir, "runden")
        self.sound = sound
        self.online_s = online_s
        self.clock = clock
        self.lock = threading.RLock()
        self.round = None
        self.last = None            # Ergebnis der letzten Runde (fuer Beamer/Portal)
        self.seen = {}              # Pruefer -> zuletzt gesehen
        self.count = self._load_count()

    # --------------------------------------------------------------- online
    def heartbeat(self, name):
        with self.lock:
            self.seen[name] = self.clock()

    def online(self, names):
        now = self.clock()
        with self.lock:
            return [n for n in names if now - self.seen.get(n, -1e9) <= self.online_s]

    # ---------------------------------------------------------------- Runde
    def start(self, warped, suggestions, radius, model, participants, show_ai=False):
        import cv2
        with self.lock:
            if self.round is not None:
                raise ValueError("Es läuft schon eine Prüfrunde")
            if not participants:
                raise ValueError("Kein Prüfer angemeldet - Prüfer öffnen /pruefen und melden sich mit ihrem Code an")
            os.makedirs(self.dir, exist_ok=True)
            rid = self.recorder._new_name()
            h, w = warped.shape[:2]
            cv2.imwrite(os.path.join(self.dir, rid + ".jpg"), warped, [cv2.IMWRITE_JPEG_QUALITY, 92])
            self.count += 1
            self.round = {
                "id": rid, "no": self.count, "width": w, "height": h, "radius": float(radius),
                "model": model, "suggestions": [_ball_dict(b) for b in suggestions],
                "participants": list(participants), "submissions": {}, "started": self.clock(),
                "show_ai": bool(show_ai),
            }
            self._save()
            return self.round

    def view(self, name):
        """Was ein Pruefer sieht."""
        with self.lock:
            r = self.round
            if r is None or name not in r["participants"]:
                return {"round": None, "last": self.last}
            mine = r["submissions"].get(name)
            return {"round": {
                "id": r["id"], "no": r["no"], "width": r["width"], "height": r["height"], "radius": r["radius"],
                "balls": mine if mine is not None else (r["suggestions"] if r["show_ai"] else []),
                "submitted": mine is not None, "model": r["model"] if r["show_ai"] else None,
                "blind": bool(r["model"]) and not r["show_ai"], "status": self._status_list(r)},
                "last": self.last}

    def submit(self, name, round_id, balls):
        with self.lock:
            r = self.round
            if r is None or r["id"] != round_id:
                raise ValueError("Diese Prüfrunde ist schon vorbei")
            if name not in r["participants"]:
                raise ValueError("Du bist in dieser Runde nicht dabei")
            balls = [b for b in balls if b.get("kind") != "none"]
            k = missing_numbers(balls)
            if k:
                raise ValueError(f"{k} Kugel(n) ohne Nummer - Kugel antippen und Nummer wählen")
            r["submissions"][name] = [_ball_dict(b) for b in balls]
            self._save()
            if len(r["submissions"]) >= len(r["participants"]):
                return self._finish()
            return None

    def finish(self):
        """Admin: Runde mit den bisherigen Ergebnissen abschliessen (z. B. wenn jemand gegangen ist)."""
        with self.lock:
            if self.round is None:
                raise ValueError("Es läuft keine Prüfrunde")
            if not self.round["submissions"]:
                raise ValueError("Noch hat niemand gespeichert - sonst 'Abbrechen'")
            self.round["participants"] = [p for p in self.round["participants"] if p in self.round["submissions"]]
            return self._finish()

    def cancel(self):
        with self.lock:
            if self.round is None:
                raise ValueError("Es läuft keine Prüfrunde")
            self.round["cancelled"] = True
            self._save()
            self.round = None
            self.last = {"t": self.clock(), "ok": False, "text": "Prüfrunde abgebrochen"}

    def _finish(self):
        r = self.round
        img = os.path.join(self.dir, r["id"] + ".jpg")
        final, disputed, agreement = consensus(r["submissions"], r["radius"])
        r.update(result=final, disputed=disputed, agreement=agreement, finished=self.clock())
        self._save()
        extra = {"reviewers": r["submissions"], "agreement": round(agreement, 3), "round": r["no"]}

        # KI-Abgleich: Vorschlag gegen das Ergebnis der Pruefer
        ai = None
        ai_diffs = []
        if r["model"]:
            ai_diffs, ai_ok, ai_total = compare_ai(final, r["suggestions"], r["radius"])
            ai = {"correct": ai_ok, "total": ai_total, "wrong": len(ai_diffs)}
            # Trefferquote zaehlt gegen das Pruefer-Ergebnis (nicht spaeter nochmal beim Admin)
            self.recorder._score({"model": r["model"], "radius": r["radius"], "balls": r["suggestions"]}, final)
            extra["ai"] = dict(ai, model=r["model"], suggestion=r["suggestions"])
        r.update(ai=ai, ai_diffs=ai_diffs)
        self._save()

        if not disputed and not ai_diffs:
            self.recorder.add_verified(r["id"], img, final, r["radius"], r["width"], r["height"], extra)
            how = "alle einig" if agreement >= 0.999 else f"Mehrheit hat entschieden ({round(agreement * 100)} % einstimmig)"
            text = f"Bild {r['no']}: {how}" + (" · KI lag überall richtig" if ai else "") + " - in den Trainingsdaten"
        else:
            # "?" an allen Stellen, an denen die Pruefer uneinig sind oder die KI falsch lag
            q = [{"x": d["x"], "y": d["y"], "kind": "solid", "color": "", "number": None,
                  "note": "Prüfer uneinig: " + ", ".join(f"{k} {_label(v)}" for k, v in d["votes"].items())}
                 for d in disputed]
            # "human": was die Pruefer gesagt haben (None = keine Kugel) - fuer "Pruefer-Ergebnis uebernehmen"
            q += [{"x": d["x"], "y": d["y"], "kind": "solid", "color": "", "number": None, "note": d["note"],
                   "human": d["human"], "has_human": True} for d in ai_diffs]
            wrong_xy = [(d["x"], d["y"]) for d in ai_diffs]
            keep = [b for b in final if not any((b["x"] - x) ** 2 + (b["y"] - y) ** 2 < 1 for x, y in wrong_xy)]
            self.recorder.add_pending(r["id"], img, dict(
                {"width": r["width"], "height": r["height"], "radius": r["radius"], "model": None,
                 "balls": keep + q, "disputed": disputed, "ai_diffs": ai_diffs}, **extra))
            parts = []
            if disputed:
                parts.append(f"{len(disputed)} × Prüfer uneinig")
            if ai_diffs:
                parts.append(f"{len(ai_diffs)} × KI falsch")
            text = f"Bild {r['no']}: " + ", ".join(parts) + " - mit ? beim Admin unter den offenen Bildern"
        self.last = {"t": self.clock(), "ok": not disputed and not ai_diffs, "text": text, "no": r["no"],
                     "agreement": round(agreement * 100), "disputed": len(disputed),
                     "names": list(r["submissions"]), "ai": ai}
        self.round = None
        if self.sound is not None:
            self.sound.ding()
        return self.last

    # -------------------------------------------------------------- Anzeige
    def _status_list(self, r):
        return [{"name": p, "done": p in r["submissions"]} for p in r["participants"]]

    def status(self):
        with self.lock:
            r = self.round
            if r is None:
                return {"active": False, "last": self.last, "count": self.count}
            return {"active": True, "id": r["id"], "no": r["no"], "participants": self._status_list(r),
                    "done": len(r["submissions"]), "total": len(r["participants"]),
                    "last": self.last, "count": self.count}

    # ------------------------------------------------------------ speichern
    def _save(self):
        r = self.round
        if r is None:
            return
        os.makedirs(self.dir, exist_ok=True)
        tmp = os.path.join(self.dir, r["id"] + ".json.tmp")
        with open(tmp, "w") as f:
            json.dump(r, f)
        os.replace(tmp, os.path.join(self.dir, r["id"] + ".json"))
        with open(os.path.join(self.dir, "zaehler.json"), "w") as f:
            json.dump({"count": self.count}, f)

    def _load_count(self):
        try:
            with open(os.path.join(self.dir, "zaehler.json")) as f:
                return int(json.load(f)["count"])
        except (OSError, ValueError, KeyError):
            return 0
