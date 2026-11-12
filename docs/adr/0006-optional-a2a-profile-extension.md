# 0006: PharmA2A is an optional A2A profile extension

**Status:** Accepted

**Date:** 2026-11-03

## Context

A2A defines extensions, identified by a URI and declared in the agent card. A profile
extension narrows what messages carry without adding methods or states, which is exactly
what PharmA2A does. Without a declaration, nothing standard tells an A2A client that an
agent speaks the dictionary.

## Options considered

1. No declaration; the media types alone identify the messages.
2. Declare the extension as required.
3. Declare it as optional.

## Decision

Option 3. The agent card declares `https://pharma2a.org/ext/v1` in
`capabilities.extensions` with `required: false`, and artifacts that carry a dictionary
message list it in `extensions`. Clients may ask for it with the `A2A-Extensions` header. It
is optional because the agent validates every message against the dictionary anyway: a
required extension would only reject clients that do not send the header. The URI carries
the dictionary version; a breaking change gets a new URI.

## Consequences

PharmA2A is identified in A2A's own terms, and the URI stays under the project's domain. The
official A2A extension track (a repository under the A2A organisation, a URI under
a2a-protocol.org, a vote of the technical committee) remains possible later and is not
pursued for version 1.
