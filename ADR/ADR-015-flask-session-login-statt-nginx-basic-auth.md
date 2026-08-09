# ADR-015: Flask-Session-Login statt nginx Basic-Auth

**Datum:** 2026-08-09
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Seit dem Fable-5-Review (2026-07-07, Finding K4) war bekannt: `GET /api/portfolio` liefert Kaufpreise/P&L öffentlich, `POST /api/config`/`POST`/`PATCH`/`DELETE /api/portfolio` sind ungeschützt schreibbar. Es brauchte eine Architekturentscheidung, welcher Zugriffsschutz für ein Single-User-Tool angemessen ist.

## Entscheidung

**Flask-Session-Login** (eigener `/login`-Endpoint, Session-Cookie, `@login_required`-Decorator auf 23 Routen) statt nginx Basic-Auth – nach exakt dem bereits produktiven Muster von Claude Remote (`passlib.apache.HtpasswdFile`, `app.secret_key`, `login_required`-Decorator).

## Begründung

Ursprünglich wurde **nginx Basic-Auth** gewählt und implementiert (strukturell die robustere Variante: Durchsetzung auf Infrastruktur-Ebene, unabhängig von App-Code-Korrektheit – kein Risiko eines vergessenen `@login_required` auf einem der vielen neuen Endpoints dieser Session). Diese Einschätzung war technisch korrekt, aber **live widerlegt durch einen echten, reproduzierbaren Funktionsfehler**:

- Basic-Auth funktionierte über `curl` und in einem normalen Browser-Tab einwandfrei (401 + korrekter `WWW-Authenticate`-Header).
- In der **installierten Home-Bildschirm-PWA** (Josefs tatsächliches Nutzungsszenario) erschien **kein Login-Dialog**, nur ein nackter 401-Seiteninhalt.
- Root Cause verifiziert: `pwa/sw.js` fängt `document`-Navigationsanfragen ab und ruft selbst `fetch(e.request)` auf (network-first-Strategie). `fetch()` wirft bei einer 401-Antwort keine Exception (nur bei echten Netzwerkfehlern) – der Service Worker bekommt die 401-Response normal zurück und rendert sie als Seiteninhalt. Der native Basic-Auth-Dialog des Browsers wird nur bei einer direkten Top-Level-Navigation ausgelöst, nicht wenn ein Service Worker die Anfrage im Namen der Seite stellt.

Session-Cookies + normale HTTP-Redirects (302 auf `/login`) funktionieren dagegen zuverlässig auch innerhalb des Service-Worker-Fetch-Handlers – `fetch()` folgt Redirects transparent, die Login-Seite wird ganz normal als Dokument gerendert und im Browser angezeigt. Damit ist Flask-Session-Login für PWA-Nutzung strukturell die richtige Wahl, nicht nur eine Geschmacksfrage.

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| nginx Basic-Auth (ursprünglich gewählt) | Live als nicht funktionsfähig in der installierten PWA verifiziert (Service-Worker-Fetch-Interception + Basic-Auth-Interaktion) – trotz theoretisch besserer Sicherheitseigenschaften in der Praxis unbrauchbar für Josefs tatsächliches Nutzungsszenario |
| Token-Header (`X-API-Token`) | Hätte clientseitig eine Token-Ablage + Anpassung an jeder bestehenden `fetch()`-Stelle im PWA-Code gebraucht (dutzende Stellen) – strukturell mehr Fläche für Fehler als ein zentraler Cookie-basierter Login, kein klarer Vorteil für einen Single-User-Fall |
| IP-Allowlist | Unpraktikabel für eine mobil genutzte PWA ohne stabile/statische IP |

## Gilt unter

Jeder neue `@app.route()` muss manuell mit `@login_required` versehen werden – anders als bei nginx-Location-weitem Basic-Auth gibt es keinen automatischen Blanket-Schutz. Muss bei künftigen Endpoints aktiv gegengecheckt werden (siehe Pitfall-Eintrag in `CLAUDE.md`).

## Konsequenzen

- `requirements.txt` um `passlib>=1.7` erweitert.
- Neue Dateien: `pwa/login.html`, `.session_key` (autogeneriert, gitignored, `chmod 600`).
- Session-Dauer 30 Tage (Sliding Window) statt Claude Remotes 15 Minuten – bewusst abweichend, da hier nur Portfolio-Einsicht (kein Server-/SSH-Zugriff) auf dem Spiel steht.
- `manifest.json`/`sw.js`/Icons bleiben bewusst öffentlich (keine sensiblen Daten, verhindert dass der Service Worker beim ersten Cache-Install eine Login-Weiterleitung statt der echten PWA-Shell cached).
