// Simple test for the 16x2 LCD with I2C backpack on an Arduino Uno.
// Wiring: SDA -> A4, SCL -> A5, VCC -> 5V, GND -> GND

#include <Wire.h>
#include <LiquidCrystal_I2C.h>

#define LCD_ADDR 0x27   // if LCD stays blank, try 0x3F instead of 0x27

LiquidCrystal_I2C lcd(LCD_ADDR, 16, 2);

void setup() {
  Serial.begin(9600);
  Wire.begin();   // Uno I2C is fixed: SDA = A4, SCL = A5

  // Print every I2C address found, so you know whether to use 0x27 or 0x3F.
  Serial.println("Scanning I2C...");
  for (byte addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.print("Found device at 0x");
      Serial.println(addr, HEX);
    }
  }

  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0); lcd.print("Hey hey hey");
  lcd.setCursor(0, 1); lcd.print("LCD 16x2 OK");
}

void loop() {
  // Seconds counter on the right of line 2, so you can see the LCD is updating.
  unsigned long seconds = millis() / 1000;
  lcd.setCursor(12, 1);
  if (seconds < 1000) lcd.print(" ");
  if (seconds < 100)  lcd.print(" ");
  if (seconds < 10)   lcd.print(" ");
  lcd.print(seconds);
  delay(1000);
}
