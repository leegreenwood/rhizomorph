# Raycast script commands

Scripts in this folder show up as commands in [Raycast](https://www.raycast.com) via its
[Script Commands](https://github.com/raycast/script-commands) feature.

## Linking the folder

Raycast → Settings (⌘,) → Extensions → **+** → **Add Script Directory** → choose this
`raycast/` folder. Raycast watches it, so new or edited scripts appear without re-linking.

## Adding a script

1. Create an executable file here (`chmod +x`) — any language with a shebang works.
2. Add the metadata comments at the top; `schemaVersion`, `title` and `mode` are required:

   ```bash
   # @raycast.schemaVersion 1
   # @raycast.title My Command
   # @raycast.mode compact        # silent | compact | fullOutput | inline
   # @raycast.packageName Rhizomorph
   # @raycast.icon 🍄
   ```

Keep the repo's no-autonomous-actions rule in mind: scripts here run straight from the
launcher with no approval prompt, so keep them read-only unless a mutation is something
you are deliberately triggering by hand.
