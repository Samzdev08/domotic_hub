#include <Adafruit_SSD1306.h>
#include <ESP32Servo.h>
#include <DHT.h>
#include <WiFi.h>
#include <WebServer.h>

#define BLANC SSD1306_WHITE

const int PIN_SERVO = 4;
const int PIN_PIR = 19;
const int PIN_VERT = 5;
const int PIN_ROUGE = 23;
const int PIN_BUZZER = 15;
const int PIN_DHT = 18;


const char* ssid = "VOTRE_SSID";
const char* password = "VOTRE_MOT_DE_PASSE";

bool ouverte = false;
unsigned long tempsAction = 0;
bool ancienPIR = LOW;

WebServer server(80);

Servo servo;
DHT dht(PIN_DHT, DHT11);
Adafruit_SSD1306 ecran(128, 64, &Wire, -1);

float temp = NAN, hum = NAN;
unsigned long dernierReleve = 0;

const unsigned char ICONE_THERMO[] PROGMEM = {
  0x03,0xC0, 0x04,0x20, 0x04,0x20, 0x05,0xA0, 0x05,0xA0, 0x05,0xA0, 0x05,0xA0, 0x05,0xA0,
  0x05,0xA0, 0x09,0x90, 0x13,0xC8, 0x17,0xE8, 0x17,0xE8, 0x13,0xC8, 0x08,0x10, 0x07,0xE0
};

const unsigned char ICONE_GOUTTE[] PROGMEM = {
  0x01,0x80, 0x01,0x80, 0x03,0xC0, 0x03,0xC0, 0x07,0xE0, 0x0F,0xF0, 0x0F,0xF0, 0x1F,0xF8,
  0x3F,0xFC, 0x37,0xFC, 0x37,0xFC, 0x33,0xFC, 0x39,0xFC, 0x1F,0xF8, 0x0F,0xF0, 0x03,0xC0
};

const unsigned char ICONE_FERME[] PROGMEM = {
  0x00,0x00, 0x07,0xE0, 0x0C,0x30, 0x08,0x10, 0x08,0x10, 0x08,0x10, 0x3F,0xFC, 0x3F,0xFC,
  0x3F,0xFC, 0x3E,0x7C, 0x3E,0x7C, 0x3E,0x7C, 0x3F,0xFC, 0x3F,0xFC, 0x3F,0xFC, 0x00,0x00
};

const unsigned char ICONE_OUVERT[] PROGMEM = {
  0x07,0xE0, 0x0C,0x30, 0x08,0x10, 0x08,0x10, 0x08,0x00, 0x08,0x00, 0x3F,0xFC, 0x3F,0xFC,
  0x3F,0xFC, 0x3E,0x7C, 0x3E,0x7C, 0x3E,0x7C, 0x3F,0xFC, 0x3F,0xFC, 0x3F,0xFC, 0x00,0x00
};

void afficher(bool ouvert) {
  ecran.clearDisplay();
  ecran.setTextColor(BLANC);
  ecran.setTextWrap(false);

  ecran.setTextSize(1);
  ecran.setCursor(0, 0);
  ecran.print("HUB ACCES");
  ecran.drawLine(0, 10, 127, 10, BLANC);

  ecran.drawBitmap(4, 16, ouvert ? ICONE_OUVERT : ICONE_FERME, 16, 16, BLANC);
  ecran.setTextSize(2);
  ecran.setCursor(28, 17);
  ecran.print(ouvert ? "OUVERT" : "FERME");

  ecran.drawLine(0, 38, 127, 38, BLANC);

  ecran.drawBitmap(0, 44, ICONE_THERMO, 16, 16, BLANC);
  ecran.setCursor(18, 45);
  if (isnan(temp)) ecran.print("--");
  else { ecran.print((int)round(temp)); ecran.print((char)247); ecran.print("C"); }

  ecran.drawBitmap(70, 44, ICONE_GOUTTE, 16, 16, BLANC);
  ecran.setCursor(88, 45);
  if (isnan(hum)) ecran.print("--");
  else { ecran.print((int)round(hum)); ecran.print("%"); }

  ecran.display();
}

void handleRoot() {
  String html = "<HTML><head><meta charset='UTF-8'>";
  html += "<meta name='viewport' content='width=device-width, initial-scale=1.0'>";
  html += "<meta name='color-scheme' content='dark'>";
  html += "<meta name='theme-color' content='#000000'>";
  html += "<title>HUB ACCES // ESP32</title>";
  html += "<style>";
  html += "*{box-sizing:border-box}";
  html += "html{background:#000}";
  html += "body{margin:0;background:#000;color:#5b8ea6;font-family:'Courier New',monospace;font-size:10px;letter-spacing:.5px;padding:20px 14px;display:flex;flex-direction:column;align-items:center}";
  html += ".w{width:100%;max-width:820px}";
  html += "header{border-bottom:1px solid #12405c;padding-bottom:12px;margin-bottom:14px;display:flex;align-items:center;gap:12px}";
  html += "h1{margin:0;font-size:19px;letter-spacing:4px;color:#e8faff;text-transform:uppercase}";
  html += "h1 span{color:#00e5ff}";
  html += ".sub{font-size:11px;letter-spacing:2px;color:#3d7893;margin-top:5px}";
  html += ".spin{animation:rot 6s linear infinite;transform-origin:center}@keyframes rot{to{transform:rotate(360deg)}}";
  html += ".row{display:flex;gap:12px;flex-wrap:wrap}";
  html += ".pane{border-top:1px solid #123246;padding:10px 0 16px;flex:1;min-width:210px;animation:in .6s ease both}";
  html += ".pane:nth-child(2){animation-delay:.12s}";
  html += ".pane:nth-child(3){animation-delay:.24s}";
  html += "@keyframes in{from{opacity:0;transform:translateY(10px)}}";
  html += ".tag{display:flex;align-items:center;gap:6px;color:#2f5f78;letter-spacing:2px;text-transform:uppercase}";
  html += ".tag b{color:#7de3ff;font-weight:400}";
  html += ".o .tag b{color:#ffb347}";
  html += ".ico{animation:bob 2.6s ease-in-out infinite}";
  html += "@keyframes bob{50%{transform:translateY(-2px)}}";
  html += ".big{font-size:40px;line-height:1;color:#e8faff;margin:6px 0 10px}";
  html += ".big.o{color:#ffb347}.big u{text-decoration:none;font-size:14px;color:#2f5f78;margin-left:4px}";
  html += ".bar{height:3px;background:#0d2436;border-radius:2px;overflow:hidden}";
  html += ".bar i{display:block;height:100%;border-radius:2px;background:#00e5ff;box-shadow:0 0 8px #00e5ff;animation:fill 1s ease both}";
  html += ".o .bar i{background:#ffb347;box-shadow:0 0 8px #ffb347}";
  html += "@keyframes fill{from{width:0}}";
  html += ".btn{margin-top:10px;width:100%;background:transparent;color:#00e5ff;border:1px solid #12405c;padding:8px 10px;font-family:inherit;font-size:10px;letter-spacing:2px;text-transform:uppercase;border-radius:4px}";
  html += ".btn:active{background:#0d2436}";
  html += ".btn:disabled{color:#2f5f78;border-color:#0b1c26}";
  html += "footer{max-width:820px;width:100%;margin-top:16px;color:#1d3e4f;border-top:1px solid #0b1c26;padding-top:8px}";
  html += "</style></head><body><div class='w'>";

  html += "<header><svg class='spin' width='26' height='26' viewBox='0 0 24 24' fill='none' stroke='#00e5ff' stroke-width='1.5'><circle cx='12' cy='12' r='3'/><circle cx='12' cy='12' r='9' stroke-dasharray='4 6'/></svg>";
  html += "<div><h1>HUB <span>ACCES</span> // ESP32</h1><div class='sub'>SUPERVISION TEMPS REEL · " + WiFi.localIP().toString() + "</div></div></header>";

  html += "<div class='row'>";

  html += String("<div class='pane") + (ouverte ? " o" : "") + "'><div class='tag'><svg class='ico' width='13' height='13' viewBox='0 0 24 24' fill='none' stroke='" + (ouverte ? "#ffb347" : "#00e5ff") + "' stroke-width='2'><path d='M12 3l7 4v5c0 5-3 8-7 9-4-1-7-4-7-9V7z'/></svg>PORTE · <b>ACCES</b></div>";
  html += String("<div class='big") + (ouverte ? " o" : "") + "'>" + (ouverte ? "OUVERT" : "FERME") + "</div>";
  html += "<form action='/ouvrir' method='GET'><button class='btn'>Ouvrir</button></form>";

  html += "<div class='pane o'><div class='tag'><svg class='ico' width='13' height='13' viewBox='0 0 24 24' fill='none' stroke='#ffb347' stroke-width='2'><path d='M14 14.8V4a2 2 0 1 0-4 0v10.8a4 4 0 1 0 4 0z'/></svg>TEMP · <b>DHT11 / PIN18</b></div>";
  html += "<div class='big o'>";
  if (isnan(temp)) html += "--<u>°C</u>";
  else { html += String(temp, 1); html += "<u>°C</u>"; }
  html += "</div><div class='bar'><i style='width:";
  html += isnan(temp) ? "0" : String(constrain(temp, 0, 40) / 40.0 * 100, 0);
  html += "%'></i></div></div>";

  html += "<div class='pane'><div class='tag'><svg class='ico' width='13' height='13' viewBox='0 0 24 24' fill='none' stroke='#00e5ff' stroke-width='2' style='animation-delay:.5s'><path d='M12 2.7S5.5 10 5.5 14a6.5 6.5 0 0 0 13 0c0-4-6.5-11.3-6.5-11.3z'/></svg>HUMIDITE · <b>RELATIVE</b></div>";
  html += "<div class='big'>";
  if (isnan(hum)) html += "--<u>%</u>";
  else { html += String(hum, 0); html += "<u>%</u>"; }
  html += "</div><div class='bar'><i style='width:";
  html += isnan(hum) ? "0" : String(hum, 0);
  html += "%'></i></div></div>";

  html += "</div>";

  html += "<footer>// UPTIME " + String(millis() / 1000) + "s · RSSI " + String(WiFi.RSSI()) + " dBm · HEAP " + String(ESP.getFreeHeap() / 1024) + " kB</footer>";

  html += "</div></body></HTML>";

  server.send(200, "text/html", html);
}

void handleOuvrir() {
  if (!ouverte) {
    Serial.println("Ouverture manuelle (dashboard)");
    afficher(true);
    digitalWrite(PIN_ROUGE, LOW);
    digitalWrite(PIN_VERT, HIGH);
    tone(PIN_BUZZER, 1000, 300);
    servo.write(0);
    tempsAction = millis();
    ouverte = true;
  }
  server.sendHeader("Location", "/");
  server.send(303);
}

void setup() {
  Serial.begin(115200);

  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  servo.attach(PIN_SERVO, 500, 2400);
  servo.write(140);

  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_VERT, OUTPUT);
  pinMode(PIN_ROUGE, OUTPUT);
  digitalWrite(PIN_ROUGE, HIGH);

  dht.begin();
  WiFi.begin(ssid, password);

  if (!ecran.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println("OLED introuvable");
  }
  while (WiFi.status() != WL_CONNECTED) {
    delay(1000);
    Serial.println("Connexion au Wi-Fi...");
  }

  Serial.println("Connecté ! Adresse IP :");
  Serial.println(WiFi.localIP());
  delay(60000);
  server.on("/", handleRoot);
  server.on("/ouvrir", handleOuvrir);
  server.begin();
  afficher(false);
  Serial.println("Hub pret");
}

void loop() {
  server.handleClient();

  bool pir = digitalRead(PIN_PIR);

  if (millis() - dernierReleve >= 3000) {
    dernierReleve = millis();
    float t = dht.readTemperature();
    float h = dht.readHumidity();
    if (!isnan(t) && !isnan(h)) { temp = t; hum = h; }
    Serial.printf("Temp : %.1f C | Hum : %.0f %%\n", temp, hum);
    afficher(ouverte);
  }
  if (!ouverte && pir == HIGH && ancienPIR == LOW) {
    Serial.println("Mouvement : ouverture");
    afficher(true);
    digitalWrite(PIN_ROUGE, LOW);
    digitalWrite(PIN_VERT, HIGH);
    tone(PIN_BUZZER, 1000, 300);

    servo.write(0);
    tempsAction = millis();
    ouverte = true;
  }

  ancienPIR = pir;

  if (ouverte && millis() - tempsAction >= 2500) {
    servo.write(140);
    digitalWrite(PIN_VERT, LOW);
    digitalWrite(PIN_ROUGE, HIGH);
    afficher(false);
    Serial.println("Fermeture");
    ouverte = false;
    tempsAction = millis();
  }


}
