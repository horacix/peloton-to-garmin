// Host-side unit tests for the hardware-independent firmware core.
// Build & run:  make -C tools/peloton-ble-bridge/firmware/test
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <vector>

#include "../peloton_ble_bridge/bridge_core.h"

static int failures = 0, checks = 0;
#define CHECK(cond) do { checks++; if (!(cond)) { failures++; printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } } while (0)
#define CHECK_NEAR(a, b, tol) CHECK(fabs((double)(a) - (double)(b)) <= (tol))
#define TEST(name) static void name(); struct name##_reg { name##_reg() { tests().push_back({#name, name}); } } name##_inst; static void name()

struct T { const char* name; void (*fn)(); };
static std::vector<T>& tests() { static std::vector<T> t; return t; }

using namespace bridge;
using peloton::Frame;
using peloton::Parser;

// Build a bike frame: F1 cmd len digits(LSB first) checksum F6
static std::vector<uint8_t> frame(uint8_t cmd, const char* digitsMsbFirst) {
  std::vector<uint8_t> f{PELOTON_FRAME_START, cmd, (uint8_t)strlen(digitsMsbFirst)};
  for (int i = (int)strlen(digitsMsbFirst) - 1; i >= 0; i--) f.push_back((uint8_t)digitsMsbFirst[i]);
  uint8_t sum = 0;
  for (uint8_t b : f) sum += b;
  f.push_back(sum);
  f.push_back(PELOTON_FRAME_END);
  return f;
}

static int feedAll(Parser& p, const std::vector<uint8_t>& bytes, std::vector<Frame>& out) {
  int n = 0;
  for (uint8_t b : bytes) { Frame f; if (p.feed(b, f)) { out.push_back(f); n++; } }
  return n;
}

// ---------------------------------------------------------------- parser
TEST(parser_decodes_valid_frame) {
  Parser p; std::vector<Frame> out;
  CHECK(feedAll(p, frame(PELOTON_CMD_CADENCE, "087"), out) == 1);
  CHECK(out[0].cmd == PELOTON_CMD_CADENCE);
  CHECK(out[0].value == 87);
  CHECK(p.goodFrames() == 1 && p.badFrames() == 0);
}

TEST(parser_skips_noise_and_head_unit_requests) {
  Parser p; std::vector<Frame> out;
  std::vector<uint8_t> bytes{0x00, 0xFF, 0xF5, 0x41, 0x36, 0xF6};  // garbage + a request frame
  auto f = frame(PELOTON_CMD_POWER, "1234");
  bytes.insert(bytes.end(), f.begin(), f.end());
  CHECK(feedAll(p, bytes, out) == 1);
  CHECK(out[0].value == 1234);
}

TEST(parser_rejects_bad_checksum) {
  Parser p; std::vector<Frame> out;
  auto f = frame(PELOTON_CMD_CADENCE, "90");
  f[f.size() - 2] ^= 0x55;
  CHECK(feedAll(p, f, out) == 0);
  CHECK(p.badFrames() == 1);
}

TEST(parser_rejects_non_digit_payload) {
  Parser p; std::vector<Frame> out;
  auto f = frame(PELOTON_CMD_CADENCE, "9X");
  CHECK(feedAll(p, f, out) == 0);
}

TEST(parser_resyncs_after_truncated_frame) {
  Parser p; std::vector<Frame> out;
  auto bad = frame(PELOTON_CMD_CADENCE, "90");
  bad.resize(4);  // cut off mid-frame
  auto good = frame(PELOTON_CMD_CADENCE, "75");
  std::vector<uint8_t> bytes(bad);
  bytes.insert(bytes.end(), good.begin(), good.end());
  feedAll(p, bytes, out);
  // The truncated frame swallows bytes of the next; the stream must recover
  // by the frame after that.
  feedAll(p, good, out);
  CHECK(!out.empty() && out.back().value == 75);
}

TEST(parser_rejects_oversized_length) {
  Parser p; std::vector<Frame> out;
  std::vector<uint8_t> b{PELOTON_FRAME_START, PELOTON_CMD_POWER, PELOTON_MAX_PAYLOAD + 1};
  CHECK(feedAll(p, b, out) == 0);
  CHECK(p.badFrames() == 1);
}

// ---------------------------------------------------------------- speed model
TEST(speed_model_zero_and_clamped) {
  CHECK(speedMphFromPower(0) == 0.0f);
  CHECK(speedMphFromPower(-10) == 0.0f);
  CHECK(speedMphFromPower(0.01f) >= 0.0f);
}

TEST(speed_model_monotonic_including_extrapolation) {
  float prev = 0;
  for (int w = 1; w <= 1500; w++) {
    float v = speedMphFromPower((float)w);
    CHECK(v >= prev - 1e-4f);
    prev = v;
  }
}

TEST(speed_model_continuous_at_p_max) {
  float below = speedMphFromPower(SPEED_MODEL_P_MAX - 0.01f);
  float above = speedMphFromPower(SPEED_MODEL_P_MAX + 0.01f);
  CHECK_NEAR(below, above, 0.01);
}

TEST(speed_model_matches_sample_ride_point) {
  // From cycling_just_ride.json: ~100 W -> ~16.2 mph on Peloton
  CHECK_NEAR(speedMphFromPower(100), 16.2, 0.8);
}

// ---------------------------------------------------------------- bike state
TEST(bike_state_applies_frames_and_times_out) {
  BikeState s;
  Frame c{PELOTON_CMD_CADENCE, 80, 0, {}}, w{PELOTON_CMD_POWER, 1500, 0, {}};
  s.apply(c, 1000);
  s.apply(w, 1000);
  CHECK(s.cadenceRpm(1500) == 80.0f);
  CHECK_NEAR(s.powerW(1500), 1500 / PELOTON_POWER_DIVISOR, 1e-4);
  CHECK(s.speedMph(1500) > 0);
  CHECK(s.cadenceRpm(1000 + DATA_TIMEOUT_MS + 1) == 0.0f);
  CHECK(s.speedMph(1000 + DATA_TIMEOUT_MS + 1) == 0.0f);
}

TEST(bike_state_zero_speed_without_cadence) {
  BikeState s;
  s.apply(Frame{PELOTON_CMD_POWER, 1500, 0, {}}, 0);
  s.apply(Frame{PELOTON_CMD_CADENCE, 0, 0, {}}, 0);
  CHECK(s.speedMph(10) == 0.0f);
}

TEST(bike_state_ignores_unknown_commands) {
  BikeState s;
  s.apply(Frame{0x7E, 99, 0, {}}, 0);
  CHECK(!s.live(0));
}

// ---------------------------------------------------------------- rev counter
TEST(rev_counter_counts_and_timestamps) {
  RevCounter r;
  // 2 rev/s for 10 s, ticking every 50 ms
  for (uint32_t t = 0; t <= 10000; t += 50) r.update(t, 2.0f);
  CHECK(r.revs() == 20);
  CHECK_NEAR(r.eventTime1024(), 10.0 * 1024, 2);
}

TEST(rev_counter_interpolates_event_time_between_ticks) {
  RevCounter r;
  r.update(0, 0);
  r.update(1000, 1.5f);  // 1.5 revs: last whole rev at 666.7 ms
  CHECK(r.revs() == 1);
  CHECK_NEAR(r.eventTime1024(), 666.67 * 1.024, 1.5);
}

TEST(rev_counter_derived_speed_matches_input) {
  // What Garmin does: delta revs * wheel / delta event time.
  RevCounter r;
  const float mph = 17.3f;
  const float rps = mph * MM_PER_S_PER_MPH / VIRTUAL_WHEEL_MM;
  for (uint32_t t = 0; t <= 5000; t += 50) r.update(t, rps);
  uint32_t r0 = r.revs(); uint16_t e0 = r.eventTime1024();
  for (uint32_t t = 5050; t <= 65000; t += 50) r.update(t, rps);
  double dt = (uint16_t)(r.eventTime1024() - e0) / 1024.0;
  double garminMph = (r.revs() - r0) * VIRTUAL_WHEEL_MM / dt / MM_PER_S_PER_MPH;
  CHECK_NEAR(garminMph, mph, 0.05);
}

TEST(rev_counter_freezes_when_stopped) {
  RevCounter r;
  for (uint32_t t = 0; t <= 2000; t += 50) r.update(t, 3.0f);
  uint32_t revs = r.revs(); uint16_t ev = r.eventTime1024();
  for (uint32_t t = 2050; t <= 8000; t += 50) r.update(t, 0.0f);
  CHECK(r.revs() == revs && r.eventTime1024() == ev);
}

TEST(rev_counter_survives_millis_wraparound) {
  RevCounter r;
  uint32_t t = 0xFFFFFFFFu - 500;
  for (int i = 0; i < 40; i++, t += 50) r.update(t, 2.0f);
  CHECK(r.revs() == 3);  // 39 ticks * 50 ms = 1.95 s * 2 = 3.9
}

// ---------------------------------------------------------------- encoders
TEST(csc_speed_only_payload) {
  Features f{false, false};
  RevCounter w, c;
  w.setRevs(0x01020304);
  uint8_t buf[16];
  CHECK(encodeCsc(buf, f, w, c) == 7);
  CHECK(buf[0] == 0x01);
  CHECK(buf[1] == 0x04 && buf[2] == 0x03 && buf[3] == 0x02 && buf[4] == 0x01);
  CHECK(cscFeature(f) == 0x0001);
}

TEST(csc_speed_and_cadence_payload) {
  Features f{true, false};
  RevCounter w, c;
  c.setRevs(0x1234);
  uint8_t buf[16];
  CHECK(encodeCsc(buf, f, w, c) == 11);
  CHECK(buf[0] == 0x03);
  CHECK(buf[7] == 0x34 && buf[8] == 0x12);
  CHECK(cscFeature(f) == 0x0003);
}

TEST(cps_payload_with_and_without_crank) {
  RevCounter c;
  c.setRevs(7);
  uint8_t buf[16];
  CHECK(encodeCps(buf, Features{false, true}, 254.6f, c) == 4);
  CHECK(buf[0] == 0x00 && buf[1] == 0x00);
  CHECK(buf[2] == 255 && buf[3] == 0);
  CHECK(cpsFeature(Features{false, true}) == 0);

  CHECK(encodeCps(buf, Features{true, true}, 100, c) == 8);
  CHECK(buf[0] == 0x20);
  CHECK(buf[4] == 7 && buf[5] == 0);
  CHECK(cpsFeature(Features{true, true}) == 0x08);
}

TEST(cps_power_clamped) {
  RevCounter c; uint8_t buf[16];
  encodeCps(buf, Features{false, true}, -5, c);
  CHECK(buf[2] == 0 && buf[3] == 0);
}

TEST(sc_control_point_set_cumulative_value) {
  RevCounter w; uint8_t resp[3];
  const uint8_t req[] = {0x01, 0x10, 0x00, 0x00, 0x00};
  CHECK(handleScControlPoint(req, 5, w, resp) == 3);
  CHECK(resp[0] == 0x10 && resp[1] == 0x01 && resp[2] == 0x01);
  CHECK(w.revs() == 16);
}

TEST(sc_control_point_rejects_unknown_and_malformed) {
  RevCounter w; uint8_t resp[3];
  const uint8_t unknown[] = {0x04};
  handleScControlPoint(unknown, 1, w, resp);
  CHECK(resp[1] == 0x04 && resp[2] == 0x02);
  const uint8_t shortReq[] = {0x01, 0x10};
  handleScControlPoint(shortReq, 2, w, resp);
  CHECK(resp[2] == 0x03);
}

TEST(device_name_reflects_mode) {
  char n[32];
  deviceName(Features{false, false}, n, sizeof n); CHECK(strcmp(n, "P2G Bridge S") == 0);
  deviceName(Features{true, true}, n, sizeof n);   CHECK(strcmp(n, "P2G Bridge SCP") == 0);
  deviceName(Features{false, true}, n, sizeof n);  CHECK(strcmp(n, "P2G Bridge SP") == 0);
}

int main() {
  for (auto& t : tests()) {
    int before = failures;
    t.fn();
    printf("%s %s\n", failures == before ? "ok  " : "FAIL", t.name);
  }
  printf("\n%zu tests, %d checks, %d failures\n", tests().size(), checks, failures);
  return failures ? 1 : 0;
}
