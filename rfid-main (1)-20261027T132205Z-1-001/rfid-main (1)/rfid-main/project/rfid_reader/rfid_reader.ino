#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

const char* WIFI_SSID  = "realme 8i";          // <-- put your WiFi name (2.4 GHz only!)
const char* WIFI_PASS  = "dadadada";       // <-- put your WiFi password
const char* SERVER_URL = "http://10.140.90.90:5001/tap";  // PC IPv4 on the current network
const unsigned long REGISTRATION_POLL_INTERVAL_MS = 500;
unsigned long lastRegistrationPoll = 0;
unsigned long lastRegistrationEventId = 0;

#define SS_PIN  5
#define I2C_SDA 21
#define I2C_SCL 22

// 16x2 LCD with I2C backpack (PCF8574)
#define LCD_ADDR 0x27   // if LCD stays blank, try 0x3F instead of 0x27
#define LCD_COLS 16
#define LCD_ROWS 2

MFRC522 rfid(SS_PIN, MFRC522::UNUSED_PIN);
LiquidCrystal_I2C lcd(LCD_ADDR, LCD_COLS, LCD_ROWS);

String uidToHex(MFRC522& reader) {
  String uid = "";
  for (byte i = 0; i < reader.uid.size; i++) {
    if (reader.uid.uidByte[i] < 0x10) uid += "0";  // zero-pad so UIDs always match the DB
    uid += String(reader.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();
  return uid;
}

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

String registrationEventUrl() {
  String url = SERVER_URL;
  int pathStart = url.lastIndexOf('/');
  if (pathStart >= 0) url.remove(pathStart);
  return url + "/registration-event";
}

void checkRegistrationEvent(bool showNewEvent) {
  HTTPClient http;
  http.begin(registrationEventUrl());
  int httpCode = http.GET();
  if (httpCode == 200) {
    JsonDocument doc;
    DeserializationError error = deserializeJson(doc, http.getString());
    if (error == DeserializationError::Ok) {
      unsigned long eventId = doc["event_id"] | 0UL;
      if (eventId > lastRegistrationEventId) {
        lastRegistrationEventId = eventId;
        if (showNewEvent && String(doc["status"] | "") == "registration_success") {
          String name = doc["name"] | "";
          lcdMessage("SUCCESSFULLY", "REGISTERED");
          delay(2250);
          lcdMessage(name, "CARD REGISTERED");
          delay(2250);
          lcdMessage("System Ready", "Tap your card");
        }
      }
    } else {
      Serial.println("Invalid registration event JSON: " + String(error.c_str()));
    }
  }
  http.end();
}

void showTapResult(String status, String action, String result, String name, String role, String message) {
  if (status == "capture_success") {
    lcdMessage("CARD READ", "SUCCESSFULLY");
    delay(2250);
    return;
  }
  if (status == "card_already_registered") {
    lcdMessage("CARD ALREADY", "REGISTERED");
    delay(2250);
    return;
  }
  if (status == "capture_failed") {
    lcdMessage("INVALID CARD", "UID");
    delay(2250);
    return;
  }

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

void connectWiFi() {
  lcdMessage("Connecting WiFi", "Please wait...");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.println("\nWiFi OK! ESP32 IP: " + WiFi.localIP().toString());
}

void setup() {
  Serial.begin(115200);
  SPI.begin();
  rfid.PCD_Init();
  Wire.begin(I2C_SDA, I2C_SCL);
  lcd.init();
  lcd.backlight();
  connectWiFi();
  checkRegistrationEvent(false);
  lcdMessage("System Ready", "Tap your card");
}

void loop() {
  if (millis() - lastRegistrationPoll >= REGISTRATION_POLL_INTERVAL_MS) {
    lastRegistrationPoll = millis();
    checkRegistrationEvent(true);
  }

  if (!rfid.PICC_IsNewCardPresent()) return;
  if (!rfid.PICC_ReadCardSerial()) return;

  String uid = uidToHex(rfid);
  Serial.println("Card UID: " + uid);

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
    JsonDocument doc;   // ArduinoJson v7. If you have v6, use: StaticJsonDocument<512> doc;
    DeserializationError error = deserializeJson(doc, http.getString());
    if (error == DeserializationError::Ok) {
      String status  = doc["status"] | "";
      String name    = doc["name"] | "";
      String action  = doc["action"] | "";
      String result  = doc["result"] | "";
      int cooldown   = doc["cooldown_seconds"] | 0;
      String role    = doc["role"] | "";
      String message = doc["message"] | "";
      Serial.println("Server result: " + status + " / " + result + " / " + action);
      if (result == "REJECT_COOLDOWN" && cooldown > 0) {
        message = "Please wait " + String(cooldown) + " seconds before scanning again.";
      }
      showTapResult(status, action, result, name, role, message);
    } else {
      Serial.println("Invalid server JSON: " + String(error.c_str()));
      lcdMessage("SERVER ERROR", "Bad reply");
    }
  } else {
    Serial.println("HTTP error: " + String(httpCode));
    lcdMessage("SERVER ERROR", "Code: " + String(httpCode));
  }

  http.end();
  lcdMessage("System Ready", "Tap your card");
}