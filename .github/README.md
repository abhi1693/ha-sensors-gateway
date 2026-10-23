# Repository automation

Common CI and image mechanics are maintained in [abhi1693/actions](https://github.com/abhi1693/actions).
Workflows use the released `@v1` reference for centrally maintained compatible updates.
See the [shared release history](https://github.com/abhi1693/actions/releases) for changes.
Repository-owned scripts, triggers, scanner exceptions and application smoke checks
remain alongside the application. Image manifests declare components rather than
copying workflow steps.

`ci.yml` calls the shared image manifest with publication disabled on pull
requests and requires both native AMD64 and ARM64 builds. Shared SBOM and scan
defaults include unfixed high and critical vulnerabilities. The manifest's
runtime smoke verifies source/runtime version parity, Python, non-root execution,
and pip absence, then checks the real HTTP process against an isolated local
read fixture. The release workflow uses the same manifest and smoke gate before
promoting candidate images.
