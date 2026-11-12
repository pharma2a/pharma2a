# 0003: A transcribed acknowledgement is never an automatic confirmation

**Status:** Accepted

**Date:** 2026-10-30

## Context

Until suppliers run their own agents, their answers arrive as human mails. The bridge reads
them and transcribes them into an `accuse_reception`, with a language model. A transcription
can be wrong: a quantity misread, a polite question taken for an acceptance.

## Options considered

1. Treat every acknowledgement the same way, whatever produced it.
2. Mark where each acknowledgement comes from, and let the pharmacy's agent decide what to
   accept automatically.

## Decision

Option 2. Every acknowledgement carries `origine`: `erp` when a supplier system produced it,
`transcription` when an agent read a human reply. The origin is set by the channel, never by
the extractor. A transcription is never accepted automatically by the pharmacy's agent: it
goes to the pharmacist. Fields nobody stated stay `null`; the extractor never fills them in.

## Consequences

Mail remains a usable channel from day one without trusting a model with a commercial
decision. A supplier that connects its system gets acknowledgements that can be processed
without a human, because `erp` says they were not interpreted.
