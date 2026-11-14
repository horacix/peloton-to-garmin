#pragma once
// Byte-stream parser for the bike -> head unit serial link of the original
// Peloton Bike. Pure C++ (no Arduino dependencies) so it can be unit tested on
// a PC. Frame layout (to be confirmed in Step 0):
//
//   START  CMD  LEN  D0 .. D(LEN-1)  CHECKSUM  END
//   0xF1   xx   n    ASCII digits    sum(START..D(LEN-1)) & 0xFF   0xF6
#include <stdint.h>
#include "config.h"

namespace peloton {

struct Frame {
  uint8_t cmd;
  int32_t value;    // decoded decimal payload
  uint8_t len;
  uint8_t raw[PELOTON_MAX_PAYLOAD];
};

class Parser {
 public:
  // Feed one byte. Returns true and fills `out` when a complete valid frame ends.
  bool feed(uint8_t b, Frame& out) {
    switch (state_) {
      case WAIT_START:
        if (b == PELOTON_FRAME_START) { sum_ = b; state_ = CMD; }
        return false;
      case CMD:
        cur_.cmd = b; sum_ += b; state_ = LEN;
        return false;
      case LEN:
        if (b == 0 || b > PELOTON_MAX_PAYLOAD) { bad_++; return restart(b); }
        cur_.len = b; idx_ = 0; sum_ += b; state_ = DATA;
        return false;
      case DATA:
        cur_.raw[idx_++] = b; sum_ += b;
        if (idx_ == cur_.len) state_ = CHECK;
        return false;
      case CHECK:
        checksumOk_ = (b == sum_);
        state_ = END;
        return false;
      case END:
        state_ = WAIT_START;
        if (b != PELOTON_FRAME_END) { bad_++; return restart(b); }
        if (PELOTON_VERIFY_CHECKSUM && !checksumOk_) { bad_++; return false; }
        if (!decode(cur_)) { bad_++; return false; }
        good_++;
        out = cur_;
        return true;
    }
    return false;
  }

  uint32_t goodFrames() const { return good_; }
  uint32_t badFrames() const { return bad_; }

 private:
  enum State : uint8_t { WAIT_START, CMD, LEN, DATA, CHECK, END };

  // A corrupt frame may hide the start of the next one: re-examine this byte.
  bool restart(uint8_t b) {
    state_ = WAIT_START;
    Frame ignored;
    return feed(b, ignored);
  }

  static bool decode(Frame& f) {
    int32_t v = 0;
    for (uint8_t i = 0; i < f.len; i++) {
      uint8_t c = PELOTON_DIGITS_LSB_FIRST ? f.raw[f.len - 1 - i] : f.raw[i];
      if (c < '0' || c > '9') return false;
      v = v * 10 + (c - '0');
    }
    f.value = v;
    return true;
  }

  State state_ = WAIT_START;
  Frame cur_{};
  uint8_t idx_ = 0;
  uint8_t sum_ = 0;
  bool checksumOk_ = false;
  uint32_t good_ = 0;
  uint32_t bad_ = 0;
};

}  // namespace peloton
