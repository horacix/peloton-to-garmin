#pragma once
// Hardware-independent bridge logic: bike state, speed model, revolution
// counters and BLE payload encoding. Pure C++ so it is unit tested on a PC.
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "config.h"
#include "peloton_protocol.h"
#include "speed_model.h"

namespace bridge {

constexpr float MM_PER_S_PER_MPH = 447.04f;  // 1609344 mm / 3600 s

// ---- Speed model -----------------------------------------------------------
inline float speedMphFromPower(float watts) {
  if (watts <= 0.0f) return 0.0f;
  float v;
  if (watts > SPEED_MODEL_P_MAX) {
    const float pm = SPEED_MODEL_P_MAX;
    float slope = SPEED_MODEL_A + SPEED_MODEL_B / (2.0f * sqrtf(pm));
    if (slope < 0.0f) slope = 0.0f;
    v = SPEED_MODEL_A * pm + SPEED_MODEL_B * sqrtf(pm) + SPEED_MODEL_C + slope * (watts - pm);
  } else {
    v = SPEED_MODEL_A * watts + SPEED_MODEL_B * sqrtf(watts) + SPEED_MODEL_C;
  }
  return v > 0.0f ? v : 0.0f;
}

// ---- Feature selection (DIP switches) ---------------------------------------
struct Features {
  bool cadence;
  bool power;
  bool operator==(const Features& o) const { return cadence == o.cadence && power == o.power; }
  bool operator!=(const Features& o) const { return !(*this == o); }
};

// "P2G Bridge S", "... SC", "... SP", "... SCP": one name per mode so a watch
// that cached an old mode is easy to spot and re-pair.
inline void deviceName(const Features& f, char* out, size_t n) {
  snprintf(out, n, "%s S%s%s", DEVICE_NAME_PREFIX, f.cadence ? "C" : "", f.power ? "P" : "");
}

// ---- Latest values from the bike --------------------------------------------
class BikeState {
 public:
  void apply(const peloton::Frame& f, uint32_t nowMs) {
    switch (f.cmd) {
      case PELOTON_CMD_CADENCE:    cadenceRpm_ = (float)f.value; break;
      case PELOTON_CMD_POWER:      powerW_ = (float)f.value / PELOTON_POWER_DIVISOR; break;
      case PELOTON_CMD_RESISTANCE: resistance_ = f.value; break;
      default: return;  // unknown command: not a sign of life for our metrics
    }
    lastMs_ = nowMs;
    seen_ = true;
  }

  bool live(uint32_t nowMs) const { return seen_ && (uint32_t)(nowMs - lastMs_) <= DATA_TIMEOUT_MS; }
  float cadenceRpm(uint32_t nowMs) const { return live(nowMs) ? cadenceRpm_ : 0.0f; }
  float powerW(uint32_t nowMs) const { return live(nowMs) ? powerW_ : 0.0f; }
  int32_t resistance() const { return resistance_; }
  float speedMph(uint32_t nowMs) const {
    // Peloton shows zero speed when you stop pedalling, whatever the power reading.
    return cadenceRpm(nowMs) > 0.0f ? speedMphFromPower(powerW(nowMs)) : 0.0f;
  }

 private:
  float cadenceRpm_ = 0, powerW_ = 0;
  int32_t resistance_ = 0;
  uint32_t lastMs_ = 0;
  bool seen_ = false;
};

// ---- Revolution counter with 1/1024 s event timestamps ----------------------
// Garmin derives speed/cadence from (delta revolutions / delta event time), so
// the event time must be the instant the last whole revolution completed.
class RevCounter {
 public:
  void update(uint32_t nowMs, float revsPerSec) {
    if (!started_) { lastMs_ = nowMs; started_ = true; return; }
    uint32_t dtMs = nowMs - lastMs_;  // wrap-safe
    lastMs_ = nowMs;
    clockMs_ += dtMs;
    if (revsPerSec <= 0.0f || dtMs == 0) return;
    frac_ += (double)revsPerSec * dtMs / 1000.0;
    constexpr double kEps = 1e-9;  // absorb floating-point drift (e.g. 10 x 0.1 != 1.0)
    if (frac_ >= 1.0 - kEps) {
      uint32_t whole = (uint32_t)(frac_ + kEps);
      revs_ += whole;
      frac_ -= whole;
      if (frac_ < 0.0) frac_ = 0.0;
      eventMs_ = clockMs_ - frac_ / revsPerSec * 1000.0;
    }
  }

  void setRevs(uint32_t revs) { revs_ = revs; }
  uint32_t revs() const { return revs_; }
  uint16_t eventTime1024() const { return (uint16_t)((uint64_t)llround(eventMs_ * 1.024)); }

 private:
  bool started_ = false;
  uint32_t lastMs_ = 0;
  double clockMs_ = 0, eventMs_ = 0, frac_ = 0;
  uint32_t revs_ = 0;
};

// ---- BLE payload encoders (little endian, per Bluetooth SIG specs) ----------
inline uint8_t* put16(uint8_t* p, uint16_t v) { p[0] = v & 0xFF; p[1] = v >> 8; return p + 2; }
inline uint8_t* put32(uint8_t* p, uint32_t v) { p = put16(p, v & 0xFFFF); return put16(p, v >> 16); }

// CSC Feature (0x2A5C): bit0 wheel revolution data, bit1 crank revolution data.
inline uint16_t cscFeature(const Features& f) { return 0x0001 | (f.cadence ? 0x0002 : 0); }

// CSC Measurement (0x2A5B). Returns payload length (7 or 11).
inline uint8_t encodeCsc(uint8_t* buf, const Features& f, const RevCounter& wheel, const RevCounter& crank) {
  uint8_t* p = buf;
  *p++ = 0x01 | (f.cadence ? 0x02 : 0);
  p = put32(p, wheel.revs());
  p = put16(p, wheel.eventTime1024());
  if (f.cadence) {
    p = put16(p, (uint16_t)crank.revs());
    p = put16(p, crank.eventTime1024());
  }
  return (uint8_t)(p - buf);
}

// Cycling Power Feature (0x2A65): bit3 crank revolution data supported.
inline uint32_t cpsFeature(const Features& f) { return f.cadence ? 0x00000008u : 0u; }

// Cycling Power Measurement (0x2A63). Returns payload length (4 or 8).
// Crank data is only included when cadence is enabled, so the watch never sees
// a cadence source the user switched off.
inline uint8_t encodeCps(uint8_t* buf, const Features& f, float watts, const RevCounter& crank) {
  uint8_t* p = buf;
  p = put16(p, f.cadence ? 0x0020 : 0x0000);
  int32_t w = (int32_t)lroundf(watts);
  if (w < 0) w = 0;
  if (w > 32767) w = 32767;
  p = put16(p, (uint16_t)(int16_t)w);
  if (f.cadence) {
    p = put16(p, (uint16_t)crank.revs());
    p = put16(p, crank.eventTime1024());
  }
  return (uint8_t)(p - buf);
}

// SC Control Point (0x2A55). Handles "Set Cumulative Value" (op 0x01); all
// other op codes are answered "not supported". Returns response length.
inline uint8_t handleScControlPoint(const uint8_t* req, uint16_t len, RevCounter& wheel, uint8_t* resp) {
  resp[0] = 0x10;                      // Response Code
  resp[1] = len ? req[0] : 0x00;       // Request Op Code
  if (len == 5 && req[0] == 0x01) {
    wheel.setRevs((uint32_t)req[1] | (uint32_t)req[2] << 8 | (uint32_t)req[3] << 16 | (uint32_t)req[4] << 24);
    resp[2] = 0x01;                    // Success
  } else if (len >= 1 && req[0] == 0x01) {
    resp[2] = 0x03;                    // Invalid Parameter
  } else {
    resp[2] = 0x02;                    // Op Code not supported
  }
  return 3;
}

}  // namespace bridge
