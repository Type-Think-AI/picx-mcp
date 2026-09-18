# Requirements Document

## Introduction

**PicX Official Plugin** publishes PicX into the two host directories that can send
it distribution: the OpenAI universal plugin directory (serving ChatGPT and Codex)
and Anthropic's connector directory (serving claude.ai, Claude Desktop, Cowork, and
the Claude mobile apps). Today PicX is reachable over MCP only by a user who already
knows to paste an endpoint URL and an API key into a client. The goal of this
initiative is that a user who has never heard of PicX can find it in a host's
directory, install it, authorize it, and get a usable generated result.

Both hosts consume the **same remote MCP server**, which is why this is one
initiative rather than two. The composition both hosts expect is identical: one
connector (the MCP server, providing live data, authentication, and controlled
actions) plus one or more skills (packaged instructions that teach the model how to
sequence that server's tools). The MCP server alone is not a publishable plugin.

This document supersedes and formalizes the prose scoping in
`docs/OFFICIAL_PLUGIN_REQUIREMENTS.md`, which remains useful as the narrative
rationale and source list.

## Verified starting state

Every item below was checked directly on 2026-09-18 against the live deployment and
the `picx-mcp` source, not inferred:

- `mcp.picxstudio.com` is deployed and answering (`GET /` returns 405, the expected
  response from a POST-only MCP endpoint).
- `src/picx_mcp/server.py` mounts with `stateless_http=True`, with an in-code
  rationale: the load balancer does not forward `Set-Cookie`, so sticky sessions
  cannot work and stateless is the only correct mode.
- 18 tools are registered: `picx_generate_image`, `picx_edit_image`,
  `picx_generate_video`, `picx_get_generation`, `picx_get_generation_events`,
  `picx_get_generation_deliveries`, `picx_list_generations`,
  `picx_search_templates`, `picx_get_template`, `picx_list_models`,
  `picx_get_account`, `picx_get_tier`, `picx_get_usage`, `picx_upload_asset`,
  `picx_list_assets`, `picx_delete_asset`, `picx_get_webhook_deliveries`,
  `picx_redeliver_webhook`.
- Authentication is `Authorization: Bearer pxsk_…` passthrough. `src/picx_mcp/auth.py`
  exposes `build_auth()`, which returns `None` in that passthrough mode.
- `GET /.well-known/oauth-protected-resource` returns **404**.
  `GET /.well-known/oauth-authorization-server` returns **404**.
- There is no `skills/` directory and no plugin manifest in the repository.

## Correcting one premise

Statelessness is **not** a requirement either host states. The MCP specification
(`2025-11-25`) makes sessions optional: a server **MAY** assign an `Mcp-Session-Id`
at initialization, and a server that never issues one is fully conformant. The word
"stateless" does not appear in OpenAI's plugin documentation. PicX already operates
statelessly by deliberate choice, so **Requirement 2 is largely already satisfied**
and carries verification work rather than implementation work.

## Terminology

- **Host** — OpenAI (ChatGPT, Codex) or Anthropic (Claude surfaces), acting as the
  MCP client on the user's behalf.
- **Connector** — the remote MCP server, `mcp.picxstudio.com`.
- **Skill** — a directory containing `SKILL.md` plus optional `references/`,
  `scripts/`, `assets/`, packaging workflow instructions.
- **Plugin** — the published unit: manifest + connector mapping + skills.
- **Authorization Server** — the OAuth 2.1 issuer PicX designates for MCP client
  authorization.
- **Grant** — one user's authorization of one host to act against PicX.

## Assumptions and open decisions

1. **Authorization server topology — DECIDED 2026-09-18.** PicX will front its
   existing Google-backed identity with a FastMCP `OAuthProxy` rather than
   standing up a dedicated authorization server. The proxy presents the
   registration surface MCP hosts require (Google does not support Dynamic
   Client Registration) while PicX operates no token issuer of its own.
   Rationale, alternatives considered, and the evidence behind the choice are
   recorded in `design.md`. Requirement 3 work is unblocked.

   Three findings from confirming this against source, each of which changes the
   work rather than merely supporting the decision:

   - `auth.py` already specifies this exact topology in detail as "Phase 5",
     including its security properties: revoking a grant invalidates only the
     session key while the user's own `pxsk_` keys keep working, the connector
     never holds a real `pxsk_` on the OAuth plane, and session keys carry
     per-session credit ceilings independent of the account's daily cap. The
     decision resumes a designed plan, it does not open a new one.
   - `build_auth()` **cannot currently run.** It calls `OAuthProxy(client_id=…,
     client_secret=…)`, but the installed `fastmcp==4.0.0b3` requires
     `upstream_authorization_endpoint`, `upstream_token_endpoint`,
     `upstream_client_id` and `token_verifier`, and names the secret
     `upstream_client_secret`. The call would raise `TypeError` on first OAuth
     boot. It has never executed because `oauth_configured` requires four
     secrets that are not set, so the defect is latent and invisible. Repairing
     it is the first concrete step of Task 4.
   - The primitives Requirement 3 and Requirement 4 need are native `OAuthProxy`
     parameters, not things to build: `enable_cimd` (3.4), `forward_pkce` (3.4),
     `forward_resource` (3.5), `valid_scopes` (4.1, 4.2), and
     `require_authorization_consent`. `fastmcp.server.auth.providers.google.GoogleProvider`
     also exists, which resolves the stale TODO in `auth.py` asking whether it
     ships; it is preferred over a raw `OAuthProxy` for tighter scope and claim
     mapping.
2. **Credit-spend consent is a product decision.** The generation tools spend real
   credits. Requirement 4 states the control, not the policy; the per-grant ceiling
   value and whether generation requires per-call confirmation are for the product
   owner.
3. **Anthropic's submission path is unverified.** The directory listing format is
   observed from a published bundle, but the submission mechanics and whether
   OpenAI's `SKILL.md` is byte-compatible with Anthropic's skill format have not
   been confirmed. Requirement 8 therefore requires confirmation as an explicit
   step rather than assuming parity.
4. **Tool count for submission is undecided.** Requirement 5 requires a decision on
   which of the 18 tools ship publicly; webhook delivery inspection and redelivery
   are developer-operations capabilities rather than end-user goals.

---

### Requirement 1: Plugin package and directory presence

**User Story:** As a prospective PicX user, I want to find PicX in my AI host's
directory and install it in one step, so that I can use PicX without knowing what
MCP is.

#### Acceptance Criteria

1. THE Plugin SHALL provide a root manifest declaring the portable package
   identity, including package name, version, and description.
2. THE Plugin SHALL discover skills in `skills/` and the connector in `mcp.json` at
   the package root, and SHALL NOT rely on a host-specific declaration to add,
   replace, or disable either.
3. THE Plugin SHALL provide host-specific presentation metadata including display
   name, short description, long description, developer name, category,
   capabilities, website URL, privacy policy URL, terms of service URL, starter
   prompts, brand colour, composer icon, and logo.
4. WHEN the Plugin has no user interface, THE Plugin SHALL omit screenshots.
5. THE Plugin SHALL store visual assets under `assets/` and SHALL express every
   host-specific path relative to the package root.
6. WHEN the Plugin is approved by a Host, THE publisher SHALL perform the Host's
   explicit publish step, because approval alone does not list the Plugin in the
   directory.

### Requirement 2: Connector transport conformance

**User Story:** As a Host, I want to connect to the PicX connector over a standard
transport from my own infrastructure, so that I can serve every user without
per-user configuration.

#### Acceptance Criteria

1. THE Connector SHALL be reachable over HTTPS on a publicly accessible domain and
   SHALL NOT be submitted as a local, tunnelled, or test endpoint.
2. THE Connector SHALL implement the MCP Streamable HTTP transport.
3. THE Connector SHALL operate without server-side session affinity, and SHALL NOT
   require an `Mcp-Session-Id` from the client.
4. WHEN a Host sends an `MCP-Protocol-Version` header, THE Connector SHALL respond
   according to that protocol version; IF the version is invalid or unsupported,
   THEN THE Connector SHALL return HTTP 400.
5. WHEN the Connector receives no `MCP-Protocol-Version` header and cannot otherwise
   determine the version, THE Connector SHALL assume protocol version `2025-03-26`.
6. THE Connector SHALL behave identically on every replica, and any shared secret
   required for cross-replica continuity SHALL be identical on all replicas.
7. THE publisher SHALL submit a single universal connector URL that serves all users
   and organizations, and SHALL NOT submit a per-workspace template URL.

### Requirement 3: Authorization

**User Story:** As a user installing PicX from a directory, I want to sign in to my
existing PicX account through my Host and grant it access, so that I never handle an
API key.

#### Acceptance Criteria

1. THE Connector SHALL publish protected resource metadata at
   `/.well-known/oauth-protected-resource` containing the canonical resource
   identifier and one or more authorization server issuer URLs, and SHOULD include
   supported scopes and resource documentation.
2. WHEN the Connector rejects an unauthenticated request, THE Connector SHALL return
   HTTP 401 with a `WWW-Authenticate` challenge naming the protected resource
   metadata URL and the required scope.
3. THE Authorization Server SHALL publish discovery metadata at either
   `/.well-known/oauth-authorization-server` or `/.well-known/openid-configuration`,
   declaring issuer, authorization endpoint, token endpoint, and supported token
   endpoint authentication methods.
4. THE Authorization Server SHALL support at least one Host client identification
   method among Client ID Metadata Documents, Dynamic Client Registration, and a
   predefined OAuth client, and SHALL support PKCE.
5. THE Connector and Authorization Server SHALL accept and echo the `resource`
   parameter throughout the authorization flow, using the exact value published in
   protected resource metadata.
6. THE Connector SHALL verify the access token on every request, and SHALL NOT rely
   on any state established by a previous request.
7. WHEN a token is absent, expired, malformed, or carries insufficient scope, THE
   Connector SHALL reject the request before any PicX API call, any credit
   deduction, and any provider execution.
8. THE publisher SHALL provide a demo account that a Host reviewer can sign into
   through the authorization flow with no further configuration, and SHALL NOT
   require a reviewer to obtain or paste an API key.
9. THE Connector SHALL continue to support direct API key authentication for
   developer-mode and self-hosted use, and the introduction of OAuth SHALL NOT break
   existing API key callers.

### Requirement 4: Scope and spend control

**User Story:** As a PicX account holder, I want a Host's access to be limited and
my credit balance protected, so that authorizing a plugin cannot quietly drain my
account.

#### Acceptance Criteria

1. THE Authorization Server SHALL express scopes that mirror the existing PicX API
   key scope vocabulary, and SHALL NOT define a second parallel permission model.
2. THE Connector SHALL reject a tool call whose required scope is absent from the
   presented Grant, before any credit deduction.
3. THE Connector SHALL enforce a credit ceiling per Grant, and WHEN the ceiling is
   reached THE Connector SHALL refuse further spending tool calls and SHALL return
   an error that states the ceiling was reached.
4. THE Connector SHALL annotate every tool with accurate safety hints, and SHALL
   mark each credit-spending tool as non-read-only.
5. WHEN a tool call would spend credits, THE Connector SHALL return the credit cost
   in its result so the Host can surface the cost to the user.

### Requirement 5: Submitted tool surface

**User Story:** As a Host reviewer, I want each exposed tool to map to a
recognizable user goal and to return only what that goal needs, so that I can
approve the plugin.

#### Acceptance Criteria

1. THE publisher SHALL decide which subset of the 18 registered tools is exposed in
   the published Plugin, and every exposed tool SHALL map to a recognizable end-user
   goal.
2. THE Connector SHALL NOT expose developer-operations tools in the published
   Plugin where those tools serve platform maintenance rather than an end-user goal.
3. Each exposed tool SHALL declare an action-oriented name, a human-readable title,
   a description stating when to use it, an explicit input schema, and an output
   schema where it returns structured data.
4. THE Connector SHALL NOT include auth secrets, tokens, keys, or passwords in any
   tool result.
5. THE Connector SHALL NOT include internal or diagnostic identifiers in tool
   results, including session identifiers, trace identifiers, request identifiers,
   internal account identifiers, and internal logs.
6. THE Connector SHALL limit personal data in tool results to what the user's
   request requires.
7. THE Connector SHALL return a server `instructions` value covering guidance that
   spans tools, with the most important guidance in the first 512 characters, and
   SHALL NOT restate every tool description in it.
8. Each exposed tool SHALL return stable identifiers in structured results so a
   later tool call can refer to the same record.

### Requirement 6: Skills

**User Story:** As a user asking my Host for a video, I want it to choose the right
PicX mode and supply the fields that mode needs, so that I get a result instead of a
validation error.

#### Acceptance Criteria

1. THE Plugin SHALL include at least one Skill, and each Skill SHALL occupy its own
   directory containing `SKILL.md` with a name and a description.
2. Each Skill description SHALL state the workflow and the conditions that should
   trigger it, and each Skill body SHALL state the expected input, the steps to
   follow, the output the user receives, the facts the model must not infer, and
   when to ask, stop, or decline.
3. THE Plugin SHALL include a Skill covering video mode selection that states the
   required fields for each of the seven modes, and SHALL state that `lipsync`
   requires no prompt while every other mode requires a non-empty prompt.
4. THE Plugin SHALL include a Skill covering template-first generation that states
   the three catalogue behaviours: that the result total is an estimate rather than
   a count and the caller should page until a short page returns, that the topic
   filter is supported while the topic field is always null, and that a null prompt
   indicates a premium or gated row rather than missing data.
5. WHEN a Skill depends on the Connector, THE Plugin SHALL declare that dependency
   with the streamable HTTP transport and the connector URL.
6. THE publisher SHALL treat Host-imported skills as a static snapshot taken at scan
   time, and WHEN a Skill changes THE publisher SHALL redeploy the Connector,
   rescan, and resubmit before the change reaches users.
7. THE publisher SHALL test each Skill against direct requests, indirect requests
   expressing the same goal, incomplete inputs that should trigger a follow-up
   question, requests that should not activate the Skill, and edge cases where the
   Skill must avoid inventing information.

### Requirement 7: Submission readiness

**User Story:** As the publisher, I want every non-engineering gate cleared in
parallel with the build, so that submission is not blocked on paperwork after the
code is ready.

#### Acceptance Criteria

1. THE publisher SHALL complete Host identity verification for the exact name the
   Plugin is published under, using individual verification to publish under a
   person and business verification to publish under a company.
2. IF identity verification for the publishing name is incomplete, THEN submission
   SHALL be treated as blocked, because publishing under an unverified name is
   rejected at review.
3. THE submitting account SHALL hold the Host permission required to create and
   submit plugin drafts, and the reviewing account SHALL hold the permission
   required to view drafts and review status.
4. THE publisher SHALL provide company URL, privacy policy URL, terms of service
   URL, plugin name, logo, description, connector and tool information,
   localization information, and test prompts with their expected responses.
5. THE publisher SHALL verify the Plugin end to end in the Host's developer mode
   before submitting it for review.
6. THE publisher SHALL submit the Plugin only when it is intended to be publicly
   available in the countries declared during submission, and SHALL use developer
   mode for private or workspace-only use.

### Requirement 8: Anthropic connector parity

**User Story:** As a Claude user, I want the same PicX capability from Claude that a
ChatGPT user gets, so that my choice of assistant does not decide whether I can use
PicX.

#### Acceptance Criteria

1. THE Connector SHALL remain reachable from Anthropic's published cloud IP ranges,
   because Claude connects from Anthropic's infrastructure rather than from the
   user's device, including for desktop and local Claude surfaces.
2. IF the Connector is ever placed behind an IP allowlist, VPN, or private network,
   THEN the publisher SHALL allowlist Anthropic's published ranges, because the
   connector would otherwise fail for all Claude users while remaining reachable
   from a developer machine.
3. THE Connector SHALL satisfy Anthropic connector authorization using the same
   OAuth implementation built for Requirement 3, and SHALL NOT require a
   PicX-specific credential paste flow.
4. THE publisher SHALL confirm Anthropic's submission path for directory listing and
   SHALL confirm whether the Requirement 6 Skill format is accepted unmodified,
   before assuming a single bundle serves both directories.

### Requirement 9: Maintenance after publication

**User Story:** As the publisher, I want a release process that matches how Hosts
re-review published plugins, so that a routine change does not silently break the
listing.

#### Acceptance Criteria

1. THE publisher SHALL treat tool and metadata changes as subject to continuous
   Host review after publication.
2. WHEN a tool schema, description, annotation, or the server `instructions` value
   changes, THE publisher SHALL deploy the change and allow the Host's required
   checks to complete before relying on it.
3. WHEN a Skill or the Plugin manifest changes, THE publisher SHALL submit a new
   version for review.
4. THE publisher SHALL keep the published connector URL stable, and WHEN the URL
   must change THE publisher SHALL treat it as a new submission rather than a
   deployment change.
5. THE Connector SHALL remain available and functional after publication, and the
   publisher SHALL monitor its availability independently of Host review status.
