# TigerBrowser

**English** · [简体中文](README.zh-CN.md)

TigerBrowser is a team workspace for persistent browser profiles. A team server manages profiles, members, permissions and browser versions. Members access the workspace in a web browser or through the TigerBrowser desktop client.

TigerBrowser uses published [Camoufox](https://github.com/daijro/camoufox) browser builds and preserves the Camoufox Python API. The workspace console currently uses Simplified Chinese.

## What you can do

- Keep cookies, site storage and browser settings in separate persistent profiles.
- Assign an HTTP, HTTPS or SOCKS5 proxy to a profile. Proxy passwords are stored in the host's native credential store. A failed proxy check stops the launch.
- Run profiles headlessly on the server, use Linux remote control through KasmVNC, or check out an authorized profile to run it on a member's computer.
- Manage member access, select exact browser versions and use the automation API with scoped member keys.

## Quick start

Run these commands from a clean Python virtual environment with Node.js and npm available. Start the server on the same machine for your first session.

```bash
git clone https://github.com/zhaotianshi/TigerBrowser.git
cd TigerBrowser
python -m pip install -r pythonlib/team-requirements.lock
python -m pip install --no-deps -e ./pythonlib
npm --prefix frontend/studio ci
npm --prefix frontend/studio run build
python -m camoufox fetch
python -m camoufox studio-team-init --data-dir ./my-team-workspace --name admin --display Administrator
python -m camoufox studio-team --data-dir ./my-team-workspace
```

Create the administrator password at the prompt. Open [the local console](http://127.0.0.1:8766/) and sign in as `admin`. The default server listens on localhost and starts in management mode. Enable browser execution and configure network access using the [team server guide](docs/studio-team-service.md).

In **浏览器环境** (Browser Profiles), choose **创建环境** (Create Profile). To read its metadata, select one profile and choose **详情** (Details), or click the profile's name. Configure proxies in **代理资源** (Proxies).

## Desktop client

Official downloads for Windows x64, macOS and Ubuntu x64 are listed on [GitHub Releases](https://github.com/zhaotianshi/TigerBrowser/releases). See the [client installation and build guide](docs/studio.md#client-packages).

At first launch, enter the server address exactly as configured by its `--origin` option. The desktop displays the server's console. Apply console updates by updating and restarting the team server.

## Documentation

| Topic | Guide |
| --- | --- |
| Profiles, proxies and desktop setup | [TigerBrowser user guide](docs/studio.md) |
| Team server deployment and network access | [Team server guide](docs/studio-team-service.md) |
| Browser automation and integrations | [Automation API](docs/studio-automation.md) |
| Source builds and contributions | [Contributing](CONTRIBUTING.md) |
| Release process | [Client releases](docs/studio-releases.md) |

## Upstream and licensing

TigerBrowser builds on [Camoufox](https://github.com/daijro/camoufox), Firefox and Playwright. The repository uses [MPL-2.0](LICENSE). The Python package retains its [MIT license](pythonlib/LICENSE).
