# AI container Codex seccomp profile

`seccomp-codex.json` starts from the Moby default allowlist at
https://github.com/moby/profiles/blob/main/seccomp/default.json (downloaded
2026-09-18; upstream SHA-256
`785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334`).
The only added rule allows `clone`, `unshare`, `mount`, `umount2`, and
`pivot_root`. It retains the other Docker default denies and does not add
`CAP_SYS_ADMIN`. The derivative profile SHA-256 is
`bf119a43bfbd82da3027a1d5487224da1c71ef207b3cfdfc7ef44ef5bbad24c6`.

An isolated, network-disabled `ai3:latest` container running as UID 1000
successfully created an authorized file through Codex 0.151.0's sandbox with
this profile. A narrower variant without `pivot_root` failed at that syscall.
An authorized `.env` read and a read outside the workspace were denied by the
Codex permission profile. These are platform prechecks, not a production
business-flow verification.

Changing the AI container security profile changes the Docker boundary for
everything inside that container. Keep the existing single-container runtime,
verify the exact rendered Compose configuration and backup before recreation,
and rerun the native Codex and Feishu business E2E after restart.
