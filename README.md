# LiveKit Voice Agent — Prise de rendez-vous téléphonique

Agent vocal **téléphonique** qui répond aux appels entrants, demande à l'appelant la date de rendez-vous souhaitée, raccroche poliment, puis enregistre l'appel via une API Flask.

**Stack :** LiveKit Agents (Python) · Twilio SIP trunk · OpenAI (`gpt-4.1` LLM, `gpt-4o-transcribe` STT, `gpt-4o-mini-tts` TTS) · Flask · SQLite/SQLAlchemy.

---

## Réponse au brief

| Partie | Livrable | Où |
| --- | --- | --- |
| **1 — Code Flask + agent LiveKit** | API `POST /end-of-call` + agent vocal qui demande la date, raccroche, et appelle l'API | [`api/`](api/), [`agent/`](agent/), [`tests/`](tests/) |
| **2 — Architecture** | Diagrammes système + séquence couvrant booking externe (async) et notification email (webhook LiveKit) | [`docs/architecture.md`](docs/architecture.md) |

## Pour l'évaluateur

- L'API Flask est testable localement sans compte LiveKit/Twilio : `pip install -r requirements-dev.txt && pytest -q`.
- L'agent LiveKit est conçu pour fonctionner en playground LiveKit et en appel SIP, sous réserve de fournir les credentials LiveKit/OpenAI.
- La partie architecture est volontairement séparée du MVP implémenté : elle décrit la cible prod sans alourdir le code du test.
- Les limites assumées sont explicitées en fin de README : pas d'auth, pas de retry persistant, SQLite pour rester léger.

## Guide de lecture (≈ 5 min)

1. **Ce README** — choix techniques, raisonnement, ce que j'ai reporté.
2. [`docs/architecture.md`](docs/architecture.md) — vision système (Partie 2).
3. [`agent/main.py`](agent/main.py) — l'agent, ~200 lignes, le cœur du test.
4. [`api/app.py`](api/app.py) — l'endpoint, ~50 lignes.
5. [`tests/test_api.py`](tests/test_api.py) — 3 tests verts (`pytest -q` ✓).

## Flow d'un appel

1. Appelant compose le numéro → Twilio SIP trunk → LiveKit SIP → room créée → worker dispatché.
2. Léa salue et demande la date de rendez-vous.
3. Si la date est relative (« jeudi prochain »), le LLM la résout en absolu et reformule pour confirmation.
4. Le LLM appelle le tool **`record_appointment_date(date_iso, raw_text)`** — typage forcé en ISO 8601 + texte original conservé pour audit.
5. Léa remercie, attend « au revoir ».
6. Le LLM appelle le tool **`end_call`** → wait for playout → POST `/end-of-call` → DB → `delete_room` → SIP BYE → téléphone raccroché.

---

## Sommaire

1. [Démarrage rapide (local, sans téléphone)](#1-démarrage-rapide-local-sans-téléphone)
2. [Mise en prod téléphonique (Twilio + LiveKit SIP)](#2-mise-en-prod-téléphonique-twilio--livekit-sip)
3. [API : endpoint `/end-of-call`](#3-api--endpoint-end-of-call)
4. [Tests](#4-tests)
5. [Choix techniques](#5-choix-techniques)
6. [Idempotence à deux chemins](#6-idempotence-à-deux-chemins)
7. [Ce que j'ai délibérément reporté (« minimum nécessaire »)](#7-ce-que-jai-délibérément-reporté--minimum-nécessaire-)

---

## 1. Démarrage rapide (local, sans téléphone)

Pour vérifier le code sans monter un trunk SIP, on teste l'agent dans le **playground LiveKit** (navigateur). Le code est le même qu'en téléphonique — seule l'origine du participant change.

### Pré-requis

- Python 3.11+
- Un compte [LiveKit Cloud](https://cloud.livekit.io) (gratuit) → `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`
- Une clé OpenAI avec accès `gpt-4.1`

### Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# remplis LIVEKIT_* et OPENAI_API_KEY
```

### Lancer l'API Flask

```bash
python -m api.app
# → INFO  api: Serving on http://0.0.0.0:5001
```

### Lancer l'agent (autre terminal)

```bash
# téléchargement initial des modèles (Silero VAD + turn detector multilingue)
python -m agent.main download-files

# mode dev avec auto-reload
python -m agent.main dev
```

### Tester dans le navigateur

Ouvrir [agents-playground.livekit.io](https://agents-playground.livekit.io), se connecter au projet LiveKit, démarrer une conversation. L'agent répond en français, demande la date de RDV, et raccroche après « au revoir ». L'API Flask doit logger un `end-of-call recorded`.

---

## 2. Mise en prod téléphonique (Twilio + LiveKit SIP)

Le code agent est conçu pour le SIP : il récupère le numéro via les attributs LiveKit (`sip.phoneNumber` / `sip.from`) et raccroche en supprimant la room, ce qui envoie `SIP BYE` côté trunk.

### a. Numéro et trunk Twilio

1. Acheter un numéro français dans la console Twilio.
2. Créer un **Elastic SIP Trunk** Twilio.
3. Côté Origination, pointer vers le SIP URI LiveKit : `sip:<your-project>.sip.livekit.cloud`.

### b. Trunk inbound + dispatch rule côté LiveKit

Avec le CLI [`livekit-cli`](https://github.com/livekit/livekit-cli) :

```bash
# 1. déclarer le trunk inbound (le numéro Twilio)
lk sip inbound create infra/livekit-sip-trunk.json
# → renvoie un trunk-id, à coller dans dispatch-rule.json

# 2. déclarer la règle de dispatch qui mappe le trunk → notre agent nommé "appointment-agent"
lk sip dispatch create infra/livekit-dispatch-rule.json
```

Le worker doit être démarré avec un `agent_name` qui matche la dispatch rule — c'est déjà le cas dans [`agent/main.py`](agent/main.py) :

```python
WorkerOptions(entrypoint_fnc=entrypoint, agent_name="appointment-agent")
```

> Les deux JSON dans [`infra/`](infra/) sont des shapes de référence. Vérifier avec `lk sip --help` si la version installée du CLI a fait évoluer les clés.

### c. Lancement en prod

```bash
python -m agent.main start    # mode worker, sans auto-reload
```

Désormais, tout appel entrant sur le numéro Twilio :

1. Arrive sur LiveKit SIP via le trunk Twilio.
2. Une room `call-<uuid>` est créée.
3. Le worker reçoit un job, l'agent rejoint, la conversation démarre.
4. À la fin, l'agent POST `/end-of-call`, supprime la room → Twilio envoie `BYE` → le téléphone raccroche.

---

## 3. API : endpoint `/end-of-call`

### POST `/end-of-call`

```http
POST /end-of-call HTTP/1.1
Content-Type: application/json

{
  "room_name": "call-9f3e21",
  "caller_phone": "+33612345678",
  "appointment_date": "2026-05-12",
  "appointment_raw": "le 12 mai"
}
```

**Réponses :**

| Code | Cas |
| --- | --- |
| `201` | Appel persisté |
| `200` | Appel déjà persisté pour ce `room_name` (idempotent — l'agent a retry) |
| `400` | `room_name` manquant |

**Schéma de la table `calls` :**

| Colonne | Type | Note |
| --- | --- | --- |
| `id` | int PK | autoincrement |
| `room_name` | str UNIQUE | clé naturelle pour idempotence |
| `caller_phone` | str? | depuis `sip.phoneNumber` |
| `appointment_date` | str? | ISO 8601 (`AAAA-MM-JJ`) |
| `appointment_raw` | str? | texte original prononcé, pour audit |
| `ended_at` | datetime | `now()` |

### GET `/health`

Healthcheck pour orchestration.

---

## 4. Tests

```bash
pip install -r requirements-dev.txt
pytest -q
# → 3 passed
```

Couvre `/end-of-call` : persistance, validation du champ requis, idempotence sur `room_name`. Vérifié en exécution — **3/3 verts**.

---

## 5. Choix techniques

| Choix | Pourquoi |
| --- | --- |
| **Python pour les deux services** | LiveKit Agents est natif Python ; Flask aussi. Un seul écosystème, un seul `requirements.txt`. |
| **GPT-4.1 + STT/TTS séparés** | GPT-4.1 pour le raisonnement (résolution des dates relatives type « jeudi prochain »), `gpt-4o-transcribe` pour la latence STT, `gpt-4o-mini-tts` pour la qualité voix. Plus de marge de tuning qu'une approche Realtime monolithique. |
| **`BVCTelephony` noise cancellation** | L'audio SIP est en bande étroite (G.711) et bruité. Le profil BVC tuned-telephony est conçu exactement pour ça. |
| **Turn detector multilingue** | Silero VAD seul tend à couper les locuteurs en milieu de phrase. Le turn detector estime la fin réelle d'énoncé — turn-taking nettement plus naturel. |
| **Tool calling pour la date et le hangup** | Force le LLM à structurer sa sortie. La date est typée (`date_iso`) → on évite le parsing libre côté backend. |
| **`appointment_raw` à côté de `appointment_date`** | Audit. Si la normalisation LLM date ISO se trompe, on rejoue à partir du texte original au lieu de perdre l'info. |
| **`delete_room` pour raccrocher** | API documentée pour terminer une session côté serveur. Sur trunk SIP, ça envoie `BYE` → vrai raccroché téléphonique. Fonctionne aussi en mode web sans branche conditionnelle. |
| **SQLite + SQLAlchemy** | Zéro setup. SQLAlchemy pour pouvoir migrer vers Postgres en changeant juste `DATABASE_URL`. |
| **Pas de Pydantic / Marshmallow côté API** | Un seul endpoint, validation manuelle suffit. Garder la dépendance pour quand il y aura plus de surface. |
| **Repo segmenté `api/ agent/ docs/ infra/`** | Le test mélange deux services (un web, un worker temps-réel) + de la config infra + de la doc. Les regrouper à plat ferait perdre la lisibilité ; les séparer par fonction la rend immédiate. |

---

## 6. Idempotence à deux chemins

Un appel peut se terminer de deux façons :

- **L'agent termine proprement** — le LLM appelle le tool `end_call` après le « au revoir » de l'appelant.
- **L'appelant raccroche en premier** — il appuie sur la touche fin de son téléphone avant que le LLM n'arrive au tool.

Si on ne gère que le premier cas, on perd l'enregistrement quand l'appelant coupe court. Si on gère les deux naïvement, on insère deux fois la même ligne ou on POST deux fois sur l'API.

La solution mise en place verrouille l'idempotence sur **quatre couches qui se renforcent** :

1. **Flag local `_finalized`** sur l'instance d'agent → court-circuite la 2ᵉ exécution dans le même worker.
2. **Handler `participant_disconnected`** → couvre le cas où l'appelant raccroche en premier.
3. **Tool `end_call`** → couvre le cas conversationnel normal.
4. **`UNIQUE` sur `calls.room_name`** côté DB → safety-net si jamais deux POST passent (race entre 2 et 3).

Le résultat : peu importe par quel chemin l'appel se termine, on a **exactement une ligne** en DB et **un seul POST** sur l'API. Le code reste simple parce que les deux chemins convergent vers la même méthode `_finalize_call()`.

Voir [`agent/main.py:94-116`](agent/main.py).

---

## 7. Ce que j'ai délibérément reporté (« minimum nécessaire »)

Le brief insiste sur le *minimum nécessaire*. Voici la matrice de ce que j'ai gardé en MVP vs reporté à la prod, avec le raisonnement.

| Sujet | Choix MVP | Pour la prod | Raisonnement |
| --- | --- | --- | --- |
| **Auth sur `/end-of-call`** | Aucune | HMAC partagé entre agent et API, ou mTLS | Endpoint en localhost pendant le test ; ajouter HMAC = ~10 lignes mais hors-scope du brief. |
| **Retry persistant sur le POST end-of-call** | Try/except + log | Outbox local côté worker ou bus de messages | Si Flask est down au moment du hangup, on perd l'enregistrement. C'est l'angle mort assumé. |
| **Stockage du transcript** | Non | `session.history` → colonne `transcript JSONB` | Pas demandé par le brief. 5 lignes à ajouter quand voulu. |
| **Validation déterministe de la date** | LLM résout en ISO ; pas de re-validation côté API | `dateparser` en français côté API à partir de `appointment_raw` | C'est précisément pour ça qu'on garde `appointment_raw` — le rejeu est possible quand on en aura besoin. |
| **Base de données** | SQLite (1 fichier, 0 setup) | Postgres | Concurrence d'écriture limitée en SQLite. `DATABASE_URL` permet de switch sans toucher au code. |
| **Observabilité** | Logs stdout structurés | Traces OpenTelemetry, métriques latence STT/LLM/TTS, dashboard taux de capture RDV | Logs suffisent pour le test ; les métriques deviennent critiques quand on scale. |
| **Booking externe** | Out-of-scope du code, présent dans le diagramme Partie 2 | Worker async qui consomme `call.ended` et appelle Cal.com / Google Calendar | Volontairement hors session vocale (latence externe inacceptable pendant l'appel). |
| **Notification email** | Out-of-scope du code, présent dans le diagramme Partie 2 | Webhook LiveKit `room_started` → bus → worker email | LiveKit est la source de vérité d'un appel, donc le webhook est l'accroche naturelle. |
