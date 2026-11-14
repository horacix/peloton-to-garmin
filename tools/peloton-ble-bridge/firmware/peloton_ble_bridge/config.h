#pragma once
// ---------------------------------------------------------------------------
// Peloton BLE Bridge - build-time configuration.
// Runtime options (which metrics to broadcast, debug logging) are set with the
// DIP switches; nothing in here needs changing to switch modes.
// ---------------------------------------------------------------------------

// ---- Pins (Seeed XIAO nRF52840) -------------------------------------------
// Bike -> head unit data line, after the level shifter, goes to Serial1 RX (D7).
#define PIN_SW_CADENCE   1   // D1: ON (closed to GND) = broadcast cadence
#define PIN_SW_POWER     2   // D2: ON (closed to GND) = broadcast power
#define PIN_SW_DEBUG     3   // D3: ON (closed to GND) = log traffic on USB serial

// ---- Bike serial link -------------------------------------------------------
// Original Peloton Bike (not Bike+). Baud rate and framing below are taken from
// community reverse-engineering and MUST be confirmed with the debug switch on
// before trusting the output (see README, "Step 0").
#define BIKE_BAUD               19200

#define PELOTON_FRAME_START     0xF1   // first byte of a bike -> head unit frame
#define PELOTON_FRAME_END       0xF6   // last byte of every frame
#define PELOTON_MAX_PAYLOAD     8      // payload bytes (ASCII digits)
#define PELOTON_VERIFY_CHECKSUM 1      // 1 = drop frames whose checksum fails
#define PELOTON_DIGITS_LSB_FIRST 1     // 1 = payload digits are least-significant first

// Command bytes identifying which value a frame carries. UNVERIFIED - confirm in Step 0.
#define PELOTON_CMD_CADENCE     0x41
#define PELOTON_CMD_POWER       0x44
#define PELOTON_CMD_RESISTANCE  0x4A
// Raw power value / this = watts (e.g. 10 if the bike reports deci-watts). UNVERIFIED.
#define PELOTON_POWER_DIVISOR   10.0f

// ---- Behaviour --------------------------------------------------------------
#define DATA_TIMEOUT_MS         3000   // no frames for this long -> report zero
#define VIRTUAL_WHEEL_MM        2096   // enter this exact wheel size on the Garmin
#define NOTIFY_INTERVAL_MS      1000   // BLE notification period
#define INTEGRATE_INTERVAL_MS   50     // revolution-counter update period
#define DEVICE_NAME_PREFIX      "P2G Bridge"
