"""Inbound, consented lead capture (Phase 2, source #1 in the plan).

Three entry points feed one pipeline (`forms.process_submission`):

- the signed webhook that pb4host.com's own forms post to (`connectors.inbound_webhook`);
- the hosted public forms served by Prospector itself (`api.routers.inbound`);
- Sign In with LinkedIn (OpenID Connect) on those hosted forms (`connectors.linkedin_oidc`).

Every submission creates or updates an organisation, exactly one person with lawful basis
`consent`, and a consent record carrying the notice text version and timestamp. Nothing here
ever crawls a profile or imports a list — the prospect typed (or authorised) every field.
"""
