# LiveKit Voice Agent — Prise de rendez-vous téléphonique

Agent vocal **téléphonique** qui répond aux appels entrants, demande à l'appelant la date de rendez-vous souhaitée, raccroche poliment, puis enregistre l'appel via une API Flask.

Stack : **LiveKit Agents (Python)** + **Twilio SIP trunk** + **OpenAI** (`gpt-4.1` LLM, `gpt-4o-transcribe` STT, `gpt-4o-mini-tts` TTS) + **Flask** + **SQLite/SQLAlchemy**.

## Sommaire

1. [Démarrage rapide (local, sans téléphone)](#1-démarrage-rapide-local-sans-téléphone)
2. [Mise en prod téléphonique (Twilio + LiveKit SIP)](#2-mise-en-prod-téléphonique-twilio--livekit-sip)
3. [API : endpoint `/end-of-call`](#3-api--endpoint-end-of-call)
4. [Tests](#4-tests)
5. [Architecture (Partie 2)](#5-architecture-partie-2)
6. [Choix techniques](#6-choix-techniques)
7. [Limites connues](#7-limites-connues)

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

### Lancer l'agent (en parallèle, autre terminal)

```bash
# téléchargement initial des modèles (Silero VAD + turn detector)
python -m agent.main download-files

# mode dev avec auto-reload
python -m agent.main dev
```

### Tester dans le navigateur

Ouvrir [agents-playground.livekit.io](https://agents-playground.livekit.io), se connecter au projet LiveKit, et démarrer une conversation. L'agent répond en français, demande la date de RDV, et raccroche après « au revoir ». L'API Flask doit logger un `end-of-call recorded`.

---

## 2. Mise en prod téléphonique (Twilio + LiveKit SIP)

Le code agent est déjà prêt pour le SIP : il détecte le `participant.kind == SIP`, récupère le numéro via `sip.phoneNumber`, et raccroche en supprimant la room (ce qui envoie un `SIP BYE` côté Twilio).

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
pip install pytest
pytest -q
```

Couvre `/end-of-call` : persistance, validation, idempotence sur `room_name`.

---

## 5. Architecture (Partie 2)

Diagrammes Mermaid (rendus directement sur GitHub) → [docs/architecture.md](docs/architecture.md).

Points couverts :

- prise de RDV via une **API externe** (booking en async via un bus de messages, hors session vocale, pour ne pas bloquer l'appelant),
- **notification email** sur webhook LiveKit `room_started`,
- chronologie complète d'un appel,
- ce qui change entre le MVP livré ici et la prod.

---

## 6. Choix techniques

| Choix | Pourquoi |
| --- | --- |
| **Python pour les deux services** | LiveKit Agents est natif Python ; Flask aussi. Un seul écosystème, un seul `requirements.txt`. |
| **GPT-4.1 + STT/TTS séparés** | GPT-4.1 pour le raisonnement (validation des dates relatives), `gpt-4o-transcribe` pour la latence STT, `gpt-4o-mini-tts` pour la qualité voix. Plus de marge de tuning qu'une approche Realtime monolithique. |
| **`BVCTelephony` noise cancellation** | L'audio SIP est en bande étroite (G.711) et bruité. Le profil BVC tuned-telephony est conçu exactement pour ça. |
| **Tool calling pour la date et le hangup** | Force le LLM à structurer la sortie. La date est typée (`date_iso`) → on évite le parsing libre côté backend. |
| **Idempotence sur `room_name`** | L'API peut être appelée deux fois (tool `end_call` + handler `participant_disconnected` si l'appelant raccroche en premier). Une seule ligne en DB. |
| **SQLite + SQLAlchemy** | Zéro setup. SQLAlchemy pour pouvoir migrer vers Postgres en changeant juste `DATABASE_URL`. |
| **Pas de Pydantic / Marshmallow côté API** | Un seul endpoint, validation manuelle suffit. Garder la dépendance pour quand il y aura plus de surface. |
| **`delete_room` pour raccrocher** | API documentée pour terminer une session côté serveur. Sur trunk SIP, ça envoie `BYE` → vrai raccroché téléphonique. |

## 7. Limites connues

- **Pas de retry persistant** sur le POST `/end-of-call` : si l'API Flask est down au moment du hangup, l'appel n'est pas enregistré (le log d'erreur reste, mais aucune retry queue locale). En prod, il faudrait un outbox local côté worker, ou passer par un bus.
- **Pas d'auth** sur `/end-of-call`. En prod : HMAC partagé ou mTLS entre l'agent et l'API.
- **Pas de transcript stocké**. Si on veut auditer la conversation, brancher `session.history` au moment du hangup et le pousser dans une colonne `transcript JSONB`.
- **Date relative** (« jeudi prochain ») : c'est le LLM qui la résout en ISO 8601 avant d'appeler le tool. Pas de fallback déterministe côté backend si le LLM se trompe ; en prod il faudrait re-valider côté API à partir de `appointment_raw` + un `dateparser` français.
- **SQLite write concurrency** : OK pour le test. En prod → Postgres.
- **Pas d'observabilité** au-delà des logs stdout. Manquent : traces (OpenTelemetry), métriques de latence STT/LLM/TTS, dashboard du taux de RDV captés.
