# Changelog

## 0.2.0

- Added a pluggable local-agent adapter. `agent.provider: generic` can drive any CLI agent through a configurable command template.
- Prompt is written to `prompt.txt` in the shared directory, avoiding Windows command-line length limits for all agents.
- Supports plain-text agent output in addition to CodeBuddy's stream-json events.

## 0.1.1

- Installer can optionally install CodeBuddy CLI with the official installer (`--install-cli`).
- Bridge discovers CLI installed by the native installer in addition to PATH and `.workbuddy`.
- Added `install_cli.bat` for Windows users.

## 0.1.0

- First packaged release.
- Step-by-step generation and execution of bpy patches.
- Scrollable chat panel with connection indicator.
- Streaming reasoning output from CodeBuddy CLI.
- Independent heartbeat thread and single-instance lock.
- Cross-platform path discovery; no user-specific absolute paths.
- Installer and uninstaller scripts, Blender extension manifest, MIT license.
