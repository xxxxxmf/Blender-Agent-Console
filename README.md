# Agent Console Bridge

A Blender add-on plus a tiny local bridge that lets you control Blender with natural language through the CodeBuddy CLI. The model streams its reasoning, the bridge splits it into individual `.py` steps, and Blender applies each step immediately with its own undo entry.

> This is a developer tool. It generates and executes arbitrary `bpy` code. Use it on disposable or backed-up `.blend` files.

## Features

- Step-by-step execution: the model thinks and acts one step at a time; you see each action as it happens.
- Scrollable chat panel in the Blender sidebar with connection indicator.
- Scene context sent with every request: selected objects, full object list, materials, collections, viewport screenshot.
- Independent heartbeat thread and single-instance lock so the "bridge online" indicator stays accurate.
- **Pause / resume button**: stop generation at any time; the bridge keeps draining the model stream without applying steps, then resumes exactly where it left off.
- All shared files are written atomically with retry, so Windows file contention never interrupts a request.
- Only local CLI connection is exposed in the panel; any CLI agent can be wired through the generic adapter.
- No third-party Python packages; the bridge uses only the standard library.

## Requirements

- Blender 4.2 or newer (tested on 5.3).
- Python 3.9+ for the installer / bridge launcher (Blender's bundled Python also works).
- [CodeBuddy CLI](https://www.codebuddy.ai/) installed and logged in.
- Windows, Linux or macOS. Paths are detected automatically.

## Install

### Option A — installer script (recommended)

```bash
python install.py                 # install into all detected Blender versions
python install.py --version 4.2   # install into one version
python install.py --install-cli   # also install CodeBuddy CLI if it is missing
```

The installer copies the addon into your Blender user scripts directory, writes `bridge/config.json` from the example, and records the bridge script path. With `--install-cli` it runs the official CodeBuddy CLI installer for your platform. On Windows you can also double-click `install_cli.bat`.

> CLI installation can be automated, but the first login cannot. After installation, run `codebuddy` once in a terminal and complete the site/OAuth login shown by the CLI.

Then in Blender: **Edit > Preferences > Add-ons > Enable "Agent Console"**.

### Option B — Blender Extension zip

Zip this whole package directory (including `blender_manifest.toml`, `addon/` and `bridge/`) and install it through **Edit > Preferences > Add-ons > Install from Disk**. Keep the `bridge/` folder in a stable location and set **Bridge Service Script** in the addon preferences to `<extracted>/bridge/agent_bridge.py`.

## Run

1. Start the bridge:

   ```bash
   python start_bridge.py          # or double-click start_bridge.bat on Windows
   ```

   Or click **Start / Restart** in the addon panel — it launches the bridge with Blender's own Python in a new console window.

2. In Blender, open the **Agent Console** tab in the 3D-view sidebar.
3. Select objects if relevant, type a message, and click **Send**.
4. Watch the chat list: thinking text appears live, then each step is applied one by one.
5. Something wrong? `Ctrl+Z` in Blender undoes the last step.

Useful bridge commands:

```bash
python start_bridge.py --doctor     # check CLI, config, addon and a tiny model call
python start_bridge.py --selftest   # simulate one request without Blender
python start_bridge.py --once       # process one pending request and exit
```

## Configuration

`bridge/config.json` is created from `config.example.json` on first install. Leave a field empty to auto-detect it.

| Field | Meaning |
| --- | --- |
| `shared_dir` | Directory for `request.json`, `steps.json`, `progress.json`, `heartbeat.json`. Defaults to the addon bridge directory of your Blender version. |
| `blender_version` | Blender config folder such as `4.2`. Auto-detected if empty. |
| `cli.command` | CodeBuddy CLI command. Empty means auto-discover on PATH and in `.workbuddy`. |
| `cli.model` | Optional model override. |
| `cli.timeout` | Seconds allowed for one model call. |
| `cli.system_prompt_file` | Path to the prompt rules; relative paths resolve next to `agent_bridge.py`. |
| `agent.provider` | `codebuddy` (default) or `generic`. |
| `agent.command_template` | Command used when provider is `generic`. Supports `{prompt_file}`, `{system_prompt_file}` and `{shared_dir}`. |
| `agent.output_format` | `stream-json` (CodeBuddy-style events) or `text` (plain stdout). |
| `agent.stdin_mode` | `none` or `prompt` (send the full prompt on stdin). |

### Using a different local agent

The bridge is not tied to CodeBuddy. Set `agent.provider` to `generic` and give it a command template. The full prompt is written to `prompt.txt` in the shared directory so it never exceeds the Windows command-line limit:

```json
{
  "agent": {
    "provider": "generic",
    "command_template": "my-agent --prompt-file \"{prompt_file}\" --system \"{system_prompt_file}\"",
    "output_format": "text",
    "stdin_mode": "none"
  }
}
```

If your agent reads prompts from stdin instead, use `\"stdin_mode\": \"prompt\"` and put only flags in the template. Any agent can be used as long as it follows the output protocol: a `# 思考:` line followed by a ```python code block for each step. `output_format: "text"` works for agents that print plain text; `stream-json` understands the CodeBuddy event stream format for live reasoning display.

## How it works

```text
Blender addon  --writes-->  shared/request.json
      ^                         |
      |                    bridge service
      |                         |  streams CLI output
      |                         v
      |                    shared/steps.json  (one patch per completed code block)
      |                         |
      +-------- polls ----------+

Chat/progress updates are written to shared/progress.json.
A daemon thread writes shared/heartbeat.json every second.
```

The addon applies new entries in `steps.json` as soon as they appear, so you do not wait for the whole answer before Blender starts changing.

## Safety and privacy

- Generated code is **not sandboxed**. The bridge blocks a small list of dangerous calls (`os.system`, `subprocess`, `open`, `eval`, `exec`, etc.), but this is a guardrail, not a security boundary.
- Scene data and a viewport screenshot are sent to CodeBuddy CLI and therefore to the configured model provider. Do not use on confidential scenes.
- Every step pushes an undo entry. Review results and keep backups.
- The addon preference **Auto Apply** can be disabled to inspect patches before applying.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Panel says "bridge offline" | Only one `agent_bridge.py` process should run; check `heartbeat.json` timestamp. |
| `path too long` error | Prompts must be passed via stdin, never as command-line arguments. This is already how the bridge works. |
| "not logged in" | Run `codebuddy` in a terminal and finish the login flow. |
| No steps appear | Run `--selftest` and inspect `bridge.log` and `progress.json`. |
| Addon not visible | Enable it in Blender preferences; check the installed path printed by `install.py`. |

## Uninstall

```bash
python install.py --uninstall
```

Remove the extracted bridge folder and `bridge/config.json` if you no longer need them.

## License

MIT — see `LICENSE`.
