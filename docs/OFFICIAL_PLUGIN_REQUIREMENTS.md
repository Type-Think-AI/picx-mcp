# PicX official plugin — requirements

Status: draft for review. Owner: unassigned. Last verified against live systems and
vendor docs on 2026-09-18.

Goal: publish PicX in the OpenAI universal plugin directory (ChatGPT + Codex) and in
Anthropic's connector/skill directory, so PicX gains distribution from both hosts
instead of only being reachable by users who already know to configure an MCP URL.

Everything in the "Verified today" column was checked directly — live HTTP requests
against `mcp.picxstudio.com`, reading `picx-mcp` source, and reading current vendor
documentation. Nothing here is assumed from memory.

---

## 1. Correcting two premises before we scope

**"The new MCP protocol is stateless."** Not quite, and the difference matters.
Statelessness is not something the protocol or OpenAI mandates — the MCP spec
(`2025-11-25`) makes sessions *optional*: a server **MAY** assign an
`Mcp-Session-Id` at initialization, and if it does, clients must echo it on every
later request. A server that never issues one is stateless, and that is a valid,
fully-conformant choice. "Stateless" does not appear anywhere in OpenAI's plugin
documentation.

This is good news: **PicX already made that choice.** `src/picx_mcp/server.py`
mounts with `stateless_http=True` and documents why — the deployment's load
balancer does not forward `Set-Cookie`, so sticky sessions cannot work and
stateless is the only correct mode. So the transport requirement is already met
and needs no work.

**"One connector and one skill combination."** Correct, and it is now the shape
both vendors use. OpenAI's model is explicit: a plugin is *skills + an MCP
server*, where the server provides live data, auth and controlled actions, and
the skills provide the workflow instructions that teach the model how to
sequence those tools. The Claude directory listing in the reference screenshot
shows the same composition — 1 Connector, 8 Skills.

The important consequence: **the MCP server alone is not a plugin.** We have 18
working tools and zero skills. The skills are the missing half, and they are
what makes the difference between "the model can call PicX" and "the model knows
how to produce a good result with PicX."

---

## 2. Where PicX stands today

| Requirement | Verified today | Gap |
|---|---|---|
| Remote MCP server on a public HTTPS domain | `mcp.picxstudio.com` is live (`GET /` → 405, i.e. a POST-only MCP endpoint answering) | none |
| Streamable HTTP transport | `stateless_http=True` in `server.py` | none |
| Stateless / horizontally scalable | stateless by design; `request_state_key` is documented as needing to be identical on every replica | verify the key is actually pinned in the deployed env |
| Tool surface | 18 tools: image/video generation, edit, assets, generations, events, deliveries, webhooks, templates, models, account, tier, usage | see §4 on trimming for review |
| Template catalogue exposed | `picx_search_templates`, `picx_get_template` over ~50k rows via `/v1/templates` | none (shipped today) |
| Per-user authentication | `Authorization: Bearer pxsk_…` passthrough — each caller supplies their own PicX key. `build_auth()` returns `None` in that mode | **blocker**, see §3 |
| OAuth 2.1 per MCP authorization spec | `/.well-known/oauth-protected-resource` → **404**; `/.well-known/oauth-authorization-server` → **404** | **blocker** |
| Skills bundle | no `skills/` directory in the repo | **blocker** |
| Plugin manifest (`plugin.json`, `mcp.json`) | none | **blocker** |
| Org identity verification | not confirmed | must be done before submission |

The headline: the hard engineering — a deployed, stateless, conformant MCP server
with a real tool surface — is **done**. What remains is an authorization layer, a
skills bundle, packaging metadata, and the submission process.

---

## 3. Authentication — the one genuine blocker

Today PicX asks the caller to paste a `pxsk_…` key. That works for developer-mode
MCP but **cannot ship as an official plugin that touches user-specific data**:
OpenAI expects an OAuth 2.1 flow conforming to the MCP authorization spec, and
the review team must be able to log into a demo account *with no further
configuration*. "Paste your API key" is further configuration.

Required, per the MCP authorization spec as OpenAI documents it:

1. **Protected resource metadata** at
   `GET https://mcp.picxstudio.com/.well-known/oauth-protected-resource`,
   returning `resource`, `authorization_servers`, and ideally `scopes_supported`
   plus `resource_documentation`. Currently 404.
2. **A `WWW-Authenticate` challenge on 401**, carrying
   `resource_metadata="…/.well-known/oauth-protected-resource"` and the required
   scope, so the host can discover the metadata URL cold.
3. **Authorization server metadata** at either
   `/.well-known/oauth-authorization-server` or `/.well-known/openid-configuration`
   on whichever issuer we designate, publishing `issuer`,
   `authorization_endpoint`, `token_endpoint`,
   `token_endpoint_auth_methods_supported`, and
   `client_id_metadata_document_supported`.
4. **Echo the `resource` parameter** throughout the flow. ChatGPT sends the exact
   `resource` value from our metadata.
5. **Client identification**: support at least one of Client ID Metadata
   Documents (CIMD), Dynamic Client Registration (DCR), or a predefined OAuth
   client. PKCE is expected.
6. **Token verification on every request** — the MCP server is the resource
   server and must validate the access token per request. This is naturally
   compatible with our stateless mode.

Open design decision, and the one thing I would flag for a human call: we can
either stand up PicX's own authorization server, or designate the existing
identity provider behind `ai.picxstudio.com`. `settings.py` already carries
`google_client_id`, `google_client_secret`, `jwt_signing_key` and
`storage_encryption_key`, and `auth.py` already has an
`exchange_oauth_token_for_session_key`-shaped path (OAuth access token → PicX
session key). So a path exists; what does not exist is the spec-mandated
discovery surface. Scoping that properly is backend work — Alex's lane, not mine.

Scopes should mirror the existing API key scopes rather than inventing a second
vocabulary (`images:generate`, `videos:generate`, `templates:read`,
`assets:write`, …), so that a token issued to ChatGPT cannot exceed what the
equivalent PicX key would allow.

**Credit-spend consent is a product decision, not just a technical one.** These
tools spend real credits. `session_credit_ceiling` already exists in settings —
it should be enforced per OAuth grant, and the generation tools should carry
accurate `destructiveHint` / `openWorldHint` annotations so the host can ask for
confirmation. A plugin that silently burns a user's balance is both a review risk
and a support problem.

---

## 4. Tool surface for review

OpenAI's review guidance is explicit that tool responses must not leak
unnecessary data: no PII beyond what the request needs, and specifically no
internal identifiers — session, trace or request IDs, timestamps, internal
account IDs, logs — and no auth secrets. Two actions:

- **Audit every tool response** against that bar before submission. Our tools
  return API payloads fairly directly, so `X-Correlation-Id`-style fields and
  internal IDs are the likely offenders.
- **Reconsider the surface we submit.** 18 tools is a lot for a first
  submission, and OpenAI's guidance is that every tool should map to a
  recognizable user goal. Webhook delivery inspection and redelivery are
  developer-operations tools, not consumer goals; they are strong candidates to
  keep in the developer-mode server and leave out of the published plugin.

Each submitted tool also needs an action-oriented name, a description saying
*when* to use it, an explicit input schema, an output schema where it returns
structured data, and accurate safety annotations. We largely have this; it needs
an audit pass rather than new work.

Server `instructions` should be populated: shared guidance across tools, most
important detail in the first 512 characters. Good candidates — always search
templates before generating from a vague prompt, check `picx_get_tier` before
promising a resolution, treat a null template `prompt` as gated.

---

## 5. The skills bundle

A skill is a directory with a required `SKILL.md` carrying frontmatter (`name`,
`description`) plus instructions, and optional `references/`, `scripts/`, and
`assets/`. The `description` decides *when* the model considers the skill, so it
must state the workflow and its trigger conditions; procedure and format detail
belong in the body.

Proposed initial set, each mapped to a real user goal and to tools we actually
have:

| Skill | Goal | Tools it sequences |
|---|---|---|
| `picx-overview` | Explain what PicX is, credits, models, tiers — the orientation skill | `picx_list_models`, `picx_get_tier` |
| `template-first-generation` | Find a template in the ~50k catalogue, then generate from it instead of from a cold prompt | `picx_search_templates`, `picx_get_template`, `picx_generate_image` |
| `video-modes` | Pick the right one of the seven video modes and supply the fields it requires | `picx_generate_video`, `picx_get_generation` |
| `image-editing` | Iterative edit chains on an existing image | `picx_upload_asset`, `picx_edit_image` |
| `async-and-delivery` | Long-running generations: poll, stream events, inspect delivery | `picx_get_generation_events`, `picx_get_generation_deliveries` |
| `credits-and-limits` | Answer "what will this cost" and "why was I throttled" before spending | `picx_get_usage`, `picx_get_tier`, `picx_get_account` |

`video-modes` is the highest-value one and should be written first. The seven
modes with their per-mode required fields are exactly the kind of knowledge a
model gets wrong unaided, and the rules are already written down authoritatively
in `picx-studio` at `api/app/public_api/schemas.py`. The skill must state that
`lipsync` takes no prompt while every other mode requires one.

`template-first-generation` must carry the three template behaviours that
otherwise read as bugs: `total` is an estimate rather than a count so page until
a short page returns, the topic *filter* works while the topic *field* is always
null, and a null `prompt` means a premium/gated row rather than missing data.

**Two delivery mechanics to design around.** Skills can be uploaded at
submission or imported from the MCP server, and the MCP route keeps their
instructions and files versioned with the server deployment — that is the route
to prefer. But the import is a **static snapshot** taken when "Scan Tools" runs
in the submission portal: ChatGPT and Codex do **not** fetch skills from our
server at runtime. Changing a skill therefore means deploy, rescan, resubmit. Any
"we'll iterate on the skill copy after launch" plan has to account for that.
Skills that depend on the server declare it in `agents/openai.yaml` with
`transport: "streamable_http"` and our MCP URL.

---

## 6. Packaging

The portable format is a root `plugin.json` against the Agent Plugins schema,
with `mcp.json` and `skills/` beside it at the plugin root. OpenAI-specific
presentation goes in `extensions.com.openai`, which carries `apps` (pointing at
`.app.json` for registered MCP server mappings), optional `hooks`, and an
`interface` object.

`interface` is what the install surface renders, so it is marketing-critical, not
boilerplate: `displayName`, `shortDescription`, `longDescription`,
`developerName`, `category`, `capabilities`, `websiteURL`,
`privacyPolicyURL`, `termsOfServiceURL`, `defaultPrompt` (the starter prompts
users click), `brandColor`, `composerIcon`, `logo`, `screenshots`.

Two path rules worth noting up front: skills are always discovered in `skills/`
and MCP servers in `mcp.json` — a `skills` or `mcpServers` declaration inside the
extension cannot override that for a portable package — and assets should live
under `./assets/`.

Assets we do not have yet and will need: logo, composer icon, brand colour
(PicX is a near-black neutral palette, so pick a deliberate accent rather than
defaulting), and screenshots only if we ship UI. We should not submit
screenshots for a no-UI plugin — the guidance says don't.

---

## 7. Submission gates

These are process requirements that block submission regardless of code
readiness, so they should start in parallel:

1. **Organization identity verification** in the OpenAI platform dashboard, for
   the exact name we publish under — individual verification to publish under a
   person, business verification to publish under Type-Think-AI. This is enforced
   at review and publishing under an unverified name is an outright rejection.
2. **`api.apps.write`** permission for whoever submits (`api.apps.read` to view
   drafts). Org owners have both by default.
3. **A demo account** that the review team can log into with no further
   configuration. For an OAuth plugin this means a real working account, not a
   key to paste.
4. **A universal MCP server URL.** `https://mcp.picxstudio.com/…` qualifies.
   Template URLs are only available to trusted developers with an established
   relationship, and we do not need them — we are not multi-tenant per workspace.
5. **Privacy policy and terms URLs**, company URL, test prompts with expected
   responses, and localization information — all required fields in the form.
6. **Publish explicitly after approval.** Approval alone does not list the
   plugin; there is a separate publish step before it appears in the directory.

Ongoing: tool and metadata changes are continuously reviewed after publication,
and new versions go through review again. Worth knowing before we design a
release cadence.

---

## 8. Claude / Anthropic

The same MCP server serves both hosts, which is the main reason this is one
project rather than two.

Anthropic-specific facts that change deployment assumptions: Claude connects to
our server **from Anthropic's cloud infrastructure, not the user's device** —
true even for Claude Desktop and Cowork, because remote connectors are brokered
through the user's Claude account. So the server must be reachable from
Anthropic's published IP ranges. We are on a public domain, so this is
satisfied, but if anything ever puts `mcp.picxstudio.com` behind an allowlist,
Claude breaks in a way that is invisible from a developer laptop.

OAuth is the normal auth path for Claude custom connectors too, so §3 serves both.

Unverified and needing a human to check: the exact submission path for the
Anthropic directory listing shown in the reference screenshot, and whether
Anthropic's skill format is byte-compatible with OpenAI's `SKILL.md`. OpenAI
publishes a "Submit a Claude Code plugin" guide, which suggests meaningful
overlap, but I have not confirmed one bundle can serve both directories
unmodified. Assume some duplication until proven otherwise.

---

## 9. Suggested sequencing

Auth is the critical path and the only item with real design risk, so it starts
first and alone.

1. **OAuth 2.1 / MCP authorization spec** — discovery endpoints, token
   verification, scope mapping, per-grant credit ceiling. Backend lane.
2. **In parallel:** write the skills (`video-modes` first), and complete org
   identity verification, and produce logo/icon/brand assets.
3. **Tool audit** — response hygiene for PII and internal identifiers, decide
   which of the 18 tools ship, populate server `instructions`, verify
   annotations.
4. **Package** — `plugin.json`, `mcp.json`, `skills/`, `.app.json`,
   `agents/openai.yaml`, `interface` metadata.
5. **Test in developer mode** end to end with a real account, then Scan Tools,
   then submit, then publish.

## 10. Sources

Verified 2026-09-18:

- OpenAI plugin docs: `developers.openai.com/plugins` — `build/mcp-server`,
  `build/skills`, `build/auth`, `build/plugins`, `deploy/app-review`,
  `deploy/submission`, `app-guidelines`, `guides/security-privacy`
- MCP specification `2025-11-25`, Streamable HTTP transport and session
  management: `modelcontextprotocol.io/specification/2025-11-25/basic/transports`
- Anthropic custom connectors (remote MCP), including the
  connect-from-Anthropic's-cloud requirement: `support.anthropic.com` article
  `11175166`
- PicX live: `mcp.picxstudio.com` (`GET /` → 405; both `.well-known` OAuth
  documents → 404)
- PicX source: `picx-mcp` `src/picx_mcp/{server,settings,context,auth,client}.py`
  and `src/picx_mcp/tools/*.py`
