# Security Policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for security issues. Do not
open a public issue containing webhook IDs, request bodies, location data,
network details, or deployment credentials.

Include the affected release, deployment model, expected behavior, and a minimal
reproduction with every secret replaced. Maintainers will acknowledge valid
reports as soon as practical and coordinate disclosure after a fix is available.

## Security boundary

The gateway treats each Home Assistant mobile-app webhook ID as a bearer
capability. Protect the capability map like a password, mount it read-only, and
terminate public traffic with HTTPS. A reverse proxy or tunnel provider that
terminates TLS can observe request paths and therefore the webhook capability.

Only plaintext Companion App registrations are supported. Encrypted webhook
envelopes are rejected because the gateway cannot inspect their command before
forwarding it. The gateway is not a replacement for Home Assistant
authentication and must never proxy the Home Assistant REST, WebSocket, login,
or frontend routes.
