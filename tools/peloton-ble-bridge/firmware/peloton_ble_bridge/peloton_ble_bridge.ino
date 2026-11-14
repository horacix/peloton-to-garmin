// Peloton BLE Bridge - Seeed XIAO nRF52840
//
// Listens (receive-only) to the original Peloton Bike's sensor link and
// re-broadcasts speed, and optionally cadence and power, as standard Bluetooth
// sensors a Garmin watch can pair with.
//
//   DIP 1 (D1) cadence  ON = include cadence (crank data)
//   DIP 2 (D2) power    ON = add a Cycling Power service
//   DIP 3 (D3) debug    ON = log bytes/frames/values on USB serial (115200)
//   Speed is always broadcast.
//
// Flipping DIP 1 or 2 reboots the bridge into the new mode automatically;
// forget and re-pair the sensor on the watch afterwards (its name changes).
//
// Board package: "Seeed nRF52 Boards" (non-mbed, Bluefruit-based), board
// "Seeed XIAO nRF52840".
#include <bluefruit.h>

#include "config.h"
#include "bridge_core.h"

using bridge::Features;

// ---- BLE objects -------------------------------------------------------------
BLEDis dis;

BLEService        cscService(0x1816);    // Cycling Speed and Cadence
BLECharacteristic cscMeasurement(0x2A5B);
BLECharacteristic cscFeatureChr(0x2A5C);
BLECharacteristic cscLocation(0x2A5D);
BLECharacteristic scControlPoint(0x2A55);

BLEService        cpsService(0x1818);    // Cycling Power
BLECharacteristic cpsMeasurement(0x2A63);
BLECharacteristic cpsFeatureChr(0x2A65);
BLECharacteristic cpsLocation(0x2A5D);

// ---- State ---------------------------------------------------------------------
peloton::Parser    parser;
bridge::BikeState  bike;
bridge::RevCounter wheel, crank;
Features           features;
bool               debugOn = false;
uint32_t           lastIntegrateMs = 0, lastNotifyMs = 0, lastSwitchCheckMs = 0, lastStatsMs = 0;
uint8_t            switchChangeCount = 0;

static bool switchOn(uint8_t pin) { return digitalRead(pin) == LOW; }  // closed to GND = ON

static Features readFeatureSwitches() { return Features{switchOn(PIN_SW_CADENCE), switchOn(PIN_SW_POWER)}; }

// ---- BLE callbacks -----------------------------------------------------------
void onScControlPoint(uint16_t conn, BLECharacteristic* chr, uint8_t* data, uint16_t len) {
  uint8_t resp[3];
  uint8_t n = bridge::handleScControlPoint(data, len, wheel, resp);
  if (chr->indicateEnabled(conn)) chr->indicate(conn, resp, n);
}

void onConnect(uint16_t conn) {
  if (debugOn) Serial.printf("[ble] connected (%u)\n", conn);
}

void onDisconnect(uint16_t conn, uint8_t reason) {
  if (debugOn) Serial.printf("[ble] disconnected (%u) reason 0x%02X\n", conn, reason);
}

// ---- Setup helpers -------------------------------------------------------------
static void readOnly(BLECharacteristic& c, uint8_t len) {
  c.setProperties(CHR_PROPS_READ);
  c.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  c.setFixedLen(len);
  c.begin();
}

static void setupCsc() {
  cscService.begin();

  cscMeasurement.setProperties(CHR_PROPS_NOTIFY);
  cscMeasurement.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  cscMeasurement.setMaxLen(11);
  cscMeasurement.begin();

  readOnly(cscFeatureChr, 2);
  cscFeatureChr.write16(bridge::cscFeature(features));

  readOnly(cscLocation, 1);
  cscLocation.write8(0);  // Other

  // Mandatory when wheel revolution data is supported.
  scControlPoint.setProperties(CHR_PROPS_WRITE | CHR_PROPS_INDICATE);
  scControlPoint.setPermission(SECMODE_OPEN, SECMODE_OPEN);
  scControlPoint.setMaxLen(5);
  scControlPoint.setWriteCallback(onScControlPoint);
  scControlPoint.begin();
}

static void setupCps() {
  cpsService.begin();

  cpsMeasurement.setProperties(CHR_PROPS_NOTIFY);
  cpsMeasurement.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  cpsMeasurement.setMaxLen(8);
  cpsMeasurement.begin();

  readOnly(cpsFeatureChr, 4);
  cpsFeatureChr.write32(bridge::cpsFeature(features));

  readOnly(cpsLocation, 1);
  cpsLocation.write8(0);  // Other
}

static void startAdvertising(const char* name) {
  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  // Bluetooth SIG appearance: 0x0482 speed sensor, 0x0485 speed+cadence, 0x0484 power.
  uint16_t appearance = features.power ? 0x0484 : (features.cadence ? 0x0485 : 0x0482);
  Bluefruit.setAppearance(appearance);
  Bluefruit.Advertising.addAppearance(appearance);
  Bluefruit.Advertising.addService(cscService);
  if (features.power) Bluefruit.Advertising.addService(cpsService);
  Bluefruit.ScanResponse.addName();
  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setInterval(32, 244);  // 20 ms fast, 152.5 ms slow (0.625 ms units)
  Bluefruit.Advertising.setFastTimeout(30);
  Bluefruit.Advertising.start(0);              // advertise forever
  if (debugOn) Serial.printf("[ble] advertising as \"%s\"\n", name);
}

// ---- Arduino entry points --------------------------------------------------------
void setup() {
  pinMode(PIN_SW_CADENCE, INPUT_PULLUP);
  pinMode(PIN_SW_POWER, INPUT_PULLUP);
  pinMode(PIN_SW_DEBUG, INPUT_PULLUP);
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH);  // off (active low)
  delay(10);  // let pull-ups settle
  features = readFeatureSwitches();
  debugOn = switchOn(PIN_SW_DEBUG);

  Serial.begin(115200);
  Serial1.begin(BIKE_BAUD);  // RX on D7 - only the bike -> head unit line is connected

  char name[32];
  bridge::deviceName(features, name, sizeof name);

  Bluefruit.begin(2, 0);  // up to 2 centrals (watch + phone), no central role
  Bluefruit.setName(name);
  Bluefruit.setTxPower(4);
  Bluefruit.Periph.setConnectCallback(onConnect);
  Bluefruit.Periph.setDisconnectCallback(onDisconnect);

  dis.setManufacturer("DIY");
  dis.setModel("Peloton BLE Bridge");
  dis.begin();

  setupCsc();
  if (features.power) setupCps();
  startAdvertising(name);

  if (debugOn) {
    Serial.printf("[cfg] speed=on cadence=%s power=%s wheel=%u mm\n",
                  features.cadence ? "on" : "off", features.power ? "on" : "off", VIRTUAL_WHEEL_MM);
  }
}

static void notifyAll(BLECharacteristic& chr, const uint8_t* data, uint16_t len) {
  for (uint16_t conn = 0; conn < BLE_MAX_CONNECTION; conn++) {
    if (Bluefruit.connected(conn) && chr.notifyEnabled(conn)) chr.notify(conn, data, len);
  }
}

static void pollSerial(uint32_t now) {
  while (Serial1.available()) {
    uint8_t b = (uint8_t)Serial1.read();
    if (debugOn) Serial.printf("%02X ", b);
    peloton::Frame f;
    if (parser.feed(b, f)) {
      bike.apply(f, now);
      if (debugOn) Serial.printf("\n[frame] cmd=0x%02X len=%u value=%ld\n", f.cmd, f.len, (long)f.value);
    }
  }
}

static void checkSwitches(uint32_t now) {
  if (now - lastSwitchCheckMs < 250) return;
  lastSwitchCheckMs = now;
  debugOn = switchOn(PIN_SW_DEBUG);  // debug can change live
  // Metric switches change the GATT table: debounce, then reboot into the new mode.
  if (readFeatureSwitches() != features) {
    if (++switchChangeCount >= 4) {  // stable for ~1 s
      if (debugOn) { Serial.println("[cfg] mode switch changed - rebooting"); Serial.flush(); }
      delay(50);
      NVIC_SystemReset();
    }
  } else {
    switchChangeCount = 0;
  }
}

void loop() {
  const uint32_t now = millis();
  pollSerial(now);
  checkSwitches(now);

  if (now - lastIntegrateMs >= INTEGRATE_INTERVAL_MS) {
    lastIntegrateMs = now;
    float speedMmS = bike.speedMph(now) * bridge::MM_PER_S_PER_MPH;
    wheel.update(now, speedMmS / VIRTUAL_WHEEL_MM);
    crank.update(now, bike.cadenceRpm(now) / 60.0f);
  }

  if (now - lastNotifyMs >= NOTIFY_INTERVAL_MS) {
    lastNotifyMs = now;
    uint8_t buf[16];
    uint8_t n = bridge::encodeCsc(buf, features, wheel, crank);
    notifyAll(cscMeasurement, buf, n);
    if (features.power) {
      n = bridge::encodeCps(buf, features, bike.powerW(now), crank);
      notifyAll(cpsMeasurement, buf, n);
    }
    digitalWrite(LED_BUILTIN, bike.live(now) ? LOW : HIGH);  // LED on (active low) while bike data flows
  }

  if (debugOn && now - lastStatsMs >= 5000) {
    lastStatsMs = now;
    // Floats via print() - printf %f support varies between Arduino cores.
    Serial.printf("\n[stats] frames ok=%lu bad=%lu live=%d res=%ld cadence=",
                  (unsigned long)parser.goodFrames(), (unsigned long)parser.badFrames(), bike.live(now),
                  (long)bike.resistance());
    Serial.print(bike.cadenceRpm(now), 0);
    Serial.print(" rpm power=");
    Serial.print(bike.powerW(now), 1);
    Serial.print(" W speed=");
    Serial.print(bike.speedMph(now), 2);
    Serial.println(" mph");
  }
}
