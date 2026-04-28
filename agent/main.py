import asyncio
import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomInputOptions,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import noise_cancellation, openai, silero

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("appointment-agent")

API_URL = os.getenv("FLASK_API_URL", "http://localhost:5001")

INSTRUCTIONS = """\
Tu es Léa, assistante téléphonique d'un cabinet qui prend des rendez-vous.
Tu parles uniquement français, ton naturel, chaleureux et concis (1-2 phrases max).

Déroulé strict :
1. Salue brièvement l'appelant et demande la date de rendez-vous souhaitée.
2. Si la date est ambiguë (« jeudi prochain », « la semaine prochaine »), reformule
   et confirme une date absolue avant de l'enregistrer.
3. Dès que la date est claire, appelle l'outil `record_appointment_date` avec
   la date au format ISO 8601 (AAAA-MM-JJ) et le texte original prononcé.
4. Remercie l'appelant et invite-le à dire au revoir.
5. Lorsque l'appelant dit au revoir (ou équivalent), appelle l'outil `end_call`.

Règles :
- Ne demande aucune autre information que la date.
- Si on te pose une question hors sujet, recentre poliment.
- Tu es au téléphone : phrases courtes, pas de listes, pas d'emojis.
"""


class AppointmentAgent(Agent):
    def __init__(self, *, ctx: JobContext, caller_phone: Optional[str]) -> None:
        super().__init__(instructions=INSTRUCTIONS)
        self._ctx = ctx
        self.caller_phone = caller_phone
        self.appointment_date: Optional[str] = None
        self.appointment_raw: Optional[str] = None
        self._finalized = False

    @function_tool
    async def record_appointment_date(
        self,
        ctx: RunContext,
        date_iso: str,
        raw_text: str,
    ) -> str:
        """Enregistre la date de rendez-vous donnée par l'appelant.

        Args:
            date_iso: Date au format ISO 8601 (AAAA-MM-JJ). Si l'appelant donne
                un jour relatif (« jeudi prochain »), il faut d'abord confirmer
                la date absolue avec lui avant d'appeler cet outil.
            raw_text: Texte exact prononcé par l'appelant, pour l'audit.
        """
        self.appointment_date = date_iso
        self.appointment_raw = raw_text
        logger.info("appointment captured iso=%s raw=%r", date_iso, raw_text)
        return "Date enregistrée."

    @function_tool
    async def end_call(self, ctx: RunContext) -> str:
        """Raccroche l'appel après que l'appelant a dit au revoir."""
        logger.info("end_call tool invoked")

        # Let the goodbye finish playing before tearing down the SIP leg,
        # otherwise the caller hears the line drop mid-phrase.
        speech = ctx.session.current_speech
        if speech is not None:
            await speech.wait_for_playout()

        await self._finalize_call()
        return "Appel terminé."

    async def _finalize_call(self) -> None:
        if self._finalized:
            return
        self._finalized = True

        room_name = self._ctx.room.name

        await _post_end_of_call(
            room_name=room_name,
            caller_phone=self.caller_phone,
            appointment_date=self.appointment_date,
            appointment_raw=self.appointment_raw,
        )

        # Deleting the room disconnects every participant including the SIP
        # leg, which makes LiveKit send BYE on the Twilio trunk → the caller's
        # phone hangs up.
        try:
            await self._ctx.api.room.delete_room(
                api.DeleteRoomRequest(room=room_name)
            )
        except Exception:
            logger.exception("failed to delete room %s", room_name)


async def _post_end_of_call(
    *,
    room_name: str,
    caller_phone: Optional[str],
    appointment_date: Optional[str],
    appointment_raw: Optional[str],
) -> None:
    payload = {
        "room_name": room_name,
        "caller_phone": caller_phone,
        "appointment_date": appointment_date,
        "appointment_raw": appointment_raw,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(f"{API_URL}/end-of-call", json=payload)
            r.raise_for_status()
            logger.info("end-of-call posted status=%s body=%s", r.status_code, r.json())
    except Exception:
        logger.exception("failed to POST /end-of-call payload=%s", payload)


def _extract_caller_phone(participant: rtc.RemoteParticipant) -> Optional[str]:
    # LiveKit's SIP service sets these attributes on the SIP participant when
    # an inbound call is bridged from the Twilio trunk.
    return (
        participant.attributes.get("sip.phoneNumber")
        or participant.attributes.get("sip.from")
    )


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    # In production this is the SIP participant from the Twilio trunk;
    # in dev it's a web participant from the LiveKit playground.
    participant = await ctx.wait_for_participant()
    caller_phone = _extract_caller_phone(participant)
    logger.info(
        "caller joined identity=%s kind=%s phone=%s",
        participant.identity,
        participant.kind,
        caller_phone,
    )

    agent = AppointmentAgent(ctx=ctx, caller_phone=caller_phone)

    # If the caller hangs up first (presses end on their phone), we still need
    # to persist the call. Same flag-protected helper so we never POST twice.
    @ctx.room.on("participant_disconnected")
    def _on_disconnect(p: rtc.RemoteParticipant) -> None:
        if p.identity != participant.identity:
            return
        logger.info("caller hung up identity=%s", p.identity)
        asyncio.create_task(agent._finalize_call())

    session = AgentSession(
        stt=openai.STT(model="gpt-4o-transcribe", language="fr"),
        llm=openai.LLM(model="gpt-4.1"),
        tts=openai.TTS(model="gpt-4o-mini-tts", voice="ash"),
        vad=silero.VAD.load(),
    )

    await session.start(
        room=ctx.room,
        agent=agent,
        room_input_options=RoomInputOptions(
            # BVC tuned for narrow-band telephony codecs on the SIP leg.
            noise_cancellation=noise_cancellation.BVCTelephony(),
        ),
    )

    await session.generate_reply(
        instructions=(
            "Salue brièvement l'appelant en français et demande-lui directement "
            "la date de rendez-vous souhaitée."
        )
    )


if __name__ == "__main__":
    # `agent_name` matches the LiveKit dispatch rule (see infra/livekit-dispatch-rule.json)
    # so an inbound SIP call routes to this worker explicitly.
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="appointment-agent",
        )
    )
