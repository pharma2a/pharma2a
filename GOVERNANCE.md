# Governance

**Who decides.** SyderaAI holds the PharmA2A protocol and decides what enters the dictionary.
Decisions and their reasons are written in public, in the repository's issues, and the
lasting ones as decision records in `docs/adr/`.

**How a change is proposed.** Anyone opens an issue that states the need in one sentence,
with an example message. A new field must be something one side of the exchange actually
states (the supplier or the pharmacy): nothing inferred, and never an economic figure of the
pharmacy. Code changes come as pull requests with their tests (see `CONTRIBUTING.md`).

**Versions.** The dictionary changes by versions, never silently. An added field is optional;
a removed field, or a field made required, makes a new version of the schema and a new
extension URI (`https://pharma2a.org/ext/v2`). An agent that speaks version N keeps
understanding version N minus one.

**The name.** The content is free: the dictionary is CC0 1.0, the agent Apache 2.0. The name
PharmA2A is a trademark of SyderaAI: a conforming implementation may say it speaks PharmA2A,
it may not call itself PharmA2A (see `TRADEMARK.md`).

**Relationship with A2A.** PharmA2A is a community profile extension of the A2A protocol
(Linux Foundation). It follows the A2A specification and does not change it.

*Gouvernance : SyderaAI tient le protocole ; les décisions et leurs raisons sont publiques,
dans les issues et dans `docs/adr/` ; le dictionnaire change par versions, jamais en silence.*
