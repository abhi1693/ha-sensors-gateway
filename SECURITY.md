# Security Policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for security issues. Do not
open a public issue containing webhook IDs, request bodies, location data,
network details, or deployment credentials.

Include the affected release, deployment model, expected behavior, and a minimal
reproduction with every secret replaced. Maintainers will acknowledge valid
reports as soon as practical and coordinate disclosure after a fix is available.

## Security boundary

If a VPN meets the deployment's functional requirements, it is the preferred
boundary: Home Assistant and its webhook routes remain on the private network.
The gateway is intended only for deployments where the native Companion App
webhook must remain reachable over public HTTPS without an active VPN.

Deploying the gateway means trusting its HTTP parsing, capability lookup, command
filtering, and configuration in addition to the public HTTPS ingress. The project
uses tests, resource bounds, pinned artifacts, and a deliberately small
implementation to reduce this risk, but none of those measures prove the absence
of filtering defects. A defect could broaden the intended public surface.

The gateway treats each Home Assistant mobile-app webhook ID as a bearer
capability. Protect the capability map like a password, mount it read-only, and
terminate public traffic with HTTPS. A reverse proxy or tunnel provider that
terminates TLS can observe request paths and therefore the webhook capability.

Only plaintext Companion App registrations are supported. Encrypted webhook
envelopes are rejected because the gateway cannot inspect their command before
forwarding it. The gateway is not a replacement for Home Assistant
authentication and must never proxy the Home Assistant REST, WebSocket, login,
or frontend routes.

The `update_registration` webhook command is intentionally blocked. Home
Assistant permits that command to replace the device's push URL and token, so
forwarding it would allow a leaked webhook capability to redirect future
notification contents. Perform registration changes through a trusted direct
connection to Home Assistant.
