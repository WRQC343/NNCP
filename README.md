# GMRS-Link Nextion Node Control Panel

A 5" Nextion touchscreen control panel for HamVOIP / AllStar nodes on a Raspberry Pi.
Connect and disconnect nodes, send DTMF, watch who's linked, and check your node at a
glance, without a computer.

**By [gmrs-link.com](https://gmrs-link.com)**

![The panel's screens](docs/preview.png)

## Features

- **Main screen:** six quick-connect buttons, a scrolling list of linked nodes with a count, big RX/TX lights, weather and clock
- **Node keypad:** connect, monitor or disconnect any node number
- **DTMF pad:** 0-9, *, #, A-D, sent exactly as if keyed from a radio
- **Extras:** ReConn Enable/Disable and GPS Tracking Enable/Disable/Update, with green/red status badges
- **System:** CPU temperature, uptime, IP address, load and Asterisk status, plus Restart Asterisk, Reboot and Shutdown
- **Weather:** the same weather as Supermon, plus a 3-day forecast
- Screen saver, wake on touch, and a GMRS-Link boot screen while the node starts

## What you need

- A Raspberry Pi running **HamVOIP**
- A **5" Nextion display (800x480)** already loaded with the GMRS-Link screen file
  (NX8048T050, NX8048K050 or NX8048P050). The screen file is provided by gmrs-link.com.
  You don't need Nextion Editor.
- 4 jumper wires
- An internet connection on the node

## 1. Wire the screen to the Pi

Shut down the node and unplug it first.

| Nextion wire | Pi pin | What it is |
|---|---|---|
| Red (+5V) | Pin 4 | 5V |
| Black (GND) | Pin 6 | Ground |
| Yellow (TX) | Pin 10 | Pi RXD |
| Blue (RX) | Pin 8 | Pi TXD |

Pin 1 is the corner pin nearest the SD card. Pins 2, 4, 6, 8 and 10 are the first five
on the row along the edge of the board.

> **Some Nextion cables have the yellow and blue colors swapped.** If the screen stays on
> "Waiting for the node to start..." after installing, power off and swap the wires at pins 8 and 10.

Power the node back on. The screen shows the GMRS-Link boot screen right away.

## 2. Install

Log in to the node with SSH (PuTTY, **port 222**, user **root**). If the HamVOIP menu
appears, choose the option to exit to the shell. Then paste this one line:

```
curl -sSL https://raw.githubusercontent.com/__GITHUB_REPO__/main/get.sh | bash
```

The installer asks a few questions. The value in [brackets] is used if you press Enter.

| Question | Default |
|---|---|
| Your node number | |
| Header text, e.g. your call sign | NODE &lt;your node&gt; |
| How is the screen connected? (2 = GPIO pins, 1 = USB adapter) | 2 |
| Minutes before the screen saver (0 = never) | 10 |
| Turn the screen on when someone keys up? | y (answer n on a busy hub) |
| Brightness, 10-100 | 100 |
| Use a 24-hour clock? | n |
| Button 1-6 node number and label | Enter leaves a button empty |
| ZIP code for weather (- = no weather) | |
| Units, F or C | F |
| ReConn / GPS Tracking DTMF codes | *917, *918, *A50, *A51, *A52 |
| Show the ReConn badge? / Show the GPS badge? | y |

The first time, it switches on the Pi's serial port and asks you to **reboot**. After the
reboot, the screen fills in by itself within a minute.

## Update

Run the same line again and choose **1) Update**. Your settings are kept.

```
curl -sSL https://raw.githubusercontent.com/__GITHUB_REPO__/main/get.sh | bash
```

## Change your settings

Run the same line and choose **2**, or:

```
cd /root/nextion-hamvoip
bash install.sh
```

Choose **2) Keep my settings** to change only the weather, Extras codes and badges, or
**1) Start fresh** to answer everything again. Or edit `/etc/nextion-node.json` and run
`systemctl restart nextion-node`.

## Uninstall

```
cd /root/nextion-hamvoip
bash install.sh uninstall
```

## Troubleshooting

See what the panel is doing:

```
journalctl -u nextion-node -f --no-pager
```

| Problem | Fix |
|---|---|
| Screen stays on "Waiting for the node to start..." | Power off and swap the wires at Pi pins 8 and 10. Make sure you rebooted after installing. |
| Screen completely dark | Check red is on pin 4 and black on pin 6. |
| "Asterisk not responding" | Run `asterisk -rx "rpt nodes <your node>"` to check Asterisk is running. |
| No weather | Normal for a minute or two after a reboot. If it stays empty, run `python3 /usr/local/sbin/nextion_node.py --weather-test` |
| GPS badge red after a reboot | `/tmp` is cleared at boot, so it stays red until GPS Tracking is enabled again. |
| Screen won't go dark | With "turn on when someone keys up" set to y, every transmission restarts the timer. Rerun the installer and answer n. |

## Credits

Created by **[gmrs-link.com](https://gmrs-link.com)**. Please keep this credit if you share it.
