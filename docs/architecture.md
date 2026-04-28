# Architecture (Partie 2)

Vue d'ensemble du système couvrant le brief :

- prise de rendez-vous via une **API externe** déclenchée par la demande client,
- **notification email** à chaque nouvel appel entrant.

L'idée directrice : pendant l'appel on reste sur le **chemin temps-réel** (LiveKit + LLM), tout ce qui n'a pas besoin d'être synchrone (booking, email, audit) part sur un **chemin asynchrone** déclenché par des événements. Ça isole la latence vocale du reste et permet de retry sans perturber l'appelant.

## 1. Vue système

```mermaid
flowchart LR
    classDef ext fill:#fff5e6,stroke:#e89a3c,color:#222
    classDef rt  fill:#e8f1ff,stroke:#3b82f6,color:#222
    classDef be  fill:#eafff0,stroke:#10b981,color:#222

    Caller(("📞 Appelant")):::ext
    Twilio[Twilio<br/>SIP trunk]:::ext
    BookingAPI[API booking<br/>Cal.com / Google Calendar]:::ext
    EmailProvider[Email provider<br/>SES / Postmark]:::ext
    Staff[(Boîte mail staff)]:::ext

    subgraph LK[LiveKit Cloud]
        SIPSvc[SIP service<br/>inbound trunk + dispatch rule]:::rt
        Room[Room temporaire]:::rt
        Hooks[Webhooks<br/>room_started / participant_joined]:::rt
    end

    subgraph Worker[Agent worker - Python]
        Agent[AppointmentAgent<br/>function_tools:<br/>record_appointment_date<br/>end_call]:::rt
    end

    OpenAI[(OpenAI<br/>gpt-4.1<br/>gpt-4o-transcribe<br/>gpt-4o-mini-tts)]:::ext

    subgraph BE[Backend]
        API[Flask API<br/>POST /end-of-call<br/>POST /livekit-webhook]:::be
        DB[(Calls DB<br/>Postgres en prod)]:::be
        Bus[[Message bus<br/>Redis / SQS]]:::be
        BookingWk[Booking worker]:::be
        EmailWk[Email worker]:::be
    end

    Caller -- PSTN --> Twilio
    Twilio -- SIP INVITE --> SIPSvc
    SIPSvc -- crée --> Room
    SIPSvc -- dispatch job --> Agent
    Agent <-- WebRTC audio --> Room
    Agent -- STT / LLM / TTS --> OpenAI

    Hooks -- room_started --> API
    API -- call.started --> Bus
    Bus --> EmailWk
    EmailWk --> EmailProvider --> Staff

    Agent -- POST /end-of-call --> API
    API --> DB
    API -- call.ended --> Bus
    Bus --> BookingWk
    BookingWk --> BookingAPI

    Agent -- delete_room / hangup --> SIPSvc
    SIPSvc -- SIP BYE --> Twilio
    Twilio -- raccroche --> Caller
```

## 2. Chronologie d'un appel

```mermaid
sequenceDiagram
    autonumber
    actor C as Appelant
    participant T as Twilio
    participant L as LiveKit
    participant A as Agent
    participant O as OpenAI
    participant API as Flask API
    participant Q as Bus
    participant E as Email worker
    participant B as Booking worker

    C->>T: compose le numéro
    T->>L: SIP INVITE
    L->>A: dispatch (job)
    A->>L: join room
    L-->>API: webhook room_started
    API->>Q: publish call.started
    Q->>E: consume
    E-->>API: notif "nouvel appel +33…" envoyée

    loop Conversation
        C->>A: parle
        A->>O: STT
        O-->>A: texte
        A->>O: LLM (gpt-4.1)
        O-->>A: réponse + éventuel tool_call
        A->>O: TTS
        O-->>A: audio
        A->>C: parle
    end

    Note over A: tool record_appointment_date(date_iso)
    C->>A: « au revoir »
    Note over A: tool end_call
    A->>API: POST /end-of-call
    API->>API: persist row
    API->>Q: publish call.ended
    A->>L: delete_room
    L->>T: SIP BYE
    T-->>C: ligne raccrochée
    Q->>B: consume
    B->>B: book(date, phone) sur API externe
```

## 3. Choix d'architecture

| Décision | Raison |
| --- | --- |
| **Booking en async, hors session vocale** | La prise de RDV externe peut prendre 1-3 s. À ce délai, l'appelant attend ou raccroche. On confirme oralement la date pendant l'appel ; le booking effectif part dans le bus et est rejoué si l'API externe est down. |
| **Email via webhook LiveKit `room_started`** | Source de vérité côté LiveKit (un appel = une room). Pas besoin pour l'agent worker de notifier en plus, ce qui éviterait les double-notifs si le worker crash après le premier message. |
| **Bus de messages entre l'API et les workers** | Découplage classique : retry, dead-letter, scaling indépendant des workers booking/email. En MVP on peut commencer avec une simple table `outbox` SQL polling-based avant de sortir Redis/SQS. |
| **Stocker `appointment_raw` en plus du `appointment_date` ISO** | Audit. Si la normalisation LLM date ISO se trompe, on peut rejouer à partir du texte original. |
| **Hangup via `delete_room`** | Force LiveKit à envoyer SIP BYE sur le trunk, donc le téléphone de l'appelant raccroche réellement. C'est l'API documentée pour terminer une session côté serveur. |
| **`participant_disconnected` sur le worker** | Si l'appelant raccroche en premier, on persiste quand même l'appel. L'idempotence est garantie par `room_name UNIQUE` côté DB + un flag local côté agent. |

## 4. Ce qui change en prod par rapport au MVP livré

- **Postgres** au lieu de SQLite (SQLite est OK pour le test mais 1 writer/many readers en prod c'est limite).
- **Bus de messages réel** (Redis Streams / SQS) au lieu d'appels HTTP synchrones depuis l'API.
- **Workers séparés** (booking, email, transcription) déployés indépendamment.
- **Auth signée** sur le webhook LiveKit (`Authorization: Bearer <token>`) et sur `/end-of-call` (HMAC partagé entre l'agent et l'API).
- **Observabilité** : logs structurés (JSON), traces OpenTelemetry, métriques sur la latence STT→LLM→TTS et sur le taux de RDV captés.
- **Numéro Twilio + dispatch rule** par locataire si on multi-tenant.
