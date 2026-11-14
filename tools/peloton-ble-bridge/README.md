# Peloton BLE Bridge

A small receive-only device for the **original Peloton Bike** (not the Bike+). It listens to the data the bike sends the tablet over the sensor cable and re-broadcasts it as standard Bluetooth sensors a Garmin watch can pair with.

Peloton calculates speed from **power**, not cadence. The bridge uses a power-to-speed model fitted from your own P2G ride history. Your Garmin's speed and distance then match the Peloton screen (on the sample rides, distance error averaged 0.2% on held-out rides), instead of the 7–15% you get from a crank-mounted speed sensor.

```
Peloton tablet ◄──── sensor cable ────► bike sensor board
                   │ (tap: listen only, never transmits)
                   ▼
           level shifter (RS-232 → 3.3 V)
                   ▼ D7 / Serial1 RX
           Seeed XIAO nRF52840 ──Bluetooth──► Garmin watch
```

| Folder | Contents |
|---|---|
| `model/` | `fit_speed_model.py`: fits speed = f(power) from P2G JSON and writes `speed_model.h` |
| `firmware/peloton_ble_bridge/` | Arduino sketch. `bridge_core.h` and `peloton_protocol.h` are plain C++ with no Arduino dependencies |
| `firmware/test/` | Unit tests for the firmware core that run on a PC |

---

## Modes (DIP switches, no reflashing)

| DIP | Pin | OFF | ON |
|---|---|---|---|
| 1 Cadence | D1 | Speed only | Adds cadence (crank data) |
| 2 Power | D2 | No power service | Adds a Cycling Power service |
| 3 Debug | D3 | Quiet | Logs bytes, frames and values on USB serial at 115200 baud |

Speed is always on. Common setups:

| Your setup | DIP 1 | DIP 2 | Shows up as |
|---|---|---|---|
| **Using Vector pedals** (they provide power and cadence) | OFF | OFF | `P2G Bridge S` |
| No pedals: everything from the bike | ON | ON | `P2G Bridge SCP` |

- **Changing a mode:** flipping DIP 1 or 2 makes the bridge reboot itself into the new mode about a second later. The Bluetooth name changes with it. On the watch, **forget the old sensor and pair the new one**, because watches remember a sensor's capabilities from when it was paired.
- **Debug:** DIP 3 can be flipped at any time and takes effect immediately.

---

## Hardware

| Part | Notes |
|---|---|
| Seeed Studio XIAO nRF52840 | Plain or Sense version |
| Level shifter, **receive only** (one of the two below) | Converts the bike's signal to 3.3 V |
| 3-position DIP switch | Common pin to GND; positions to D1, D2, D3 |
| Tap into the sensor cable | Breakout or pass-through for the connector, so nothing is cut |
| USB-C 5 V supply | Powers the XIAO |

### Level shifter

The original Bike's link is reported to use **RS-232 voltage levels**, which would damage the XIAO if wired directly. **Confirm this with a multimeter or scope first (Step 0).**

**Option A: one transistor (recommended, light load on the line)**

```
 bike TX ──[22 kΩ]──┬──── base    2N3904
                    │             collector ──┬── D7 (RX)
          1N4148 ───┘ (anode to GND,          │
          cathode to base)                 [10 kΩ]
                                              │
                         emitter ── GND      3V3
```

- An RS-232 idle line (negative voltage) leaves the transistor off, so D7 reads HIGH, which is a logic 1. A positive voltage turns it on and D7 reads LOW, a logic 0. This is the correct polarity for the XIAO's serial input.
- The 22 kΩ input barely loads the bike's line, so the tablet keeps working.

**Option B: MAX3232 module, receiver side only.** Wire `R1IN` to bike TX and `R1OUT` to D7. Its ~5 kΩ input adds to the tablet's own load, which could push the bike's driver near its limit. Test that the tablet still works with it connected.

**Ground:** the XIAO's GND must connect to the cable's signal ground.

---

## Step 0: verify the protocol (required)

The frame format and command codes in `config.h` come from community reverse-engineering and are **not yet confirmed** on your bike.

1. Use a multimeter or scope to find the cable's ground, the bike→tablet data line and its voltage levels.
2. Flash the firmware, turn DIP 3 (debug) ON, open a serial monitor at 115200 baud and pedal.
3. Check the output:

| What you see | What it means / what to do |
|---|---|
| `[frame] cmd=0x41 … value=…` lines, and `[stats] cadence/power` matching the tablet | It works |
| Raw hex but no frames | Find the start and end bytes in the hex, then adjust `PELOTON_FRAME_*` and `PELOTON_DIGITS_LSB_FIRST`. If `bad=` keeps climbing, try `PELOTON_VERIFY_CHECKSUM 0` |
| Frames, but values don't line up | Match each `cmd=` to the tablet's numbers, then fix `PELOTON_CMD_*` and `PELOTON_POWER_DIVISOR` |
| Nothing at all | Check the baud rate (`BIKE_BAUD`), the polarity (Option A inverts the signal) and the wiring |

---

## Build and flash

1. In Arduino IDE, add the board manager URL `https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json`.
2. Install **Seeed nRF52 Boards**. Use the non-mbed version: it includes the Bluefruit library.
3. Open `firmware/peloton_ble_bridge/peloton_ble_bridge.ino`, select **Seeed XIAO nRF52840** and upload.

## Fit your own speed model

The `speed_model.h` that ships here was fitted on the repo's 5 sample rides (15–165 W). Refit it on your own history so it covers your full power range:

```bash
# In P2G, set "Format": { "Json": true } and sync your rides, then:
python3 tools/peloton-ble-bridge/model/fit_speed_model.py output/json \
    --header tools/peloton-ble-bridge/firmware/peloton_ble_bridge/speed_model.h
```

Above the highest power in your training data, the firmware continues the curve in a straight line along its slope at the top, so speed never drops as power rises.

## Pair with Garmin

1. On the watch, go to **Sensors → Add new**, then pair `P2G Bridge …` as **Speed** (and as **Power** if DIP 2 is on).
2. Set the speed sensor's wheel size **manually to `2096` mm**. This must equal `VIRTUAL_WHEEL_MM` in `config.h`.
3. Record using an **Indoor Bike** activity.
4. **With Vector pedals:** keep DIP 1 and DIP 2 OFF so that Vector is the only source of power and cadence.

## Tests

```bash
make -C tools/peloton-ble-bridge/firmware/test                        # firmware core (C++)
cd tools/peloton-ble-bridge/model && python3 -m unittest -v           # speed model fitter
```

## Limitations

- **Original Bike only.** The Bike+ uses a different architecture.
- **Untested on hardware.** The Bluetooth layer has not run on a real board yet. The protocol constants need Step 0.
- **Duplicate activities.** If the watch records the ride and P2G also uploads it, Garmin Connect will have two copies of each ride.
