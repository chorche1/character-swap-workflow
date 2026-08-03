# Character Swap Editor

Ett videoredigeringsverktyg som kör lokalt på din egen Mac. Du laddar upp
klipp, och den klipper bort tystnader, jämnar ut taltempot, byter röst och
bränner in textremsor — allt i ett svep.

Ingenting laddas upp till någon molntjänst utom själva ljudet, som skickas
för transkribering. Dina videofiler stannar på datorn.

---

## Komma igång (en gång)

**1. Flytta ut mappen ur nedladdningarna.** Dra `character-swap-editor` till
skrivbordet eller dokumentmappen. Kör den inte från zip-filen.

**2. Högerklicka på `1 Installera` → Öppna → Öppna.**

Just den här filen måste öppnas med högerklick första gången — macOS
blockerar annars program som inte kommer från App Store. (Efter det kan du
dubbelklicka som vanligt.)

Installationen tar **5–15 minuter** och laddar ner ungefär 700 MB. Den
hämtar Python, alla programpaket, Node och en renderingsmotor. Det ser ut
att stå still ibland — det gör det inte. Låt fönstret vara öppet tills det
står **KLART**.

**3. Dubbelklicka på `2 Starta Editor`.**

Ett terminalfönster öppnas och webbläsaren dyker upp efter några sekunder.

---

## Varje gång du ska jobba

Dubbelklicka på **`2 Starta Editor`**. Klart.

Terminalfönstret som öppnas **är** programmet. Låt det ligga kvar medan du
jobbar. När du är klar: stäng fönstret, så stängs Editor.

Om webbläsaren inte öppnar sig av sig själv står adressen i
terminalfönstret — vanligtvis `http://127.0.0.1:8000`.

---

## Så använder du Editor

**Ett klipp:** dra in videon i rutan, välj textremsmall, klicka på
auto-redigera. Du får tillbaka en färdig fil.

**Flera klipp:** lägg in alla klippen plus manuset. Programmet lyssnar
igenom varje klipp, listar ut vilken ordning de ska ha utifrån manuset,
jämnar ut tempot i vart och ett och fogar ihop dem.

**Efteråt** kan du finjustera: klipp om i tidslinjen, redigera texten i
textremsorna ord för ord, eller rendera om med en annan mall — utan att
göra om hela jobbet.

---

## Om något krånglar

**"Kan inte öppnas eftersom den kommer från en icke identifierad
utvecklare"** — högerklicka på filen → Öppna → Öppna. Gäller bara första
gången.

**Videon avvisas som för stor** — öppna filen `.env` i en textredigerare
och höj siffran på raden `MAX_UPLOAD_BYTES`.

**De animerade textremsmallarna saknas i listan** — då gick Node-steget i
installationen fel. Kör `1 Installera` igen med fungerande internet. De
vanliga mallarna fungerar under tiden.

**Ingenting händer när jag startar** — kolla att du kört `1 Installera`
först, och att terminalfönstret inte visar ett felmeddelande.

**Hela sidan är tom** — programmet hämtar en del av gränssnittet från
internet vid start. Kontrollera anslutningen och ladda om sidan.

---

## Det finstilta

Programmet är förkonfigurerat med API-nycklar som betalas av den som gav
dig kopian. Transkribering och röstbyte kostar små belopp per klipp.
Använd det som det var tänkt, så är det inga problem.

Det här är en ögonblicksbild av programmet. Nyare versioner kommer inte
automatiskt — be om ett nytt paket om du vill uppdatera.
