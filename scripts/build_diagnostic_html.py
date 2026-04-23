"""
Build reports/diagnostic.html — bilingual (ES default, EN toggle) engineering
diagnosis fusing:
  - Structural gaps from Claude Chat's UI review + our data mining.
  - Cross-references to actual scraped journey blocks + tools + KB articles.
  - Concrete Sierra-format drafts for new Journeys / Rules / Tools.
  - Clickable Sierra session links for every reference in the issue log.

Design: Anthropic visual language. Language toggle in the top-right.

Transparency note: we scraped the 5 Journeys + 17 tools + 197 KB articles from
the Sierra GraphQL API, but could NOT scrape the Global context blocks (Rules,
Response phrasing, Policies, Glossary) because (1) GraphQL introspection is
disabled and (2) the `journeyBlockNames` query returns 'insufficient permissions
(CONTENT_EDITOR|AGENT_MANAGER required)'. Recommendations that require
cross-referencing those specific blocks are flagged with a badge.
"""
from __future__ import annotations

import html
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config

OUT_PATH = Path(__file__).resolve().parent.parent / "reports" / "diagnostic.html"
SIERRA_AGENT_SLUG = config.SIERRA_AGENT_ID.removeprefix("bot-")
SIERRA_SESSION_URL = (
    f"https://euronet.sierra.ai/agents/{SIERRA_AGENT_SLUG}/sessions/"
)


def session_link(session_id: str) -> str:
    if not session_id:
        return ""
    return f'<a href="{SIERRA_SESSION_URL}{session_id}" target="_blank" class="session-link">{html.escape(session_id)}</a>'


# ---------------------------------------------------------------------------
# Gaps — each with bilingual content.
# `grounded_in`: what scraped data confirms this gap.
# `samples`: list of session IDs demonstrating the gap.
# ---------------------------------------------------------------------------

GAPS = [
    {
        "id": 1, "source": "config-review",
        "title_en": "No Journey for Payout Failure / Receiver issue",
        "title_es": "No existe Journey para falla de pago / problema con el receptor",
        "severity": "Critical",
        "evidence_en": (
            "Aria only has Check Order Status, Cancel, and Check ETA. Calls where "
            "the receiver cannot collect the money default into Check Order Status "
            "and get \"your order is sent and on its way\" — functionally useless. "
            "Session `audit-01KPVBJ9RQP098PVHB0SWNDF1M` (10 min call) is a direct example."
        ),
        "evidence_es": (
            "Aria solo tiene Check Order Status, Cancel, y Check ETA. Las llamadas "
            "donde el receptor no puede cobrar el dinero caen en Check Order Status "
            "y reciben \"su orden fue enviada y va en camino\" — respuesta "
            "funcionalmente inútil. La sesión `audit-01KPVBJ9RQP098PVHB0SWNDF1M` "
            "(llamada de 10 min) es un ejemplo directo."
        ),
        "data_signal_en": "15 sessions tagged `unsupportedIntent:transaction-status-not-recognized`, 21 `tool:order-overview:failed`",
        "data_signal_es": "15 sesiones con tag `unsupportedIntent:transaction-status-not-recognized`, 21 `tool:order-overview:failed`",
        "grounded_in": "scraped-data",
        "samples": ["audit-01KPTGYHCVVCD8C2YYCZXG7C2M", "audit-01KPTXX528P4E934M6427V28AG"],
        "issues": [6, 8, 11],
    },
    {
        "id": 2, "source": "config-review",
        "title_en": "Transfer-to-human logic lives only inside Cancel flow",
        "title_es": "La lógica de transfer a humano vive solo dentro del flujo Cancel",
        "severity": "Critical",
        "evidence_en": (
            "Eligibility logic (CheckTransactionCancellationEligibility → Confirm "
            "→ CareCancellation) is solid, but Aria has no standalone "
            "TransferToLiveRep action. She says \"I can't transfer you yet since "
            "your order isn't marked for cancellation,\" leaking internal logic."
        ),
        "evidence_es": (
            "La lógica de eligibility (CheckTransactionCancellationEligibility → "
            "Confirm → CareCancellation) está bien, pero Aria no tiene acción "
            "TransferToLiveRep independiente. Dice: \"no puedo transferirlo todavía "
            "ya que su orden no está marcada para cancelación,\" filtrando lógica "
            "interna al usuario."
        ),
        "data_signal_en": "47 sessions `tool:transfer:invoked`, 3 during `tool:transfer:offline-hours`, 8 issues in log related to transfer failures",
        "data_signal_es": "47 sesiones `tool:transfer:invoked`, 3 en `tool:transfer:offline-hours`, 8 issues relacionados a fallas de transfer",
        "grounded_in": "scraped-block",
        "samples": ["audit-01KPVA5ENW9GSF2PD4B33YBY8R", "audit-01KPV1X8VSPPRHH5BCE5AYJVHM"],
        "issues": [1, 7, 17, 20],
    },
    {
        "id": 3, "source": "config-review",
        "title_en": "No Modification Journey (name / payer / address / amount)",
        "title_es": "No hay Modification Journey (nombre / pagador / dirección / monto)",
        "severity": "High",
        "evidence_en": (
            "No path exists for payer-change, beneficiary name correction, address "
            "update, etc. Customers hit the config wall; Aria routes them to Cancel "
            "or Status by default, neither of which resolves."
        ),
        "evidence_es": (
            "No existe camino para cambio de pagador, corrección de nombre de "
            "beneficiario, actualización de dirección, etc. Los clientes chocan con "
            "la pared del config; Aria los rutea a Cancel o Status por default, "
            "ninguno de los cuales resuelve."
        ),
        "data_signal_en": "8 sessions `unsupportedIntent:change-order-details`",
        "data_signal_es": "8 sesiones `unsupportedIntent:change-order-details`",
        "grounded_in": "scraped-data",
        "samples": [],
        "issues": [10, 15],
    },
    {
        "id": 4, "source": "config-review",
        "title_en": "Authentication required before any intent routing",
        "title_es": "Autenticación requerida antes de enrutar el intent",
        "severity": "High",
        "evidence_en": (
            "Root journey rule (quoted verbatim from scraped content): \"After "
            "confirming it is a customer calling, you must first immediately find "
            "the customer in Euronet systems by asking for the order number.\" → "
            "Aria must authenticate ~5 exchanges before hearing the actual intent. "
            "For payout failures the customer already said the problem in message #1."
        ),
        "evidence_es": (
            "Regla verbatim del journey root scrapeado: \"After confirming it is a "
            "customer calling, you must first immediately find the customer in "
            "Euronet systems by asking for the order number.\" → Aria debe "
            "autenticar ~5 intercambios antes de oír el intent real. Para payout "
            "failures el cliente YA dijo el problema en el mensaje #1."
        ),
        "data_signal_en": "22 sessions `milestone:requests-immediate-transfer` — caller asks for human before auth even finishes",
        "data_signal_es": "22 sesiones `milestone:requests-immediate-transfer` — el caller pide humano antes de que termine el auth",
        "grounded_in": "scraped-block",
        "samples": ["audit-01KPTFEP5R00BNKN7ZXZR9960W"],
        "issues": [4, 13, 16, 18],
    },
    {
        "id": 5, "source": "config-review",
        "title_en": "No language detection / switching policy",
        "title_es": "Sin política de detección / switch de idioma",
        "severity": "High",
        "evidence_en": (
            "Agent answers in wrong language (Italian when caller speaks Spanish or "
            "French). No rule for proactive language offer when caller has a "
            "Hispanic name or LATAM corridor. Whether such a rule exists in "
            "Global > Rules we cannot verify — that block was not accessible via "
            "our scraping (see §0 caveat)."
        ),
        "evidence_es": (
            "El agente responde en el idioma equivocado (italiano cuando el caller "
            "habla español o francés). No hay regla para ofrecer cambio de idioma "
            "cuando el caller tiene nombre hispano o corredor LATAM. No pudimos "
            "verificar si existe regla equivalente en Global > Rules — ese bloque "
            "no fue accesible en el scrape (ver §0)."
        ),
        "data_signal_en": "5 sessions tagged `language:unsupported`, 47 `language:es` vs 33 `language:en`",
        "data_signal_es": "5 sesiones con tag `language:unsupported`, 47 `language:es` vs 33 `language:en`",
        "grounded_in": "data-only",
        "samples": ["audit-01KPTYR1QY1XXS0DV7NGVYP4AM", "audit-01KPTDEGAV0D992Q03C8N04QNR"],
        "issues": [14],
    },
    {
        "id": 6, "source": "config-review",
        "title_en": "No end-of-call termination policy",
        "title_es": "Sin política de cierre de llamada",
        "severity": "Medium",
        "evidence_en": (
            "Agent keeps emitting \"I'm still here\" messages after mutual "
            "goodbye, creating false expectations. Likely missing rule in "
            "Global context blocks → Rules (not verifiable — see §0)."
        ),
        "evidence_es": (
            "El agente sigue emitiendo mensajes \"sigo aquí\" después del adiós "
            "mutuo, creando falsas expectativas. Probablemente falta regla en "
            "Global context blocks → Rules (no verificable — ver §0)."
        ),
        "data_signal_en": "Present in transcripts of long/trailing sessions; harder to quantify from tags",
        "data_signal_es": "Visible en transcripts de sesiones largas; difícil de cuantificar por tags",
        "grounded_in": "data-only",
        "samples": [],
        "issues": [19],
    },
    {
        "id": 7, "source": "config-review",
        "title_en": "Caller-type loop on ambiguous answers",
        "title_es": "Loop en identificación de tipo de caller ante respuestas ambiguas",
        "severity": "Medium",
        "evidence_en": (
            "When the caller answers the customer-vs-agent disambiguation with "
            "anything ambiguous (\"no\", \"yes\", silence), the agent re-asks the "
            "same question indefinitely instead of defaulting after N retries."
        ),
        "evidence_es": (
            "Cuando el caller responde la pregunta customer-vs-agent con algo "
            "ambiguo (\"no\", \"sí\", silencio), el agente re-pregunta lo mismo "
            "indefinidamente en vez de usar un default después de N reintentos."
        ),
        "data_signal_en": "Multiple sessions with disambiguation loops (Agent Looping monitor hits 40%)",
        "data_signal_es": "Múltiples sesiones con loops de desambiguación (Agent Looping monitor dispara 40%)",
        "grounded_in": "data-only",
        "samples": [],
        "issues": [],
    },

    # Additional gaps from our data analysis
    {
        "id": 8, "source": "data-mining",
        "title_en": "Monitors detect problems but never intervene",
        "title_es": "Los monitores detectan problemas pero nunca intervienen",
        "severity": "Critical",
        "evidence_en": (
            "Sierra ships 4 monitors (Agent Looping, False Transfer, Frustration "
            "Increase, Repeated Escalation). In our 111-session sample they detect "
            "at 40 / 12 / 11 / 3 % rates — but detection does not trigger any "
            "auto-recovery behaviour. The agent continues looping even as the "
            "monitor is watching it loop."
        ),
        "evidence_es": (
            "Sierra trae 4 monitores (Agent Looping, False Transfer, Frustration "
            "Increase, Repeated Escalation). En nuestra muestra de 111 sesiones "
            "detectan a 40 / 12 / 11 / 3 % — pero la detección no dispara ninguna "
            "auto-recuperación. El agente sigue loopeando mientras el monitor lo ve "
            "loopear."
        ),
        "data_signal_en": "44/111 Agent Looping, 13 False Transfer, 12 Frustration Increase, 3 Repeated Escalation",
        "data_signal_es": "44/111 Agent Looping, 13 False Transfer, 12 Frustration Increase, 3 Repeated Escalation",
        "grounded_in": "scraped-data",
        "samples": [],
        "issues": [1, 3],
    },
    {
        "id": 9, "source": "data-mining",
        "title_en": "`unsupportedIntent` taxonomy exists but does not route anywhere",
        "title_es": "La taxonomía `unsupportedIntent` existe pero no rutea a ningún lado",
        "severity": "High",
        "evidence_en": (
            "The agent tags 46 sessions (41%) with `unsupportedIntent` + a sub-label "
            "(`recall`, `change-order-details`, `account-department`, "
            "`technical-issues`, `accounts-receivable`, `agent-provides-department`) "
            "— but there is no corresponding Journey that catches any of these "
            "labels. Tags are observability-only; they have no effect on routing."
        ),
        "evidence_es": (
            "El agente tagea 46 sesiones (41%) con `unsupportedIntent` + sub-label "
            "(`recall`, `change-order-details`, `account-department`, "
            "`technical-issues`, `accounts-receivable`, `agent-provides-department`) "
            "— pero no existe un Journey que capture ninguno. Los tags son solo de "
            "observabilidad; no afectan routing."
        ),
        "data_signal_en": "46 sessions `unsupportedIntent`; top sub-intents: `transaction-status-not-recognized` (15), `recall` (10), `change-order-details` (8)",
        "data_signal_es": "46 sesiones `unsupportedIntent`; top sub-intents: `transaction-status-not-recognized` (15), `recall` (10), `change-order-details` (8)",
        "grounded_in": "scraped-data",
        "samples": [],
        "issues": [6, 10, 12, 15],
    },
    {
        "id": 10, "source": "data-mining",
        "title_en": "No retry budget on tool failures (OrderOverview, CVP)",
        "title_es": "Sin retry budget en fallos de tools (OrderOverview, CVP)",
        "severity": "High",
        "evidence_en": (
            "`tool:order-overview:failed` fires 21 times and `tool:cvp:failed` 8 "
            "times with no cap on retries and no fallback branch. The agent retries "
            "the same tool with the same arguments until the caller hangs up."
        ),
        "evidence_es": (
            "`tool:order-overview:failed` dispara 21 veces y `tool:cvp:failed` 8 "
            "veces sin tope de reintentos y sin rama de fallback. El agente "
            "reintenta el mismo tool con los mismos argumentos hasta que el caller "
            "cuelga."
        ),
        "data_signal_en": "21 `tool:order-overview:failed`, 8 `tool:cvp:failed`",
        "data_signal_es": "21 `tool:order-overview:failed`, 8 `tool:cvp:failed`",
        "grounded_in": "scraped-data",
        "samples": ["audit-01KPVBJ9RQP098PVHB0SWNDF1M"],
        "issues": [2, 3, 10],
    },
    {
        "id": 11, "source": "data-mining",
        "title_en": "No DTMF / keypad input handling",
        "title_es": "Sin manejo de DTMF / teclado",
        "severity": "Medium",
        "evidence_en": (
            "Callers press digits on their phone keypad expecting an IVR-style "
            "response, but Aria is voice-only and silently ignores DTMF. This "
            "creates dead air followed by caller frustration or hangup."
        ),
        "evidence_es": (
            "Los callers presionan dígitos en el teclado esperando una respuesta "
            "tipo IVR, pero Aria es solo voz y silenciosamente ignora el DTMF. "
            "Esto genera silencio muerto seguido de frustración o cuelgue."
        ),
        "data_signal_en": "Transcripts confirm keypad-press events treated as silence",
        "data_signal_es": "Los transcripts confirman que los keypress son tratados como silencio",
        "grounded_in": "data-only",
        "samples": ["audit-01KPV7MMDTBXYMH9H7MFK2732R"],
        "issues": [],
    },
    {
        "id": 12, "source": "data-mining",
        "title_en": "Zendesk-ticket creation is reactive, not proactive fallback",
        "title_es": "Creación de ticket Zendesk es reactiva, no un fallback planeado",
        "severity": "High",
        "evidence_en": (
            "`tool:CreateZendeskTicket` succeeds 74 times but almost always AFTER "
            "the conversation has already failed. There is no rule that says 'if "
            "auth fails N times, create a ticket with partial context and tell the "
            "caller a human will follow up'. Tickets are never part of a planned "
            "graceful-degradation path."
        ),
        "evidence_es": (
            "`tool:CreateZendeskTicket` tiene éxito 74 veces pero casi siempre "
            "DESPUÉS de que la conversación ya falló. No hay regla que diga 'si "
            "auth falla N veces, crea un ticket con contexto parcial y dile al "
            "caller que un humano llamará'. Los tickets nunca son parte de un "
            "camino de degradación planeado."
        ),
        "data_signal_en": "74 `api:zendesk:ticket:create:success` — but mostly as last-ditch escalation",
        "data_signal_es": "74 `api:zendesk:ticket:create:success` — pero casi todas como escalación de último recurso",
        "grounded_in": "scraped-data",
        "samples": [],
        "issues": [1, 2, 3, 4, 9],
    },
    {
        "id": 13, "source": "data-mining",
        "title_en": "No partial-data continuity (discards half-captured order numbers)",
        "title_es": "Sin continuidad de datos parciales (descarta order numbers a medias)",
        "severity": "Medium",
        "evidence_en": (
            "When ASR captures a partial order number (e.g. 'ES9...') the agent "
            "discards it and restarts from scratch rather than confirming the "
            "partial with the caller (\"Did you say ES9 something?\") and "
            "completing the known-prefix match."
        ),
        "evidence_es": (
            "Cuando el ASR captura un order number parcial (ej. 'ES9...'), el "
            "agente lo descarta y reinicia de cero en vez de confirmar el prefijo "
            "con el caller (\"¿Dijo ES9 algo?\") y completar el match."
        ),
        "data_signal_en": "Observed in the 10-min session audit-01KPVBJ9RQP098PVHB0SWNDF1M and other long calls",
        "data_signal_es": "Observado en la sesión de 10 min audit-01KPVBJ9RQP098PVHB0SWNDF1M y otras sesiones largas",
        "grounded_in": "data-only",
        "samples": ["audit-01KPVBJ9RQP098PVHB0SWNDF1M"],
        "issues": [3],
    },
    {
        "id": 14, "source": "data-mining",
        "title_en": "No ASR confidence threshold for alphanumeric confirmations",
        "title_es": "Sin threshold de confianza ASR para confirmar alfanuméricos",
        "severity": "High",
        "evidence_en": (
            "Order numbers are used downstream without any check on the word-level "
            "confidence scores from the speech model. Words with confidence < 0.5 "
            "are treated the same as 0.99 confident words. The raw confidence data "
            "IS captured in `transcriptionMetadata.words` but not surfaced to any "
            "decision logic."
        ),
        "evidence_es": (
            "Los order numbers se usan río abajo sin revisar la confianza word-level "
            "del modelo de speech. Palabras con confianza < 0.5 se tratan igual que "
            "las de 0.99. La confianza SE captura en `transcriptionMetadata.words` "
            "pero no se expone a lógica de decisión."
        ),
        "data_signal_en": "Word-level confidence captured in `transcriptionMetadata` but unused",
        "data_signal_es": "Confianza word-level capturada en `transcriptionMetadata` pero no se usa",
        "grounded_in": "scraped-data",
        "samples": ["audit-01KPVBJ9RQP098PVHB0SWNDF1M"],
        "issues": [3, 11],
    },
    {
        "id": 15, "source": "data-mining",
        "title_en": "No structured path for 'I want a human' — fallback ladder missing",
        "title_es": "Sin camino estructurado para 'quiero un humano' — falta escalera de fallbacks",
        "severity": "High",
        "evidence_en": (
            "22 sessions tagged `milestone:requests-immediate-transfer`. The "
            "consulting guideline says to attempt all fallbacks before transfer — "
            "which is correct for containment. But the current config has neither "
            "the fast-acknowledge response nor the fallback ladder. Callers get "
            "ignored initially, then escalated without context once all auth fails."
        ),
        "evidence_es": (
            "22 sesiones con tag `milestone:requests-immediate-transfer`. La guía "
            "de la consultora dice intentar todos los fallbacks antes de transfer "
            "— lo cual es correcto para containment. Pero el config actual no tiene "
            "ni el acknowledge rápido, ni la escalera de fallbacks. Los callers son "
            "ignorados al principio y luego escalados sin contexto cuando el auth "
            "falla."
        ),
        "data_signal_en": "22 `milestone:requests-immediate-transfer`, 33 `cxi_5_escalation_requested`",
        "data_signal_es": "22 `milestone:requests-immediate-transfer`, 33 `cxi_5_escalation_requested`",
        "grounded_in": "scraped-data",
        "samples": ["audit-01KPV1X8VSPPRHH5BCE5AYJVHM", "audit-01KPVA5ENW9GSF2PD4B33YBY8R"],
        "issues": [9, 15, 17, 20],
    },
]


# ---------------------------------------------------------------------------
# New Journey drafts (config code stays English; reason is bilingual)
# ---------------------------------------------------------------------------

NEW_JOURNEYS = [
    {
        "name": "Payout Failure / Receiver Issue",
        "reason_en": "Gap #1 — currently collapses into Status with useless response",
        "reason_es": "Gap #1 — actualmente cae en Status con respuesta inútil",
        "spec": """JOURNEY: Payout Failure / Receiver Issue
  CONDITION (any of):
    - Caller reports receiver cannot collect funds
    - Caller reports agent/correspondent refused the payout
    - Caller reports correspondent is out of cash
    - unsupportedIntent:transaction-status-not-recognized fires
    - tool:OrderOverview status = 'paid' AND caller reports non-receipt

  GOAL: Identify the root cause and hand off to the right resolution
        (payer change, correspondent support, refund, or live rep).

  RULES:
    1. Do NOT run the full Status flow first. The caller already knows
       the money was sent.
    2. Call tool:DetailedOrder once authenticated to inspect correspondent
       and payout method.
    3. If correspondent has known issue (OXXO shortage, bank outage)
       acknowledge and offer payer-change via tool:RequestModification.
    4. Eligible for live rep transfer via tool:TransferToLiveRep.
    5. If paid > 2 business days and recipient reports non-receipt, create
       tool:CreateZendeskTicket(reason='paid-not-received') and give the
       caller the ticket number.

  TOOLS: DetailedOrder, RequestModification (NEW), TransferToLiveRep (NEW),
         CreateZendeskTicket, CorrespondentHealthCheck (NEW)

  RESPONSE PHRASING:
    - Never say "your order is sent and on its way" when the caller reports
      a payout problem.
    - Acknowledge specifically: "I hear that the recipient wasn't able to
      collect — let me check what's happening with the correspondent."
""",
    },
    {
        "name": "Modification",
        "reason_en": "Gap #3 — missing entirely; 8 sessions tagged change-order-details",
        "reason_es": "Gap #3 — no existe; 8 sesiones con tag change-order-details",
        "spec": """JOURNEY: Modification
  SUB-JOURNEYS:
    - Modify Beneficiary Name
    - Modify Payer / Correspondent
    - Modify Address
    - Amount Modification (typically denied — handle gracefully)

  CONDITION (any of):
    - Caller wants to change recipient, payer, address, or amount
    - unsupportedIntent:change-order-details fires

  GOAL: Recognize the modification intent and either execute the allowed
        sub-flow or escalate with context — never silently fall back to
        Cancel or Status.

  RULES:
    1. Authenticate first (Intents Where User Needs to Authenticate).
    2. Select the transaction.
    3. Route to the sub-journey matching the modification type.
    4. If unsupported modification (e.g. amount) or transaction is paid,
       explain and offer: (a) Refund journey, (b) live rep transfer with
       Modification context pre-populated in the Zendesk ticket.
    5. NEVER offer Cancel as a consolation for a modification request — it
       is a different remedy with financial consequences.

  TOOLS: DetailedOrder, RequestModification (NEW), TransferToLiveRep (NEW),
         CreateZendeskTicket
""",
    },
    {
        "name": "Structured Escalation Ladder",
        "reason_en": (
            "Gap #15 — 22 sessions explicitly ask for a human. Consulting guidance "
            "says: try all fallbacks before transfer. This journey codifies that "
            "ladder so callers are acknowledged first AND still get containment "
            "tried, but have a bounded path to human when all self-service fails."
        ),
        "reason_es": (
            "Gap #15 — 22 sesiones piden humano explícitamente. La consultora "
            "indica: intentar todos los fallbacks antes de transfer. Este journey "
            "codifica esa escalera: al caller se le reconoce primero, igual se "
            "intenta containment, pero hay un camino acotado a humano cuando todo "
            "el self-service falla."
        ),
        "spec": """JOURNEY: Structured Escalation Ladder
  CONDITION (any of):
    - First user utterance contains any of: "agent", "representative",
      "human", "person", "operador", "agente", "someone real"
    - milestone:requests-immediate-transfer fires
    - Caller repeats escalation request >=2 times in any flow

  GOAL: Acknowledge the caller's request quickly, attempt containment
        through the approved fallback ladder, and only transfer when all
        ladder steps are exhausted — with full context pre-populated.

  RULES — acknowledge first, DO NOT transfer immediately:
    1. On trigger, respond within 1 turn:
       "I understand you'd like to speak with someone. I can connect you,
        and I can also try to help you right here. Which would you prefer?"
    2. If caller still insists on human immediately, proceed to Ladder Step A.
       If caller accepts self-service attempt, continue the normal journey.

  RULES — fallback ladder (must attempt steps in order before transfer):
    STEP A — Identify without order number:
      >> call tool:CustomerByTelephone(caller_ani)
      IF match >> surface: "I see an account linked to your number. Are you
                            [NAME]?" — proceed with auth.
      IF no match >> STEP B.

    STEP B — Order-number rescue with partial match:
      >> if ASR captured partial prefix (conf > 0.5)
         >> confirm prefix verbally: "Did you say ES9 something?"
         >> call tool:CustomerByOrderNumber(best_effort)
      IF match >> proceed with auth.
      IF no match >> STEP C.

    STEP C — Intent-only handoff:
      >> ask ONE question only: "Could you tell me what the call is about so
         the right person can help?"
      >> classify via tool:ClassifyIntent
      >> if intent ∈ {payout-failure, modification, immediate-escalation}
         AND business hours, proceed to STEP D.
      >> if offline hours, go to STEP E.

    STEP D — Contextual live transfer:
      >> call tool:TransferToLiveRep(
            reason=classified_intent,
            language=session_locale,
            authenticated=FALSE,
            partial_context=json({captured_fields}))
      >> announce ONCE: "Connecting you now to our team."
      >> do NOT repeat the announcement. If transfer fails, STEP E.

    STEP E — Callback ticket:
      >> call tool:CreateZendeskTicket(
            priority='high',
            reason='requested-human-callback',
            context=captured_context)
      >> read back ticket number + callback window.
      >> end call gracefully.

  TOOLS: ClassifyIntent (NEW), CustomerByTelephone, CustomerByOrderNumber,
         TransferToLiveRep (NEW), CreateZendeskTicket
""",
    },
    {
        "name": "General FAQ",
        "reason_en": "Handles non-transactional questions without forcing auth",
        "reason_es": "Atiende preguntas no-transaccionales sin forzar autenticación",
        "spec": """JOURNEY: General FAQ
  CONDITION (any of):
    - Caller asks a non-transactional question (fees, hours, locations,
      app usage, Ria Wallet, etc.)
    - unsupportedIntent:account-department, :technical-issues, :general fire

  GOAL: Answer via KB lookup without requiring authentication.

  RULES:
    1. Skip CVP authentication — the question is not account-specific.
    2. Call tool:SearchFAQKnowledge with the caller's question.
    3. If tool:kb_result_sufficiency returns 'yes', answer with the KB
       article and cite the title.
    4. If no sufficient answer, offer: (a) retry rephrased, (b) live rep.
    5. After answer, ask if there is anything else — do NOT forcibly route
       the caller into authentication.

  TOOLS: SearchFAQKnowledge, kb_result_sufficiency, TransferToLiveRep (NEW)
""",
    },
]


# ---------------------------------------------------------------------------
# Global rules — each with bilingual description + example transcript
# ---------------------------------------------------------------------------

GLOBAL_RULES_TO_ADD = [
    {
        "name_en": "Pre-auth intent triage",
        "name_es": "Triage de intent antes de autenticación",
        "target": "Global context blocks → Rules",
        "text_en": (
            "Before running CVP authentication, capture the caller's first "
            "substantive utterance and run tool:ClassifyIntent. If the intent is "
            "{payout-failure, modification, general-faq, immediate-escalation} "
            "route to the matching journey WITHOUT requiring full CVP. Only "
            "{status, cancel, ETA} require the full authentication flow."
        ),
        "text_es": (
            "Antes de correr autenticación CVP, captura la primera frase "
            "sustantiva del caller y ejecuta tool:ClassifyIntent. Si el intent "
            "es {payout-failure, modification, general-faq, immediate-escalation} "
            "rutea al journey correspondiente SIN exigir CVP completo. Solo "
            "{status, cancel, ETA} requieren el flujo completo de autenticación."
        ),
        "example_en": (
            'Example: Caller says "My transfer is in review since Tuesday, will '
            'they release it?" → ClassifyIntent returns `general-faq / review-status`. '
            'Route to FAQ journey with KB article "My transfer is under review, when '
            'will it be released?" (9 mentions in data). Auth not needed.'
        ),
        "example_es": (
            'Ejemplo: El caller dice "Mi transferencia está en revisión desde el '
            'martes, ¿la liberan?" → ClassifyIntent devuelve `general-faq / '
            'review-status`. Rutea a FAQ journey con el artículo KB "My transfer '
            'is under review, when will it be released?" (9 menciones en data). '
            'No se pide auth.'
        ),
        "sample_sessions": [],
    },
    {
        "name_en": "Auth hard-exit after 2 failures",
        "name_es": "Salida dura tras 2 fallos de CVP",
        "target": "Intents Where User Needs to Authenticate → Rules",
        "text_en": (
            "If tool:AttemptCvpAuthentication fails twice with materially different "
            "parameters, STOP looping. Call tool:CreateZendeskTicket("
            "reason='auth-failed') with all captured context and inform the caller "
            "a team member will call back. Do NOT attempt a third time."
        ),
        "text_es": (
            "Si tool:AttemptCvpAuthentication falla dos veces con parámetros "
            "materialmente diferentes, DEJA de loopear. Llama a "
            "tool:CreateZendeskTicket(reason='auth-failed') con todo el contexto "
            "capturado y dile al caller que un compañero llamará de vuelta. NO "
            "intentes una tercera vez."
        ),
        "example_en": (
            'Example: Caller failed CVP 3 times on session audit-01KPTFEP5R... '
            'Current behavior: agent asks DOB (not allowed) as 4th question. '
            'Desired: after 2nd fail, say "I cannot verify your identity on this '
            'call, I\'m creating a ticket #TK-12345 — our team will call you back '
            'within 2 hours." End call.'
        ),
        "example_es": (
            'Ejemplo: Caller falló CVP 3 veces en sesión audit-01KPTFEP5R... '
            'Comportamiento actual: el agente pregunta DOB (no permitido) como 4ta '
            'pregunta. Esperado: después del 2do fallo, decir "No puedo verificar '
            'su identidad en esta llamada, le creo ticket #TK-12345 — un compañero '
            'le llamará en las próximas 2 horas." Termina llamada.'
        ),
        "sample_sessions": ["audit-01KPTFEP5R00BNKN7ZXZR9960W", "audit-01KPTYR1QY1XXS0DV7NGVYP4AM"],
    },
    {
        "name_en": "CustomerByTelephone fallback",
        "name_es": "Fallback por número telefónico",
        "target": "Intents Where User Needs to Authenticate → Rules",
        "text_en": (
            "If tool:CustomerByOrderNumber fails twice, call "
            "tool:CustomerByTelephone with the caller's ANI. If that returns a "
            "match, ask the caller to confirm their last order number from our "
            "records rather than dictating it again."
        ),
        "text_es": (
            "Si tool:CustomerByOrderNumber falla dos veces, llama a "
            "tool:CustomerByTelephone con el ANI del caller. Si devuelve match, "
            "pide al caller que confirme su último order number desde nuestros "
            "registros en vez de que lo dicte de nuevo."
        ),
        "example_en": (
            'Example: session audit-01KPVBJ9RQP098PVHB0SWNDF1M spent 10 min dictating "ES9..." order numbers that '
            'CustomerByOrderNumber never recognized. With this rule: after 2 fails, '
            'agent says "Let me try looking you up by your phone number instead. I '
            'see you\'re calling from +34-xxx — is that the number on your account?" '
            'Auth completes in 10 seconds.'
        ),
        "example_es": (
            'Ejemplo: la sesión audit-01KPVBJ9RQP098PVHB0SWNDF1M pasó 10 min dictando order numbers "ES9..." que '
            'CustomerByOrderNumber nunca reconoció. Con esta regla: tras 2 fallos, '
            'el agente dice "Déjeme buscarlo por su número de teléfono. Veo que '
            'llama desde +34-xxx — ¿es el número en su cuenta?" Auth completa en '
            '10 segundos.'
        ),
        "sample_sessions": ["audit-01KPVBJ9RQP098PVHB0SWNDF1M"],
    },
    {
        "name_en": "Language detection & switching",
        "name_es": "Detección y cambio de idioma",
        "target": "Global context blocks → Rules",
        "text_en": (
            "Detect the caller's language from their first utterance. If non-English "
            "and Aria has locale support, switch fully to that locale. Never answer "
            "in a third language. If `language:unsupported`, offer transfer to a "
            "language-capable rep via tool:TransferToLiveRep."
        ),
        "text_es": (
            "Detecta el idioma del caller desde su primera frase. Si es diferente "
            "a inglés y Aria soporta ese locale, cambia completamente a ese idioma. "
            "Nunca respondas en un tercer idioma. Si es `language:unsupported`, "
            "ofrece transfer a un rep capacitado en ese idioma vía "
            "tool:TransferToLiveRep."
        ),
        "example_en": (
            'Example: Caller in audit-01KPTDEGAV0D992Q03C8N04QNR speaks Spanish + '
            'broken English. Agent responded in ITALIAN for the entire call. '
            'Desired: detect "es-ES" from first utterance, switch to Spanish, never '
            'attempt Italian.'
        ),
        "example_es": (
            'Ejemplo: El caller en audit-01KPTDEGAV0D992Q03C8N04QNR habla español '
            '+ inglés roto. El agente respondió en ITALIANO toda la llamada. '
            'Esperado: detectar "es-ES" desde la primera frase, cambiar a español, '
            'nunca intentar italiano.'
        ),
        "sample_sessions": ["audit-01KPTDEGAV0D992Q03C8N04QNR", "audit-01KPTYR1QY1XXS0DV7NGVYP4AM"],
    },
    {
        "name_en": "End-of-call termination",
        "name_es": "Terminación de llamada",
        "target": "Global context blocks → Rules",
        "text_en": (
            "After a mutual closing exchange (caller thanks + agent farewell) end "
            "the session silently. Do NOT emit 'I'm still here' messages. If there "
            "is silence > 8 seconds after the close, disconnect."
        ),
        "text_es": (
            "Después de un cierre mutuo (caller agradece + agente despide) termina "
            "la sesión en silencio. NO emitas mensajes tipo 'sigo aquí'. Si hay "
            "silencio > 8 segundos tras el cierre, desconecta."
        ),
        "example_en": (
            'Example: Customer said "Thank you", Aria replied "You\'re welcome, '
            'customer! Take care.", then Aria emitted "I\'m still here if you need '
            'any help..." twice more. Desired: disconnect silently after the '
            'mutual farewell.'
        ),
        "example_es": (
            'Ejemplo: Cliente dijo "Thank you", Aria replicó "You\'re welcome, '
            'customer! Take care.", y luego emitió "I\'m still here if you need '
            'any help..." dos veces más. Esperado: desconectar silenciosamente '
            'tras el adiós mutuo.'
        ),
        "sample_sessions": [],
    },
    {
        "name_en": "DTMF input handling",
        "name_es": "Manejo de DTMF / teclado",
        "target": "Global context blocks → Rules",
        "text_en": (
            "When a DTMF keypress is received, respond immediately: \"I'm a voice "
            "assistant — could you say that instead of pressing the keypad?\" Do "
            "not ignore the event; treat DTMF as a signal that the caller expects "
            "IVR-style flow."
        ),
        "text_es": (
            "Cuando recibas un keypress DTMF, responde de inmediato: \"Soy un "
            "asistente de voz — ¿podría decírmelo en lugar de presionar el "
            "teclado?\" No ignores el evento; trata el DTMF como señal de que el "
            "caller espera un flujo tipo IVR."
        ),
        "example_en": (
            'Example: Caller in audit-01KPV7MMDTBXYMH9H7MFK2732R pressed "3" on '
            'keypad. Agent treated it as silence → dead air → caller hung up.'
        ),
        "example_es": (
            'Ejemplo: El caller en audit-01KPV7MMDTBXYMH9H7MFK2732R presionó "3" '
            'en el teclado. El agente lo trató como silencio → aire muerto → '
            'caller colgó.'
        ),
        "sample_sessions": ["audit-01KPV7MMDTBXYMH9H7MFK2732R"],
    },
    {
        "name_en": "Monitor-triggered auto-recovery",
        "name_es": "Auto-recuperación por monitor",
        "target": "Global context blocks → Rules",
        "text_en": (
            "When the Agent Looping monitor fires, BREAK the current flow, "
            "apologize ('I'm going in circles — let me try a different approach'), "
            "and either fall back to CustomerByTelephone, create a Zendesk ticket, "
            "or offer live-rep transfer. The monitor firing must change behaviour, "
            "not just log."
        ),
        "text_es": (
            "Cuando dispare el monitor Agent Looping, ROMPE el flujo actual, "
            "disculpate ('Estoy dando vueltas — déjeme intentar otra forma'), y "
            "haz uno de: fallback a CustomerByTelephone, crear ticket Zendesk, u "
            "ofrecer transfer a rep. El monitor disparando DEBE cambiar "
            "comportamiento, no solo loggear."
        ),
        "example_en": (
            'Example: 40% of sessions trigger Agent Looping, but the agent '
            'continues with identical prompts indefinitely. This rule makes the '
            'monitor actionable: break → apologize → pivot strategy.'
        ),
        "example_es": (
            'Ejemplo: 40% de sesiones disparan Agent Looping pero el agente sigue '
            'con prompts idénticos indefinidamente. Esta regla vuelve el monitor '
            'accionable: romper → disculparse → pivotear estrategia.'
        ),
        "sample_sessions": ["audit-01KPVBJ9RQP098PVHB0SWNDF1M", "audit-01KPTXX528P4E934M6427V28AG"],
    },
    {
        "name_en": "PII gating before disclosure",
        "name_es": "Gating de PII antes de divulgación",
        "target": "Global context blocks → Policies",
        "text_en": (
            "Do not disclose the caller's full name, transaction amount, status, or "
            "destination country until tool:AttemptCvpAuthentication returns success. "
            "The pre-auth tool:OrderOverview may only confirm that an order exists — "
            "nothing else."
        ),
        "text_es": (
            "No divulgues nombre completo, monto, status, ni país destino hasta que "
            "tool:AttemptCvpAuthentication retorne éxito. El tool:OrderOverview pre-"
            "auth solo puede confirmar que la orden existe — nada más."
        ),
        "example_en": (
            'Example: In audit-01KPTGYHCVVCD8C2YYCZXG7C2M agent told caller "your '
            'order is sent and on its way" BEFORE CVP auth completed — leaking '
            'status. Also revealed full name verbatim on a failed-auth retry.'
        ),
        "example_es": (
            'Ejemplo: En audit-01KPTGYHCVVCD8C2YYCZXG7C2M el agente dijo "su orden '
            'está enviada y va en camino" ANTES de completar auth CVP — filtrando '
            'status. También reveló el nombre completo en un retry de auth fallido.'
        ),
        "sample_sessions": ["audit-01KPTGYHCVVCD8C2YYCZXG7C2M"],
    },
    {
        "name_en": "Offline-hours transfer policy",
        "name_es": "Política de transfer en horario offline",
        "target": "Global context blocks → Rules",
        "text_en": (
            "Before calling tool:transfer, verify business hours. If offline, do "
            "NOT initiate the transfer. Create tool:CreateZendeskTicket with "
            "priority=high and reason='offline-callback' and tell the caller: "
            "'We're outside live-rep hours. I've logged your request and someone "
            "will call you back within X hours.'"
        ),
        "text_es": (
            "Antes de llamar a tool:transfer, verifica el horario de atención. Si "
            "estás offline, NO inicies el transfer. Crea "
            "tool:CreateZendeskTicket(priority=high, reason='offline-callback') y "
            "dile al caller: 'Estamos fuera de horario — registré su solicitud y "
            "alguien le llamará en las próximas X horas.'"
        ),
        "example_en": (
            'Example: 3 sessions tagged `tool:transfer:offline-hours` — agent '
            'attempted transfer outside business hours, ending in silence and '
            'frustration. Desired: never attempt, go straight to callback ticket.'
        ),
        "example_es": (
            'Ejemplo: 3 sesiones con tag `tool:transfer:offline-hours` — el agente '
            'intentó transfer fuera de horario, terminando en silencio y '
            'frustración. Esperado: nunca intentar, ir directo a ticket de callback.'
        ),
        "sample_sessions": [],
    },
    {
        "name_en": "ASR confidence gating for alphanumerics",
        "name_es": "Gating de confianza ASR para alfanuméricos",
        "target": "Global context blocks → Rules",
        "text_en": (
            "When capturing an order number, phone number, or any alphanumeric "
            "value, reject word-level transcriptions with confidence < 0.7 and "
            "ask the caller to re-spell using NATO phonetic alphabet for the "
            "uncertain characters ('F as in Foxtrot'). Do not call downstream "
            "tools with low-confidence values."
        ),
        "text_es": (
            "Al capturar un order number, teléfono, o cualquier alfanumérico, "
            "rechaza las palabras con confianza < 0.7 y pide al caller deletrear "
            "con alfabeto fonético OTAN para los caracteres inciertos ('F de "
            "Foxtrot'). No llames a tools río abajo con valores de baja confianza."
        ),
        "example_en": (
            'Example: Order "ES9..." was transcribed with 0.37 confidence on '
            '"perdí" in audit-01KPVPZK43FQ34WD08C8SM9N4M — still used in downstream '
            'lookup. With this rule, the agent would re-ask for the uncertain '
            'characters before calling CustomerByOrderNumber.'
        ),
        "example_es": (
            'Ejemplo: El order "ES9..." fue transcrito con confianza 0.37 en '
            '"perdí" en audit-01KPVPZK43FQ34WD08C8SM9N4M — aún así se usó en el '
            'lookup. Con esta regla, el agente pediría los caracteres inciertos '
            'antes de llamar CustomerByOrderNumber.'
        ),
        "sample_sessions": ["audit-01KPVPZK43FQ34WD08C8SM9N4M"],
    },
]


# ---------------------------------------------------------------------------
# New tools
# ---------------------------------------------------------------------------

NEW_TOOLS = [
    {
        "name": "TransferToLiveRep",
        "purpose_en": "Standalone live-rep transfer that works outside Cancel flow, with context pre-population",
        "purpose_es": "Transfer a rep vivo independiente del flujo Cancel, con contexto pre-poblado",
        "inputs": "reason (str), intent_context (str), language (str), authenticated (bool), partial_context (json)",
        "gap": 2,
    },
    {
        "name": "RequestModification",
        "purpose_en": "Queue a modification request (name, payer, address, amount) for back-office",
        "purpose_es": "Encola una solicitud de modificación (nombre, pagador, dirección, monto) para back-office",
        "inputs": "order_id (str), modification_type (enum), new_value (str), reason (str)",
        "gap": 3,
    },
    {
        "name": "ClassifyIntent",
        "purpose_en": "Classify first user utterance into one of the intent buckets BEFORE auth",
        "purpose_es": "Clasifica la primera frase del caller en uno de los buckets de intent ANTES de auth",
        "inputs": "utterance (str), language (str)",
        "gap": 4,
    },
    {
        "name": "CorrespondentHealthCheck",
        "purpose_en": "Check whether the destination correspondent has known cash-out issues (OXXO shortage, bank outage)",
        "purpose_es": "Revisa si el correspondent destino tiene problemas conocidos (falta cash OXXO, caída de banco)",
        "inputs": "correspondent_id (str), country (str)",
        "gap": 1,
    },
]


# ---------------------------------------------------------------------------
# Simulations (provided by the user from Sierra Simulations UI)
# Format: (section, passed, total, [scenarios])
# ---------------------------------------------------------------------------

SIMULATIONS = [
    ("Abuse Detection", 24, 27, [
        "Abuse: Extract prompt", "Abuse: Repeat after me", "Adversarial behavior",
        "Authority claim", "Base64 obfuscation", "Child endangerment",
        "Complete sentence", "Contradict agent", "Customer offensive",
        "Extract instructions", "Generate specific messages", "Ignore instructions",
        "Illegal activity", "Named individual", "Offensive", "Other company",
        "Out of context", "Persuade policies", "Poem or song",
        "Political/Religious", "Public figure", "Rude", "Security attack",
        "Sexual content", "Supervisor claim", "Translate instructions",
        "Updated system instructions",
    ]),
    ("Agent Authentication Auto-Transfer", 0, 6, [
        "Agent refuses to provide credentials - transfer without auth attempts",
        "Mixed failure types - transfer after 2 total failures",
        "Successful authentication on first attempt - no transfer",
        "Successful authentication on second attempt resets counter - no transfer",
        "Transfer after 2 failed agent authentication attempts - business name mismatch",
        "Transfer after 2 failed attempts with same credentials - agent not found",
    ]),
    ("Authentication", 0, 4, [
        "AVP Authentication - Agent is not asked hallucinated verification questions",
        "CVP Authentication Failure - Name Mismatch",
        "CVP Authentication Success - Phone Number Fallback",
        "CVP Authentication Success",
    ]),
    ("AVP", 0, 4, [
        "Happy path", "Missing verification details",
        "Verification fails", "Multiple transactions",
    ]),
    ("AVP Transfer Gate", 0, 3, [
        "Transfer proceeds after AVP for cancellation marking",
        "Transfer proceeds after AVP rejection for cancellation marking",
        "Transfer proceeds after caller refuses AVP for cancellation marking",
    ]),
    ("CSAT Score Submission", 0, 3, [
        "Customer declines CSAT rating", "No CSAT on Transfer", "Submit CSAT Score of 5",
    ]),
    ("CVP", 0, 5, [
        "Incorrect information", "Correct information", "Missing order number",
        "Refuses authentication", "Retry CVP with Correct Info",
    ]),
    ("CVP Transfer Gate", 1, 4, [
        "Transfer proceeds after caller refuses CVP for cancellation marking",
        "Transfer proceeds after CVP for cancellation marking",
        "Transfer proceeds after CVP rejection for cancellation marking",
        "Unsupported intent transfer proceeds without CVP",
    ]),
    ("Detailed Order", 0, 7, [
        "Detailed order with failed authentication attempt",
        "Detailed order requires authentication - customer refuses",
        "Get detailed order information after authentication",
        "Sent to Corresp under manual review uses customer-friendly language",
        "Detailed order with no transaction selected",
        "Get detailed order for second order",
        "Sent to Correspondent status uses customer-friendly language",
    ]),
    ("Estimated Delivery", 0, 2, [
        "ETA auto-included for Sent to Correspondent order",
        "Sent to Corresp with substatus uses customer-friendly language",
    ]),
    ("ETA Hallucination Prevention", 0, 1, [
        "Agent must not fabricate delivery time from paidTime on a Paid order",
    ]),
    ("ETA Journey", 0, 2, [
        "Customer authenticates to receive ETA information",
        "Order overview proactively offers ETA for Sent to Correspondent status",
    ]),
    ("KB Contact Support Prevention", 1, 1, [
        "Agent does not tell caller to contact Ria Customer Care for locked account",
    ]),
    ("Language Switching (Voice)", 0, 4, [
        "Voice: Foreign greeting followed by full foreign conversation switches language",
        "Voice: Mid-conversation Hola does not trigger language switch",
        "Voice: Order overview in Spanish from the start",
        "Voice: Single foreign greeting does not trigger language switch",
    ]),
    ("Legal Hold CEC Codes", 1, 7, [
        "CEC 1001: Agent explains routine security check without transferring",
        "CEC 1001: Customer demands transfer but transfer is blocked",
        "CEC 1002: Customer refuses identity verification and asks for a human",
        "CEC 1002: Agent explains document requirement, customer accepts",
        "CEC 1002: Customer cannot find email and insists on speaking to someone",
        "CEC 1004: AVP agent hits termination when selecting a CEC 1004 transaction",
        "CEC 1004: Agent informs customer transfer cannot be completed and ends call",
    ]),
    ("Legal Hold No Auto Transfer", 0, 1, [
        "Legal Hold order is explained without auto-transferring",
    ]),
    ("Legal Hold Transfer Deadlock", 0, 1, [
        "Customer with Legal Hold order is transferred to a human agent",
    ]),
    ("Order Cancellation", 0, 3, [
        "Attempt to cancel an ineligible order", "Cancel an order",
        "Mark an order for cancellation",
    ]),
    ("Order cancellation journey", 0, 1, ["Ineligible order"]),
    ("Order Not Found Escalation", 0, 6, [
        "Transfer after 3 failed order lookups",
        "No transfer after 2 failures then successful lookup",
        "Successful lookup resets counter - no transfer despite 3 total failures",
        "Successful lookup resets - no transfer after 2 failures then success",
        "Transfer after 3 failed lookups with the same order number",
        "Transfer after 3 failed order lookups",
    ]),
    ("Order Overview", 4, 5, [
        "Get order overview by order number - successful lookup",
        "Get order overview for a different order",
        "Order overview for Sent to Corresp under manual review uses customer-friendly language",
        "Order overview for Sent to Correspondent status uses customer-friendly language",
        "Order overview with invalid order number",
    ]),
    ("Order Status Journey", 0, 6, [
        "Skip providing order number", "Requests paid order to be cancelled",
        "Upset on order status", "Pretending to be a different customer",
        "Multiple transactions with the same requester",
        "Multiple transactions but for a different customer",
    ]),
    ("Transfer Behavior", 0, 1, ["Transfer Request for Order Cancellation"]),
    ("Uncovered Intents", 4, 4, [
        "Requesting to Change Account Information", "Recall Request",
        "Account Locked", "Order on hold",
    ]),
    ("Zendesk Ticket Reason for Contact", 0, 4, [
        "Non-order-related contact (app support) - keeps reason for contact",
        "Order refund request WITH order number - keeps reason and order number",
        "Order-related contact with INVALID order number - drops reason and order number",
        "Profile update request (non-order) - keeps reason, no order number needed",
    ]),
]


# Mapping: our 15 gaps → existing Sierra simulation coverage
# status: covered-passing / covered-failing / covered-partial / missing
GAP_SIM_COVERAGE = [
    (1,  "Payout Failure",            "missing",          []),
    (2,  "Transfer outside Cancel",   "covered-failing",  ["Transfer Behavior", "CVP Transfer Gate", "AVP Transfer Gate"]),
    (3,  "Modification",              "covered-partial",  ["Uncovered Intents"]),
    (4,  "Auth before intent",        "covered-failing",  ["Authentication", "AVP", "CVP"]),
    (5,  "Language policy",           "covered-failing",  ["Language Switching (Voice)"]),
    (6,  "End-of-call termination",   "covered-failing",  ["CSAT Score Submission"]),
    (7,  "Caller-type loop",          "missing",          []),
    (8,  "Monitor auto-recovery",     "missing",          []),
    (9,  "unsupportedIntent routing", "covered-partial",  ["Uncovered Intents"]),
    (10, "Retry budget",              "covered-failing",  ["Order Not Found Escalation", "Agent Authentication Auto-Transfer"]),
    (11, "DTMF handling",             "missing",          []),
    (12, "Proactive Zendesk fallback","covered-failing",  ["Zendesk Ticket Reason for Contact"]),
    (13, "Partial-data continuity",   "missing",          []),
    (14, "ASR confidence",            "missing",          []),
    (15, "Structured escalation",     "covered-failing",  ["Transfer Behavior", "Legal Hold Transfer Deadlock"]),
]


JOURNEY_CHANGES = [
    {
        "journey": "Intents Where User Needs to Authenticate",
        "change_en": (
            "Add fallback tool:CustomerByTelephone after 2 failed "
            "tool:CustomerByOrderNumber. Replace the rule \"DO NOT transfer or "
            "offer to transfer to a live agent\" with a hard-exit after 2 failed "
            "CVP attempts. Remove force that ties authentication before intent "
            "triage (see Global Rule #1)."
        ),
        "change_es": (
            "Agregar fallback tool:CustomerByTelephone tras 2 fallos de "
            "tool:CustomerByOrderNumber. Reemplazar la regla \"DO NOT transfer "
            "or offer to transfer to a live agent\" por un hard-exit tras 2 "
            "fallos de CVP. Quitar la fuerza que amarra autenticación antes del "
            "triage de intent (ver Global Rule #1)."
        ),
        "scraped_quote": (
            "\"If you can't find the customer details, read back the order "
            "number you heard and ask the customer to verify it is correct. "
            "DO NOT transfer or offer to transfer to a live agent.\""
        ),
    },
    {
        "journey": "Check Order Status",
        "change_en": (
            "Add conditional branch: if DetailedOrder.status == 'paid' and "
            "caller reports non-receipt, route to the NEW Payout Failure "
            "journey. Remove mention of tool:OrderOverview for status — move to "
            "pre-auth only."
        ),
        "change_es": (
            "Agregar rama condicional: si DetailedOrder.status == 'paid' y el "
            "caller reporta no-recepción, rutear al NUEVO journey Payout "
            "Failure. Quitar mención de tool:OrderOverview para status — mover "
            "a solo pre-auth."
        ),
        "scraped_quote": (
            "\"Help the caller check the specific order status. You can use "
            "tool:DetailedOrder when the selectedTransaction is set via "
            "tool:AttemptToSelectTransaction to check on order details for the "
            "client.\""
        ),
    },
    {
        "journey": "Cancel Customer Order",
        "change_en": (
            "Extract the live-rep transfer logic to a standalone tool "
            "tool:TransferToLiveRep (see section 4). Keep the three eligibility "
            "checks, but decouple from transfer capability."
        ),
        "change_es": (
            "Extraer la lógica de transfer a humano a un tool standalone "
            "tool:TransferToLiveRep (ver sección 4). Mantener los tres "
            "eligibility-checks, pero desacoplar de la capacidad de "
            "transferir."
        ),
        "scraped_quote": (
            "\"You must first call tool:CheckTransactionCancellationEligibility "
            "to inform the caller of the cancellation eligibility on this "
            "transaction.\""
        ),
    },
    {
        "journey": "Check Order ETA",
        "change_en": (
            "If tool:GetEstimatedDelivery returns ETA in the past and "
            "DetailedOrder.status != 'paid', create Zendesk ticket "
            "reason='delayed-transfer' and offer rep without asking the caller "
            "to re-justify the question."
        ),
        "change_es": (
            "Si tool:GetEstimatedDelivery devuelve ETA en el pasado y "
            "DetailedOrder.status != 'paid', crear ticket Zendesk "
            "reason='delayed-transfer' y ofrecer rep sin pedirle al caller que "
            "re-justifique la pregunta."
        ),
        "scraped_quote": (
            "\"If the tool:GetEstimatedDelivery tool returns no ETA (e.g. the "
            "transfer is already paid, cancelled, or in a different status), "
            "let the caller know the current status and explain that a "
            "specific ETA is not available for this order.\""
        ),
    },
    {
        "journey": "Select Order",
        "change_en": (
            "Handle the partial-capture case: if ASR returned a valid prefix "
            "(e.g. 'ES9') with confidence > 0.5, ask the caller \"Is it ES9…?\" "
            "instead of restarting from zero."
        ),
        "change_es": (
            "Manejar el caso de partial-capture: si ASR devolvió un prefijo "
            "válido (ej. 'ES9') con confianza > 0.5, preguntar al caller \"¿Es "
            "ES9…?\" en vez de reiniciar desde cero."
        ),
        "scraped_quote": (
            "\"When asking for the order number, you can tell the caller to "
            "spell it out phonetically so that you ensure you get the right "
            "ID.\""
        ),
    },
]


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_issues() -> list[dict]:
    raw = OUT_PATH.parent / "issue_log_raw.json"
    if not raw.exists():
        return []
    text = raw.read_text(encoding="utf-8").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("` \n")
    data = json.loads(text)
    issues = data.get("issues") or []
    return [
        {
            "idx":      i,
            "journey":  it.get("journey"),
            "title":    it.get("issue_title"),
            "impacted": it.get("impacted_count"),
            "severity": (it.get("severity") or "").title(),
            "refs":     it.get("reference_sessions") or [],
            "proposed": it.get("proposed_outcome"),
            "description": it.get("description"),
        }
        for i, it in enumerate(issues, 1)
    ]


def load_totals() -> dict:
    conn = sqlite3.connect(config.DB_PATH)
    t = lambda sql: conn.execute(sql).fetchone()[0]
    out = {
        "sessions":         t("SELECT COUNT(*) FROM sessions"),
        "session_details":  t("SELECT COUNT(*) FROM session_details"),
        "classifications":  t("SELECT COUNT(*) FROM classifications"),
        "journey_blocks":   t("SELECT COUNT(*) FROM journey_blocks"),
        "tools":            t("SELECT COUNT(*) FROM tools"),
        "kb_articles":      t("SELECT COUNT(*) FROM kb_articles"),
        "agent_looping":    t("SELECT COUNT(*) FROM monitor_results WHERE name='Agent Looping' AND detected=1"),
        "frustration":      t("SELECT COUNT(*) FROM monitor_results WHERE name='Frustration Increase' AND detected=1"),
        "false_transfer":   t("SELECT COUNT(*) FROM monitor_results WHERE name='False Transfer' AND detected=1"),
        "transfer_invoked": t("SELECT COUNT(DISTINCT session_id) FROM session_tags WHERE tag='tool:transfer:invoked'"),
        "zendesk_success":  t("SELECT COUNT(DISTINCT session_id) FROM session_tags WHERE tag='api:zendesk:ticket:create:success'"),
        "unsupported":      t("SELECT COUNT(DISTINCT session_id) FROM session_tags WHERE tag='unsupportedIntent'"),
    }
    conn.close()
    return out


# ---------------------------------------------------------------------------
# Chart builders — Plotly figures returned as embeddable HTML divs
# ---------------------------------------------------------------------------

ANTHROPIC_COLORS = {
    "accent":  "#c44f3a",
    "accent2": "#da7756",
    "scraped": "#3f6d39",
    "inferred": "#7a6019",
    "muted":   "#6b6257",
    "bg":      "#faf9f5",
    "bg_alt":  "#f2efe5",
    "sev_crit": "#c44f3a",
    "sev_high": "#d97e5a",
    "sev_med":  "#c9a449",
    "sev_low":  "#6c8d5a",
}

_LAYOUT_BASE = dict(
    font=dict(family="Inter, -apple-system, sans-serif", size=12, color="#1a1a1a"),
    paper_bgcolor="#ffffff",
    plot_bgcolor="#ffffff",
    margin=dict(l=10, r=10, t=50, b=10),
)


def _fig_html(fig: go.Figure, height: int = 480) -> str:
    fig.update_layout(height=height, **_LAYOUT_BASE)
    return fig.to_html(full_html=False, include_plotlyjs=False,
                       config={"displayModeBar": False, "responsive": True})


def chart_master_sunburst(conn: sqlite3.Connection) -> str:
    """
    Center → category → severity. Two rings only — pain points are mostly
    unique per session and would turn the outer ring into unreadable tiny
    text. The 20 clustered issues (see Charts tab chart #2) are the right
    third-ring visualisation.
    """
    rows = conn.execute(
        "SELECT category, severity FROM classifications"
    ).fetchall()
    rec: Counter = Counter()
    for cat, sev in rows:
        cat = cat or "other"
        sev = (sev or "low").title()
        rec[(cat, sev)] += 1

    df = pd.DataFrame(
        [{"category": c, "severity": s, "count": n}
         for (c, s), n in rec.items()]
    )
    if df.empty:
        return "<p>No classification data.</p>"

    fig = px.sunburst(
        df,
        path=["category", "severity"],
        values="count",
        color="count",
        color_continuous_scale="Oranges",
        title="Click a category slice to expand severity  ·  Click centre to reset",
    )
    fig.update_traces(
        hovertemplate=(
            "<b>%{label}</b><br>"
            "Sessions: %{value}<br>"
            "Share of parent: %{percentParent:.1%}"
            "<extra></extra>"
        ),
        insidetextorientation="radial",
        textinfo="label+value",
    )
    return _fig_html(fig, height=560)


def chart_gaps_priority_bar() -> str:
    """Priority horizontal bar — one row per gap, sorted by severity then by
    number of linked issues. Colour = severity. Source (UI review / Data
    mining) is shown as a small tag inside the bar. Reads top-to-bottom as
    an engineering priority list."""
    sev_rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    sev_color = {
        "Critical": ANTHROPIC_COLORS["sev_crit"],
        "High":     ANTHROPIC_COLORS["sev_high"],
        "Medium":   ANTHROPIC_COLORS["sev_med"],
        "Low":      ANTHROPIC_COLORS["sev_low"],
    }
    rows = []
    for g in GAPS:
        rows.append({
            "gap":      f"#{g['id']}  {g['title_en']}",
            "issues":   max(1, len(g["issues"])),
            "severity": g["severity"],
            "source":   "UI review" if g["source"] == "config-review" else "Data mining",
            "_rank":    sev_rank.get(g["severity"], 99),
        })
    df = pd.DataFrame(rows)
    # Sort: critical > high > medium > low, then by most issues, then by gap id.
    df = df.sort_values(["_rank", "issues"], ascending=[True, True])
    df["gap_label"] = df["gap"]
    df["hover"] = df.apply(
        lambda r: f"<b>{r.gap}</b><br>Severity: {r.severity}<br>"
                  f"Linked issues: {r.issues}<br>Source: {r.source}", axis=1
    )

    fig = go.Figure()
    for sev in ["Critical", "High", "Medium", "Low"]:
        sub = df[df["severity"] == sev]
        if sub.empty:
            continue
        fig.add_trace(go.Bar(
            y=sub["gap_label"], x=sub["issues"],
            name=sev, orientation="h",
            marker=dict(color=sev_color[sev]),
            text=sub.apply(
                lambda r: f"{int(r.issues)} issues · {r.source}", axis=1
            ),
            textposition="inside", insidetextanchor="start",
            hovertext=sub["hover"], hoverinfo="text",
            textfont=dict(color="#ffffff", size=11),
        ))
    fig.update_layout(
        title="15 structural gaps ordered by priority — severity (colour) × issues impacted (length)",
        xaxis_title="Linked issues in the issue log",
        yaxis_title="",
        barmode="stack",
        legend=dict(orientation="h", y=-0.08),
        height=640,
    )
    fig.update_xaxes(tick0=0, dtick=1)
    return _fig_html(fig, height=640)


def chart_journey_issues_sunburst(issues: list[dict]) -> str:
    """Journey → severity → issue title, sized by impacted_count."""
    rec = []
    for it in issues:
        rec.append({
            "journey":  it.get("journey") or "Other",
            "severity": (it.get("severity") or "Low").title(),
            "issue":    (it.get("title") or "")[:55],
            "impacted": int(it.get("impacted") or 1),
        })
    df = pd.DataFrame(rec)
    if df.empty:
        return "<p>No issues yet.</p>"
    fig = px.sunburst(
        df,
        path=["journey", "severity", "issue"],
        values="impacted",
        color="impacted",
        color_continuous_scale="Reds",
        title="Issue log — drill by journey → severity → specific issue (size = sessions impacted)",
    )
    fig.update_traces(
        hovertemplate="<b>%{label}</b><br>Sessions impacted: %{value}<extra></extra>",
        insidetextorientation="radial",
    )
    return _fig_html(fig, height=700)


def chart_monitor_detection(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT name, SUM(detected) detected, COUNT(*) total "
        "FROM monitor_results GROUP BY name ORDER BY detected DESC"
    ).fetchall()
    df = pd.DataFrame(rows, columns=["monitor", "detected", "total"])
    df["pct"] = (df["detected"] / df["total"].clip(lower=1) * 100).round(1)
    df["not_detected"] = df["total"] - df["detected"]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=df["monitor"], x=df["detected"], name="Detected",
        orientation="h",
        marker_color=ANTHROPIC_COLORS["sev_crit"],
        text=df.apply(lambda r: f"{int(r.detected)}  ({r.pct}%)", axis=1),
        textposition="inside",
    ))
    fig.add_trace(go.Bar(
        y=df["monitor"], x=df["not_detected"], name="Clean",
        orientation="h",
        marker_color=ANTHROPIC_COLORS["sev_low"],
        text=df["not_detected"].astype(int).astype(str),
        textposition="inside",
    ))
    fig.update_layout(
        barmode="stack",
        title="Sierra system monitors — detection rate across 111 sessions",
        xaxis_title="", yaxis_title="",
        legend=dict(orientation="h", y=-0.15),
    )
    return _fig_html(fig, height=380)


def chart_tool_frequency(conn: sqlite3.Connection) -> str:
    framework = ("goalsdk_respond", "ask_ai", "sleep", "should_query_kb",
                 "deadlock_detector", "detect_abuse", "param_validation", "turn",
                 "classify_observations", "threat_evaluation",
                 "personalized_progress_indicator", "classify_agent_monitor",
                 "safety_monitor", "classify_interruption")
    placeholders = ",".join(["?"] * len(framework))
    rows = conn.execute(
        f"""SELECT tool_name, COUNT(*) n FROM traces
            WHERE tool_name IS NOT NULL AND tool_name NOT IN ({placeholders})
            GROUP BY tool_name ORDER BY n DESC LIMIT 15""",
        framework,
    ).fetchall()
    df = pd.DataFrame(rows, columns=["tool", "calls"])
    df = df.sort_values("calls")

    fig = px.bar(
        df, x="calls", y="tool", orientation="h", text="calls",
        color="calls", color_continuous_scale="Oranges",
        title="External tool call frequency (framework internals excluded)",
    )
    fig.update_layout(xaxis_title="", yaxis_title="",
                      coloraxis_showscale=False)
    fig.update_traces(textposition="outside", cliponaxis=False)
    return _fig_html(fig, height=500)


def chart_simulation_pass_rate() -> str:
    """Stacked horizontal bar: passed vs failing per simulation section."""
    rows = []
    for section, passed, total, _ in SIMULATIONS:
        rows.append({"section": section, "passed": passed,
                     "failing": total - passed, "total": total,
                     "pct": round(100 * passed / total) if total else 0})
    df = pd.DataFrame(rows).sort_values(["pct", "total"], ascending=[True, False])

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=df["section"], x=df["passed"], name="Passing",
        orientation="h", marker_color=ANTHROPIC_COLORS["sev_low"],
        text=df.apply(lambda r: f"{int(r.passed)}/{int(r.total)} ({r.pct}%)", axis=1),
        textposition="inside",
    ))
    fig.add_trace(go.Bar(
        y=df["section"], x=df["failing"], name="Failing / Untested",
        orientation="h", marker_color=ANTHROPIC_COLORS["sev_crit"],
        text=df["failing"].astype(int).astype(str), textposition="inside",
    ))
    fig.update_layout(
        barmode="stack",
        title=f"Sierra Simulation pass rate — {sum(r[1] for r in SIMULATIONS)}/{sum(r[2] for r in SIMULATIONS)} scenarios passing across {len(SIMULATIONS)} sections",
        xaxis_title="", yaxis_title="",
        legend=dict(orientation="h", y=-0.1),
    )
    return _fig_html(fig, height=720)


def chart_gap_coverage_matrix() -> str:
    """Horizontal bar — each gap shown with coverage status colour."""
    status_order = ["missing", "covered-failing", "covered-partial", "covered-passing"]
    color_map = {
        "missing":          ANTHROPIC_COLORS["sev_crit"],
        "covered-failing":  ANTHROPIC_COLORS["sev_high"],
        "covered-partial":  ANTHROPIC_COLORS["sev_med"],
        "covered-passing":  ANTHROPIC_COLORS["sev_low"],
    }
    rows = []
    for gid, title, status, sims in GAP_SIM_COVERAGE:
        rows.append({
            "gap": f"#{gid} · {title}",
            "status": status,
            "simulations": ", ".join(sims) if sims else "— no sim —",
            "value": 1,
        })
    df = pd.DataFrame(rows)
    df["status_rank"] = df["status"].map({s: i for i, s in enumerate(status_order)})
    df = df.sort_values("status_rank")

    fig = px.bar(
        df, x="value", y="gap", color="status", orientation="h",
        color_discrete_map=color_map, text="simulations",
        category_orders={"status": status_order},
        title="Gap × existing Sierra Simulation coverage",
    )
    fig.update_layout(xaxis_visible=False, yaxis_title="", showlegend=True,
                      legend=dict(orientation="h", y=-0.1))
    fig.update_traces(textposition="inside", insidetextanchor="start")
    return _fig_html(fig, height=560)


def chart_tag_distribution(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """SELECT tag, COUNT(DISTINCT session_id) n FROM session_tags
           WHERE tag NOT LIKE '\\_%' ESCAPE '\\'
             AND tag NOT LIKE 'cxi_%'
             AND tag NOT LIKE 'milestone:%'
             AND tag NOT LIKE 'transcription-locale:%'
           GROUP BY tag ORDER BY n DESC LIMIT 20"""
    ).fetchall()
    df = pd.DataFrame(rows, columns=["tag", "sessions"])
    df = df.sort_values("sessions")
    fig = px.bar(
        df, x="sessions", y="tag", orientation="h", text="sessions",
        color="sessions", color_continuous_scale="Blues",
        title="Top 20 session tags (what Sierra itself is labelling)",
    )
    fig.update_layout(xaxis_title="", yaxis_title="", coloraxis_showscale=False)
    fig.update_traces(textposition="outside", cliponaxis=False)
    return _fig_html(fig, height=560)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg:        #faf9f5;
  --bg-alt:    #f2efe5;
  --text:      #1a1a1a;
  --muted:     #6b6257;
  --border:    #d8d1c2;
  --accent:    #c44f3a;
  --accent-2:  #da7756;
  --sev-crit:  #c44f3a;
  --sev-high:  #d97e5a;
  --sev-med:   #c9a449;
  --sev-low:   #6c8d5a;
  --scraped:   #3f6d39;
  --inferred:  #7a6019;
}
* { box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: var(--bg); color: var(--text);
  margin: 0; padding: 0; line-height: 1.55;
}
.container { max-width: 1150px; margin: 0 auto; padding: 2rem 2rem 4rem; }

.lang-toggle {
  position: sticky; top: 0; z-index: 50;
  background: var(--bg); padding: 0.8rem 0;
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 0.7rem;
  justify-content: flex-end;
  margin: 0 -2rem 1.5rem; padding: 0.8rem 2rem;
}
.lang-toggle button {
  background: #fff; border: 1px solid var(--border); color: var(--muted);
  padding: 0.35rem 0.9rem; font: inherit; font-size: 0.82rem;
  font-weight: 600; letter-spacing: 0.04em; cursor: pointer;
  border-radius: 2px;
}
.lang-toggle button.active { background: var(--accent); color: #fff; border-color: var(--accent); }

h1, h2, h3, h4 { font-weight: 600; line-height: 1.2; margin-top: 2.2rem; margin-bottom: 0.5rem; }
h1 { font-size: 2.4rem; margin-top: 0; letter-spacing: -0.02em; }
h2 { font-size: 1.6rem; padding-top: 0.5rem; border-top: 1px solid var(--border); }
h3 { font-size: 1.15rem; color: var(--text); }
h4 { font-size: 1rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
p { margin: 0.6rem 0; }

.hero .kicker { color: var(--accent); font-size: 0.85rem; font-weight: 600;
               letter-spacing: 0.12em; text-transform: uppercase; }
.hero .meta  { color: var(--muted); font-size: 0.9rem; }

.thesis {
  background: var(--bg-alt); border-left: 4px solid var(--accent);
  padding: 1.2rem 1.4rem; margin: 1.5rem 0; border-radius: 2px;
  font-size: 1.05rem;
}

.caveat {
  background: #fff8ec; border-left: 4px solid #c9a449;
  padding: 1.1rem 1.3rem; margin: 1.4rem 0; border-radius: 2px;
  font-size: 0.95rem;
}
.caveat strong { color: #7a6019; }

.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
              gap: 0.8rem; margin: 1.5rem 0 2rem; }
.stat { background: #fff; border: 1px solid var(--border);
        padding: 0.8rem 1rem; border-radius: 4px; }
.stat .val   { font-size: 1.5rem; font-weight: 600; }
.stat .label { color: var(--muted); font-size: 0.78rem; text-transform: uppercase;
               letter-spacing: 0.05em; margin-top: 0.2rem; }
.stat.alert .val { color: var(--accent); }

table { width: 100%; border-collapse: collapse; margin: 1rem 0 2rem; font-size: 0.92rem; }
th, td { text-align: left; padding: 0.6rem 0.75rem; border-bottom: 1px solid var(--border); vertical-align: top; }
th { background: var(--bg-alt); font-weight: 600; font-size: 0.82rem;
     text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
tr:hover td { background: #fcfaf3; }

.gap { border: 1px solid var(--border); background: #fff;
       padding: 1.2rem 1.4rem; border-radius: 4px; margin: 1rem 0;
       display: grid; grid-template-columns: 1fr 165px; gap: 1.3rem; }
.gap-main { min-width: 0; }
.gap-stats {
  border-left: 1px solid var(--border); padding-left: 1.1rem;
  display: flex; flex-direction: column; gap: 0.8rem;
  align-self: start;
}
.gap-stat { }
.gap-stat .val { font-size: 1.6rem; font-weight: 600;
                 color: var(--accent); line-height: 1; }
.gap-stat .lbl { color: var(--muted); text-transform: uppercase;
                 letter-spacing: 0.05em; font-size: 0.68rem;
                 margin-top: 0.15rem; }
.gap-stat.muted .val { color: var(--muted); font-size: 1.2rem; }
@media (max-width: 800px) {
  .gap { grid-template-columns: 1fr; }
  .gap-stats { border-left: none; padding-left: 0; border-top: 1px solid var(--border); padding-top: 0.8rem; flex-direction: row; gap: 1.5rem; flex-wrap: wrap; }
}
.gap h3 { margin-top: 0; display: flex; align-items: baseline; gap: 0.6rem; flex-wrap: wrap; }
.gap .num { color: var(--accent); font-weight: 700; font-size: 1rem; white-space: nowrap; }
.gap .signal { background: var(--bg-alt); padding: 0.5rem 0.8rem; border-radius: 3px;
               font-size: 0.88rem; color: var(--muted); margin-top: 0.7rem; }
.gap .evidence { font-style: italic; color: #4a4238; }
.gap .source-tag, .gap .grounding-tag {
  font-size: 0.7rem; padding: 0.15rem 0.5rem;
  border-radius: 2px; text-transform: uppercase;
  letter-spacing: 0.05em; font-weight: 600;
  background: var(--bg-alt); color: var(--muted);
}
.source-tag.config  { background: #efe5c8; color: #7a6019; }
.source-tag.data    { background: #d4e2d0; color: var(--scraped); }
.grounding-tag.scraped-block { background: #d4e2d0; color: var(--scraped); }
.grounding-tag.scraped-data  { background: #e2deca; color: var(--inferred); }
.grounding-tag.data-only     { background: #f5e5dd; color: var(--accent); }
.samples { margin-top: 0.7rem; font-size: 0.85rem; }
.samples strong { color: var(--muted); margin-right: 0.4rem; }
.session-link {
  color: var(--accent); text-decoration: none; font-family: 'JetBrains Mono', 'Consolas', monospace;
  font-size: 0.78rem; padding: 0.1rem 0.35rem; background: #fff6f3;
  border-radius: 2px; margin-right: 0.3rem; border: 1px solid #eddbd4;
}
.session-link:hover { background: #ffe8e0; text-decoration: underline; }

.badge { display: inline-block; padding: 0.15rem 0.6rem; border-radius: 2px;
         font-size: 0.72rem; font-weight: 600; text-transform: uppercase;
         letter-spacing: 0.06em; color: #fff; white-space: nowrap; }
.badge.Critical { background: var(--sev-crit); }
.badge.High     { background: var(--sev-high); }
.badge.Medium   { background: var(--sev-med); color: #1a1a1a; }
.badge.Low      { background: var(--sev-low); }

pre { background: #1a1a1a; color: #e5dfd0; padding: 1rem 1.2rem;
      border-radius: 4px; overflow-x: auto; font-size: 0.82rem;
      line-height: 1.5; font-family: 'JetBrains Mono', 'Consolas', monospace; }
code { background: var(--bg-alt); padding: 0.1rem 0.35rem; border-radius: 2px;
       font-size: 0.88em; font-family: 'JetBrains Mono', 'Consolas', monospace; }

.quote {
  background: var(--bg-alt); padding: 0.5rem 0.8rem;
  border-left: 3px solid var(--muted); margin: 0.7rem 0;
  font-size: 0.88rem; font-family: 'JetBrains Mono', 'Consolas', monospace;
  color: #4a4238;
}

.rule-card { background: #fff; border: 1px solid var(--border);
             padding: 1rem 1.2rem; border-radius: 4px; margin: 0.8rem 0; }
.rule-card h4 { margin: 0 0 0.3rem 0; text-transform: none; letter-spacing: 0;
                font-size: 1rem; color: var(--text); }
.rule-card .target { font-size: 0.78rem; color: var(--muted);
                     font-family: monospace; margin-bottom: 0.6rem; }
.rule-card .example { margin-top: 0.6rem; padding: 0.6rem 0.8rem;
                      background: var(--bg-alt); border-radius: 3px;
                      font-size: 0.88rem; }
.rule-card .example strong { color: var(--accent); }

.issue-row {
  display: grid; grid-template-columns: 40px 140px 1fr 70px 85px;
  gap: 0.8rem; padding: 0.65rem 0.5rem; border-bottom: 1px solid var(--border);
  font-size: 0.9rem; align-items: start;
}
.issue-row:hover { background: #fcfaf3; }
.issue-row .num { font-weight: 600; color: var(--muted); }
.issue-row .journey { color: var(--muted); font-size: 0.82rem; }
.issue-row .title { font-weight: 500; }
.issue-row .refs { font-size: 0.75rem; margin-top: 0.25rem; }

.roadmap { counter-reset: phase; }
.roadmap .phase {
  border-left: 3px solid var(--accent);
  padding: 1rem 1.4rem; margin: 1.2rem 0; background: #fff;
  display: grid; grid-template-columns: 1fr 1.3fr; gap: 1.5rem;
}
.roadmap .phase-header { grid-column: 1 / -1; }
.roadmap .phase-header::before {
  counter-increment: phase; content: "Sprint " counter(phase);
  color: var(--accent); font-weight: 600; font-size: 0.82rem;
  text-transform: uppercase; letter-spacing: 0.08em;
  display: block; margin-bottom: 0.2rem;
}
.roadmap .phase h3 { margin: 0 0 0.3rem 0; }
.roadmap .phase .what h4, .roadmap .phase .how h4 {
  margin: 0 0 0.5rem 0; font-size: 0.78rem; letter-spacing: 0.08em;
}
.roadmap .phase .what h4  { color: var(--accent); }
.roadmap .phase .how  h4  { color: var(--scraped); }
.roadmap .phase .how {
  background: var(--bg-alt); border-radius: 3px; padding: 0.8rem 1rem;
}
.roadmap .phase .how ul, .roadmap .phase .what ul {
  padding-left: 1.2rem; margin: 0.3rem 0;
}
.roadmap .phase .how li, .roadmap .phase .what li {
  margin-bottom: 0.35rem; font-size: 0.9rem;
}
.roadmap .phase .outcome {
  grid-column: 1 / -1; margin-top: 0.6rem; font-size: 0.9rem;
  padding-top: 0.7rem; border-top: 1px dashed var(--border);
}
@media (max-width: 900px) {
  .roadmap .phase { grid-template-columns: 1fr; }
}

footer { margin-top: 4rem; padding-top: 1.5rem; border-top: 1px solid var(--border);
         color: var(--muted); font-size: 0.82rem; text-align: center; }

.lang { }
body.lang-es .lang.en { display: none; }
body.lang-en .lang.es { display: none; }

.tabs-nav {
  position: sticky; top: 52px; z-index: 40;
  background: var(--bg); padding: 0.6rem 2rem;
  margin: 0 -2rem 1.5rem;
  border-bottom: 1px solid var(--border);
  display: flex; gap: 0.3rem; overflow-x: auto;
}
.tabs-nav button {
  background: transparent; border: 1px solid transparent;
  color: var(--muted); padding: 0.5rem 1rem;
  font: inherit; font-size: 0.88rem; font-weight: 500;
  cursor: pointer; border-radius: 3px 3px 0 0;
  white-space: nowrap; border-bottom: 2px solid transparent;
}
.tabs-nav button:hover { color: var(--text); background: var(--bg-alt); }
.tabs-nav button.active {
  color: var(--accent); border-bottom-color: var(--accent);
  background: transparent; font-weight: 600;
}
.tab { display: none; }
.tab.active { display: block; }

.chart-card {
  background: #fff; border: 1px solid var(--border);
  border-radius: 4px; padding: 1.2rem 1rem 0.5rem;
  margin: 1.2rem 0;
}
.chart-card h3 { margin-top: 0; margin-bottom: 0.4rem; font-size: 1.05rem; }
.chart-card .hint {
  font-size: 0.82rem; color: var(--muted); margin-bottom: 0.5rem;
}

.sim-grid {
  display: grid; grid-template-columns: repeat(3, 1fr);
  gap: 1rem; margin: 1rem 0 2rem;
}
.sim-col { background: #fff; border: 1px solid var(--border);
           padding: 1rem 1.2rem; border-radius: 4px; }
.sim-col h3 { margin-top: 0; font-size: 1.05rem; }
.sim-col.zero h3    { color: var(--sev-crit); }
.sim-col.partial h3 { color: var(--sev-med); }
.sim-col.full h3    { color: var(--scraped); }
.sim-list { list-style: none; padding: 0; margin: 0; font-size: 0.88rem; }
.sim-list li { padding: 0.5rem 0; border-bottom: 1px dashed var(--border); }
.sim-ratio { display:inline-block; padding: 0.05rem 0.4rem;
             border-radius: 2px; font-size: 0.72rem; font-weight: 600;
             margin-left: 0.4rem; }
.sim-ratio.zero    { background: #fadbd4; color: #7a2a1e; }
.sim-ratio.partial { background: #fbefd0; color: #7a6019; }
.sim-ratio.full    { background: #d4e2d0; color: #3f6d39; }
.sim-pct { font-size: 0.72rem; color: var(--muted); margin-left: 0.4rem; }
.sim-scenarios { font-size: 0.78rem; color: var(--muted); }

@media (max-width: 900px) {
  .sim-grid { grid-template-columns: 1fr; }
}
"""

JS = """
function setLang(l) {
  document.body.classList.remove('lang-es', 'lang-en');
  document.body.classList.add('lang-' + l);
  document.querySelectorAll('.lang-toggle button').forEach(b => b.classList.toggle('active', b.dataset.l === l));
  localStorage.setItem('diagnostic_lang', l);
}
function showTab(t) {
  document.querySelectorAll('.tab').forEach(e => e.classList.toggle('active', e.dataset.tab === t));
  document.querySelectorAll('.tabs-nav button').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
  localStorage.setItem('diagnostic_tab', t);
  window.scrollTo({top: 0, behavior: 'smooth'});
  // Plotly charts need a resize nudge when their tab becomes visible.
  if (window.Plotly) {
    document.querySelectorAll('.tab.active .plotly-graph-div').forEach(div => {
      try { window.Plotly.Plots.resize(div); } catch(e){}
    });
  }
}
window.addEventListener('DOMContentLoaded', () => {
  setLang(localStorage.getItem('diagnostic_lang') || 'es');
  showTab(localStorage.getItem('diagnostic_tab') || 'overview');
});
"""


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

def bi(content_en: str, content_es: str) -> str:
    return (
        f'<div class="lang es">{content_es}</div>'
        f'<div class="lang en">{content_en}</div>'
    )


def session_links_block(session_ids: list[str]) -> str:
    if not session_ids:
        return ""
    links = "".join(session_link(s) for s in session_ids[:5])
    return (
        '<div class="samples">'
        '<span class="lang es"><strong>Sesiones:</strong></span>'
        '<span class="lang en"><strong>Sessions:</strong></span>'
        f'{links}</div>'
    )


def render_gap(g: dict, issue_impacted_by_idx: dict) -> str:
    src_class = "config" if g["source"] == "config-review" else "data"
    src_label_en = "UI review" if g["source"] == "config-review" else "Data mining"
    src_label_es = "Revisión UI" if g["source"] == "config-review" else "Data mining"
    grounded = g["grounded_in"]
    grounded_labels = {
        "scraped-block": ("Block content scraped", "Contenido de bloque scrapeado"),
        "scraped-data":  ("Data signals scraped",   "Señales de datos scrapeadas"),
        "data-only":     ("Inferred from data",     "Inferido por datos"),
    }
    gl_en, gl_es = grounded_labels.get(grounded, ("", ""))

    issues_line = ""
    if g["issues"]:
        refs = ", ".join(f'#{i}' for i in g["issues"])
        issues_line = bi(
            f'<p style="margin-top:.7rem;font-size:.88rem;"><strong>Issue rows:</strong> {refs}</p>',
            f'<p style="margin-top:.7rem;font-size:.88rem;"><strong>Rows del issue log:</strong> {refs}</p>',
        )

    # Derived per-gap stats
    n_issues      = len(g["issues"])
    n_impacted    = sum(issue_impacted_by_idx.get(i, 0) for i in g["issues"])
    n_samples     = len(g.get("samples") or [])
    severity_pos  = {"Critical": 1, "High": 2, "Medium": 3, "Low": 4}.get(g["severity"], 4)

    stats_html = f'''
<div class="gap-stats">
  <div class="gap-stat">
    <div class="val">{n_issues}</div>
    <div class="lbl"><span class="lang es">Issues ligados</span><span class="lang en">Linked issues</span></div>
  </div>
  <div class="gap-stat">
    <div class="val">{n_impacted or "—"}</div>
    <div class="lbl"><span class="lang es">Sesiones impactadas</span><span class="lang en">Sessions impacted</span></div>
  </div>
  <div class="gap-stat">
    <div class="val">{n_samples or "—"}</div>
    <div class="lbl"><span class="lang es">Ejemplos linkeados</span><span class="lang en">Linked samples</span></div>
  </div>
  <div class="gap-stat muted">
    <div class="val">#{severity_pos}</div>
    <div class="lbl"><span class="lang es">Prioridad severidad</span><span class="lang en">Severity rank</span></div>
  </div>
</div>'''

    return f'''
<div class="gap">
  <div class="gap-main">
    <h3>
      <span class="num">Gap {g["id"]}</span>
      <span class="lang es">{html.escape(g["title_es"])}</span>
      <span class="lang en">{html.escape(g["title_en"])}</span>
      <span style="margin-left:auto"></span>
      <span class="badge {g["severity"]}">{g["severity"]}</span>
      <span class="source-tag {src_class}"><span class="lang es">{src_label_es}</span><span class="lang en">{src_label_en}</span></span>
      <span class="grounding-tag {grounded}"><span class="lang es">{gl_es}</span><span class="lang en">{gl_en}</span></span>
    </h3>
    {bi(f'<p class="evidence">{html.escape(g["evidence_en"])}</p>',
        f'<p class="evidence">{html.escape(g["evidence_es"])}</p>')}
    {bi(f'<div class="signal"><strong>Signal:</strong> {html.escape(g["data_signal_en"])}</div>',
        f'<div class="signal"><strong>Señal:</strong> {html.escape(g["data_signal_es"])}</div>')}
    {session_links_block(g.get("samples") or [])}
    {issues_line}
  </div>
  {stats_html}
</div>'''


def render_new_journey(j: dict) -> str:
    return f'''
<div class="gap">
  <h3>{html.escape(j["name"])}</h3>
  {bi(f'<p class="evidence">{html.escape(j["reason_en"])}</p>',
      f'<p class="evidence">{html.escape(j["reason_es"])}</p>')}
  <pre>{html.escape(j["spec"])}</pre>
</div>'''


def render_rule(r: dict) -> str:
    samples = session_links_block(r.get("sample_sessions") or [])
    return f'''
<div class="rule-card">
  <h4>
    <span class="lang es">{html.escape(r["name_es"])}</span>
    <span class="lang en">{html.escape(r["name_en"])}</span>
  </h4>
  <div class="target">{html.escape(r["target"])}</div>
  {bi(f'<p>{html.escape(r["text_en"])}</p>', f'<p>{html.escape(r["text_es"])}</p>')}
  {bi(f'<div class="example"><strong>Example:</strong> {html.escape(r["example_en"])}</div>',
      f'<div class="example"><strong>Ejemplo:</strong> {html.escape(r["example_es"])}</div>')}
  {samples}
</div>'''


def render_tool(t: dict) -> str:
    return f'''
<tr>
  <td><strong><code>tool:{html.escape(t["name"])}</code></strong></td>
  <td>{bi(html.escape(t["purpose_en"]), html.escape(t["purpose_es"]))}</td>
  <td><code>{html.escape(t["inputs"])}</code></td>
  <td>Gap #{t["gap"]}</td>
</tr>'''


def render_journey_change(c: dict) -> str:
    return f'''
<tr>
  <td><strong>{html.escape(c["journey"])}</strong></td>
  <td>
    {bi(html.escape(c["change_en"]), html.escape(c["change_es"]))}
    <div class="quote">{html.escape(c["scraped_quote"])}</div>
  </td>
</tr>'''


def render_issue_row(iss: dict) -> str:
    refs = "".join(session_link(r) for r in iss["refs"][:3])
    return f'''
<div class="issue-row">
  <div class="num">#{iss["idx"]}</div>
  <div class="journey">{html.escape(iss["journey"] or "")}</div>
  <div>
    <div class="title">{html.escape(iss["title"] or "")}</div>
    <div class="refs">{refs}</div>
  </div>
  <div>{iss["impacted"] or ""}</div>
  <div><span class="badge {iss["severity"]}">{iss["severity"] or ""}</span></div>
</div>'''


def build_html() -> str:
    issues = load_issues()
    totals = load_totals()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Per-gap derived: map issue row-index → impacted session count
    issue_impacted_by_idx = {it["idx"]: int(it.get("impacted") or 0) for it in issues}
    gaps_html      = "\n".join(render_gap(g, issue_impacted_by_idx) for g in GAPS)
    journeys_html  = "\n".join(render_new_journey(j) for j in NEW_JOURNEYS)
    rules_html     = "\n".join(render_rule(r) for r in GLOBAL_RULES_TO_ADD)
    tools_rows     = "\n".join(render_tool(t) for t in NEW_TOOLS)
    changes_rows   = "\n".join(render_journey_change(c) for c in JOURNEY_CHANGES)
    issue_rows     = "\n".join(render_issue_row(it) for it in issues)

    # Build charts once — they reopen the DB briefly each
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        chart_master     = chart_master_sunburst(conn)
        chart_monitors   = chart_monitor_detection(conn)
        chart_tools      = chart_tool_frequency(conn)
        chart_tags       = chart_tag_distribution(conn)
    finally:
        conn.close()
    chart_gaps        = chart_gaps_priority_bar()
    chart_issues      = chart_journey_issues_sunburst(issues)
    chart_sims        = chart_simulation_pass_rate()
    chart_gap_coverage = chart_gap_coverage_matrix()

    # Simulation summary stats
    sim_total_scenarios = sum(r[2] for r in SIMULATIONS)
    sim_total_passing   = sum(r[1] for r in SIMULATIONS)
    sim_zero_sections   = sum(1 for _, p, _, _ in SIMULATIONS if p == 0)
    sim_full_sections   = sum(1 for _, p, t, _ in SIMULATIONS if p == t and t > 0)
    sim_partial_sections = len(SIMULATIONS) - sim_zero_sections - sim_full_sections
    sim_missing_coverage = sum(1 for _, _, s, _ in GAP_SIM_COVERAGE if s == "missing")
    sim_failing_coverage = sum(1 for _, _, s, _ in GAP_SIM_COVERAGE if s == "covered-failing")

    # Build the list of "zero-passing sections" (need-review) + "fully passing"
    zero_sections = [s for s in SIMULATIONS if s[1] == 0]
    full_sections = [s for s in SIMULATIONS if s[1] == s[2] and s[2] > 0]
    partial_sections = [s for s in SIMULATIONS if 0 < s[1] < s[2]]

    def render_sim_section_list(sections, cls):
        rows = []
        for section, passed, total, scenarios in sections:
            pct = round(100 * passed / total) if total else 0
            rows.append(
                f'<li><strong>{html.escape(section)}</strong> '
                f'<span class="sim-ratio {cls}">{passed}/{total}</span>'
                f'<span class="sim-pct">{pct}%</span><br>'
                f'<span class="sim-scenarios">{"; ".join(html.escape(s) for s in scenarios[:5])}'
                f'{" …" if len(scenarios) > 5 else ""}</span></li>'
            )
        return "<ul class='sim-list'>" + "\n".join(rows) + "</ul>"

    sim_zero_html    = render_sim_section_list(zero_sections,    "zero")
    sim_partial_html = render_sim_section_list(partial_sections, "partial")
    sim_full_html    = render_sim_section_list(full_sections,    "full")

    # Gap × sim coverage rows (missing = top)
    coverage_rows = []
    for gid, title, status, sims in GAP_SIM_COVERAGE:
        status_label = {
            "missing":         ("No simulation",       "Sin simulación",    "sev-crit"),
            "covered-failing": ("Sim exists, failing", "Sim existe, falla", "sev-high"),
            "covered-partial": ("Partially covered",   "Cobertura parcial", "sev-med"),
            "covered-passing": ("Covered & passing",   "Cubierto y pasa",   "sev-low"),
        }[status]
        sim_list = ", ".join(f"<code>{html.escape(s)}</code>" for s in sims) if sims else "—"
        coverage_rows.append(
            f'<tr>'
            f'<td>Gap #{gid}</td>'
            f'<td>{html.escape(title)}</td>'
            f'<td><span class="badge" style="background:var(--{status_label[2]});color:#fff">'
            f'<span class="lang es">{status_label[1]}</span>'
            f'<span class="lang en">{status_label[0]}</span></span></td>'
            f'<td>{sim_list}</td>'
            f'</tr>'
        )
    coverage_rows_html = "\n".join(coverage_rows)

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sierra / Aria — Diagnóstico / Diagnosis</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>{CSS}</style>
</head>
<body class="lang-es">
<div class="lang-toggle">
  <span style="font-size:.8rem;color:var(--muted);letter-spacing:.06em;text-transform:uppercase;">Language</span>
  <button data-l="es" onclick="setLang('es')">ES</button>
  <button data-l="en" onclick="setLang('en')">EN</button>
</div>

<div class="tabs-nav">
  <button data-tab="overview"  onclick="showTab('overview')">
    <span class="lang es">Resumen</span><span class="lang en">Overview</span>
  </button>
  <button data-tab="gaps"      onclick="showTab('gaps')">
    <span class="lang es">Gaps</span><span class="lang en">Gaps</span>
  </button>
  <button data-tab="charts"    onclick="showTab('charts')">
    <span class="lang es">Gráficas</span><span class="lang en">Charts</span>
  </button>
  <button data-tab="simulations" onclick="showTab('simulations')">
    <span class="lang es">Simulaciones</span><span class="lang en">Simulations</span>
  </button>
  <button data-tab="solutions" onclick="showTab('solutions')">
    <span class="lang es">Soluciones</span><span class="lang en">Solutions</span>
  </button>
  <button data-tab="issues"    onclick="showTab('issues')">
    <span class="lang es">Issues + Roadmap</span><span class="lang en">Issues + Roadmap</span>
  </button>
</div>

<div class="container">

<div class="tab" data-tab="overview">

<div class="hero">
  <div class="kicker">Diagnosis · Ria Money Transfer · Aria Voice Agent</div>
  {bi(
      f'<h1>What to change in Sierra so Aria stops getting stuck</h1>',
      f'<h1>Qué cambiar en Sierra para que Aria deje de quedar atascada</h1>',
  )}
  <div class="meta">{now} · Sample: {totals['session_details']}/111 sessions with full detail · {totals['classifications']} classified by Sonnet 4.6</div>
</div>

{bi(
  '<div class="thesis">The current configuration is designed around what Aria <em>can do</em> (status, cancel, ETA), not around what the customer <em>needs</em>. When a customer arrives with a case that does not fit those three buckets, Aria has no fallback — she forces the conversation into the closest bucket and delivers a useless answer or loops. 40% of todays sessions triggered the "Agent Looping" monitor.</div>',
  '<div class="thesis">La configuración actual está diseñada alrededor de lo que Aria <em>puede hacer</em> (status, cancel, ETA), no de lo que el cliente <em>necesita</em>. Cuando un cliente llega con un caso que no cabe en esos tres cajones, Aria no tiene fallback — fuerza la conversación al cajón más cercano y entrega una respuesta inútil o entra en loop. 40% de las sesiones de hoy dispararon el monitor "Agent Looping".</div>'
)}

{bi(
  '<h2>§0 · Scope caveat</h2>'
  '<div class="caveat"><strong>Scraped via API (5 Journeys, 17 tools, 197 KB articles, 111 sessions, 19,845 trace events, 444 monitor results, 2,185 tags).</strong> <strong>Provided manually by user from the Sierra UI:</strong> Global context blocks content (Policies, Glossary, Response phrasing), Components, Sections, Simulations inventory + pass ratios, and several journey condition criteria. Recommendations are tagged: <span class="grounding-tag scraped-block">block scraped</span> = API-scraped journey content, <span class="grounding-tag scraped-data">data signals</span> = tag/trace/monitor counts, <span class="grounding-tag data-only">inferred</span> = transcript pattern, not verified against a specific block. The <em>journeyBlockNames</em> GraphQL query requires CONTENT_EDITOR permissions we do not have, which is why the Global context block content had to come from manual UI paste.</div>',
  '<h2>§0 · Alcance / Nota de transparencia</h2>'
  '<div class="caveat"><strong>Scrapeado vía API (5 Journeys, 17 tools, 197 KB articles, 111 sesiones, 19,845 eventos de trace, 444 monitor results, 2,185 tags).</strong> <strong>Pegado manualmente desde la UI por el usuario:</strong> contenido de Global context blocks (Policies, Glossary, Response phrasing), Components, Sections, inventario de Simulations + ratios de pass, y criterios de condición de varios journeys. Las recomendaciones se etiquetan: <span class="grounding-tag scraped-block">bloque scrapeado</span> = contenido journey via API, <span class="grounding-tag scraped-data">señales de data</span> = tags/traces/monitors, <span class="grounding-tag data-only">inferido</span> = patrón del transcript, no verificado contra bloque específico. El query GraphQL <em>journeyBlockNames</em> requiere permisos CONTENT_EDITOR que no tenemos — por eso el contenido de Global context blocks vino de paste manual.</div>'
)}

<div class="stats-grid">
  <div class="stat alert"><div class="val">{totals['agent_looping']}/{totals['session_details']}</div>
    <div class="label"><span class="lang es">Agent looping</span><span class="lang en">Agent looping</span></div></div>
  <div class="stat alert"><div class="val">{totals['frustration']}</div>
    <div class="label"><span class="lang es">Frustración</span><span class="lang en">Frustration</span></div></div>
  <div class="stat"><div class="val">{totals['false_transfer']}</div>
    <div class="label"><span class="lang es">Transfer fallido</span><span class="lang en">False transfer</span></div></div>
  <div class="stat"><div class="val">{totals['transfer_invoked']}</div>
    <div class="label"><span class="lang es">Transfer pedido</span><span class="lang en">Transfer requested</span></div></div>
  <div class="stat"><div class="val">{totals['zendesk_success']}</div>
    <div class="label"><span class="lang es">Tickets Zendesk</span><span class="lang en">Zendesk tickets</span></div></div>
  <div class="stat"><div class="val">{totals['unsupported']}</div>
    <div class="label"><span class="lang es">Intent no soportado</span><span class="lang en">Unsupported intent</span></div></div>
  <div class="stat"><div class="val">{len(GAPS)}</div>
    <div class="label"><span class="lang es">Gaps estructurales</span><span class="lang en">Structural gaps</span></div></div>
  <div class="stat"><div class="val">{len(issues)}</div>
    <div class="label"><span class="lang es">Issues</span><span class="lang en">Issues</span></div></div>
</div>

<div class="chart-card">
  {bi(
    '<h3>Master drill-down — what went wrong and how bad</h3><p class="hint">Click any slice to zoom in · center = 110 classified sessions · ring 1 = category · ring 2 = severity · ring 3 = specific pain point. Click the centre to zoom back out.</p>',
    '<h3>Drill-down principal — qué salió mal y qué tan grave</h3><p class="hint">Click en cualquier slice para expandir · centro = 110 sesiones clasificadas · anillo 1 = categoría · anillo 2 = severidad · anillo 3 = pain point específico. Click en el centro para volver.</p>'
  )}
  {chart_master}
</div>

</div>  <!-- /tab overview -->

<div class="tab" data-tab="gaps">

{bi(
  f'<h2>§1 · Structural gaps ({len(GAPS)})</h2><p>The first 7 come from reviewing the Sierra UI screenshots; the remaining {len(GAPS)-7} were surfaced by cross-referencing our 111 sessions against 19,845 trace events.</p>',
  f'<h2>§1 · Gaps estructurales ({len(GAPS)})</h2><p>Los primeros 7 vienen de revisar las pantallas de Sierra; los restantes {len(GAPS)-7} surgieron cruzando las 111 sesiones contra 19,845 eventos de trace.</p>'
)}
{gaps_html}

</div>  <!-- /tab gaps -->

<div class="tab" data-tab="charts">

{bi(
  '<h2>Drill-down charts</h2><p>Every chart is interactive. Click a slice / section to drill in; click outside to zoom out. Hover for exact counts.</p>',
  '<h2>Gráficas drill-down</h2><p>Cada gráfica es interactiva. Click en un slice / sección para expandir; click fuera para reducir. Hover para ver conteos exactos.</p>'
)}

<div class="chart-card">
  {bi(
    '<h3>1 · Engineering priority list — 15 gaps ordered by severity</h3><p class="hint">Read top to bottom: Critical gaps first, then High / Medium / Low. Bar length = number of issues the gap generates in the log. Inside-bar label shows source (UI review vs Data mining). Hover for full text.</p>',
    '<h3>1 · Lista de prioridad de ingeniería — 15 gaps ordenados por severidad</h3><p class="hint">Se lee de arriba hacia abajo: primero los Critical, luego High / Medium / Low. Longitud de la barra = número de issues que el gap genera en el log. La etiqueta adentro muestra el origen (revisión UI vs Data mining). Hover para texto completo.</p>'
  )}
  {chart_gaps}
</div>

<div class="chart-card">
  {bi(
    '<h3>2 · Issue log drill-down</h3><p class="hint">Of the 20 clustered issues: start from journey → severity → specific issue. Slice size = sessions impacted.</p>',
    '<h3>2 · Drill-down del issue log</h3><p class="hint">De los 20 issues clusterizados: empieza por journey → severidad → issue específico. Tamaño del slice = sesiones impactadas.</p>'
  )}
  {chart_issues}
</div>

<div class="chart-card">
  {bi(
    '<h3>3 · Sierra system monitors — detection rates</h3><p class="hint">How often each of the 4 built-in monitors flags a session across our 111-sample.</p>',
    '<h3>3 · Monitores de Sierra — tasa de detección</h3><p class="hint">Cuántas veces cada uno de los 4 monitores de Sierra marca una sesión en nuestra muestra de 111.</p>'
  )}
  {chart_monitors}
</div>

<div class="chart-card">
  {bi(
    '<h3>4 · External tool call frequency</h3><p class="hint">Top 15 business tools Aria actually invokes. Internal framework tools (classify_observations, threat_evaluation, etc.) are excluded.</p>',
    '<h3>4 · Frecuencia de llamadas a tools externos</h3><p class="hint">Top 15 tools de negocio que Aria efectivamente invoca. Se excluyen tools internos del framework (classify_observations, threat_evaluation, etc.).</p>'
  )}
  {chart_tools}
</div>

<div class="chart-card">
  {bi(
    '<h3>5 · Top session tags</h3><p class="hint">Tags Sierra itself assigns to sessions — observability signal for flow coverage.</p>',
    '<h3>5 · Top tags de sesión</h3><p class="hint">Tags que Sierra asigna a las sesiones — señal de observabilidad sobre cobertura de flujo.</p>'
  )}
  {chart_tags}
</div>

</div>  <!-- /tab charts -->

<div class="tab" data-tab="simulations">

{bi(
  f'<h2>§S · Sierra Simulations — coverage, passing, missing</h2><p>The Sierra agent ships <strong>{len(SIMULATIONS)}</strong> simulation sections containing <strong>{sim_total_scenarios}</strong> test scenarios. Only <strong>{sim_total_passing}</strong> are currently passing ({round(100*sim_total_passing/sim_total_scenarios)}%). <strong>{sim_zero_sections} of {len(SIMULATIONS)} sections have ZERO passing tests</strong> — these are the highest-priority areas to fix, and they correlate directly with the gaps we identified from live data.</p>',
  f'<h2>§S · Simulaciones de Sierra — cobertura, pasando, faltantes</h2><p>El agente de Sierra trae <strong>{len(SIMULATIONS)}</strong> secciones de simulación con <strong>{sim_total_scenarios}</strong> escenarios de test. Solo <strong>{sim_total_passing}</strong> están pasando ({round(100*sim_total_passing/sim_total_scenarios)}%). <strong>{sim_zero_sections} de {len(SIMULATIONS)} secciones tienen CERO tests pasando</strong> — son las áreas más críticas para arreglar, y se correlacionan directamente con los gaps que encontramos en la data en vivo.</p>'
)}

<div class="stats-grid">
  <div class="stat alert"><div class="val">{sim_zero_sections}/{len(SIMULATIONS)}</div>
    <div class="label"><span class="lang es">Secciones con 0 pasando</span><span class="lang en">Sections with 0 passing</span></div></div>
  <div class="stat"><div class="val">{sim_partial_sections}</div>
    <div class="label"><span class="lang es">Pasando parcial</span><span class="lang en">Partial pass</span></div></div>
  <div class="stat"><div class="val">{sim_full_sections}</div>
    <div class="label"><span class="lang es">Pasando 100%</span><span class="lang en">Full pass</span></div></div>
  <div class="stat alert"><div class="val">{sim_missing_coverage}/{len(GAP_SIM_COVERAGE)}</div>
    <div class="label"><span class="lang es">Gaps sin simulación</span><span class="lang en">Gaps without sim</span></div></div>
  <div class="stat"><div class="val">{sim_failing_coverage}</div>
    <div class="label"><span class="lang es">Gaps con sim fallando</span><span class="lang en">Gaps with failing sim</span></div></div>
  <div class="stat"><div class="val">{sim_total_passing}/{sim_total_scenarios}</div>
    <div class="label"><span class="lang es">Escenarios pasando</span><span class="lang en">Scenarios passing</span></div></div>
</div>

<div class="chart-card">
  {bi(
    '<h3>Pass rate by simulation section</h3><p class="hint">Green = passing, red = failing or untested. Sorted by pass %, worst first. 80% of sections have 0 passing tests.</p>',
    '<h3>Tasa de pasada por sección de simulación</h3><p class="hint">Verde = pasando, rojo = fallando o sin testear. Ordenado por % de pasada, peor primero. 80% de secciones tienen 0 tests pasando.</p>'
  )}
  {chart_sims}
</div>

<div class="chart-card">
  {bi(
    '<h3>Gap × simulation coverage</h3><p class="hint">For each of our 15 gaps, whether an equivalent simulation exists in Sierra today. Red = no sim at all, orange = sim exists but fails, yellow = partial coverage.</p>',
    '<h3>Gap × cobertura de simulación</h3><p class="hint">Por cada uno de los 15 gaps, si existe una simulación equivalente en Sierra hoy. Rojo = no hay sim, naranja = sim existe pero falla, amarillo = cobertura parcial.</p>'
  )}
  {chart_gap_coverage}
</div>

<h3>{bi('Coverage table — action required', 'Tabla de cobertura — acción requerida')}</h3>
<table>
  <thead><tr>
    <th>Gap</th><th><span class="lang es">Descripción</span><span class="lang en">Description</span></th>
    <th><span class="lang es">Estado</span><span class="lang en">Status</span></th>
    <th><span class="lang es">Simulación(es)</span><span class="lang en">Simulation(s)</span></th>
  </tr></thead>
  <tbody>{coverage_rows_html}</tbody>
</table>

<div class="sim-grid">
  <div class="sim-col zero">
    {bi(
      f'<h3>{sim_zero_sections} · Need review (0 passing)</h3>',
      f'<h3>{sim_zero_sections} · Necesitan revisión (0 pasando)</h3>'
    )}
    {sim_zero_html}
  </div>
  <div class="sim-col partial">
    {bi(
      f'<h3>{sim_partial_sections} · Partially passing</h3>',
      f'<h3>{sim_partial_sections} · Pasando parcial</h3>'
    )}
    {sim_partial_html}
  </div>
  <div class="sim-col full">
    {bi(
      f'<h3>{sim_full_sections} · Fully passing</h3>',
      f'<h3>{sim_full_sections} · Pasando 100%</h3>'
    )}
    {sim_full_html}
  </div>
</div>

{bi(
  '<h3>Missing simulation coverage — suggested additions</h3><p>Based on our 15 gaps, Sierra should add simulation sections for the following areas that currently have no coverage at all:</p><ul>'
  '<li><strong>Payout Failure / Receiver Issue</strong> — receiver cannot collect, correspondent refused, OXXO shortage scenarios (Gap #1)</li>'
  '<li><strong>Caller-type disambiguation</strong> — ambiguous "yes/no" answers, silence during caller-type detection (Gap #7)</li>'
  '<li><strong>Monitor auto-recovery</strong> — when Agent Looping fires, agent must break flow (Gap #8)</li>'
  '<li><strong>DTMF / keypad input</strong> — caller presses digits, agent must acknowledge (Gap #11)</li>'
  '<li><strong>Partial-data continuity</strong> — partial order number captured, agent must confirm rather than restart (Gap #13)</li>'
  '<li><strong>ASR confidence-aware capture</strong> — low-confidence alphanumerics must trigger re-spell (Gap #14)</li>'
  '</ul>',
  '<h3>Cobertura de simulación faltante — sugerencias</h3><p>Con base en nuestros 15 gaps, Sierra debería agregar secciones de simulación para las siguientes áreas sin cobertura:</p><ul>'
  '<li><strong>Payout Failure / Problema con el receptor</strong> — receptor no puede cobrar, correspondent rechazó, falta de cash OXXO (Gap #1)</li>'
  '<li><strong>Desambiguación de tipo de caller</strong> — respuestas ambiguas, silencio durante detección (Gap #7)</li>'
  '<li><strong>Auto-recuperación por monitor</strong> — cuando dispara Agent Looping, el agente debe romper el flujo (Gap #8)</li>'
  '<li><strong>Entrada DTMF / teclado</strong> — el caller presiona dígitos, el agente debe reconocerlo (Gap #11)</li>'
  '<li><strong>Continuidad de datos parciales</strong> — order number parcial capturado, confirmar en vez de reiniciar (Gap #13)</li>'
  '<li><strong>Captura con conciencia de confianza ASR</strong> — alfanuméricos con baja confianza deben disparar re-dictado (Gap #14)</li>'
  '</ul>'
)}

</div>  <!-- /tab simulations -->

<div class="tab" data-tab="solutions">

{bi(
  '<h2>§2 · New Journeys to create</h2><p>Ready-to-paste format for the Sierra Agent Builder. Each journey has <em>Condition</em>, <em>Goal</em>, <em>Rules</em> and <em>Tools</em> like the existing ones. <strong>Note on Structured Escalation Ladder:</strong> the consulting guidance to "try all fallbacks before transfer" is built into the ladder (Steps A-E).</p>',
  '<h2>§2 · Nuevos Journeys que crear</h2><p>Formato listo para pegar en el Agent Builder de Sierra. Cada journey tiene <em>Condition</em>, <em>Goal</em>, <em>Rules</em> y <em>Tools</em> como los existentes. <strong>Nota sobre Structured Escalation Ladder:</strong> la guía de la consultora de "intentar todos los fallbacks antes de transferir" está integrada como la escalera (Steps A-E).</p>'
)}
{journeys_html}

{bi(
  '<h2>§3 · Global rules to add</h2><p>Place in <code>Global context blocks → Rules</code> (or Policies, as indicated). <strong>Each rule includes a concrete example</strong> from our sessions.</p>',
  '<h2>§3 · Reglas globales a agregar</h2><p>Colocar en <code>Global context blocks → Rules</code> (o Policies, según se indique). <strong>Cada regla incluye un ejemplo concreto</strong> de nuestras sesiones.</p>'
)}
{rules_html}

{bi(
  '<h2>§4 · New Tools to implement</h2><p>These four tools are prerequisite for the new Journeys and global rules to work. Integration-team work required.</p>',
  '<h2>§4 · Nuevos Tools a implementar</h2><p>Estos cuatro tools son prerequisito para que los Journeys nuevos y las reglas globales funcionen. Requieren trabajo del equipo de integraciones.</p>'
)}
<table>
<thead><tr>
  <th><span class="lang es">Tool</span><span class="lang en">Tool</span></th>
  <th><span class="lang es">Propósito</span><span class="lang en">Purpose</span></th>
  <th><span class="lang es">Inputs</span><span class="lang en">Inputs</span></th>
  <th><span class="lang es">Cierra gap</span><span class="lang en">Closes gap</span></th>
</tr></thead>
<tbody>{tools_rows}</tbody>
</table>

{bi(
  '<h2>§5 · Changes to existing Journeys</h2><p>Each change is accompanied by the <strong>verbatim quote</strong> from the journey block we scraped — this is the actual rule in production.</p>',
  '<h2>§5 · Cambios a Journeys existentes</h2><p>Cada cambio viene con la <strong>cita verbatim</strong> del journey block que scrapeamos — es la regla que efectivamente está en producción hoy.</p>'
)}
<table>
<thead><tr>
  <th><span class="lang es">Journey</span><span class="lang en">Journey</span></th>
  <th><span class="lang es">Cambio propuesto</span><span class="lang en">Proposed change</span></th>
</tr></thead>
<tbody>{changes_rows}</tbody>
</table>

</div>  <!-- /tab solutions -->

<div class="tab" data-tab="issues">

{bi(
  '<h2>§6 · Implementation roadmap</h2>',
  '<h2>§6 · Roadmap de implementación</h2>'
)}
<div class="roadmap">

  <div class="phase">
    <div class="phase-header">
      {bi('<h3>Stop the bleeding</h3>', '<h3>Detener el sangrado</h3>')}
    </div>
    <div class="what">
      {bi('<h4>What · Pain addressed</h4>', '<h4>Qué · Dolor a cerrar</h4>')}
      {bi(
        '<ul><li><strong>Gap 8</strong> · Monitors detect but never intervene (40% Agent Looping)</li><li><strong>Gap 10</strong> · No retry budget for Order lookup + CVP</li><li><strong>Gap 12</strong> · Zendesk tickets reactive, not a planned fallback</li></ul>',
        '<ul><li><strong>Gap 8</strong> · Monitores detectan pero no intervienen (40% Agent Looping)</li><li><strong>Gap 10</strong> · Sin retry budget en Order lookup + CVP</li><li><strong>Gap 12</strong> · Tickets Zendesk reactivos, no fallback planeado</li></ul>'
      )}
    </div>
    <div class="how">
      {bi('<h4>How · Concrete steps</h4>', '<h4>Cómo · Pasos concretos</h4>')}
      {bi(
        '<ul>'
        '<li>In <code>Global context blocks → Rules</code>: add "When <code>Agent Looping</code> monitor fires, break flow, apologize, pivot to CustomerByTelephone or CreateZendeskTicket."</li>'
        '<li>In <em>Intents Where User Needs to Authenticate</em>: cap <code>CustomerByOrderNumber</code> at 2 attempts → then <code>CustomerByTelephone</code> fallback.</li>'
        '<li>Cap <code>AttemptCvpAuthentication</code> at 2 failures → <code>CreateZendeskTicket(reason=\\\'auth-failed\\\')</code> with captured context + ticket-number read-back.</li>'
        '<li>Every tool-failure branch must have <code>CreateZendeskTicket</code> as planned exit, not just "retry same tool".</li>'
        '<li>Simulations to fix: <code>Order Not Found Escalation</code> (0/6), <code>Agent Authentication Auto-Transfer</code> (0/6), <code>Authentication</code> (0/4).</li>'
        '</ul>',
        '<ul>'
        '<li>En <code>Global context blocks → Rules</code>: añadir "Cuando dispare el monitor <code>Agent Looping</code>, romper flujo, disculparse, pivotear a CustomerByTelephone o CreateZendeskTicket."</li>'
        '<li>En <em>Intents Where User Needs to Authenticate</em>: cap <code>CustomerByOrderNumber</code> a 2 intentos → luego fallback a <code>CustomerByTelephone</code>.</li>'
        '<li>Cap <code>AttemptCvpAuthentication</code> a 2 fallos → <code>CreateZendeskTicket(reason=\\\'auth-failed\\\')</code> con contexto + lectura de ticket al caller.</li>'
        '<li>Toda rama de fallo de tool debe tener <code>CreateZendeskTicket</code> como salida planeada, no solo "reintentar mismo tool".</li>'
        '<li>Simulaciones a arreglar: <code>Order Not Found Escalation</code> (0/6), <code>Agent Authentication Auto-Transfer</code> (0/6), <code>Authentication</code> (0/4).</li>'
        '</ul>'
      )}
    </div>
    <div class="outcome">
      {bi(
        '<strong>Expected outcome:</strong> Agent looping drops from 40% → &lt; 15%. Auth-fail loops eliminated.',
        '<strong>Outcome esperado:</strong> Agent looping baja de 40% → &lt; 15%. Loops de auth-fail eliminados.'
      )}
    </div>
  </div>

  <div class="phase">
    <div class="phase-header">
      {bi('<h3>Intent taxonomy</h3>', '<h3>Taxonomía de intents</h3>')}
    </div>
    <div class="what">
      {bi('<h4>What · Pain addressed</h4>', '<h4>Qué · Dolor a cerrar</h4>')}
      {bi(
        '<ul><li><strong>Gap 1</strong> · No Payout Failure journey (caller reports receiver can\\\'t collect → falls into Status)</li><li><strong>Gap 3</strong> · No Modification journey (payer change, name correction)</li><li><strong>Gap 4</strong> · Auth forced before intent triage</li><li><strong>Gap 9</strong> · <code>unsupportedIntent</code> sub-tags fire but route nowhere</li></ul>',
        '<ul><li><strong>Gap 1</strong> · No existe Payout Failure journey (receptor no puede cobrar → cae en Status)</li><li><strong>Gap 3</strong> · No existe Modification journey (cambio de pagador, corrección de nombre)</li><li><strong>Gap 4</strong> · Auth forzada antes del triage de intent</li><li><strong>Gap 9</strong> · Los sub-tags <code>unsupportedIntent</code> disparan pero no rutean a ningún lado</li></ul>'
      )}
    </div>
    <div class="how">
      {bi('<h4>How · Concrete steps</h4>', '<h4>Cómo · Pasos concretos</h4>')}
      {bi(
        '<ul>'
        '<li>Build <strong>new tool</strong> <code>ClassifyIntent(utterance, language)</code> returning one of {payout-failure, modification, status, cancel, ETA, general-faq, immediate-escalation}.</li>'
        '<li>Create <strong>new Journey</strong> <em>Payout Failure / Receiver Issue</em> with condition "receiver can\\\'t collect / correspondent refused" + correspondent health check + payer-change offer (full spec in §2).</li>'
        '<li>Create <strong>new Journey</strong> <em>Modification</em> with sub-journeys for name / payer / address / amount.</li>'
        '<li>Add <code>Global context blocks → Rules</code> entry: "Before CVP auth, call <code>ClassifyIntent</code>; route non-{status, cancel, ETA} intents without forcing auth."</li>'
        '<li>Wire <code>unsupportedIntent:change-order-details</code> → Modification, <code>:recall</code> → Cancel, <code>:account-department</code> → General FAQ.</li>'
        '<li>New simulation sections: <code>Payout Failure</code>, <code>Modification</code>, expand <code>Uncovered Intents</code>.</li>'
        '</ul>',
        '<ul>'
        '<li>Construir <strong>nuevo tool</strong> <code>ClassifyIntent(utterance, language)</code> que retorne uno de {payout-failure, modification, status, cancel, ETA, general-faq, immediate-escalation}.</li>'
        '<li>Crear <strong>nuevo Journey</strong> <em>Payout Failure / Receiver Issue</em> con condition "receptor no puede cobrar / correspondent rechazó" + chequeo de correspondent + oferta de payer-change (spec completa en §2).</li>'
        '<li>Crear <strong>nuevo Journey</strong> <em>Modification</em> con sub-journeys para nombre / pagador / dirección / monto.</li>'
        '<li>Añadir a <code>Global context blocks → Rules</code>: "Antes de CVP auth, llamar <code>ClassifyIntent</code>; rutear intents no-{status, cancel, ETA} sin forzar auth."</li>'
        '<li>Wire <code>unsupportedIntent:change-order-details</code> → Modification, <code>:recall</code> → Cancel, <code>:account-department</code> → General FAQ.</li>'
        '<li>Nuevas sim sections: <code>Payout Failure</code>, <code>Modification</code>, expandir <code>Uncovered Intents</code>.</li>'
        '</ul>'
      )}
    </div>
    <div class="outcome">
      {bi(
        '<strong>Expected:</strong> <code>unsupportedIntent</code> drops from 41% → &lt; 15%. Payout-failure callers receive a relevant resolution path instead of "order sent and on its way".',
        '<strong>Esperado:</strong> <code>unsupportedIntent</code> baja de 41% → &lt; 15%. Callers con payout-failure reciben resolución relevante en vez de "orden enviada y va en camino".'
      )}
    </div>
  </div>

  <div class="phase">
    <div class="phase-header">
      {bi('<h3>Escalation ladder, language, termination</h3>', '<h3>Escalera, lenguaje, cierre</h3>')}
    </div>
    <div class="what">
      {bi('<h4>What · Pain addressed</h4>', '<h4>Qué · Dolor a cerrar</h4>')}
      {bi(
        '<ul><li><strong>Gap 2</strong> · Transfer only works inside Cancel flow</li><li><strong>Gap 15</strong> · No structured fallback ladder for "I want a human"</li><li><strong>Gap 5</strong> · No language detection / switch policy</li><li><strong>Gap 6</strong> · No end-of-call termination policy (agent keeps talking)</li><li><strong>Gap 7</strong> · Caller-type loops on ambiguous answers</li></ul>',
        '<ul><li><strong>Gap 2</strong> · Transfer solo funciona dentro del flujo Cancel</li><li><strong>Gap 15</strong> · Sin escalera estructurada de fallbacks antes de transfer</li><li><strong>Gap 5</strong> · Sin política de detección / cambio de idioma</li><li><strong>Gap 6</strong> · Sin política de cierre de llamada (el agente sigue hablando)</li><li><strong>Gap 7</strong> · Loop de caller-type con respuestas ambiguas</li></ul>'
      )}
    </div>
    <div class="how">
      {bi('<h4>How · Concrete steps</h4>', '<h4>Cómo · Pasos concretos</h4>')}
      {bi(
        '<ul>'
        '<li>Build <strong>new tool</strong> <code>TransferToLiveRep</code> (standalone, decoupled from Cancel). Inputs: reason, intent_context, language, authenticated, partial_context.</li>'
        '<li>Create <strong>new Journey</strong> <em>Structured Escalation Ladder</em> (Steps A→E): A) <code>CustomerByTelephone</code> identify · B) partial order-number rescue · C) single-question intent capture · D) contextual transfer · E) callback ticket if offline.</li>'
        '<li>Add rule to <code>Global → Rules</code>: "Detect locale from first utterance. If <code>language:unsupported</code>, offer <code>TransferToLiveRep</code> to language-capable rep. Never answer in a third language."</li>'
        '<li>Add rule: "After mutual goodbye + 8s silence, disconnect silently. Do not emit \\\'I\\\'m still here\\\' probes."</li>'
        '<li>Add rule: "Caller-type disambiguation — default to Customer after 2 ambiguous replies."</li>'
        '<li>Simulations to fix: <code>Language Switching (Voice)</code> (0/4), <code>Transfer Behavior</code> (0/1), <code>Legal Hold Transfer Deadlock</code> (0/1).</li>'
        '</ul>',
        '<ul>'
        '<li>Construir <strong>nuevo tool</strong> <code>TransferToLiveRep</code> (independiente, desacoplado de Cancel). Inputs: reason, intent_context, language, authenticated, partial_context.</li>'
        '<li>Crear <strong>nuevo Journey</strong> <em>Structured Escalation Ladder</em> (Steps A→E): A) identify con <code>CustomerByTelephone</code> · B) rescate de order-number parcial · C) captura de intent en una sola pregunta · D) transfer con contexto · E) ticket de callback si offline.</li>'
        '<li>Añadir a <code>Global → Rules</code>: "Detectar locale en la primera frase. Si <code>language:unsupported</code>, ofrecer <code>TransferToLiveRep</code> a rep con ese idioma. Nunca responder en un tercer idioma."</li>'
        '<li>Añadir regla: "Después de despedida mutua + 8s silencio, desconectar en silencio. No emitir mensajes tipo \\\'sigo aquí\\\'."</li>'
        '<li>Añadir regla: "Desambiguación de caller-type — default a Customer tras 2 respuestas ambiguas."</li>'
        '<li>Simulaciones a arreglar: <code>Language Switching (Voice)</code> (0/4), <code>Transfer Behavior</code> (0/1), <code>Legal Hold Transfer Deadlock</code> (0/1).</li>'
        '</ul>'
      )}
    </div>
    <div class="outcome">
      {bi(
        '<strong>Expected:</strong> The "I want a human" use case becomes a controlled ladder with measurable containment at each step. Language-mismatch drops to near zero.',
        '<strong>Esperado:</strong> El caso "quiero un humano" se vuelve una escalera controlada con containment medible en cada paso. Desajuste de idioma cae a casi cero.'
      )}
    </div>
  </div>

  <div class="phase">
    <div class="phase-header">
      {bi('<h3>Speech quality & observability</h3>', '<h3>Calidad de speech y observabilidad</h3>')}
    </div>
    <div class="what">
      {bi('<h4>What · Pain addressed</h4>', '<h4>Qué · Dolor a cerrar</h4>')}
      {bi(
        '<ul><li><strong>Gap 11</strong> · No DTMF / keypad handling (silently ignored)</li><li><strong>Gap 13</strong> · Partial order numbers are discarded instead of confirmed</li><li><strong>Gap 14</strong> · No ASR confidence threshold for alphanumeric capture</li></ul>',
        '<ul><li><strong>Gap 11</strong> · Sin manejo de DTMF / teclado (silenciosamente ignorado)</li><li><strong>Gap 13</strong> · Order numbers parciales se descartan en vez de confirmar</li><li><strong>Gap 14</strong> · Sin threshold de confianza ASR para captura de alfanuméricos</li></ul>'
      )}
    </div>
    <div class="how">
      {bi('<h4>How · Concrete steps</h4>', '<h4>Cómo · Pasos concretos</h4>')}
      {bi(
        '<ul>'
        '<li>Implement DTMF event handler that returns: "I\\\'m a voice assistant — please speak your answer." Do not swallow the event.</li>'
        '<li>Add rule to <code>Global → Rules</code>: "On alphanumeric capture (order number, phone, PIN), reject words with confidence &lt; 0.7 and re-ask using NATO phonetic for uncertain chars."</li>'
        '<li>Add rule: "If ASR captured a valid prefix (e.g. \\\'ES9\\\') with confidence &gt; 0.5, confirm with caller: \\\'Did you say ES9 something?\\\' before restarting the capture."</li>'
        '<li>Set up a weekly cron to re-run <code>scripts/scrape_sessions.py --today --sample 100</code> + classifier + this diagnostic — drift detection.</li>'
        '<li>Publish new simulation sections: <code>DTMF Handling</code>, <code>ASR Confidence</code>, <code>Partial Order Number Continuity</code>.</li>'
        '</ul>',
        '<ul>'
        '<li>Implementar handler de evento DTMF que devuelva: "Soy un asistente de voz — ¿podría decírmelo en voz?" No tragarse el evento.</li>'
        '<li>Añadir a <code>Global → Rules</code>: "En captura alfanumérica (order number, teléfono, PIN), rechazar palabras con confianza &lt; 0.7 y re-pedir con fonético OTAN para los caracteres inciertos."</li>'
        '<li>Añadir regla: "Si ASR capturó un prefijo válido (ej. \\\'ES9\\\') con confianza &gt; 0.5, confirmar con el caller: \\\'¿Dijo ES9 algo?\\\' antes de reiniciar la captura."</li>'
        '<li>Configurar cron semanal que corra <code>scripts/scrape_sessions.py --today --sample 100</code> + classifier + este diagnóstico — detección de drift.</li>'
        '<li>Publicar nuevas sim sections: <code>DTMF Handling</code>, <code>ASR Confidence</code>, <code>Partial Order Number Continuity</code>.</li>'
        '</ul>'
      )}
    </div>
    <div class="outcome">
      {bi(
        '<strong>Expected:</strong> ASR-driven loops on alphanumeric capture disappear. DTMF users no longer experience dead air. Diagnostic becomes a recurring operational artefact, not a one-shot.',
        '<strong>Esperado:</strong> Loops por ASR en captura de alfanuméricos desaparecen. Los usuarios que marcan DTMF ya no experimentan silencio muerto. El diagnóstico se vuelve un artefacto operativo recurrente, no one-shot.'
      )}
    </div>
  </div>

</div>

{bi(
  f'<h2>§7 · Underlying issue log ({len(issues)} items)</h2><p>Each issue is backed by 1-3 real sessions. Click a session ID to open it directly in Sierra Review. Full IF/THEN suggestions in <code>reports/issue_log.xlsx</code>.</p>',
  f'<h2>§7 · Log de issues ({len(issues)} items)</h2><p>Cada issue está respaldado por 1-3 sesiones reales. Click en el session ID para abrirlo directo en Sierra Review. Sugerencias IF/THEN completas en <code>reports/issue_log.xlsx</code>.</p>'
)}
<div class="issue-row" style="background:var(--bg-alt);font-weight:600;color:var(--muted);text-transform:uppercase;font-size:.72rem;letter-spacing:.06em;">
  <div>#</div>
  <div><span class="lang es">Journey</span><span class="lang en">Journey</span></div>
  <div><span class="lang es">Issue</span><span class="lang en">Issue</span></div>
  <div><span class="lang es">Impacted</span><span class="lang en">Impacted</span></div>
  <div><span class="lang es">Sev</span><span class="lang en">Sev</span></div>
</div>
{issue_rows}

</div>  <!-- /tab issues -->

<footer>
{bi(
  f'Dataset: {totals["sessions"]} listed · {totals["session_details"]} detailed · {totals["classifications"]} classified with Sonnet 4.6 · {totals["journey_blocks"]} journey blocks · {totals["tools"]} tools · {totals["kb_articles"]} KB articles.<br>Files: <code>reports/diagnostic.html</code> · <code>reports/issue_log.xlsx</code> · <code>reports/transaction_status_improvements.md</code>.',
  f'Dataset: {totals["sessions"]} listadas · {totals["session_details"]} con detalle · {totals["classifications"]} clasificadas con Sonnet 4.6 · {totals["journey_blocks"]} journey blocks · {totals["tools"]} tools · {totals["kb_articles"]} KB articles.<br>Archivos: <code>reports/diagnostic.html</code> · <code>reports/issue_log.xlsx</code> · <code>reports/transaction_status_improvements.md</code>.'
)}
</footer>

</div>
<script>{JS}</script>
</body>
</html>"""


def main() -> int:
    out = build_html()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(out, encoding="utf-8")
    print(f"Wrote {OUT_PATH}  ({OUT_PATH.stat().st_size/1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
