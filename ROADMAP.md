# Studio upgrade roadmap

The accepted architecture and numbered acceptance tasks are in
[the client/server design](docs/studio-client-server.md). Features below remain
planned until their implementation and required platform evidence are recorded.

## Current scope

TigerBrowser ships the team server, Vue console and desktop client. The client/server design describes the service boundaries and what still requires real platform acceptance.

## Product scope

The maintainer confirmed on 2026-10-17 that Studio retains one team server and
its graphical desktop client. There is no separate serverless product, project
directory, installer or update channel. Local execution uses checkout and return;
server workspaces and client caches must never share a writable data directory.
The standalone Python launcher APIs remain supported. See
[design section 7.6](docs/studio-client-server.md#76-仅提供连接服务器的客户端).

## Integration and release work

The open items, the local results they start from and their completion criteria
are listed in [design section 12.1](docs/studio-client-server.md#121-交接待完成事项).

1. Finish the current fixes and record the branch, exact commit, validation
   results and remaining failures. Keep the release scope fixed.
2. Require the complete GitHub Actions gate and platform packages on the latest
   commit of the issue-linked PR. Reuse versioned, verified dependency
   caches; perform focused local regressions when fixing failures.
3. Review the integrated diff, especially checkout/run exclusion, interrupted
   returns, old-token rejection, secret handling and desktop bridge authorization.
4. Exercise an isolated Ubuntu 24.04 server and Windows client first: remote
   execution, local checkout, browser shutdown and return, login-state persistence,
   competing starts, disconnected returns and forced release. Complete the
   remaining macOS and Ubuntu client acceptance separately.
5. Verify installation and update artifacts, perform a small prerelease trial,
   and keep code integration distinct from release acceptance. Merge only with
   maintainer authorization after the required checks pass; release acceptance
   still requires the recorded physical checks. Do not label mocked checks or a
   successful build as physical acceptance.
