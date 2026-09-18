# Implementation Plan: PicX Official Plugin Directory

## Overview

Publish the already-deployed PicX MCP connector (`mcp.picxstudio.com`) as an official plugin in the OpenAI universal plugin directory (ChatGPT + Codex) and the Anthropic connector directory (claude.ai, Claude Desktop, Cowork, mobile). Both hosts consume the same remote MCP server, so this is one initiative: one connector plus one or more skills, packaged with a manifest and presentation metadata, gated behind OAuth authorization and identity verification.

The hard engineering is done. Verified live on 2026-09-18: the connector answers, `server.py` mounts `stateless_http=True`, 18 tools are registered and working, and template search over ~50k rows ships via `picx_search_templates` / `picx_get_template`. This plan therefore writes verification tasks for transport and statelessness, not implementation. The real work is the authorization surface (both `.well-known` documents return 404 today, `build_auth()` returns `None`), a skills bundle (no `skills/` directory exists), a plugin manifest and brand assets (none exist), identity verification, response hygiene, and the submission/publish/maintenance process.

Tasks are ordered so each produces contracts or durable behaviour later tasks need. Authorization is the critical path and the only item with real design risk, so its topology decision (Task 1) gates its implementation (Tasks 3–6) and comes first. Skills authoring (Task 8), identity and submission material (Task 12), and brand assets/packaging (Task 11) are genuinely parallel to authorization and are marked as such. Submission (Task 15) never precedes developer-mode end-to-end verification (Task 14). Test/verification sub-tasks marked `*` after the checkbox are optional execution accelerators; the core tasks are required. Dependencies are stated explicitly. No effort estimates or calendar dates are implied. Nothing is marked `- [x]` except where the verified starting state records the work as already shipped — those carry verification-only sub-tasks.

Source files read before writing this plan: `src/picx_mcp/{server,settings,context,auth,client}.py`. The authoritative video-mode contract lives in picx-studio at `/Users/yash/Projects/picx/picx-studio/api/app/public_api/schemas.py` (seven modes `text|image|reference|frames|extend|lipsync|edit`, per-mode required fields enforced by validators, `lipsync` prompt-exempt). Skill definitions follow `docs/OFFICIAL_PLUGIN_REQUIREMENTS.md`.

## Tasks

- [ ] 1. Decide the authorization server topology (human decision, gates all Requirement 3 work)
  - Choose between standing up a dedicated PicX authorization server and designating the identity provider already behind `ai.picxstudio.com`, and record the decision, its owner, and its rationale in this spec directory.
  - Frame the decision against the scaffolding that already exists: `settings.py` carries `google_client_id`, `google_client_secret`, `jwt_signing_key`, `storage_encryption_key`, and `request_state_key`; `auth.py` documents a two-plane design with an OAuth-token-to-session-key exchange path (`exchange_token_for_session_key`, currently `NotImplementedError`).
  - Decide the client identification method to support (Client ID Metadata Documents, Dynamic Client Registration, or a predefined OAuth client) and confirm PKCE support, since this choice shapes the discovery metadata in Task 3.
  - Decide the canonical `resource` identifier value that protected resource metadata will publish and that the whole flow must echo unchanged.
  - _Dependencies: None._
  - _Requirements: 3.3, 3.4, 3.5._
  - _Validation: A written decision record naming the chosen issuer, the client identification method, PKCE support, and the canonical resource identifier exists and names a human owner slot; no implementation task in Tasks 3–6 begins until this record is signed off._

- [x] 2. Verify connector transport and stateless conformance
  - The connector is deployed and answering, and `build_app()` mounts `mcp.http_app(stateless_http=True, host_origin_protection=True, allowed_hosts=…, path="/")`; this task confirms conformance rather than building it.
  - [ ]* 2.1 Verify Streamable HTTP transport and public HTTPS reachability
    - Confirm `mcp.picxstudio.com` is reachable over HTTPS on the public domain, that `GET /` returns 405 (POST-only MCP endpoint answering), and that the endpoint is not a local, tunnelled, or test URL.
    - _Dependencies: None._
    - _Requirements: 2.1, 2.2._
  - [ ]* 2.2 Verify stateless operation and protocol-version handling
    - Confirm the connector operates without server-side session affinity and does not require an `Mcp-Session-Id`; confirm that a valid `MCP-Protocol-Version` header is honoured, that an invalid or unsupported version returns HTTP 400, and that a missing/undeterminable version is treated as `2025-03-26`.
    - _Dependencies: None._
    - _Requirements: 2.3, 2.4, 2.5._
  - [ ]* 2.3 Verify replica-identical behaviour and cross-replica secret pinning
    - Confirm the deployed environment pins `request_state_key` to an identical value on every replica (the in-code warning fires when it is unset), so a round started on one replica validates on another; confirm no per-replica state diverges.
    - _Dependencies: None._
    - _Requirements: 2.6._
  - [ ] 2.4 Confirm and record the single universal connector URL for submission
    - Record `https://mcp.picxstudio.com` as the universal connector URL that serves all users and organizations, and confirm no per-workspace template URL is submitted.
    - _Dependencies: None._
    - _Requirements: 2.7._

- [ ] 3. Publish OAuth discovery surface (protected resource + authorization server metadata)
  - Implement the discovery documents whose absence is the current blocker: `GET /.well-known/oauth-protected-resource` and `GET /.well-known/oauth-authorization-server` (or `/.well-known/openid-configuration`) both return 404 today. Add these as custom routes alongside the existing `/health` route in `server.py`, outside auth middleware so hosts can discover them cold.
  - [ ] 3.1 Serve protected resource metadata
    - Return the canonical `resource` identifier from Task 1, one or more `authorization_servers` issuer URLs, and, where useful, `scopes_supported` and `resource_documentation`.
    - _Dependencies: 1._
    - _Requirements: 3.1._
  - [ ] 3.2 Serve authorization server metadata
    - On the designated issuer, publish `issuer`, `authorization_endpoint`, `token_endpoint`, `token_endpoint_auth_methods_supported`, and the client identification method chosen in Task 1; confirm PKCE is advertised.
    - _Dependencies: 1._
    - _Requirements: 3.3, 3.4._
  - [ ] 3.3 Echo the `resource` parameter throughout the flow
    - Accept and echo the `resource` parameter at the connector and authorization server, using the exact value published in protected resource metadata, so ChatGPT's round-trip validates.
    - _Dependencies: 3.1, 3.2._
    - _Requirements: 3.5._
  - [ ]* 3.4 Add tests for discovery documents and resource echo
    - Assert both `.well-known` documents return 200 with the required fields, that the published `resource` value matches across documents, and that the `resource` parameter is echoed unchanged.
    - _Dependencies: 3.1, 3.2, 3.3._
    - _Requirements: 3.1, 3.3, 3.5._

- [ ] 4. Implement token verification, the 401 challenge, and API-key coexistence
  - Build on `context.py`, which already resolves a bearer token per request and today raises 501 in the OAuth path. Wire the `exchange_token_for_session_key` path so a verified OAuth access token resolves to a scoped session key server-side, and verify the token on every request without relying on prior-request state (the stateless mode makes this natural).
  - [ ] 4.1 Verify the access token on every request before any side effect
    - Verify the token per request; when a token is absent, expired, malformed, or carries insufficient scope, reject before any PicX API call, any credit deduction, and any provider execution.
    - _Dependencies: 1, 3.2._
    - _Requirements: 3.6, 3.7._
  - [ ] 4.2 Return a spec-compliant 401 `WWW-Authenticate` challenge
    - On rejecting an unauthenticated request, return HTTP 401 with a `WWW-Authenticate` challenge naming the protected resource metadata URL and the required scope, so the host can discover metadata cold.
    - _Dependencies: 3.1, 4.1._
    - _Requirements: 3.2._
  - [ ] 4.3 Preserve direct API-key authentication for developer and self-hosted use
    - Keep the `Authorization: Bearer pxsk_…` passthrough working unchanged; introducing OAuth must not break existing API-key callers (the `context.py` resolution order already prefers a direct `pxsk_`).
    - _Dependencies: 4.1._
    - _Requirements: 3.9._
  - [ ]* 4.4 Add tests for token verification, the 401 challenge, and API-key coexistence
    - Assert absent/expired/malformed/insufficient-scope tokens are rejected before any side effect, that the 401 carries the correct `WWW-Authenticate` challenge, and that a valid `pxsk_` still authenticates unchanged.
    - _Dependencies: 4.1, 4.2, 4.3._
    - _Requirements: 3.2, 3.6, 3.7, 3.9._

- [ ] 5. Implement scope vocabulary and per-grant scope enforcement
  - [ ] 5.1 Map OAuth scopes onto the existing PicX API-key scope vocabulary
    - Express scopes that mirror the PicX API-key scopes (e.g. `images:generate`, `videos:generate`, `templates:read`, `assets:write`); do not define a second parallel permission model. A token issued to a host must not exceed what the equivalent PicX key would allow.
    - _Dependencies: 1, 4.1._
    - _Requirements: 4.1._
  - [ ] 5.2 Reject a tool call whose required scope is absent, before any credit deduction
    - Enforce the required scope for each tool at the connector and reject before any credit deduction or provider execution.
    - _Dependencies: 5.1._
    - _Requirements: 4.2._
  - [ ]* 5.3 Add tests for scope mapping and pre-deduction rejection
    - Assert that a call missing its required scope is rejected before any side effect and that scope values match the PicX API-key vocabulary.
    - _Dependencies: 5.1, 5.2._
    - _Requirements: 4.1, 4.2._

- [ ] 6. Enforce the per-grant credit ceiling and surface credit cost
  - [ ] 6.1 Decide the per-grant credit ceiling and per-call confirmation policy (product decision)
    - Set the per-grant value (settings already carry `session_credit_ceiling` defaulting to 2000 and `confirm_credit_threshold` defaulting to 200) and decide whether generation requires per-call confirmation. Record the decision and its product owner.
    - _Dependencies: None._
    - _Requirements: 4.3._
  - [ ] 6.2 Enforce the credit ceiling per grant and refuse spend when reached
    - Track credits spent per grant; when the ceiling is reached, refuse further spending tool calls and return an error stating the ceiling was reached.
    - _Dependencies: 4.1, 5.2, 6.1._
    - _Requirements: 4.3._
  - [ ] 6.3 Annotate credit-spending tools and return credit cost in results
    - Mark each credit-spending tool as non-read-only with accurate safety hints (destructive/open-world), and return the credit cost in the tool result so the host can surface it to the user.
    - _Dependencies: 6.2._
    - _Requirements: 4.4, 4.5._
  - [ ]* 6.4 Add tests for ceiling enforcement, safety hints, and cost reporting
    - Assert that reaching the ceiling refuses further spend with the stated error, that spending tools are marked non-read-only, and that results carry the credit cost.
    - _Dependencies: 6.2, 6.3._
    - _Requirements: 4.3, 4.4, 4.5._

- [ ] 7. Decide the published tool surface and audit response hygiene
  - [ ] 7.1 Decide which of the 18 registered tools ship publicly (human decision)
    - Decide the exposed subset so every published tool maps to a recognizable end-user goal, and keep developer-operations tools (webhook delivery inspection `picx_get_webhook_deliveries`, redelivery `picx_redeliver_webhook`, and other platform-maintenance tools) out of the published plugin. Record the decision and rationale.
    - _Dependencies: None._
    - _Requirements: 5.1, 5.2._
  - [ ] 7.2 Ensure each exposed tool has an action-oriented contract and stable identifiers
    - Confirm each exposed tool declares an action-oriented name, a human-readable title, a description stating when to use it, an explicit input schema, and an output schema where it returns structured data; confirm structured results return stable identifiers a later call can refer to.
    - _Dependencies: 7.1._
    - _Requirements: 5.3, 5.8._
  - [ ] 7.3 Audit every exposed tool result for response hygiene
    - Audit results so they contain no auth secrets, tokens, keys, or passwords; no internal or diagnostic identifiers (session IDs, trace IDs, request IDs, internal account IDs, internal logs); and no personal data beyond what the user's request requires. The tools forward `/v1` payloads fairly directly, so correlation-style fields and internal IDs are the likely offenders.
    - _Dependencies: 7.1._
    - _Requirements: 5.4, 5.5, 5.6._
  - [ ]* 7.4 Add response-hygiene assertion tests over exposed tool results
    - For each exposed tool, assert results carry no secret, no internal/diagnostic identifier, and no PII beyond the request's need, and that stable record identifiers are present.
    - _Dependencies: 7.2, 7.3._
    - _Requirements: 5.4, 5.5, 5.6, 5.8._

- [ ] 8. Author the server `instructions` value
  - Populate the server `instructions` (currently set in `build_server()` to generation-preference guidance) with guidance that spans tools, most important detail in the first 512 characters, without restating every tool description. Good candidates: search templates before generating from a vague prompt, check `picx_get_tier` before promising a resolution, treat a null template `prompt` as gated.
  - _Dependencies: 7.1._
  - _Requirements: 5.7._

- [ ] 9. Author the skills bundle (parallel to authorization; no `skills/` directory exists yet)
  - Create `skills/` at the plugin root, one directory per skill, each containing `SKILL.md` with a `name` and a `description` that states the workflow and its trigger conditions; each body states expected input, steps, the output the user receives, the facts the model must not infer, and when to ask, stop, or decline. Where a skill depends on the connector, declare the dependency with the streamable HTTP transport and the connector URL. Author `video-modes` first as the highest-value skill.
  - [ ] 9.1 Author `video-modes` skill (highest value, first)
    - State the required fields for each of the seven modes and that `lipsync` requires no prompt while every other mode requires a non-empty prompt. Cite the authoritative source `/Users/yash/Projects/picx/picx-studio/api/app/public_api/schemas.py` for the per-mode field rules (modes `text|image|reference|frames|extend|lipsync|edit`; `frames` needs `start_frame_url`; `extend` needs `source_video_url`; `lipsync` needs `source_video_url` + `audio_url`; `edit` needs `source_video_url` + `image_url`) rather than restating rules not read. Sequences `picx_generate_video`, `picx_get_generation`.
    - _Dependencies: None._
    - _Requirements: 6.1, 6.2, 6.3, 6.5._
  - [ ] 9.2 Author `template-first-generation` skill
    - State the three catalogue behaviours: the result `total` is an estimate rather than a count, so page until a short page returns; the topic filter is supported while the topic field is always null; a null `prompt` indicates a premium or gated row rather than missing data. Sequences `picx_search_templates`, `picx_get_template`, `picx_generate_image`.
    - _Dependencies: None._
    - _Requirements: 6.1, 6.2, 6.4, 6.5._
  - [ ] 9.3 Author `picx-overview` skill
    - Orientation skill: what PicX is, credits, models, tiers. Sequences `picx_list_models`, `picx_get_tier`.
    - _Dependencies: None._
    - _Requirements: 6.1, 6.2, 6.5._
  - [ ] 9.4 Author `image-editing` skill
    - Iterative edit chains on an existing image; state that `/v1/images/edit` rejects data URIs so a local file must be uploaded to an https URL first. Sequences `picx_upload_asset`, `picx_edit_image`.
    - _Dependencies: None._
    - _Requirements: 6.1, 6.2, 6.5._
  - [ ] 9.5 Author `async-and-delivery` skill
    - Long-running generations: poll status, stream events, inspect delivery. Sequences `picx_get_generation_events`, `picx_get_generation_deliveries`.
    - _Dependencies: None._
    - _Requirements: 6.1, 6.2, 6.5._
  - [ ] 9.6 Author `credits-and-limits` skill
    - Answer "what will this cost" and "why was I throttled" before spending. Sequences `picx_get_usage`, `picx_get_tier`, `picx_get_account`.
    - _Dependencies: None._
    - _Requirements: 6.1, 6.2, 6.5._
  - [ ]* 9.7 Test each skill against direct, indirect, incomplete, non-activating, and no-invention cases
    - For every skill, exercise direct requests, indirect requests expressing the same goal, incomplete inputs that should trigger a follow-up question, requests that should not activate the skill, and edge cases where the skill must avoid inventing information.
    - _Dependencies: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6._
    - _Requirements: 6.7._

- [ ] 10. Record the skills static-snapshot release rule
  - Document, in the plugin's release notes and this spec, that host-imported skills are a static snapshot taken when the host scans the server, not fetched at runtime, so any skill change requires redeploy, rescan, and resubmit before it reaches users. This ordering constraint governs Tasks 14–15 and Task 17.
  - _Dependencies: 9.1._
  - _Requirements: 6.6._

- [ ] 11. Build the packaging manifest, connector mapping, and brand assets (parallel to authorization)
  - [ ] 11.1 Create the root manifest and connector mapping
    - Author the root `plugin.json` declaring portable package identity (package name, version, description); place `mcp.json` and `skills/` at the package root so skills are discovered in `skills/` and the connector in `mcp.json` without a host-specific declaration overriding either.
    - _Dependencies: 2.4._
    - _Requirements: 1.1, 1.2._
  - [ ] 11.2 Produce brand assets under `assets/`
    - Create the logo, composer icon, and a deliberate brand colour accent (PicX is a near-black neutral palette, so pick an intentional accent). Store all visual assets under `assets/` and express every host-specific path relative to the package root. Omit screenshots because the plugin has no user interface.
    - _Dependencies: None._
    - _Requirements: 1.4, 1.5._
  - [ ] 11.3 Author host-specific presentation metadata
    - Provide display name, short description, long description, developer name, category, capabilities, website URL, privacy policy URL, terms of service URL, starter prompts, brand colour, composer icon, and logo in the host-specific interface object.
    - _Dependencies: 11.1, 11.2._
    - _Requirements: 1.3._

- [ ] 12. Clear submission-readiness gates (parallel to the build)
  - [ ] 12.1 Complete host identity verification for the publishing name
    - Complete identity verification for the exact name the plugin publishes under — individual verification to publish under a person, business verification to publish under a company. Treat submission as blocked until verification for the publishing name is complete, since an unverified name is rejected at review.
    - _Dependencies: None._
    - _Requirements: 7.1, 7.2._
  - [ ] 12.2 Confirm submitting and reviewing account permissions
    - Confirm the submitting account holds the permission to create and submit plugin drafts and the reviewing account holds the permission to view drafts and review status.
    - _Dependencies: None._
    - _Requirements: 7.3._
  - [ ] 12.3 Provision a demo account reachable through the authorization flow
    - Provide a demo account a host reviewer can sign into through the OAuth authorization flow with no further configuration, requiring no API-key paste.
    - _Dependencies: 4.1._
    - _Requirements: 3.8._
  - [ ] 12.4 Assemble the submission-form material
    - Assemble company URL, privacy policy URL, terms of service URL, plugin name, logo, description, connector and tool information, localization information, and test prompts with their expected responses.
    - _Dependencies: 11.3, 12.1._
    - _Requirements: 7.4._

- [ ] 13. Confirm the Anthropic connector path and skill-format parity
  - [ ] 13.1 Confirm cross-cloud reachability and OAuth reuse for Anthropic
    - Confirm the connector stays reachable from Anthropic's published cloud IP ranges (Claude connects from Anthropic's infrastructure, not the user's device, including desktop and local surfaces); if the connector is ever placed behind an allowlist, VPN, or private network, allowlist Anthropic's published ranges. Confirm Anthropic authorization is satisfied by the same OAuth implementation from Tasks 3–6, with no PicX-specific credential paste.
    - _Dependencies: 4.1._
    - _Requirements: 8.1, 8.2, 8.3._
  - [ ] 13.2 Confirm Anthropic's submission path and skill-format acceptance
    - Confirm Anthropic's submission path for the directory listing and confirm whether the Requirement 6 `SKILL.md` format is accepted unmodified, before assuming a single bundle serves both directories. Record the finding; assume some duplication until proven otherwise.
    - _Dependencies: 9.1, 13.1._
    - _Requirements: 8.4._

- [ ] 14. Verify the plugin end to end in the host's developer mode
  - Verify the full plugin (connector + skills + manifest + presentation) end to end in the host's developer mode with the demo account before any submission for review: sign in through OAuth, exercise the exposed tools through the skills, and confirm authorization, scope enforcement, credit-ceiling behaviour, and response hygiene all hold live.
  - _Dependencies: 4.1, 5.2, 6.2, 7.3, 8, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 11.3, 12.3._
  - _Requirements: 7.5._

- [ ] 15. Scan, submit, and publish
  - [ ] 15.1 Scan tools to import the static skill snapshot, then submit
    - Run the host's tool scan so the skills are imported as the static snapshot, then submit the plugin only when it is intended to be publicly available in the declared countries; use developer mode for any private or workspace-only use.
    - _Dependencies: 10, 12.4, 14._
    - _Requirements: 7.6._
  - [ ] 15.2 Perform the explicit publish step after approval
    - After host approval, perform the host's explicit publish step, because approval alone does not list the plugin in the directory.
    - _Dependencies: 15.1._
    - _Requirements: 1.6._

- [ ] 16. Publish and list on Anthropic
  - Following the path confirmed in Task 13.2, submit and list the plugin in the Anthropic connector directory using the same connector and the OAuth implementation, applying any skill-format adjustment the confirmation surfaced.
  - _Dependencies: 13.2, 15.2._
  - _Requirements: 8.4._

- [ ] 17. Establish the post-publication maintenance and release process
  - [ ] 17.1 Define the continuous-review release process for tool and metadata changes
    - Treat tool and metadata changes as subject to continuous host review after publication; when a tool schema, description, annotation, or the server `instructions` value changes, deploy the change and let the host's required checks complete before relying on it. When a skill or the plugin manifest changes, submit a new version for review (redeploy, rescan, resubmit per Task 10).
    - _Dependencies: 15.2._
    - _Requirements: 9.1, 9.2, 9.3._
  - [ ] 17.2 Pin the connector URL and define independent availability monitoring
    - Keep the published connector URL stable; when it must change, treat it as a new submission rather than a deployment change. Monitor connector availability and function independently of host review status.
    - _Dependencies: 15.2._
    - _Requirements: 9.4, 9.5._

- [ ] 18. Final checkpoint — verify coverage and submission readiness
  - Confirm every one of the 9 requirements is traceable to at least one completed task, the authorization-topology decision (Task 1) and credit-ceiling/tool-surface decisions (Tasks 6.1, 7.1) are signed off by their named owners, and no task submitted before developer-mode verification (Task 14) completed.
  - Confirm the discovery surface returns 200 on both `.well-known` documents, token verification rejects before any side effect, scope and credit-ceiling enforcement hold, response hygiene passed its audit, all six skills exist with their trigger conditions, the manifest/connector-mapping/assets are in place, identity verification is complete, and the Anthropic path is confirmed rather than assumed.
  - _Dependencies: 15.2, 16, 17.1, 17.2._
