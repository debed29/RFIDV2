#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

const char* WIFI_SSID  = "ZZZ";
const char* WIFI_PASS  = "12345678";
const char* SERVER_URL = "http://192.168.56.64:5001/tap";

// Pin map (no overlaps):
//   RC522 SPI : SS=5, SCK=18, MISO=19, MOSI=23, RST=3V3
//   LEDs      : RED=25, GREEN=26, BLUE=27
//   LCD I2C   : SDA=21, SCL=22
#define SS_PIN  5
#define I2C_SDA 21
#define I2C_SCL 22

// Status LEDs (change pins to match your wiring; common-cathode / active-HIGH).
#define GREEN_LED_PIN 26   // successful TIME IN
#define BLUE_LED_PIN  27   // successful TIME OUT
#define RED_LED_PIN   25   // access denied / warning / any error

#define LED_BLINK_MS  500  // 0.5 s lit

// 16x2 LCD with I2C backpack (PCF8574)
#define LCD_ADDR 0x27   // if LCD stays blank, try 0x3F instead of 0x27
#define LCD_COLS 16
#define LCD_ROWS 2

MFRC522 rfid(SS_PIN, MFRC522::UNUSED_PIN);
LiquidCrystal_I2C lcd(LCD_ADDR, LCD_COLS, LCD_ROWS);

// Cut to 16 chars and pad with spaces so old text is overwritten (no clear() flicker).
String fitLine(String text) {
  text = text.substring(0, LCD_COLS);
  while (text.length() < LCD_COLS) text += " ";
  return text;
}

void lcdMessage(String line1, String line2) {
  lcd.setCursor(0, 0); lcd.print(fitLine(line1));
  lcd.setCursor(0, 1); lcd.print(fitLine(line2));
}

void showTapResult(String status, String action, String result, String name, String role, String message) {
  String title;
  if (status == "granted") {
    title = action == "OUT" ? "TIME OUT RECORDED" : "TIME IN RECORDED";
  } else if (status == "warning") {
    title = result == "REJECT_COOLDOWN" ? "PLEASE WAIT" : "ALREADY CHECKED IN";
  } else {
    title = "ACCESS DENIED";
  }

  if (name.length() == 0 && status == "denied") name = "Unknown card";
  role.toUpperCase();
  if (title == "TIME OUT RECORDED") {
    lcdMessage("TIME OUT", "RECORDED");
    delay(1125);
    lcdMessage(title, name);
    delay(1125);
  } else if (title == "ALREADY CHECKED IN") {
    lcdMessage("ALREADY CHECKED", "IN");
    delay(1125);
    lcdMessage(title, name);
    delay(1125);
  } else {
    lcdMessage(title, name);
    delay(2250);
  }
  lcdMessage(role, message);
  delay(2250);
}

void flashLed(uint8_t pin) {
  digitalWrite(pin, HIGH);
  delay(LED_BLINK_MS);
  digitalWrite(pin, LOW);
}

String uidToHex(MFRC522& reader) {
  String uid = "";
  for (byte i = 0; i < reader.uid.size; i++) {
    if (reader.uid.uidByte[i] < 0x10) uid += "0";
    uid += String(reader.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();
  return uid;
}

void connectWiFi() {
  Serial.print("Connecting to WiFi");
  lcdMessage("Connecting WiFi", "Please wait...");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.println("WiFi connected. ESP32 IP: " + WiFi.localIP().toString());
}

void setup() {
  Serial.begin(115200);

  pinMode(GREEN_LED_PIN, OUTPUT);
  pinMode(BLUE_LED_PIN, OUTPUT);
  pinMode(RED_LED_PIN, OUTPUT);
  digitalWrite(GREEN_LED_PIN, LOW);
  digitalWrite(BLUE_LED_PIN, LOW);
  digitalWrite(RED_LED_PIN, LOW);

  Wire.begin(I2C_SDA, I2C_SCL);
  lcd.init();
  lcd.backlight();

  SPI.begin();
  rfid.PCD_Init();
  connectWiFi();
  Serial.println("System ready. Tap your card.");
  lcdMessage("System Ready", "Tap your card");
}

void loop() {
  if (!rfid.PICC_IsNewCardPresent()) return;
  if (!rfid.PICC_ReadCardSerial()) return;

  String uid = uidToHex(rfid);
  Serial.println("Card UID: " + uid);
  lcdMessage("Card read", "Please wait...");
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();

  if (WiFi.status() != WL_CONNECTED) connectWiFi();

  HTTPClient http;
  Serial.println("POST target: " + String(SERVER_URL));
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");

  String body = "{\"uid\":\"" + uid + "\"}";
  int httpCode = http.POST(body);
  Serial.println("HTTP status: " + String(httpCode));

  if (httpCode == 200) {
    JsonDocument doc;
    DeserializationError error = deserializeJson(doc, http.getString());
    if (error == DeserializationError::Ok) {
      String status = doc["status"] | "";
      String name = doc["name"] | "";
      String action = doc["action"] | "";
      String result = doc["result"] | "";
      int cooldown = doc["cooldown_seconds"] | 0;
      String role = doc["role"] | "";
      String message = doc["message"] | "";

      if (result == "REJECT_COOLDOWN" && cooldown > 0) {
        message = "Please wait " + String(cooldown) + " seconds before scanning again.";
      }
      Serial.println(status + " / " + result + " / " + action + " / " + name);
      showTapResult(status, action, result, name, role, message);
      if (status == "granted") flashLed(action == "OUT" ? BLUE_LED_PIN : GREEN_LED_PIN);
      else flashLed(RED_LED_PIN);
    } else {
      Serial.println("Invalid server JSON: " + String(error.c_str()));
      lcdMessage("SERVER ERROR", "Bad reply");
      flashLed(RED_LED_PIN);
    }
  } else {
    Serial.println("HTTP error: " + String(httpCode));
    if (httpCode < 0) Serial.println("Check server address, WiFi, and firewall port 5001.");
    lcdMessage("SERVER ERROR", "Code: " + String(httpCode));
    flashLed(RED_LED_PIN);
  }

  http.end();
  Serial.println("Ready to scan.");
  lcdMessage("System Ready", "Tap your card");
}