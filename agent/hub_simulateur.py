#!/usr/bin/env python3
"""
Simulateur du hub domotique ESP32 (servo, PIR, DHT11, LEDs, buzzer).

Il expose la même API HTTP que celle que ton agent utilisera, pour tester
l'agent sans le matériel. Aucune dépendance : Python 3.8+ suffit.

Lancement :
    python hub_simulateur.py
    python hub_simulateur.py --historique --mouvements-auto 30

Puis ouvre http://127.0.0.1:8080 pour voir le hub en temps réel.
"""

import argparse
import json
import math
import random
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def horodatage(ts=None):
    """Timestamp Unix -> texte ISO local, à la seconde."""
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def borner(valeur, mini, maxi):
    return max(mini, min(maxi, valeur))


class ErreurRequete(Exception):
    """Erreur de paramètre envoyée au client avec un code HTTP."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def entier(valeur, nom, mini, maxi, defaut=None):
    """Valide un entier venant du JSON ou de l'URL."""
    if valeur is None:
        if defaut is None:
            raise ErreurRequete(400, f"Paramètre '{nom}' manquant")
        return defaut
    if isinstance(valeur, bool):
        raise ErreurRequete(400, f"'{nom}' doit être un entier")
    try:
        v = int(valeur)
    except (TypeError, ValueError):
        raise ErreurRequete(400, f"'{nom}' doit être un entier")
    if isinstance(valeur, float) and valeur != v:
        raise ErreurRequete(400, f"'{nom}' doit être un entier")
    if not mini <= v <= maxi:
        raise ErreurRequete(400, f"'{nom}' doit être entre {mini} et {maxi}")
    return v


# ---------------------------------------------------------------------------
# Modèle du hub
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.journal = deque(maxlen=300)
        self.reinitialiser(historique=cfg.historique)

    # --- état général -----------------------------------------------------

    def reinitialiser(self, historique=False):
        with self.lock:
            self.t0 = time.time()
            self.porte_ouverte = False
            self.fermeture_auto_a = None
            self.derniere_ouverture = None
            self.derniere_fermeture = None
            self.leds = {"verte": False, "rouge": True}
            self.buzzer_fin = 0.0
            self.temp_base = float(self.cfg.temperature)
            self.hum_base = float(self.cfg.humidite)
            self.mouvements = deque(maxlen=2000)   # éléments : (ts, pendant_stabilisation)
            self.climat_hist = deque(maxlen=1440)  # éléments : (ts, temp, hum), 1 par minute
            self.prochain_echantillon = time.time()
            self.pannes = {"dht": False, "taux_erreur": 0.0, "latence_ms": 0}
            if historique:
                self._generer_historique()
            self.evenement(
                f"Démarrage du hub, PIR en stabilisation pendant {self.cfg.stabilisation} s"
            )

    def evenement(self, texte):
        self.journal.append({
            "horodatage": horodatage(),
            "type": "hub",
            "texte": texte,
        })

    def requete(self, methode, chemin, corps, code, reponse):
        self.journal.append({
            "horodatage": horodatage(),
            "type": "requete",
            "methode": methode,
            "chemin": chemin,
            "corps": corps,
            "code": code,
            "reponse": reponse,
        })

    # --- PIR --------------------------------------------------------------

    def stabilisation_restante(self):
        return max(0, int(math.ceil(self.cfg.stabilisation - (time.time() - self.t0))))

    def pir_pret(self):
        return self.stabilisation_restante() == 0

    def detection(self, source="PIR"):
        with self.lock:
            pendant_stab = not self.pir_pret()
            self.mouvements.append((time.time(), pendant_stab))
            if pendant_stab:
                self.evenement(f"Détection ({source}) pendant la stabilisation, ignorée par le hub")
                return
            self.evenement(f"Détection de mouvement ({source})")
            if self.cfg.auto_ouverture:
                self.ouvrir("PIR", self.cfg.delai_fermeture)

    # --- porte / servo ----------------------------------------------------

    def ouvrir(self, source, duree_s):
        with self.lock:
            deja = self.porte_ouverte
            self.porte_ouverte = True
            self.leds["verte"], self.leds["rouge"] = True, False
            self.fermeture_auto_a = time.time() + duree_s if duree_s > 0 else None
            if not deja:
                self.derniere_ouverture = time.time()
                self.bip(150)
            suite = f"fermeture auto dans {duree_s} s" if duree_s > 0 else "reste ouverte"
            self.evenement(f"Porte {'maintenue ouverte' if deja else 'ouverte'} ({source}), {suite}")

    def fermer(self, source):
        with self.lock:
            if not self.porte_ouverte:
                return False
            self.porte_ouverte = False
            self.fermeture_auto_a = None
            self.derniere_fermeture = time.time()
            self.leds["verte"], self.leds["rouge"] = False, True
            self.evenement(f"Porte fermée ({source})")
            return True

    def etat_porte(self):
        restant = None
        if self.porte_ouverte and self.fermeture_auto_a:
            restant = max(0, int(round(self.fermeture_auto_a - time.time())))
        return {
            "etat": "ouverte" if self.porte_ouverte else "fermee",
            "angle_servo": 90 if self.porte_ouverte else 0,
            "fermeture_auto_dans_s": restant,
            "derniere_ouverture": horodatage(self.derniere_ouverture) if self.derniere_ouverture else None,
            "derniere_fermeture": horodatage(self.derniere_fermeture) if self.derniere_fermeture else None,
        }

    # --- buzzer -----------------------------------------------------------

    def bip(self, duree_ms):
        with self.lock:
            self.buzzer_fin = max(self.buzzer_fin, time.time() + duree_ms / 1000)

    def buzzer_actif(self):
        return time.time() < self.buzzer_fin

    # --- DHT11 ------------------------------------------------------------

    def _valeurs_climat(self, ts):
        """Valeurs « vraies » à un instant donné (lente oscillation + bruit)."""
        phase = 2 * math.pi * (ts - self.t0) / 900   # cycle de 15 min
        temp = self.temp_base + 1.5 * math.sin(phase) + random.gauss(0, 0.3)
        hum = self.hum_base - 3 * math.sin(phase) + random.gauss(0, 1)
        # Le DHT11 donne des entiers, 0-50 °C et 20-90 % HR
        return int(round(borner(temp, 0, 50))), int(round(borner(hum, 20, 90)))

    def lire_climat(self):
        """Lecture capteur : None si le DHT11 est en panne."""
        if self.pannes["dht"]:
            return None
        t, h = self._valeurs_climat(time.time())
        return {"temperature_c": t, "humidite_pct": h, "horodatage": horodatage()}

    # --- historique simulé ------------------------------------------------

    def _generer_historique(self):
        """Remplit les dernières 24 h avec des données réalistes."""
        maintenant = time.time()
        debut = maintenant - 24 * 3600
        for i in range(1440):
            ts = debut + i * 60
            heure = datetime.fromtimestamp(ts).hour + datetime.fromtimestamp(ts).minute / 60
            # plus chaud vers 15h, plus froid vers 5h
            journee = math.sin(2 * math.pi * (heure - 9) / 24)
            t = self.temp_base - 1 + 2.5 * journee + random.gauss(0, 0.4)
            h = self.hum_base + 1 - 5 * journee + random.gauss(0, 1.2)
            self.climat_hist.append((ts, int(round(borner(t, 0, 50))), int(round(borner(h, 20, 90)))))

        # passages : surtout entre 7h et 23h
        instants = []
        while len(instants) < random.randint(18, 30):
            ts = random.uniform(debut, maintenant - 120)
            heure = datetime.fromtimestamp(ts).hour
            if 7 <= heure <= 22 or random.random() < 0.08:
                instants.append(ts)
        for ts in sorted(instants):
            self.mouvements.append((ts, False))

    # --- boucle de fond ---------------------------------------------------

    def tick(self, dt):
        with self.lock:
            now = time.time()
            if self.porte_ouverte and self.fermeture_auto_a and now >= self.fermeture_auto_a:
                self.fermer("fermeture automatique")

            # Le vrai PIR détecte n'importe quoi pendant sa stabilisation
            if not self.pir_pret() and random.random() < 0.12 * dt:
                self.detection("faux positif PIR")

            if self.pir_pret() and self.cfg.mouvements_auto > 0:
                if random.random() < dt / self.cfg.mouvements_auto:
                    self.detection("passage simulé")

            if now >= self.prochain_echantillon:
                self.prochain_echantillon = now + 60
                if not self.pannes["dht"]:
                    t, h = self._valeurs_climat(now)
                    self.climat_hist.append((now, t, h))

    # --- vues JSON --------------------------------------------------------

    def etat_complet(self):
        with self.lock:
            dernier = self.mouvements[-1][0] if self.mouvements else None
            climat = self.lire_climat()
            return {
                "horodatage": horodatage(),
                "uptime_s": int(time.time() - self.t0),
                "porte": self.etat_porte(),
                "climat": climat if climat else {"erreur": "Lecture DHT11 échouée"},
                "pir": {
                    "pret": self.pir_pret(),
                    "stabilisation_restante_s": self.stabilisation_restante(),
                    "derniere_detection": horodatage(dernier) if dernier else None,
                },
                "leds": dict(self.leds),
                "buzzer_actif": self.buzzer_actif(),
            }


# ---------------------------------------------------------------------------
# Serveur HTTP
# ---------------------------------------------------------------------------

class Gestionnaire(BaseHTTPRequestHandler):
    server_version = "HubSimulateur/1.0"
    hub = None  # injecté au démarrage

    # --- plomberie --------------------------------------------------------

    def log_message(self, fmt, *args):
        pass  # on affiche nos propres logs plus lisibles

    def _envoyer(self, code, contenu, type_contenu="application/json; charset=utf-8"):
        if isinstance(contenu, (dict, list)):
            donnees = json.dumps(contenu, ensure_ascii=False, indent=2).encode("utf-8")
        else:
            donnees = contenu.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", type_contenu)
        self.send_header("Content-Length", str(len(donnees)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(donnees)

    def _lire_corps(self):
        longueur = int(self.headers.get("Content-Length") or 0)
        brut = self.rfile.read(longueur) if longueur else b""
        if not brut.strip():
            return {}
        try:
            corps = json.loads(brut.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ErreurRequete(400, "Corps JSON invalide")
        if not isinstance(corps, dict):
            raise ErreurRequete(400, "Le corps doit être un objet JSON")
        return corps

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        self._traiter("GET")

    def do_POST(self):
        self._traiter("POST")

    def _traiter(self, methode):
        url = urlparse(self.path)
        chemin = url.path.rstrip("/") or "/"
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        est_api = chemin.startswith("/api")
        corps = None

        try:
            corps = self._lire_corps() if methode == "POST" else None
            if est_api:
                # pannes réseau / firmware simulées (seulement pour l'API agent)
                latence = self.hub.pannes["latence_ms"]
                if latence:
                    time.sleep(latence / 1000)
                if random.random() < self.hub.pannes["taux_erreur"]:
                    raise ErreurRequete(500, "Erreur interne simulée du hub")

            route = ROUTES.get((methode, chemin))
            if route is None:
                if any(c == chemin for (_, c) in ROUTES):
                    raise ErreurRequete(405, f"Méthode {methode} non autorisée sur {chemin}")
                raise ErreurRequete(404, f"Route inconnue : {chemin}")

            code, reponse = route(self.hub, query, corps or {})
        except ErreurRequete as e:
            code, reponse = e.code, {"erreur": e.message}

        if isinstance(reponse, str):
            self._envoyer(code, reponse, "text/html; charset=utf-8")
        else:
            self._envoyer(code, reponse)

        if est_api:
            cible = chemin + (("?" + url.query) if url.query else "")
            self.hub.requete(methode, cible, corps, code, reponse)
            print(f"[{datetime.now():%H:%M:%S}] {methode:4} {cible} -> {code}")


# ---------------------------------------------------------------------------
# Routes de l'API (celles que l'agent utilise)
# ---------------------------------------------------------------------------

def api_etat(hub, q, c):
    return 200, hub.etat_complet()


def api_climat(hub, q, c):
    with hub.lock:
        climat = hub.lire_climat()
    if climat is None:
        raise ErreurRequete(503, "Lecture DHT11 échouée")
    return 200, climat


def api_climat_historique(hub, q, c):
    minutes = entier(q.get("minutes"), "minutes", 1, 1440, defaut=60)
    pas = entier(q.get("pas_minutes"), "pas_minutes", 1, 120, defaut=10)
    limite = time.time() - minutes * 60
    with hub.lock:
        points = [p for p in hub.climat_hist if p[0] >= limite]
    if not points:
        return 200, {"minutes": minutes, "nombre_mesures": 0, "points": []}
    temps = [p[1] for p in points]
    hums = [p[2] for p in points]
    # on garde un point tous les `pas` minutes pour ne pas noyer l'agent
    echantillon = points[::pas]
    return 200, {
        "minutes": minutes,
        "nombre_mesures": len(points),
        "temperature_c": {"min": min(temps), "max": max(temps), "moyenne": round(sum(temps) / len(temps), 1)},
        "humidite_pct": {"min": min(hums), "max": max(hums), "moyenne": round(sum(hums) / len(hums), 1)},
        "pas_minutes": pas,
        "points": [
            {"horodatage": horodatage(ts), "temperature_c": t, "humidite_pct": h}
            for ts, t, h in echantillon
        ],
    }


def api_porte(hub, q, c):
    with hub.lock:
        return 200, hub.etat_porte()


def api_porte_ouvrir(hub, q, c):
    duree = entier(c.get("duree_s"), "duree_s", 0, 60, defaut=hub.cfg.delai_fermeture)
    hub.ouvrir("API", duree)
    with hub.lock:
        return 200, {"ok": True, "porte": hub.etat_porte()}


def api_porte_fermer(hub, q, c):
    changement = hub.fermer("API")
    with hub.lock:
        return 200, {"ok": True, "deja_fermee": not changement, "porte": hub.etat_porte()}


def api_mouvements(hub, q, c):
    if "depuis" in q:
        try:
            limite = datetime.fromisoformat(q["depuis"]).timestamp()
        except ValueError:
            raise ErreurRequete(400, "'depuis' doit être une date ISO, ex. 2026-11-21T14:00:00")
        texte_depuis = q["depuis"]
    else:
        minutes = entier(q.get("minutes"), "minutes", 1, 1440, defaut=60)
        limite = time.time() - minutes * 60
        texte_depuis = horodatage(limite)
    with hub.lock:
        liste = [m for m in hub.mouvements if m[0] >= limite]
    return 200, {
        "depuis": texte_depuis,
        "nombre": len(liste),
        "detections": [
            {"horodatage": horodatage(ts), "pendant_stabilisation": stab}
            for ts, stab in liste
        ],
    }


def api_buzzer(hub, q, c):
    duree = entier(c.get("duree_ms"), "duree_ms", 50, 5000)
    hub.bip(duree)
    hub.evenement(f"Buzzer activé {duree} ms (API)")
    return 200, {"ok": True, "duree_ms": duree}


def api_led(hub, q, c):
    couleur = c.get("couleur")
    allumee = c.get("allumee")
    if couleur not in ("verte", "rouge"):
        raise ErreurRequete(400, "'couleur' doit être 'verte' ou 'rouge'")
    if not isinstance(allumee, bool):
        raise ErreurRequete(400, "'allumee' doit être true ou false")
    with hub.lock:
        hub.leds[couleur] = allumee
        hub.evenement(f"LED {couleur} {'allumée' if allumee else 'éteinte'} (API)")
        return 200, {"ok": True, "leds": dict(hub.leds)}


# ---------------------------------------------------------------------------
# Routes de simulation (pour toi, pas pour l'agent)
# ---------------------------------------------------------------------------

def sim_dashboard(hub, q, c):
    return 200, DASHBOARD_HTML


def sim_etat(hub, q, c):
    etat = hub.etat_complet()
    etat["pannes"] = dict(hub.pannes)
    etat["config"] = {
        "auto_ouverture": hub.cfg.auto_ouverture,
        "delai_fermeture_s": hub.cfg.delai_fermeture,
        "mouvements_auto_s": hub.cfg.mouvements_auto,
    }
    return 200, etat


def sim_journal(hub, q, c):
    n = entier(q.get("n"), "n", 1, 300, defaut=50)
    with hub.lock:
        return 200, list(hub.journal)[-n:][::-1]


def sim_mouvement(hub, q, c):
    hub.detection("simulation manuelle")
    return 200, {"ok": True}


def sim_bouton(hub, q, c):
    hub.ouvrir("bouton manuel", hub.cfg.delai_fermeture)
    return 200, {"ok": True}


def sim_climat(hub, q, c):
    with hub.lock:
        if "temperature" in c:
            hub.temp_base = float(entier(c["temperature"], "temperature", 0, 50))
        if "humidite" in c:
            hub.hum_base = float(entier(c["humidite"], "humidite", 20, 90))
        hub.evenement(f"Simulation : climat de base réglé à {hub.temp_base:.0f} °C / {hub.hum_base:.0f} %")
    return 200, {"ok": True}


def sim_pannes(hub, q, c):
    with hub.lock:
        if "dht" in c:
            if not isinstance(c["dht"], bool):
                raise ErreurRequete(400, "'dht' doit être true ou false")
            hub.pannes["dht"] = c["dht"]
        if "taux_erreur" in c:
            try:
                taux = float(c["taux_erreur"])
            except (TypeError, ValueError):
                raise ErreurRequete(400, "'taux_erreur' doit être un nombre")
            if not 0 <= taux <= 1:
                raise ErreurRequete(400, "'taux_erreur' doit être entre 0 et 1")
            hub.pannes["taux_erreur"] = taux
        if "latence_ms" in c:
            hub.pannes["latence_ms"] = entier(c["latence_ms"], "latence_ms", 0, 10000)
        hub.evenement(f"Simulation : pannes = {hub.pannes}")
        return 200, {"ok": True, "pannes": dict(hub.pannes)}


def sim_reset(hub, q, c):
    hub.reinitialiser(historique=bool(c.get("historique", hub.cfg.historique)))
    return 200, {"ok": True}


ROUTES = {
    ("GET", "/api/etat"): api_etat,
    ("GET", "/api/climat"): api_climat,
    ("GET", "/api/climat/historique"): api_climat_historique,
    ("GET", "/api/porte"): api_porte,
    ("POST", "/api/porte/ouvrir"): api_porte_ouvrir,
    ("POST", "/api/porte/fermer"): api_porte_fermer,
    ("GET", "/api/mouvements"): api_mouvements,
    ("POST", "/api/buzzer"): api_buzzer,
    ("POST", "/api/led"): api_led,

    ("GET", "/"): sim_dashboard,
    ("GET", "/sim/etat"): sim_etat,
    ("GET", "/sim/journal"): sim_journal,
    ("POST", "/sim/mouvement"): sim_mouvement,
    ("POST", "/sim/bouton"): sim_bouton,
    ("POST", "/sim/climat"): sim_climat,
    ("POST", "/sim/pannes"): sim_pannes,
    ("POST", "/sim/reset"): sim_reset,
}


# ---------------------------------------------------------------------------
# Dashboard de simulation
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Simulateur du hub</title>
<style>
  :root {
    --fond: #0f1216; --carte: #181c22; --bord: #2a3038; --texte: #e6e9ee;
    --doux: #8b95a3; --vert: #3ecf6e; --rouge: #ef5350; --accent: #5aa9ff; --jaune: #f4c542;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
         background: var(--fond); color: var(--texte); }
  header { padding: 18px 24px; border-bottom: 1px solid var(--bord);
           display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  header h1 { font-size: 18px; margin: 0; }
  header span { color: var(--doux); }
  main { padding: 20px 24px; display: grid; gap: 16px;
         grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
  .carte { background: var(--carte); border: 1px solid var(--bord); border-radius: 10px; padding: 16px; }
  .carte h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em;
              color: var(--doux); margin: 0 0 12px; font-weight: 600; }
  .grand { font-size: 28px; font-weight: 600; }
  .doux { color: var(--doux); font-size: 13px; }
  .ligne { display: flex; align-items: center; gap: 10px; margin: 6px 0; }
  .led { width: 16px; height: 16px; border-radius: 50%; border: 1px solid var(--bord); background: #222; }
  .led.verte.on { background: var(--vert); box-shadow: 0 0 10px var(--vert); }
  .led.rouge.on { background: var(--rouge); box-shadow: 0 0 10px var(--rouge); }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 99px; font-size: 12px; font-weight: 600; }
  .ok { background: #1c3a27; color: var(--vert); }
  .attente { background: #3a3218; color: var(--jaune); }
  .ko { background: #3d1d1d; color: var(--rouge); }
  button { background: #232a33; color: var(--texte); border: 1px solid var(--bord); border-radius: 7px;
           padding: 7px 12px; cursor: pointer; font: inherit; }
  button:hover { border-color: var(--accent); }
  button.principal { background: var(--accent); color: #08121f; border-color: var(--accent); font-weight: 600; }
  input[type=number] { width: 70px; background: #11151a; color: var(--texte); border: 1px solid var(--bord);
                       border-radius: 6px; padding: 5px; font: inherit; }
  label { display: flex; align-items: center; gap: 8px; margin: 8px 0; }
  .large { grid-column: 1 / -1; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--bord); vertical-align: top; }
  th { color: var(--doux); font-weight: 600; }
  td.code { font-family: ui-monospace, Consolas, monospace; }
  tr.hub td { color: var(--doux); }
  .c2 { color: var(--vert); } .c4 { color: var(--jaune); } .c5 { color: var(--rouge); }
  svg .battant { transition: transform .4s ease; transform-origin: 20px 70px; }
  details summary { cursor: pointer; color: var(--accent); }
  pre { white-space: pre-wrap; margin: 6px 0 0; color: var(--doux); }
</style>
</head>
<body>
<header>
  <h1>Simulateur du hub domotique</h1>
  <span id="horloge">connexion...</span>
</header>
<main>
  <section class="carte">
    <h2>Porte (servo)</h2>
    <div class="ligne">
      <svg width="110" height="80" viewBox="0 0 110 80" aria-hidden="true">
        <rect x="18" y="8" width="74" height="64" fill="none" stroke="#2a3038" stroke-width="2"/>
        <g class="battant" id="battant">
          <rect x="20" y="10" width="70" height="60" fill="#232a33" stroke="#5aa9ff" stroke-width="2"/>
          <circle cx="80" cy="42" r="3" fill="#5aa9ff"/>
        </g>
      </svg>
      <div>
        <div class="grand" id="porte-etat">-</div>
        <div class="doux" id="porte-detail"></div>
      </div>
    </div>
    <div class="doux" id="porte-dates"></div>
  </section>

  <section class="carte">
    <h2>Climat (DHT11)</h2>
    <div class="grand" id="temp">-</div>
    <div class="doux" id="hum"></div>
  </section>

  <section class="carte">
    <h2>Capteur PIR</h2>
    <div id="pir-etat"></div>
    <div class="doux" id="pir-derniere" style="margin-top:8px"></div>
  </section>

  <section class="carte">
    <h2>LEDs et buzzer</h2>
    <div class="ligne"><div class="led verte" id="led-verte"></div> LED verte</div>
    <div class="ligne"><div class="led rouge" id="led-rouge"></div> LED rouge</div>
    <div class="ligne"><span class="badge" id="buzzer">buzzer</span></div>
  </section>

  <section class="carte">
    <h2>Simuler le monde réel</h2>
    <div class="ligne" style="flex-wrap:wrap">
      <button class="principal" onclick="post('/sim/mouvement')">Passage devant le PIR</button>
      <button onclick="post('/sim/bouton')">Bouton manuel</button>
    </div>
    <label>Température <input type="number" id="in-temp" min="0" max="50"> °C</label>
    <label>Humidité <input type="number" id="in-hum" min="20" max="90"> %</label>
    <button onclick="reglerClimat()">Appliquer</button>
  </section>

  <section class="carte">
    <h2>Pannes (pour tester ton agent)</h2>
    <label><input type="checkbox" id="in-dht" onchange="pannes()"> DHT11 en panne</label>
    <label>Erreurs aléatoires <input type="number" id="in-taux" min="0" max="100" step="5" value="0"> %</label>
    <label>Latence <input type="number" id="in-lat" min="0" max="10000" step="100" value="0"> ms</label>
    <div class="ligne">
      <button onclick="pannes()">Appliquer</button>
      <button onclick="if(confirm('Redémarrer le hub ?')) post('/sim/reset')">Redémarrer le hub</button>
    </div>
  </section>

  <section class="carte large">
    <h2>Journal : requêtes de ton agent et événements du hub</h2>
    <table>
      <thead><tr><th>Heure</th><th>Source</th><th>Détail</th><th>Code</th></tr></thead>
      <tbody id="journal"></tbody>
    </table>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
let initialise = false;

async function post(url, corps) {
  await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                     body: JSON.stringify(corps || {}) });
  rafraichir();
}

function reglerClimat() {
  post('/sim/climat', { temperature: +$('in-temp').value, humidite: +$('in-hum').value });
}

function pannes() {
  post('/sim/pannes', {
    dht: $('in-dht').checked,
    taux_erreur: Math.min(100, Math.max(0, +$('in-taux').value)) / 100,
    latence_ms: Math.max(0, Math.round(+$('in-lat').value))
  });
}

function heure(iso) { return iso ? iso.split('T')[1] : '-'; }
function echapper(s) { return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

async function rafraichir() {
  try {
    const e = await (await fetch('/sim/etat')).json();
    $('horloge').textContent = 'en ligne, uptime ' + e.uptime_s + ' s';

    const ouverte = e.porte.etat === 'ouverte';
    $('porte-etat').textContent = ouverte ? 'Ouverte' : 'Fermée';
    $('porte-detail').textContent = 'servo ' + e.porte.angle_servo + '°' +
      (e.porte.fermeture_auto_dans_s != null ? ', fermeture dans ' + e.porte.fermeture_auto_dans_s + ' s' : '');
    $('porte-dates').textContent = 'Dernière ouverture : ' + heure(e.porte.derniere_ouverture);
    $('battant').style.transform = ouverte ? 'scaleX(0.25)' : 'scaleX(1)';

    if (e.climat.erreur) {
      $('temp').innerHTML = '<span class="badge ko">' + e.climat.erreur + '</span>';
      $('hum').textContent = '';
    } else {
      $('temp').textContent = e.climat.temperature_c + ' °C';
      $('hum').textContent = 'Humidité ' + e.climat.humidite_pct + ' %';
    }

    $('pir-etat').innerHTML = e.pir.pret
      ? '<span class="badge ok">Prêt</span>'
      : '<span class="badge attente">Stabilisation : ' + e.pir.stabilisation_restante_s + ' s</span>';
    $('pir-derniere').textContent = 'Dernière détection : ' + heure(e.pir.derniere_detection);

    $('led-verte').classList.toggle('on', e.leds.verte);
    $('led-rouge').classList.toggle('on', e.leds.rouge);
    $('buzzer').className = 'badge ' + (e.buzzer_actif ? 'attente' : '');
    $('buzzer').textContent = e.buzzer_actif ? 'Buzzer actif' : 'Buzzer silencieux';

    if (!initialise) {
      if (!e.climat.erreur) { $('in-temp').value = e.climat.temperature_c; $('in-hum').value = e.climat.humidite_pct; }
      $('in-dht').checked = e.pannes.dht;
      $('in-taux').value = Math.round(e.pannes.taux_erreur * 100);
      $('in-lat').value = e.pannes.latence_ms;
      initialise = true;
    }

    const j = await (await fetch('/sim/journal?n=60')).json();
    $('journal').innerHTML = j.map(l => {
      if (l.type === 'hub') {
        return '<tr class="hub"><td>' + heure(l.horodatage) + '</td><td>hub</td><td>' +
               echapper(l.texte) + '</td><td></td></tr>';
      }
      const corps = l.corps && Object.keys(l.corps).length ? ' ' + JSON.stringify(l.corps) : '';
      return '<tr><td>' + heure(l.horodatage) + '</td><td>agent</td><td class="code">' +
             '<details><summary>' + echapper(l.methode + ' ' + l.chemin + corps) + '</summary><pre>' +
             echapper(JSON.stringify(l.reponse, null, 2)) + '</pre></details></td>' +
             '<td class="code c' + String(l.code)[0] + '">' + l.code + '</td></tr>';
    }).join('');
  } catch (err) {
    $('horloge').textContent = 'simulateur injoignable';
  }
}

rafraichir();
setInterval(rafraichir, 1000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Démarrage
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Simulateur du hub domotique ESP32")
    p.add_argument("--hote", default="127.0.0.1", help="adresse d'écoute (défaut 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080, help="port HTTP (défaut 8080)")
    p.add_argument("--stabilisation", type=int, default=60,
                   help="durée de stabilisation du PIR en secondes (défaut 60, 0 pour désactiver)")
    p.add_argument("--delai-fermeture", type=int, default=5,
                   help="fermeture automatique de la porte en secondes (défaut 5)")
    p.add_argument("--mouvements-auto", type=float, default=0,
                   help="un passage simulé toutes les N secondes en moyenne (0 = désactivé)")
    p.add_argument("--historique", action="store_true",
                   help="pré-remplit 24 h de mesures et de passages")
    p.add_argument("--temperature", type=int, default=22, help="température de base (°C)")
    p.add_argument("--humidite", type=int, default=45, help="humidité de base (%%)")
    p.add_argument("--pas-auto-ouverture", dest="auto_ouverture", action="store_false",
                   help="le PIR n'ouvre plus la porte automatiquement")
    cfg = p.parse_args()

    hub = Hub(cfg)
    Gestionnaire.hub = hub

    def boucle():
        precedent = time.time()
        while True:
            time.sleep(0.5)
            now = time.time()
            hub.tick(now - precedent)
            precedent = now

    threading.Thread(target=boucle, daemon=True).start()

    serveur = ThreadingHTTPServer((cfg.hote, cfg.port), Gestionnaire)
    base = f"http://{cfg.hote}:{cfg.port}"
    print(f"Simulateur du hub lancé")
    print(f"  Dashboard : {base}/")
    print(f"  API agent : {base}/api/etat")
    print(f"  PIR en stabilisation pendant {cfg.stabilisation} s")
    print("Ctrl+C pour arrêter\n")
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du simulateur")
        serveur.server_close()


if __name__ == "__main__":
    main()
