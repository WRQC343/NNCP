# Changelog

Each version lists what changed and whether it needs a new screen file.
`update.sh` shows the entry for the version being installed.

## 3.0 - 2026-10-25
- Supports the 4.3" Nextion screen (480 x 272, NX4827P043-011C-Y).
- The installer asks for the screen size. Existing installs keep working as 5"/7".
- Screen file: 4.3" screens need the 4.3" GMRS-Link screen file. No change for 5" or 7".

## 2.9 - 2026-10-25
- First public release on GitHub.
- One-line installer and updater (`get.sh`).
- Works with 5" and 7" 800x480 Nextion screens. Intelligent (P) screens connect through a
  USB-to-TTL adapter (the installer's default) or GPIO with a 1k/2k resistor divider.
- Screen file: GMRS-Link screen with boot screen. If your screen came from
  gmrs-link.com with the GMRS-Link boot screen, no change is needed.
