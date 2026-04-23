# Sierra / Aria — Análisis de `Check Order Status` (transaction status)

*Generado: 2026-04-22T17:06:11*  
Dataset: **8113** sesiones listadas · **111** con detalle · **110** clasificadas por Sonnet.

## 1. Distribución por categoría × severidad

| Categoría | Critical | High | Medium | Low | **Total** |
|---|---|---|---|---|---|
| authentication | 3 | 6 | 0 | 0 | **9** |
| cancel_transaction | 2 | 6 | 1 | 0 | **9** |
| complaint | 1 | 1 | 0 | 0 | **2** |
| general_info | 0 | 1 | 1 | 0 | **2** |
| greeting_drop | 0 | 0 | 2 | 26 | **28** |
| other | 0 | 2 | 1 | 1 | **4** |
| refund | 0 | 2 | 0 | 0 | **2** |
| technical_issue | 1 | 3 | 2 | 0 | **6** |
| transaction_status | 4 | 17 | 0 | 0 | **21** |
| transfer_to_human | 2 | 16 | 4 | 5 | **27** |

## 2. Top pain points (todas las categorías, agrupados)

| # | Pain point | Frecuencia |
|---|---|---|
| 1 | Agent transferred to human immediately without attempting any self-service resolution | 2 |
| 2 | Agent looping detected: repeatedly asked for order number without resolving it | 2 |
| 3 | Agent transferred caller to human without attempting agent authentication via tool:AuthenticateAgent | 2 |
| 4 | Agent never attempted tool:CustomerByTelephone to identify caller without order number | 2 |
| 5 | Caller dropped immediately after agent greeting with no user message | 2 |
| 6 | Caller pressed '3' on keypad but agent could not interpret DTMF input | 1 |
| 7 | Agent asked customer-vs-agent question after single unintelligible input with no context | 1 |
| 8 | Agent revealed order status ('sent and on its way') before completing identity verification | 1 |
| 9 | Agent transferred to human immediately after caller requested rep, without attempting to resolve the transfer issue | 1 |
| 10 | Agent did not attempt to understand the specific transfer issue before escalating | 1 |
| 11 | Agent used tool:OrderOverview to share status info prior to authentication, leaking transaction data | 1 |
| 12 | Agent looped through transfer attempts 3 times before finally providing self-service instructions | 1 |
| 13 | Agent said 'I can't remove your debit card directly' multiple times without attempting KB lookup first | 1 |
| 14 | Agent wasted caller time with repeated 'please hold' messages and no resolution for ~60 seconds | 1 |
| 15 | Transfer to human was nearly completed for a task fully self-serviceable via app or website | 1 |
| 16 | KB search was invoked only once and late; answer was available immediately via 'How do I delete a payment method online?' | 1 |
| 17 | Agent asked for order number before clarifying intent, causing confusion for caller without one | 1 |
| 18 | Agent gave generic 3-5 business day estimate without calling tool:SearchFAQKnowledge or tool:GetEstimatedDelivery for country-specific timing | 1 |
| 19 | Agent did not offer to help caller start a transfer or direct them to the app/website after resolving query | 1 |
| 20 | Multiple turns wasted interpreting unclear speech ('Ending money') before reaching actual intent | 1 |
| 21 | Caller pressed '9' on keypad with no apparent intent; session ended immediately | 1 |
| 22 | Agent asked for CVP amount and destination before collecting the order number via tool:CustomerByOrderNumber | 1 |
| 23 | Authentication attempted without first calling tool:CustomerByOrderNumber as required by journey block | 1 |
| 24 | Agent revealed full name back to caller verbatim during failed-auth feedback, exposing PII unnecessarily | 1 |
| 25 | Agent transferred to human after single CVP failure instead of prompting caller to retry | 1 |

## 3. Deep-dive — `transaction_status` + flujos relacionados

**41** sesiones caen en categorías transaccionales (transaction_status / cancel_transaction / refund / authentication).

### 3.1 Journey blocks más implicados

| Block | Sesiones |
|---|---|
| Intents Where User Needs to Authenticate | 41 |
| Select Order | 33 |
| Check Order Status | 26 |
| Cancel Customer Order | 10 |
| Check Order ETA | 5 |

### 3.2 Tools invocados en sesiones transaccionales

| Tool | Llamadas |
|---|---|
| `classify_observations` | 1069 |
| `threat_evaluation` | 804 |
| `personalized_progress_indicator` | 787 |
| `classify_agent_monitor` | 644 |
| `safety_monitor` | 641 |
| `classify_interruption` | 219 |
| `toolcall` | 215 |
| `knowledge_search` | 43 |
| `kb_result_sufficiency` | 43 |
| `system_monitor_repeated_escalation` | 40 |
| `system_monitor_frustration_increase` | 40 |
| `system_monitor_false_transfer` | 40 |
| `system_monitor_agent_looping` | 40 |
| `respond_instructive` | 21 |
| `missing_policy_reasoning` | 5 |

### 3.3 Sugerencias concretas de Sonnet (top 15)

1. **(1×)** In the 'Intents Where User Needs to Authenticate' block, add a fallback sub-flow: if tool:CustomerByOrderNumber fails after two attempts, immediately call tool:CustomerByTelephone using the caller's ANI to locate the customer record and pre-populate the order number, before asking the caller to spell it again. This avoids the current infinite loop when callers cannot reliably dictate an alphanumeric order number by voice.
2. **(1×)** In the 'Intents Where User Needs to Authenticate' block, add a fallback branch: after two failed order-number collection attempts, invoke tool:CustomerByTelephone using the caller's CLI before re-asking for the order number; if that also fails to identify the customer, permit transfer to a live agent rather than looping indefinitely — especially when the caller has explicitly requested one and money is at risk.
3. **(1×)** In the 'Intents Where User Needs to Authenticate' block, enforce strict CVP question gating: before calling `tool:AttemptCvpAuthentication`, validate that the agent has collected exactly (1) full name, (2) country of most recent destination, and (3) total amount paid including fees in local currency — and no other questions. Add a pre-call guard that rejects any deviation (e.g. DOB, address, city) and re-prompts with the correct question. Additionally, when `tool:AttemptCvpAuthentication` fails twice, the block should instruct the agent to call `tool:CreateZendeskTicket` and provide the caller with a ticket reference number, rather than looping infinitely.
4. **(1×)** In the 'Intents Where User Needs to Authenticate' block, add a hard exit condition: after 2 consecutive failed `tool:AttemptCvpAuthentication` calls with the same parameters, the agent must stop re-prompting and instead offer to create a Zendesk ticket via `tool:CreateZendeskTicket` and inform the caller that a human agent will follow up — rather than looping indefinitely with the same unverifiable credentials.
5. **(1×)** In the 'Intents Where User Needs to Authenticate' block, add a fallback branch: after 3 consecutive failed attempts to collect a valid order number, invoke `tool:CustomerByTelephone` using the caller's ANI to attempt customer lookup without an order number; if that also fails, create a `tool:CreateZendeskTicket` with reason 'unable_to_authenticate_order_number' and the detected cancellation intent, then inform the caller a human agent will follow up, rather than continuing to loop.
6. **(1×)** In the 'Intents Where User Needs to Authenticate' block, add a language-detection step before `tool:SetCallerType` that calls `tool:SearchFAQKnowledge` or triggers a language-switch prompt when transcription locales include non-matching tags (e.g., es-ES detected while agent responds in Italian). The agent should explicitly offer 'Would you prefer to continue in Spanish?' and switch fully to Spanish before proceeding to `tool:CustomerByOrderNumber`, preventing the entire authentication loop from occurring in the wrong language.
7. **(1×)** In the 'Intents Where User Needs to Authenticate' block, enforce the full auth flow before any transaction disclosure: call `tool:CustomerByOrderNumber`, complete `tool:AttemptCvpAuthentication`, then call `tool:AttemptToSelectTransaction` and `tool:DetailedOrder` to retrieve recipient name and bank details internally — never ask the caller to re-supply information already on file. If status is paid but recipient reports non-receipt beyond 2 business days, the 'Check Order Status' block should surface a clear next-step path (e.g., create a Zendesk ticket via `tool:CreateZendeskTicket` with reason 'paid transfer not received by recipient') rather than toggling between 'contact a specialist' and 'nothing we can do'.
8. **(1×)** In the Cancel Customer Order block, before routing to a human, the agent must attempt to collect the order number and call tool:CustomerByOrderNumber followed by tool:AttemptCvpAuthentication, then tool:CheckTransactionCancellationEligibility. Only if those tools fail or the transaction is ineligible should escalation be offered. Additionally, the transfer logic must be guarded so that tool:transfer is only invoked once and confirmed successful before announcing it to the caller; repeated transfer announcement loops should trigger a fallback that informs the caller of a technical issue rather than repeating the same message.
9. **(1×)** In the 'Intents Where User Needs to Authenticate' block, add a guard that prevents invoking tool:transfer (SIP transfer) until tool:CustomerByOrderNumber returns a valid result AND tool:AttemptCvpAuthentication succeeds. Specifically, after the caller provides an order number (e.g. DE6...), the agent must immediately call tool:CustomerByOrderNumber with that number and proceed through the CVP flow before any transfer decision is made. The current path that triggers escalation on 'unsupportedIntent:recall' should instead route to the 'Cancel Customer Order' block after successful authentication, since a 'recall' or 'zurückmachen' intent maps directly to cancel.
10. **(1×)** In the 'Intents Where User Needs to Authenticate' journey block, enforce a hard gate: tool:CustomerByOrderNumber must return a success response before the agent collects any CVP answers, and tool:AttemptCvpAuthentication must return success before tool:GetEstimatedDelivery or tool:DetailedOrder is invoked. Add an explicit pre-condition check in the block that aborts to a 'cannot proceed without authentication' message if either call fails, rather than allowing the agent to improvise alternative questions (date of birth, destination amount) or surface order details unauthenticated.
11. **(1×)** In the 'Check Order ETA' journey block, add a post-ETA check: if tool:GetEstimatedDelivery returns an ETA that is in the past and tool:DetailedOrder shows the transfer is still in-transit (not paid), the agent must immediately acknowledge the missed delivery, call tool:CreateZendeskTicket with a 'delayed transfer' reason, and offer escalation to a live agent without requiring the caller to re-justify their request. Do not loop on generic delay explanations in this scenario.
12. **(1×)** In the 'Intents Where User Needs to Authenticate' journey block, add a hard gate before invoking tool:GetEstimatedDelivery or tool:DetailedOrder that checks for a successful tool:AttemptCvpAuthentication response; also enforce the three-question-only rule by removing any logic paths that prompt for phone number or email as substitutes for the required CVP parameters (full name, destination country, most recent send amount).
13. **(1×)** In the 'Intents Where User Needs to Authenticate' block, after two failed order-number parse attempts, invoke `tool:CustomerByOrderNumber` with the best-effort string captured so far and surface the result to the caller for confirmation (e.g., 'I found an order for [name] — is that you?') before requesting a re-spell; this breaks the looping pattern and allows authentication to proceed even when voice transcription of alphanumeric codes is noisy.
14. **(1×)** In the 'Intents Where User Needs to Authenticate' block, add a branch for callers without an online account who present an in-agency receipt: after collecting the order number via CustomerByOrderNumber, invoke CustomerByTelephone as a fallback identifier, then proceed with the three-question AttemptCvpAuthentication flow before calling OrderOverview or DetailedOrder. Never surface transaction status data (e.g. 'security review') to a caller who has not passed AttemptCvpAuthentication.
15. **(1×)** In the 'Intents Where User Needs to Authenticate' block, after two consecutive `tool:AttemptCvpAuthentication` failures, the agent should explicitly offer the caller a third attempt while reading back each collected field one at a time for the caller to confirm or correct — specifically probing the full name field, which was the most likely mismatch — before falling back to a human transfer. Additionally, the order-number collection step should be restructured to gather the full string digit-by-digit in a single turn rather than multiple partial confirmation loops.

## 4. Contenido actual de los journey blocks de referencia

### 4.Check Order Status
*type: `condition-block`*

```markdown
### [tag: ]

[criteria: ]
### **Check Order Status** [tag: ]

- [criteria: ]

- Help the caller check the specific order status.

- You can use `tool:DetailedOrder` when the **selectedTransaction** is set via `tool:AttemptToSelectTransaction` to check on order details for the client.

- Before telling a caller about the order status, always include an initial phrase to give the customer context on what they about to hear.  "Ok! Found the order status you were looking for."
- If the order status or sub-status contains the word "other," do not include that in your response. Instead, paraphrase the order status or sub status based on the description.
- Refer to the caller by name, for example, "Thanks Angela! Looking that up for you now."
- Ask the caller if there is anything else that you can help with.

### **Cancel Customer Order** [tag: ]

- [criteria: ]

- Help the customer cancel the order.

- You must first call  `tool:CheckTransactionCancellationEligibility` to inform the caller of the cancellation eligibility on this transaction.
`tool:CheckTransactionCancellationEligibility``tool:ConfirmCancellationIntent``tool:CareCancellation`
- If the order is eligible, ask the caller to confirm they want to cancel their order. Only then can you call `tool:ConfirmCancellationIntent` prior to cancelling the order.
- Only after getting a successful response from `tool:ConfirmCancellationIntent`, you can cancel the order with `tool:CareCancellation` and then follow the instructions returned.

### **Check Order ETA** [tag: ]

- [criteria: ]

- Provide the caller with the specific estimated time of arrival for their transfer.

- When the caller asks about ETA or you previously offered to share the ETA, use the `tool:GetEstimatedDelivery` tool to look up the estimated delivery time for the selected transaction.
- Share ONLY the estimated time of arrival. Do NOT give the full order details (amounts, addresses, correspondent info, etc.) unless the caller specifically asks for them.
- If the `tool:GetEstimatedDelivery` tool returns no ETA (e.g. the transfer is already paid, cancelled, or in a different status), let the caller know the current status and explain that a specific ETA is not available for this order.

- Keep the response focused and concise. Example: 'Your transfer is expected to arrive by Wednesday, February 18, 2026 at 12:29 PM CET.'
- Do not dump all the order details. The caller asked about the ETA, so answer that specifically.
- After sharing the ETA, as…
```

### 4.Check Order ETA
*type: `journey-block`*

```markdown
### **Check Order ETA** [tag: ]

- [criteria: ]

- Provide the caller with the specific estimated time of arrival for their transfer.

- When the caller asks about ETA or you previously offered to share the ETA, use the `tool:GetEstimatedDelivery` tool to look up the estimated delivery time for the selected transaction.
- Share ONLY the estimated time of arrival. Do NOT give the full order details (amounts, addresses, correspondent info, etc.) unless the caller specifically asks for them.
- If the `tool:GetEstimatedDelivery` tool returns no ETA (e.g. the transfer is already paid, cancelled, or in a different status), let the caller know the current status and explain that a specific ETA is not available for this order.

- Keep the response focused and concise. Example: 'Your transfer is expected to arrive by Wednesday, February 18, 2026 at 12:29 PM CET.'
- Do not dump all the order details. The caller asked about the ETA, so answer that specifically.
- After sharing the ETA, ask if there is anything else you can help with.
`tool:GetEstimatedDelivery`
```

### 4.Select Order
*type: `condition-block`*

```markdown
### [tag: ]

[criteria: ]
### **Select Order** [tag: ]

- [criteria: ]

- Update the caller on their order status.

- If the caller is asking about a specific transaction, you must find and select it using the order number and `tool:AttemptToSelectTransaction` to have more information about it, answer any questions, and take any actions.

- Sometimes the caller may have asked about one transaction, but also the caller had to authenticate with a different transaction. If you are not confident which transaction the caller wants to discuss, you should conversationally ensure to clarify which transaction the caller wants to select with `tool:AttemptToSelectTransaction`.

- When asking for the order number, you can tell the caller to spell it out phonetically so that you ensure you get the right ID.

### [tag: ]

[criteria: ]
### **Check Order Status** [tag: ]

- [criteria: ]

- Help the caller check the specific order status.

- You can use `tool:DetailedOrder` when the **selectedTransaction** is set via `tool:AttemptToSelectTransaction` to check on order details for the client.

- Before telling a caller about the order status, always include an initial phrase to give the customer context on what they about to hear.  "Ok! Found the order status you were looking for."
- If the order status or sub-status contains the word "other," do not include that in your response. Instead, paraphrase the order status or sub status based on the description.
- Refer to the caller by name, for example, "Thanks Angela! Looking that up for you now."
- Ask the caller if there is anything else that you can help with.

### **Cancel Customer Order** [tag: ]

- [criteria: ]

- Help the customer cancel the order.

- You must first call  `tool:CheckTransactionCancellationEligibility` to inform the caller of the cancellation eligibility on this transaction.
`tool:CheckTransactionCancellationEligibility``tool:ConfirmCancellationIntent``tool:CareCancellation`
- If the order is eligible, ask the caller to confirm they want to cancel their order. Only then can you call `tool:ConfirmCancellationIntent` prior to cancelling the order.
- Only after getting a successful response from `tool:ConfirmCancellationIntent`, you can cancel the order with `tool:CareCancellation` and then follow the instructions returned.

### **Check Order ETA** [tag: ]

- [criteria: ]

- Provide the caller with the specific estimated time of arrival for their transfer.

- When the caller asks about ETA or you previously…
```

### 4.Cancel Customer Order
*type: `journey-block`*

```markdown
### **Cancel Customer Order** [tag: ]

- [criteria: ]

- Help the customer cancel the order.

- You must first call  `tool:CheckTransactionCancellationEligibility` to inform the caller of the cancellation eligibility on this transaction.
`tool:CheckTransactionCancellationEligibility``tool:ConfirmCancellationIntent``tool:CareCancellation`
- If the order is eligible, ask the caller to confirm they want to cancel their order. Only then can you call `tool:ConfirmCancellationIntent` prior to cancelling the order.
- Only after getting a successful response from `tool:ConfirmCancellationIntent`, you can cancel the order with `tool:CareCancellation` and then follow the instructions returned.
```

### 4.Intents Where User Needs to Authenticate
*type: `journey-block`*

```markdown
### **Intents Where User Needs to Authenticate** [tag: ]

- [criteria: ]

- Help the caller understand their transaction and potentially take action.

### [tag: ]

[criteria: ]
- **Authentication Rules - THE CUSTOMER SHOULD ONLY BE SUCCESSFULLY AUTHENTICATED ONCE PER CALL.**
- - After confirming it is a customer calling, you must first immediately find the customer in Euronet systems by asking for the order number. The order number starts with two letters followed by digits. Ask the customer to spell the order number out phonetically such as 'A as in apple'. Use `tool:CustomerByOrderNumber` to find customer information.
  - - You may not attempt CVP Authentication without calling `tool:CustomerByOrderNumber`
    - If you can't find the customer details, read back the order number you heard and ask the customer to verify it is correct. DO NOT transfer or offer to transfer to a live agent.
    - Never refer to the customer by their full name prior to calling this function.
  - When verifying the customer:
  - - Always inform the customer that you are asking verification questions for security purposes to confirm their identity.
    - Always ask the following three verification questions -- and only these questions -- in order to fill in the parameters of `tool:AttemptCvpAuthentication` .
    - - Ask the customer to confirm their full name as it appears on their Ria account.
      - Ask the customer to provide the country where they sent money to most recently with this account.
      - - If the customer says that they've never sent money before, this will be equivalent to 0 for tool parameter purposes.
      - Ask the customer for the total amount they sent in their local currency in their most recent transaction. This amount will be the full amount they paid on the bottom of their  receipt.
      - - If the customer says that they've never sent money before, this will be equivalent to an empty string "" for tool parameter purposes.
      - If you need to repeat back the information to the customer to confirm it is correct, specify that this is the information you collected for their most recent transaction.
    - Never ask any questions other than the above as part of the Authentication flow.
  - The customer must be authenticated with `tool:AttemptCvpAuthentication` in order to discuss a specific transaction.
  - - If customer is not authenticated, ask the customer to confirm the information. DO NOT transfer to a live agent.
  - **You should only authentic…
```

## 5. KB articles candidatos a reforzar

| Artículo (por título) | Menciones |
|---|---|
| How can I track my transfer? | 24 |
| Cancellations  – Ria Help Center | 18 |
| How do I cancel my money transfer? | 17 |
| How do I request a refund? | 12 |
| What's the difference between my order number and PIN? | 11 |
| How do refunds work? | 11 |
| My transfer is paid, but my recipient hasn't received it | 10 |
| My transfer is under review, when will it be released? | 9 |
| Why is my transfer taking longer than usual? | 8 |
| How do I get confirmation my transfer was paid? | 8 |
| I want to cancel a transfer that's been deposited | 8 |
| Why is my transfer on hold? | 7 |
| How to make a complaint | 6 |
| What info do I need to provide Ria to release the transfer? | 5 |
| How long do transfers take? | 4 |

## 6. Sesiones críticas / high para revisión humana

- **audit-01KPVBJ9RQP098PVHB0SWNDF1M** — `transaction_status` · `critical` · 619s · *{"text": "Hello?"}*
  - Agent looped on order number collection for 10+ minutes without resolving
  - Agent attempted live-agent transfer during offline hours without warning caller first
  - Agent abandoned partial order number 'ES9...' mid-collection and reverted to deflection
  - **Sugerencia:** In the 'Intents Where User Needs to Authenticate' block, add a fallback sub-flow: if tool:CustomerByOrderNumber fails after two attempts, immediately call tool:CustomerByTelephone using the caller's ANI to locate the customer record and pre-populate the order number, before asking the caller to spell it again. This avoids the current infinite loop when callers cannot reliably dictate an alphanumeric order number by voice.

- **audit-01KPTXX528P4E934M6427V28AG** — `transaction_status` · `critical` · 474s · *{"text": "J'ai fait un transfert à l'Italie, mais la personne ne le contient pas*
  - Agent switched languages repeatedly (French/German) despite caller requesting French
  - Agent looped on order number request for entire call without any alternative path
  - Caller provided reference 'FR965802709' but agent rejected it without attempting lookup
  - **Sugerencia:** In the 'Intents Where User Needs to Authenticate' block, add a fallback branch: after two failed order-number collection attempts, invoke tool:CustomerByTelephone using the caller's CLI before re-asking for the order number; if that also fails to identify the customer, permit transfer to a live agent rather than looping indefinitely — especially when the caller has explicitly requested one and money is at risk.

- **audit-01KPTFEP5R00BNKN7ZXZR9960W** — `authentication` · `critical` · 434s · *{"text": "Yes."}*
  - Agent asked wrong CVP questions: DOB and address instead of country sent to and transaction amount
  - Agent asked for transaction amount in wrong currency context, causing repeated failed auth attempts
  - Authentication failed twice leaving caller unresolved with money at stake
  - **Sugerencia:** In the 'Intents Where User Needs to Authenticate' block, enforce strict CVP question gating: before calling `tool:AttemptCvpAuthentication`, validate that the agent has collected exactly (1) full name, (2) country of most recent destination, and (3) total amount paid including fees in local currency — and no other questions. Add a pre-call guard that rejects any deviation (e.g. DOB, address, city) and re-prompts with the correct question. Additionally, when `tool:AttemptCvpAuthentication` fails twice, the block should instruct the agent to call `tool:CreateZendeskTicket` and provide the caller with a ticket reference number, rather than looping infinitely.

- **audit-01KPTYR1QY1XXS0DV7NGVYP4AM** — `authentication` · `critical` · 360s · *{"text": "Italiano."}*
  - Agent attempted CVP authentication 3+ times with identical data, looping without resolution
  - Agent never offered to transfer to human after repeated authentication failures
  - Caller's Italian language unsupported, increasing miscommunication risk during verification
  - **Sugerencia:** In the 'Intents Where User Needs to Authenticate' block, add a hard exit condition: after 2 consecutive failed `tool:AttemptCvpAuthentication` calls with the same parameters, the agent must stop re-prompting and instead offer to create a Zendesk ticket via `tool:CreateZendeskTicket` and inform the caller that a human agent will follow up — rather than looping indefinitely with the same unverifiable credentials.

- **audit-01KPTY35SHW3D8062E7VC7HQSW** — `authentication` · `critical` · 327s · *{"text": "Infor... Cancellata."}*
  - Caller never provided valid order number; agent looped on same request 7+ times without escalating
  - Agent failed to detect multilingual confusion (Italian, Spanish, Hindi) and adapt strategy
  - No fallback path offered when caller could not supply order number after repeated attempts
  - **Sugerencia:** In the 'Intents Where User Needs to Authenticate' block, add a fallback branch: after 3 consecutive failed attempts to collect a valid order number, invoke `tool:CustomerByTelephone` using the caller's ANI to attempt customer lookup without an order number; if that also fails, create a `tool:CreateZendeskTicket` with reason 'unable_to_authenticate_order_number' and the detected cancellation intent, then inform the caller a human agent will follow up, rather than continuing to loop.

- **audit-01KPTDEGAV0D992Q03C8N04QNR** — `transaction_status` · `critical` · 203s · *{"text": "Yes."}*
  - Agent conducted entire session in Italian despite caller speaking Spanish and English
  - Agent looping detected: repeated clarification requests without resolving caller intent
  - Agent failed to detect mixed Spanish/Italian/English speech and adapt language
  - **Sugerencia:** In the 'Intents Where User Needs to Authenticate' block, add a language-detection step before `tool:SetCallerType` that calls `tool:SearchFAQKnowledge` or triggers a language-switch prompt when transcription locales include non-matching tags (e.g., es-ES detected while agent responds in Italian). The agent should explicitly offer 'Would you prefer to continue in Spanish?' and switch fully to Spanish before proceeding to `tool:CustomerByOrderNumber`, preventing the entire authentication loop from occurring in the wrong language.

- **audit-01KPTGYHCVVCD8C2YYCZXG7C2M** — `transaction_status` · `critical` · 177s · *{"text": "Hi, ••. I was sending money for Ethiopia Bank, but it will not deliver*
  - Agent used OrderOverview instead of authenticating caller first and calling DetailedOrder
  - Agent told caller 'no further action can be taken right now' — incorrect and dismissive
  - Agent offered transfer to specialist then immediately reversed and contradicted itself
  - **Sugerencia:** In the 'Intents Where User Needs to Authenticate' block, enforce the full auth flow before any transaction disclosure: call `tool:CustomerByOrderNumber`, complete `tool:AttemptCvpAuthentication`, then call `tool:AttemptToSelectTransaction` and `tool:DetailedOrder` to retrieve recipient name and bank details internally — never ask the caller to re-supply information already on file. If status is paid but recipient reports non-receipt beyond 2 business days, the 'Check Order Status' block should surface a clear next-step path (e.g., create a Zendesk ticket via `tool:CreateZendeskTicket` with reason 'paid transfer not received by recipient') rather than toggling between 'contact a specialist' and 'nothing we can do'.

- **audit-01KPVA5ENW9GSF2PD4B33YBY8R** — `cancel_transaction` · `critical` · 139s · *{"text": "Necesito hablar con un agente, por favor."}*
  - Agent announced transfer to human at least 4 times but never completed it
  - Agent looping detected: repeated identical transfer announcement messages
  - Agent never attempted CustomerByOrderNumber or CVP authentication before escalating
  - **Sugerencia:** In the Cancel Customer Order block, before routing to a human, the agent must attempt to collect the order number and call tool:CustomerByOrderNumber followed by tool:AttemptCvpAuthentication, then tool:CheckTransactionCancellationEligibility. Only if those tools fail or the transaction is ineligible should escalation be offered. Additionally, the transfer logic must be guarded so that tool:transfer is only invoked once and confirmed successful before announcing it to the caller; repeated transfer announcement loops should trigger a fallback that informs the caller of a technical issue rather than repeating the same message.

- **audit-01KPT2QFF2J4PN14JCZQ87CVMG** — `cancel_transaction` · `critical` · 120s · *{"text": "Ja, hallo. Hallo, •••••. Ich will helfen. Und ich habe Geld geschickt *
  - Agent transferred to human immediately after receiving order number DE6, without attempting authentication or lookup
  - tool:CustomerByOrderNumber was never called despite caller providing order number
  - tool:CheckTransactionCancellationEligibility was never invoked; cancellation eligibility unknown
  - **Sugerencia:** In the 'Intents Where User Needs to Authenticate' block, add a guard that prevents invoking tool:transfer (SIP transfer) until tool:CustomerByOrderNumber returns a valid result AND tool:AttemptCvpAuthentication succeeds. Specifically, after the caller provides an order number (e.g. DE6...), the agent must immediately call tool:CustomerByOrderNumber with that number and proceed through the CVP flow before any transfer decision is made. The current path that triggers escalation on 'unsupportedIntent:recall' should instead route to the 'Cancel Customer Order' block after successful authentication, since a 'recall' or 'zurückmachen' intent maps directly to cancel.

- **audit-01KPTXP3815F0S4DEV2X5PTS4M** — `transaction_status` · `high` · 679s · *{"text": "Devo prendere dei soldi con la patente romana e il codice fiscale ital*
  - Agent attempted authentication using wrong questions (date of birth, destination amount) instead of mandated CVP fields (full name, destination country, last transaction amount in local currency)
  - Agent called tool:GetEstimatedDelivery without successful tool:AttemptCvpAuthentication, triggering a no-authentication error
  - Agent called tool:OrderOverview before customer was authenticated, violating intended flow
  - **Sugerencia:** In the 'Intents Where User Needs to Authenticate' journey block, enforce a hard gate: tool:CustomerByOrderNumber must return a success response before the agent collects any CVP answers, and tool:AttemptCvpAuthentication must return success before tool:GetEstimatedDelivery or tool:DetailedOrder is invoked. Add an explicit pre-condition check in the block that aborts to a 'cannot proceed without authentication' message if either call fails, rather than allowing the agent to improvise alternative questions (date of birth, destination amount) or surface order details unauthenticated.
